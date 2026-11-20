"""Where original uploads live. One interface, two backends.

LocalStorage  - files in MEDIA_STORAGE_DIR (API and workers must share that directory).
S3Storage     - any S3-compatible bucket (AWS S3, Cloudflare R2, MinIO, ...). API and workers
                only need the same bucket credentials; no shared disk.

A *key* names an object inside the backend and is what `media_assets.stored_filename` holds.
A *ref* is what the Redis job hash carries to the worker: an absolute path for local storage
(unchanged from earlier releases) or `s3://bucket/key`. `materialize(ref)` turns either into a
local file for the duration of one Gemini call.

`link(key, ...)` gives a browser an expiring URL for one object: a presigned GET for S3, so
bytes go straight from the bucket, or None for local storage, where the API signs its own
links (app/api/media_links.py). `check()` reports deployment problems (docs/STORAGE.md).
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from app.errors import ConfigError


def safe_filename(original_filename: str) -> str:
    name = Path(original_filename).name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name[:255] or "upload"


def _same_content(path: Path, sha256: str) -> bool:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == sha256


class MediaStorage(Protocol):
    def save(self, staged: Path, original_filename: str, sha256: str) -> str:
        """Persist `staged` (which may be consumed) and return its key."""

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def ref(self, key: str) -> str:
        """The value a worker receives in the job hash."""

    def materialize(self, ref: str): ...

    def read_chunks(self, key: str) -> Iterator[bytes]: ...

    def local_path(self, key: str) -> Path | None:
        """A path the API can serve directly, or None for remote backends."""

    def link(self, key: str, *, filename: str, content_type: str, ttl_s: int) -> str | None:
        """An expiring URL a browser can fetch the object from, or None if the API must sign
        its own."""

    def check(self) -> list[dict]:
        """Deployment findings: [{"level": "ok" | "warn" | "error", "check", "detail"}]."""


class LocalStorage:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _reserve(self, original_filename: str, sha256: str) -> tuple[Path, bool]:
        original = safe_filename(original_filename)
        stem, suffix = os.path.splitext(original)
        max_stem = 255 - len(suffix) - 65
        # Always carry a fingerprint of the content. The bare original name was tried first
        # once; but a deleted original's record keeps its name while the file is gone, so a
        # different upload with the same name could take that name and collide with the
        # record (a 500 under load). Same bytes still get the same name.
        candidates = [
            f"{stem[:max_stem]}-{sha256[:8]}{suffix}",
            f"{stem[:max_stem]}-{sha256}{suffix}",
        ]
        for name in candidates:
            candidate = self.directory / name
            try:
                candidate.touch(exist_ok=False)
                return candidate, True
            except FileExistsError:
                if _same_content(candidate, sha256):
                    return candidate, False
        raise FileExistsError(f"Unable to reserve a unique media filename for {original!r}")

    def save(self, staged: Path, original_filename: str, sha256: str) -> str:
        self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        target, created = self._reserve(original_filename, sha256)
        if created:
            os.replace(staged, target)
        return target.name

    def exists(self, key: str) -> bool:
        return (self.directory / key).is_file()

    def delete(self, key: str) -> None:
        (self.directory / key).unlink(missing_ok=True)

    def ref(self, key: str) -> str:
        return str(self.directory / key)

    @contextmanager
    def materialize(self, ref: str):
        yield ref

    def read_chunks(self, key: str) -> Iterator[bytes]:
        with (self.directory / key).open("rb") as f:
            while chunk := f.read(1024 * 1024):
                yield chunk

    def local_path(self, key: str) -> Path | None:
        return self.directory / key

    def link(self, key: str, *, filename: str, content_type: str, ttl_s: int) -> str | None:
        return None

    def check(self) -> list[dict]:
        ok = self.directory.is_dir() and os.access(self.directory, os.W_OK)
        return [{
            "level": "ok" if ok else "error", "check": "directory",
            "detail": f"{self.directory} is {'writable' if ok else 'missing or not writable'}",
        }]


class S3Storage:
    def __init__(self, bucket: str, *, prefix: str = "", client=None, public_client=None,
                 public_endpoint_url: str = "", sse: str = "", sse_kms_key_id: str = "",
                 **client_kw):
        if client is None or (public_endpoint_url and public_client is None):
            try:
                import boto3
                from botocore.config import Config
            except ImportError:  # pragma: no cover - boto3 ships in requirements.txt
                raise ConfigError("MEDIA_STORAGE=s3 needs boto3: pip install boto3") from None
            kw = {k: v for k, v in client_kw.items() if v}
            if client is None:  # SigV4 everywhere: presigned links must be V4 to carry expiry
                client = boto3.client("s3", **kw, config=Config(signature_version="s3v4"))
            if public_endpoint_url and public_client is None:
                # Presigning only signs; it never calls this endpoint from the server.
                public_client = boto3.client(
                    "s3", **{**kw, "endpoint_url": public_endpoint_url},
                    config=Config(signature_version="s3v4"),
                )
        self.s3, self.bucket = client, bucket
        self.presigner = public_client or client
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.sse, self.sse_kms_key_id = sse, sse_kms_key_id

    def save(self, staged: Path, original_filename: str, sha256: str) -> str:
        # Content-addressed: identical bytes always land on the same key, so a retry or a
        # concurrent upload of the same file is a harmless overwrite with identical content.
        suffix = os.path.splitext(safe_filename(original_filename))[1].lower()
        key = f"{self.prefix}{sha256}{suffix}"
        extra = {"ContentType": mimetypes.guess_type(f"x{suffix}")[0] or "application/octet-stream"}
        if self.sse:
            extra["ServerSideEncryption"] = self.sse
            if self.sse == "aws:kms" and self.sse_kms_key_id:
                extra["SSEKMSKeyId"] = self.sse_kms_key_id
        self.s3.upload_file(str(staged), self.bucket, key, ExtraArgs=extra)
        return key

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def ref(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    @contextmanager
    def materialize(self, ref: str):
        if not ref.startswith("s3://"):  # job submitted before switching backends
            yield ref
            return
        bucket, _, key = ref[len("s3://"):].partition("/")
        # Keep the extension: image/video preparation picks a codec from it.
        fd, tmp = tempfile.mkstemp(prefix="media-queue-", suffix=os.path.splitext(key)[1])
        os.close(fd)
        try:
            self.s3.download_file(bucket, key, tmp)
            yield tmp
        finally:
            os.unlink(tmp)

    def read_chunks(self, key: str) -> Iterator[bytes]:
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            yield from body.iter_chunks(1024 * 1024)
        finally:
            body.close()

    def local_path(self, key: str) -> Path | None:
        return None

    def link(self, key: str, *, filename: str, content_type: str, ttl_s: int) -> str | None:
        safe = safe_filename(filename).replace('"', "")
        return self.presigner.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket, "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": f'inline; filename="{safe}"',
                "ResponseCacheControl": f"private, max-age={ttl_s}",
            },
            ExpiresIn=ttl_s,
        )

    def check(self) -> list[dict]:
        """What a deletion policy depends on: deleted objects must really be gone (no
        versioning, or a lifecycle rule that expires old versions), the bucket must not be
        public (links are the only way in), and objects should be encrypted at rest."""
        out: list[dict] = []

        def add(level, check, detail):
            out.append({"level": level, "check": check, "detail": detail})

        def code(err):
            return getattr(err, "response", {}).get("Error", {}).get("Code", "")

        try:
            self.s3.head_bucket(Bucket=self.bucket)
            add("ok", "bucket", f"{self.bucket} is reachable")
        except Exception as err:
            add("error", "bucket", f"{self.bucket} is not reachable: {code(err) or err}")
            return out

        versioning = self.s3.get_bucket_versioning(Bucket=self.bucket).get("Status")
        if versioning in ("Enabled", "Suspended"):
            rules = []
            try:
                rules = self.s3.get_bucket_lifecycle_configuration(Bucket=self.bucket)["Rules"]
            except Exception as err:
                if code(err) != "NoSuchLifecycleConfiguration":
                    raise
            expires_old = any(r.get("Status") == "Enabled" and (
                "NoncurrentVersionExpiration" in r) for r in rules)
            add("ok" if expires_old else "warn", "versioning",
                f"versioning is {versioning}; " + (
                    "a lifecycle rule expires noncurrent versions" if expires_old else
                    "deleted originals stay recoverable as old versions: add a lifecycle rule "
                    "with NoncurrentVersionExpiration"))
        else:
            add("ok", "versioning", "versioning is off, so deleted objects are gone")

        try:
            block = self.s3.get_public_access_block(Bucket=self.bucket)[
                "PublicAccessBlockConfiguration"]
            public_ok = all(block.get(k) for k in (
                "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy",
                "RestrictPublicBuckets"))
        except Exception as err:
            if code(err) not in ("NoSuchPublicAccessBlockConfiguration", "NotImplemented"):
                raise
            public_ok = False
        add("ok" if public_ok else "warn", "public_access",
            "public access is fully blocked" if public_ok else
            "public access is not fully blocked; media must only be reachable by signed links")

        try:
            rules = self.s3.get_bucket_encryption(Bucket=self.bucket)[
                "ServerSideEncryptionConfiguration"]["Rules"]
            default = rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        except Exception as err:
            if code(err) not in ("ServerSideEncryptionConfigurationNotFoundError",
                                 "NotImplemented"):
                raise
            default = ""
        if self.sse or default:
            add("ok", "encryption",
                f"new uploads are encrypted ({self.sse or default})")
        else:
            add("warn", "encryption", "no default encryption; set S3_SSE or a bucket default")
        return out


def build_storage(
    backend: str,
    *,
    local_dir: Path,
    s3_bucket: str = "",
    s3_prefix: str = "",
    s3_endpoint_url: str = "",
    s3_region: str = "",
    s3_public_endpoint_url: str = "",
    s3_sse: str = "",
    s3_sse_kms_key_id: str = "",
) -> MediaStorage:
    if backend == "local":
        return LocalStorage(local_dir)
    if backend == "s3":
        if not s3_bucket:
            raise ConfigError("MEDIA_STORAGE=s3 needs S3_BUCKET.")
        return S3Storage(
            s3_bucket, prefix=s3_prefix, endpoint_url=s3_endpoint_url, region_name=s3_region,
            public_endpoint_url=s3_public_endpoint_url, sse=s3_sse,
            sse_kms_key_id=s3_sse_kms_key_id,
        )
    raise ConfigError(f"MEDIA_STORAGE must be 'local' or 's3', got {backend!r}")


def storage_from_env() -> MediaStorage:
    return build_storage(
        os.environ.get("MEDIA_STORAGE", "local").strip().lower() or "local",
        local_dir=Path(os.environ.get("MEDIA_STORAGE_DIR", "uploads")).expanduser().resolve(),
        s3_bucket=os.environ.get("S3_BUCKET", "").strip(),
        s3_prefix=os.environ.get("S3_PREFIX", "").strip(),
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL", "").strip(),
        s3_region=os.environ.get("S3_REGION", "").strip(),
        s3_public_endpoint_url=os.environ.get("S3_PUBLIC_ENDPOINT_URL", "").strip(),
        s3_sse=os.environ.get("S3_SSE", "").strip(),
        s3_sse_kms_key_id=os.environ.get("S3_SSE_KMS_KEY_ID", "").strip(),
    )
