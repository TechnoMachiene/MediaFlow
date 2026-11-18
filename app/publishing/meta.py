"""Meta: a Facebook Page and the Instagram business account linked to it (Graph API).

Credentials come from the operator, not from a per-user consent screen. Meta's own tools
issue them: a **system user** in Meta Business Suite (Business settings > Users > System
users) generates a Page access token that does not expire, with these permissions:

    pages_show_list, pages_read_engagement, pages_manage_posts     (Facebook Page)
    instagram_basic, instagram_content_publish                     (Instagram)
    business_management

They go in .env (see .env.example), are checked against the Graph API when an operator
connects the account in Publishing, and are then stored encrypted with the connection like
any other provider's. Rotating the token: change .env, restart, select Reconnect.

What each channel does (docs/PUBLISHING.md, "Meta"):

    facebook  photo:  POST /{page}/photos  (bytes, with the caption)       -> published
              video:  POST /{page}/videos  (bytes, on graph-video)         -> processing,
                      then GET /{video}?fields=status until ready          -> published
    instagram image:  POST /{ig}/media {image_url}  -> container
              video:  POST /{ig}/media {media_type=REELS, video_url}       -> container
                      GET /{container}?fields=status_code until FINISHED,
                      then POST /{ig}/media_publish {creation_id}          -> published

Instagram fetches the media itself, so it needs a public HTTPS URL: the worker signs a
short-lived link to `/api/v1/publish-media/<token>` under PUBLIC_BASE_URL (images are served
as JPEG, the only image format Instagram accepts).

The Graph API has no idempotency keys, so `idempotent=False`: if the worker dies mid-call the
publication becomes `unknown` and a person checks the account instead of risking a double post.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.errors import ConfigError
from app.publishing.base import (
    AuthorizationDenied,
    AuthorizationLost,
    ContentRejected,
    Credentials,
    Grant,
    Payload,
    Provider,
    ProviderInfo,
    ProviderRateLimited,
    ProviderUnavailable,
    PublishError,
    Receipt,
)

log = logging.getLogger("publishing.meta")

DEFAULT_GRAPH_VERSION = "v24.0"
GRAPH = "https://graph.facebook.com"
GRAPH_VIDEO = "https://graph-video.facebook.com"
IG_CAPTION_MAX = 2200
IG_HASHTAG_MAX = 30
# A just-created image container is usually ready within a few seconds; wait that long before
# handing it to the delivery loop's slower status checks.
IG_IMAGE_WAIT_S = (1.0, 2.0, 3.0)
CONTAINER = "container:"

# Graph API error codes (developers.facebook.com/docs/graph-api/guides/error-handling).
AUTH_CODES = {102, 190, 10} | set(range(200, 300))
RATE_CODES = {4, 17, 32, 613} | set(range(80001, 80015))
TRANSIENT_CODES = {1, 2}


@dataclass(frozen=True)
class MetaConfig:
    page_id: str
    page_token: str
    instagram_id: str = ""
    app_secret: str = ""
    graph_version: str = DEFAULT_GRAPH_VERSION
    timeout_s: float = 60.0

    @classmethod
    def from_env(cls) -> MetaConfig:
        page_id = os.environ.get("META_PAGE_ID", "").strip()
        token = os.environ.get("META_PAGE_ACCESS_TOKEN", "").strip()
        if not page_id or not token:
            raise ConfigError(
                "PUBLISHING_PROVIDERS includes 'meta', so META_PAGE_ID and "
                "META_PAGE_ACCESS_TOKEN are required (see .env.example and docs/PUBLISHING.md)."
            )
        version = os.environ.get("META_GRAPH_VERSION", "").strip() or DEFAULT_GRAPH_VERSION
        if not version.startswith("v"):
            raise ConfigError("META_GRAPH_VERSION looks like v24.0.")
        return cls(
            page_id=page_id, page_token=token,
            instagram_id=os.environ.get("META_INSTAGRAM_ACCOUNT_ID", "").strip(),
            app_secret=os.environ.get("META_APP_SECRET", "").strip(),
            graph_version=version,
        )


class MediaSource:
    """How the provider reaches one job's original: bytes for Facebook, a URL for Instagram.
    Built per publication by the delivery loop (app/publishing/media.py)."""

    def open(self):  # -> context manager yielding a local path
        raise NotImplementedError

    def public_url(self) -> str:
        raise NotImplementedError


def _error_of(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body.get("error") or {} if isinstance(body, dict) else {}


def _retry_after(response: httpx.Response) -> float:
    """Seconds until Meta lets this app or Page call again, from its usage headers."""
    for header in ("x-business-use-case-usage", "x-app-usage"):
        raw = response.headers.get(header)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        entries = [e for v in data.values() for e in (v if isinstance(v, list) else [v])] \
            if header == "x-business-use-case-usage" else [data]
        minutes = max((float(e.get("estimated_time_to_regain_access") or 0)
                       for e in entries if isinstance(e, dict)), default=0)
        if minutes:
            return minutes * 60
    return 300.0


def raise_for(response: httpx.Response) -> None:
    """Translate a Graph API failure into the delivery loop's error types."""
    if response.status_code < 400:
        return
    err = _error_of(response)
    code = err.get("code")
    message = (err.get("error_user_msg") or err.get("message") or
               f"Meta answered HTTP {response.status_code}")
    detail = f"{message} (Meta error {code}" + (
        f"/{err['error_subcode']})" if err.get("error_subcode") else ")")
    if response.status_code >= 500 or err.get("is_transient") or code in TRANSIENT_CODES:
        raise ProviderUnavailable(detail)
    if response.status_code == 429 or code in RATE_CODES:
        raise ProviderRateLimited(detail, retry_after=_retry_after(response))
    if response.status_code == 401 or code in AUTH_CODES:
        raise AuthorizationLost(
            f"{detail}. Check the token and its permissions in .env, then Reconnect.")
    raise ContentRejected(detail)


def caption(payload: Payload) -> str:
    """The body plus any hashtags the body doesn't already carry."""
    text = payload.body.strip()
    extra = [t if t.startswith("#") else f"#{t}" for t in payload.hashtags]
    extra = [t for t in extra if t.lower() not in text.lower()]
    return f"{text}\n\n{' '.join(extra)}" if extra else text


class MetaProvider(Provider):
    info = ProviderInfo(
        name="meta", display_name="Meta (Facebook and Instagram)", sandbox=False,
        channels=("facebook", "instagram"), idempotent=False, needs_media=True,
        operator_only=True,
    )

    def __init__(self, config: MetaConfig, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.http = httpx.Client(timeout=config.timeout_s, transport=transport)
        self.sleep = sleep

    @classmethod
    def from_env(cls, _session_factory=None) -> MetaProvider:
        return cls(MetaConfig.from_env())

    # ── HTTP ─────────────────────────────────────────────────────────────────────────────

    def _params(self, token: str, extra: dict | None = None) -> dict:
        params = {"access_token": token, **(extra or {})}
        if self.config.app_secret:  # proves the call comes from the app's own server
            params["appsecret_proof"] = hmac.new(
                self.config.app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()
        return params

    def _call(self, method: str, path: str, token: str, *, base: str = GRAPH,
              data: dict | None = None, files: dict | None = None,
              params: dict | None = None) -> dict:
        url = f"{base}/{self.config.graph_version}/{path.lstrip('/')}"
        try:
            response = self.http.request(method, url, params=self._params(token, params),
                                         data=data, files=files)
        except httpx.TimeoutException as err:
            raise ProviderUnavailable(f"Meta did not answer in time ({err})") from None
        except httpx.HTTPError as err:
            raise ProviderUnavailable(f"Could not reach Meta ({err})") from None
        raise_for(response)
        try:
            return response.json()
        except ValueError:
            raise ProviderUnavailable("Meta sent a reply that isn't JSON") from None

    # ── Connecting ───────────────────────────────────────────────────────────────────────

    def authorization_url(self, *, state, code_challenge, redirect_uri, options) -> str:
        # No consent screen: the operator already granted access when creating the token.
        # Straight to the app's callback, which verifies the token (exchange_code).
        return f"/#/publishing/callback?state={state}&code=configured"

    def exchange_code(self, *, code, code_verifier, redirect_uri, options,
                      channel: str = "facebook") -> Grant:
        token = self.config.page_token
        try:
            if channel == "instagram":
                if not self.config.instagram_id:
                    raise AuthorizationDenied(
                        "Set META_INSTAGRAM_ACCOUNT_ID in .env to connect Instagram.")
                if not os.environ.get("PUBLIC_BASE_URL", "").strip().startswith("https://"):
                    raise AuthorizationDenied(
                        "Instagram fetches the photo or video itself, so PUBLIC_BASE_URL must "
                        "be this app's public https:// address.")
                me = self._call("GET", self.config.instagram_id, token,
                                params={"fields": "id,username"})
                return Grant(me["id"], f"@{me.get('username') or me['id']}",
                             Credentials(access_token=token))
            me = self._call("GET", self.config.page_id, token, params={"fields": "id,name"})
            return Grant(me["id"], me.get("name") or me["id"], Credentials(access_token=token))
        except AuthorizationLost as err:
            raise AuthorizationDenied(str(err)) from None
        except ContentRejected as err:  # e.g. a wrong id: "Unsupported get request"
            raise AuthorizationDenied(f"Meta refused the account id: {err}") from None

    def refresh(self, credentials: Credentials, *, account_id: str) -> Credentials:
        return credentials  # system-user Page tokens don't expire

    # ── Publishing ───────────────────────────────────────────────────────────────────────

    def publish(self, credentials: Credentials, payload: Payload, *, account_id: str,
                options: dict, media: MediaSource | None = None) -> Receipt:
        if media is None:
            raise ContentRejected("The original photo or video is gone, so there's nothing "
                                  "to post. The drafts are kept.")
        if payload.channel == "instagram":
            return self._instagram(credentials.access_token, payload, account_id, media)
        return self._facebook(credentials.access_token, payload, account_id, media)

    def _facebook(self, token: str, payload: Payload, page: str, media: MediaSource) -> Receipt:
        text = caption(payload)
        with media.open() as path, open(path, "rb") as f:
            if payload.media_kind == "video":
                out = self._call("POST", f"{page}/videos", token, base=GRAPH_VIDEO,
                                 data={"description": text}, files={"source": f})
                return Receipt("processing", f"video:{out['id']}",
                               f"https://www.facebook.com/{page}/videos/{out['id']}")
            out = self._call("POST", f"{page}/photos", token,
                             data={"message": text, "published": "true"}, files={"source": f})
        post = out.get("post_id") or out["id"]
        return Receipt("published", post, f"https://www.facebook.com/{post}")

    def _instagram(self, token: str, payload: Payload, ig: str, media: MediaSource) -> Receipt:
        text = caption(payload)
        if len(text) > IG_CAPTION_MAX:
            raise ContentRejected(f"Instagram captions are at most {IG_CAPTION_MAX} characters; "
                                  f"this one has {len(text)}.")
        if text.count("#") > IG_HASHTAG_MAX:
            raise ContentRejected(f"Instagram allows at most {IG_HASHTAG_MAX} hashtags.")
        data = {"caption": text}
        if payload.media_kind == "video":
            data |= {"media_type": "REELS", "video_url": media.public_url()}
        else:
            data |= {"image_url": media.public_url()}
        container = self._call("POST", f"{ig}/media", token, data=data)["id"]
        if payload.media_kind != "video":
            for wait in IG_IMAGE_WAIT_S:
                receipt = self._container(token, ig, container)
                if receipt.state == "published":
                    return receipt
                self.sleep(wait)
        return Receipt("processing", CONTAINER + container)

    def _container(self, token: str, ig: str, container: str) -> Receipt:
        status = self._call("GET", container, token, params={"fields": "status_code,status"})
        code = status.get("status_code")
        if code in ("ERROR", "EXPIRED"):
            raise ContentRejected("Instagram could not process the media: "
                                  f"{status.get('status') or code}")
        if code not in ("FINISHED", "PUBLISHED"):
            return Receipt("processing", CONTAINER + container)
        media_id = self._call("POST", f"{ig}/media_publish", token,
                              data={"creation_id": container})["id"]
        try:
            link = self._call("GET", media_id, token, params={"fields": "permalink"})
            url = link.get("permalink")
        except PublishError:  # published; the link is a nicety
            url = None
        return Receipt("published", media_id, url)

    def check(self, credentials: Credentials, external_id: str, *, account_id: str,
              options: dict) -> Receipt:
        token = credentials.access_token
        if external_id.startswith(CONTAINER):
            return self._container(token, account_id, external_id[len(CONTAINER):])
        if external_id.startswith("video:"):
            video = external_id[len("video:"):]
            out = self._call("GET", video, token, params={"fields": "status,permalink_url"})
            state = (out.get("status") or {}).get("video_status")
            url = out.get("permalink_url")
            url = f"https://www.facebook.com{url}" if url and url.startswith("/") else url
            if state == "error":
                raise ContentRejected("Facebook could not process the video")
            if state == "ready":
                return Receipt("published", video, url)
            return Receipt("processing", external_id, url)
        return Receipt("published", external_id)
