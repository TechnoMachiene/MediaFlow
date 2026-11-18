import json

import pytest

from app.errors import ConfigError
from app.publishing.base import AuthorizationLost, Credentials, pkce_s256
from app.publishing.compose import compose
from app.publishing.crypto import Cipher
from app.publishing.registry import build_providers

pytestmark = pytest.mark.unit


def test_pkce_matches_the_rfc_7636_example():
    assert pkce_s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


def test_compose_takes_the_channels_section():
    draft = json.dumps({
        "instagram": {"caption": " Pier ", "hashtags": ["#a", " ", "#b"]},
        "facebook": {"post_copy": "Long copy"},
        "tiktok": {"hashtags": ["#x"]},
    })
    assert compose(draft, "instagram") == ("Pier", ("#a", "#b"))
    assert compose("```json\n" + draft + "\n```", "facebook") == ("Long copy", ())
    with pytest.raises(ValueError, match="no caption"):
        compose(draft, "tiktok")
    with pytest.raises(ValueError, match="nothing for"):
        compose(json.dumps({"instagram": {"caption": "x"}}), "facebook")
    assert compose("  plain words ", "instagram") == ("plain words", ())


def test_credentials_round_trip_and_a_wrong_key_means_reconnect():
    sealed = Cipher(jwt_secret="s" * 40).seal_credentials(Credentials("at", "rt"))
    assert Cipher(jwt_secret="s" * 40).open_credentials(sealed).refresh_token == "rt"
    with pytest.raises(AuthorizationLost):
        Cipher(jwt_secret="t" * 40).open_credentials(sealed)


def test_only_implemented_providers_can_be_enabled(monkeypatch):
    assert list(build_providers(["mock"], session_factory=None)) == ["mock"]
    with pytest.raises(ConfigError, match="not implemented"):
        build_providers(["mock", "instagram"], session_factory=None)
    # Meta needs its credentials before it can be switched on.
    monkeypatch.delenv("META_PAGE_ID", raising=False)
    monkeypatch.delenv("META_PAGE_ACCESS_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="META_PAGE_ID"):
        build_providers(["meta"], session_factory=None)
    monkeypatch.setenv("META_PAGE_ID", "1234")
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "token")
    assert list(build_providers(["mock", "meta"], session_factory=None)) == ["mock", "meta"]
