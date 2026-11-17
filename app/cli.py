"""Manual control. Needs .env with GEMINI_API_KEY and GEMINI_MODEL (real Gemini calls),
or DEMO_MODE=1 for the offline demo client.

python -m app.cli submit uploads/cat.jpg --user 1
python -m app.cli worker [--drain]     # with DATABASE_URL: also reconciles DB<->Redis + retention
python -m app.cli status <job_id>
python -m app.cli stats
python -m app.cli reconcile            # one DB<->Redis repair sweep (see docs/JOB_LIFECYCLE.md)
python -m app.cli prune-media [--dry-run]  # apply every workspace's deletion policy once
python -m app.cli storage-check        # bucket/disk readiness for deletion policies (STORAGE.md)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import threading

from dotenv import load_dotenv

from app.api.database import create_database
from app.config import Settings, env_flag
from app.errors import ConfigError, MediaError
from app.obs import metrics
from app.obs.logs import configure_logging
from app.obs.tracing import configure_tracing, instrument_engine, shutdown_tracing
from app.storage import storage_from_env
from app.wiring import build_runtime


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url and env_flag("DEMO_MODE"):
        url = "sqlite:///demo.sqlite3"  # same default as the API in demo mode
    return url


def _retention_days() -> int:
    raw = os.environ.get("MEDIA_RETENTION_DAYS", "").strip()
    return int(raw) if raw else 30


def _retention_max_days() -> int:
    raw = os.environ.get("MEDIA_RETENTION_MAX_DAYS", "").strip()
    return int(raw) if raw else 0


def _publishing():
    """The worker delivers publications only if it can decrypt the API's credentials, so it
    derives the same key the API does (PUBLISHING_KEY, else JWT_SECRET)."""
    from app.api.config import DEMO_JWT_SECRET, publishing_env
    from app.config import env_flag
    from app.publishing.crypto import Cipher

    names, key = publishing_env()
    jwt_secret = os.environ.get("JWT_SECRET", "").strip()
    if not jwt_secret and env_flag("DEMO_MODE"):
        jwt_secret = DEMO_JWT_SECRET
    if not names or not (key or jwt_secret):
        if names:
            logging.warning("publishing disabled in this worker: set PUBLISHING_KEY or JWT_SECRET")
        return None
    return names, Cipher(publishing_key=key, jwt_secret=jwt_secret)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("path")
    s.add_argument("--user", default="0")
    w = sub.add_parser("worker")
    w.add_argument("--drain", action="store_true", help="exit when queue empty")
    st = sub.add_parser("status")
    st.add_argument("job_id")
    sub.add_parser("stats")
    sub.add_parser("reconcile", help="repair DB/Redis divergence once")
    pm = sub.add_parser("prune-media", help="apply every workspace's deletion policy once")
    pm.add_argument("--dry-run", action="store_true", help="count, delete nothing")
    sub.add_parser("storage-check", help="check the media store is ready for deletion policies")
    args = ap.parse_args(argv)
    if args.cmd == "storage-check":
        # Needs no Gemini or Redis settings: run it before anything else is configured.
        try:
            findings = storage_from_env().check()
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2
        print(json.dumps(findings, indent=2))
        return 1 if any(f["level"] == "error" for f in findings) else 0

    service = "mediaflow-worker" if args.cmd == "worker" else "mediaflow-cli"
    configure_logging(service)
    if args.cmd in ("worker", "reconcile", "prune-media"):
        # Not for status/stats: the worker healthcheck runs `stats` every 30 s.
        configure_tracing(service)
    try:
        rt = build_runtime(Settings.from_env())
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "submit":
        try:
            print(json.dumps(rt.store.submit(args.user, args.path)))
        except MediaError as e:
            print(f"rejected: {e}", file=sys.stderr)
            return 1
    elif args.cmd == "worker":
        if shutil.which("ffmpeg") is None:
            logging.warning(
                "ffmpeg is not installed; videos larger than MAX_VIDEO_MB=%d will fail compression",
                rt.settings.max_video_mb,
            )
        database_url = _database_url()
        db_engine = None
        if database_url:
            from app.api.worker_setup import configure_worker

            db_engine, session_factory = create_database(database_url)
            instrument_engine(db_engine)
            configure_worker(
                rt, session_factory, storage_from_env(), retention_days=_retention_days(),
                retention_max_days=_retention_max_days(),
                publishing=_publishing(),
            )
        if not args.drain:
            metrics.serve(metrics.metrics_port())
        logging.getLogger("worker").info(
            "worker started",
            extra={"queue_prefix": rt.settings.prefix, "model": rt.settings.model,
                   "rpm": rt.settings.rpm, "rpd": rt.settings.rpd,
                   "database": bool(db_engine), "demo_mode": rt.settings.demo_mode},
        )
        try:
            if args.drain:
                print(json.dumps(rt.worker.drain()))
            else:
                stop = threading.Event()
                rt.worker.run(stop)
        except KeyboardInterrupt:
            if not args.drain:
                stop.set()
        finally:
            if db_engine is not None:
                db_engine.dispose()
            shutdown_tracing()  # flush spans still buffered for export
    elif args.cmd == "status":
        print(json.dumps(rt.store.status(args.job_id), indent=2))
    elif args.cmd == "stats":
        print(json.dumps({**rt.queue.depths(), "gemini_calls_today": rt.limiter.used_today()}))
    elif args.cmd in ("reconcile", "prune-media"):
        database_url = _database_url()
        if not database_url:
            print("config error: DATABASE_URL is required", file=sys.stderr)
            return 2
        db_engine, session_factory = create_database(database_url)
        try:
            if args.cmd == "reconcile":
                from app.api.reconcile import Reconciler

                out = Reconciler(session_factory, rt.store, rt.queue, storage_from_env()).run_once()
            else:
                from app.api.retention import prune_media

                out = prune_media(
                    session_factory, storage_from_env(), retention_days=_retention_days(),
                    max_days=_retention_max_days(), dry_run=args.dry_run,
                )
            print(json.dumps(out))
        finally:
            db_engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
