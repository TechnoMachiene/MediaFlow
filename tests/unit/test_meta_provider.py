"""Contract tests for the Meta provider against recorded Graph API replies (no network).

The reply bodies below have the shape the Graph API documents and returns: ids, `post_id`,
container `status_code`, and the `error` object with `code`, `error_subcode`, `is_transient`
and `error_user_msg`.
"""

import hashlib
import hmac
import json
from contextlib import contextmanager
from urllib.parse import parse_qs

import httpx
import pytest

from app.publishing.base import (
    AuthorizationDenied,
    AuthorizationLost,
    ContentRejected,
    Credentials,
    Payload,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.publishing.meta import MetaConfig, MetaProvider, caption

pytestmark = pytest.mark.unit

PAGE, IG = "1111", "1784"
TOKEN = Credentials(access_token="page-token")


class Graph:
    """A scripted Graph API: (method, path) -> list of replies, used in order."""

    def __init__(self, routes: dict):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path.split("/", 2)[2]  # drop the /v24.0 prefix
        replies = self.routes.get((request.method, path))
        if not replies:
            return httpx.Response(404, json={"error": {"code": 803, "message": f"no {path}"}})
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        status, body, *headers = reply
        return httpx.Response(status, json=body, headers=headers[0] if headers else None)


def provider(routes, **cfg):
    graph = Graph(routes)
    config = MetaConfig(page_id=PAGE, page_token="page-token", instagram_id=IG, **cfg)
    return MetaProvider(config, transport=httpx.MockTransport(graph), sleep=lambda s: None), graph


class Media:
    def __init__(self, tmp_path):
        self.path = tmp_path / "photo.jpg"
        self.path.write_bytes(b"\xff\xd8 fake jpeg")

    @contextmanager
    def open(self):
        yield str(self.path)

    def public_url(self):
        return "https://media.example.com/api/v1/publish-media/signed"


def payload(channel="facebook", kind="image", body="Golden hour at MGC Jewel.",
            tags=("#MGCDevelopments",)):
    return Payload(idempotency_key="pub-1", channel=channel, body=body, hashtags=tags,
                   media_kind=kind, media_sha256=None, media_ref=None)


def form(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# ── Facebook ──────────────────────────────────────────────────────────────────────────────

def test_a_facebook_photo_is_uploaded_with_its_caption(tmp_path):
    meta, graph = provider({("POST", f"{PAGE}/photos"):
                            (200, {"id": "555", "post_id": f"{PAGE}_777"})})
    receipt = meta.publish(TOKEN, payload(), account_id=PAGE, options={}, media=Media(tmp_path))
    assert (receipt.state, receipt.external_id) == ("published", f"{PAGE}_777")
    assert receipt.url == f"https://www.facebook.com/{PAGE}_777"
    sent = graph.calls[0]
    assert sent.url.host == "graph.facebook.com" and sent.url.params["access_token"] == "page-token"
    assert b"Golden hour at MGC Jewel." in sent.content and b"#MGCDevelopments" in sent.content
    assert b"fake jpeg" in sent.content  # the bytes themselves, no public URL needed


def test_a_facebook_video_is_processing_until_meta_says_ready(tmp_path):
    meta, graph = provider({
        ("POST", f"{PAGE}/videos"): (200, {"id": "999"}),
        ("GET", "999"): [(200, {"status": {"video_status": "processing"}}),
                         (200, {"status": {"video_status": "ready"},
                                "permalink_url": f"/{PAGE}/videos/999/"})],
    })
    receipt = meta.publish(TOKEN, payload(kind="video"), account_id=PAGE, options={},
                           media=Media(tmp_path))
    assert receipt.state == "processing" and receipt.external_id == "video:999"
    assert graph.calls[0].url.host == "graph-video.facebook.com"
    assert meta.check(TOKEN, "video:999", account_id=PAGE, options={}).state == "processing"
    done = meta.check(TOKEN, "video:999", account_id=PAGE, options={})
    assert (done.state, done.url) == ("published", f"https://www.facebook.com/{PAGE}/videos/999/")


def test_a_video_meta_could_not_process_is_rejected(tmp_path):
    meta, _ = provider({("GET", "999"): (200, {"status": {"video_status": "error"}})})
    with pytest.raises(ContentRejected):
        meta.check(TOKEN, "video:999", account_id=PAGE, options={})


# ── Instagram ─────────────────────────────────────────────────────────────────────────────

def test_an_instagram_photo_goes_through_a_container_then_media_publish(tmp_path):
    meta, graph = provider({
        ("POST", f"{IG}/media"): (200, {"id": "c-1"}),
        ("GET", "c-1"): [(200, {"status_code": "IN_PROGRESS"}), (200, {"status_code": "FINISHED"})],
        ("POST", f"{IG}/media_publish"): (200, {"id": "m-1"}),
        ("GET", "m-1"): (200, {"permalink": "https://www.instagram.com/p/abc/"}),
    })
    receipt = meta.publish(TOKEN, payload("instagram"), account_id=IG, options={},
                           media=Media(tmp_path))
    assert (receipt.state, receipt.external_id, receipt.url) == (
        "published", "m-1", "https://www.instagram.com/p/abc/")
    create = form(graph.calls[0])
    assert create["image_url"] == "https://media.example.com/api/v1/publish-media/signed"
    assert create["caption"].endswith("#MGCDevelopments")
    assert form(graph.calls[3]) == {"creation_id": "c-1"}


def test_a_slow_instagram_container_is_left_to_the_delivery_loop(tmp_path):
    meta, _ = provider({
        ("POST", f"{IG}/media"): (200, {"id": "c-2"}),
        ("GET", "c-2"): (200, {"status_code": "IN_PROGRESS"}),
    })
    receipt = meta.publish(TOKEN, payload("instagram"), account_id=IG, options={},
                           media=Media(tmp_path))
    assert (receipt.state, receipt.external_id) == ("processing", "container:c-2")


def test_an_instagram_reel_is_polled_and_a_failed_one_is_rejected(tmp_path):
    meta, graph = provider({
        ("POST", f"{IG}/media"): (200, {"id": "c-3"}),
        ("GET", "c-3"): (200, {"status_code": "ERROR", "status": "Error: video too long"}),
    })
    receipt = meta.publish(TOKEN, payload("instagram", "video"), account_id=IG, options={},
                           media=Media(tmp_path))
    assert receipt.state == "processing" and len(graph.calls) == 1  # no waiting for video
    assert form(graph.calls[0])["media_type"] == "REELS"
    with pytest.raises(ContentRejected, match="video too long"):
        meta.check(TOKEN, receipt.external_id, account_id=IG, options={})


def test_instagram_limits_are_enforced_before_calling_meta(tmp_path):
    meta, graph = provider({})
    with pytest.raises(ContentRejected, match="2200"):
        meta.publish(TOKEN, payload("instagram", body="x" * 2300), account_id=IG, options={},
                     media=Media(tmp_path))
    with pytest.raises(ContentRejected, match="nothing to post"):
        meta.publish(TOKEN, payload(), account_id=PAGE, options={}, media=None)
    assert graph.calls == []


# ── Errors ────────────────────────────────────────────────────────────────────────────────

def _err(code, status=400, subcode=None, transient=False, msg="boom"):
    body = {"error": {"message": msg, "type": "OAuthException", "code": code,
                      "is_transient": transient, "fbtrace_id": "A1"}}
    if subcode:
        body["error"]["error_subcode"] = subcode
    return (status, body)


@pytest.mark.parametrize(("reply", "kind"), [
    (_err(190, subcode=463, msg="Session has expired"), AuthorizationLost),
    (_err(200, status=403, msg="Requires pages_manage_posts"), AuthorizationLost),
    (_err(4, msg="Application request limit reached"), ProviderRateLimited),
    (_err(32, msg="Page request limit reached"), ProviderRateLimited),
    (_err(2, status=500, transient=True, msg="Service temporarily unavailable"),
     ProviderUnavailable),
    (_err(1, transient=True, msg="An unknown error occurred"), ProviderUnavailable),
    ((503, {}), ProviderUnavailable),
    (_err(100, msg="Invalid parameter"), ContentRejected),
    (_err(368, msg="The action attempted has been deemed abusive"), ContentRejected),
])
def test_graph_errors_map_to_the_delivery_loops_types(tmp_path, reply, kind):
    meta, _ = provider({("POST", f"{PAGE}/photos"): reply})
    with pytest.raises(kind):
        meta.publish(TOKEN, payload(), account_id=PAGE, options={}, media=Media(tmp_path))


def test_rate_limits_wait_as_long_as_meta_asks(tmp_path):
    usage = {PAGE: [{"type": "pages", "call_count": 100,
                     "estimated_time_to_regain_access": 7}]}
    reply = (*_err(32), {"x-business-use-case-usage": json.dumps(usage)})
    meta, _ = provider({("POST", f"{PAGE}/photos"): reply})
    with pytest.raises(ProviderRateLimited) as err:
        meta.publish(TOKEN, payload(), account_id=PAGE, options={}, media=Media(tmp_path))
    assert err.value.retry_after == 7 * 60


def test_timeouts_are_transient(tmp_path):
    meta, _ = provider({("POST", f"{PAGE}/photos"): httpx.ReadTimeout("slow")})
    with pytest.raises(ProviderUnavailable):
        meta.publish(TOKEN, payload(), account_id=PAGE, options={}, media=Media(tmp_path))


def test_the_app_secret_proof_is_sent_when_configured(tmp_path):
    meta, graph = provider({("GET", PAGE): (200, {"id": PAGE, "name": "MGC Developments"})},
                           app_secret="s3cret")
    meta.exchange_code(code="configured", code_verifier="", redirect_uri="", options={},
                       channel="facebook")
    expected = hmac.new(b"s3cret", b"page-token", hashlib.sha256).hexdigest()
    assert graph.calls[0].url.params["appsecret_proof"] == expected


# ── Connecting ────────────────────────────────────────────────────────────────────────────

def test_connecting_verifies_the_token_and_names_the_account(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://media.example.com")
    meta, _ = provider({("GET", PAGE): (200, {"id": PAGE, "name": "MGC Developments"}),
                        ("GET", IG): (200, {"id": IG, "username": "mgcdevelopments"})})
    page = meta.exchange_code(code="configured", code_verifier="", redirect_uri="", options={},
                              channel="facebook")
    ig = meta.exchange_code(code="configured", code_verifier="", redirect_uri="", options={},
                            channel="instagram")
    assert (page.account_id, page.account_name) == (PAGE, "MGC Developments")
    assert (ig.account_id, ig.account_name) == (IG, "@mgcdevelopments")
    assert page.credentials.access_token == "page-token"
    assert meta.refresh(page.credentials, account_id=PAGE) is page.credentials


@pytest.mark.parametrize(("env", "routes", "match"), [
    ({}, {}, "PUBLIC_BASE_URL"),
    ({"PUBLIC_BASE_URL": "http://insecure.example.com"}, {}, "PUBLIC_BASE_URL"),
    ({"PUBLIC_BASE_URL": "https://m.example.com"}, {("GET", IG): _err(190)}, "token"),
    ({"PUBLIC_BASE_URL": "https://m.example.com"}, {("GET", IG): _err(100)}, "account id"),
])
def test_instagram_connect_explains_what_is_missing(monkeypatch, env, routes, match):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    meta, _ = provider(routes)
    with pytest.raises(AuthorizationDenied, match=match):
        meta.exchange_code(code="configured", code_verifier="", redirect_uri="", options={},
                           channel="instagram")


def test_hashtags_already_in_the_caption_are_not_repeated():
    assert caption(payload(body="Hello #MGCDevelopments", tags=("#MGCDevelopments", "Rawalpindi"))
                   ) == "Hello #MGCDevelopments\n\n#Rawalpindi"


def test_config_needs_the_page_and_token(monkeypatch):
    from app.errors import ConfigError

    for name in ("META_PAGE_ID", "META_PAGE_ACCESS_TOKEN", "META_GRAPH_VERSION"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError, match="META_PAGE_ID"):
        MetaConfig.from_env()
    monkeypatch.setenv("META_PAGE_ID", PAGE)
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "t")
    monkeypatch.setenv("META_GRAPH_VERSION", "24.0")
    with pytest.raises(ConfigError, match="v24.0"):
        MetaConfig.from_env()
