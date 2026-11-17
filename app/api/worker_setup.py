"""Connect a queue worker to the durable side: DB hooks, media storage, reconciler, retention.

Used by `python -m app.cli worker` and by the API's embedded worker (EMBEDDED_WORKER=1).
"""

from __future__ import annotations

from app.api.persistence import attach
from app.api.reconcile import Reconciler
from app.api.retention import prune_media
from app.publishing.crypto import Cipher
from app.publishing.delivery import Deliverer
from app.publishing.registry import build_providers
from app.storage import MediaStorage
from app.wiring import Runtime

RECONCILE_EVERY_S = 30.0
RETENTION_EVERY_S = 3600.0
DELIVERY_EVERY_S = 10.0


def configure_worker(
    rt: Runtime,
    session_factory,
    storage: MediaStorage,
    *,
    retention_days: int,
    reconcile_every_s: float = RECONCILE_EVERY_S,
    retention_every_s: float = RETENTION_EVERY_S,
    publishing: tuple[tuple[str, ...], Cipher] | None = None,
    delivery_every_s: float = DELIVERY_EVERY_S,
) -> Reconciler:
    attach(rt.worker, session_factory)
    rt.worker.materialize = storage.materialize
    reconciler = Reconciler(session_factory, rt.store, rt.queue, storage)
    rt.worker.every(reconcile_every_s, reconciler.run_once)

    def retention() -> dict:
        return prune_media(session_factory, storage, retention_days=retention_days)

    rt.worker.every(retention_every_s, retention)

    if publishing is not None and publishing[0]:
        names, cipher = publishing
        deliverer = Deliverer(session_factory, build_providers(names, session_factory), cipher)
        rt.worker.every(delivery_every_s, deliverer.run_once)
    return reconciler
