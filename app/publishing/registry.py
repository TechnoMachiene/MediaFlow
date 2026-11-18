"""Which providers this installation enables (PUBLISHING_PROVIDERS).

`mock` is the sandbox. `meta` posts to a real Facebook Page and its Instagram business account
with the credentials in .env (app/publishing/meta.py; docs/PUBLISHING.md, "Meta"). Any other
name is a configuration error, so nothing can be switched on by accident.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy.orm import Session

from app.errors import ConfigError
from app.publishing.base import Provider
from app.publishing.meta import MetaProvider
from app.publishing.mock import MockProvider

AVAILABLE: dict[str, Callable[[Callable[[], Session]], Provider]] = {
    "mock": MockProvider,
    "meta": MetaProvider.from_env,
}


def build_providers(names: Iterable[str], session_factory) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for name in names:
        if name not in AVAILABLE:
            raise ConfigError(
                f"PUBLISHING_PROVIDERS: {name!r} is not implemented. Use 'mock' (the sandbox) "
                "and/or 'meta' (Facebook and Instagram); see docs/PUBLISHING.md."
            )
        providers[name] = AVAILABLE[name](session_factory)
    return providers
