"""Which providers this installation enables (PUBLISHING_PROVIDERS).

Only the sandbox exists today. A real platform is added here only after it passes the
readiness checklist in docs/PUBLISHING.md; until then naming one is a configuration error,
so nothing can be switched on by accident.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy.orm import Session

from app.errors import ConfigError
from app.publishing.base import Provider
from app.publishing.mock import MockProvider

AVAILABLE: dict[str, Callable[[Callable[[], Session]], Provider]] = {
    "mock": MockProvider,
}


def build_providers(names: Iterable[str], session_factory) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for name in names:
        if name not in AVAILABLE:
            raise ConfigError(
                f"PUBLISHING_PROVIDERS: {name!r} is not implemented. Only 'mock' (the sandbox) "
                "exists; see the readiness checklist in docs/PUBLISHING.md."
            )
        providers[name] = AVAILABLE[name](session_factory)
    return providers
