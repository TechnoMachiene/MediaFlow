"""Connect a queue worker to the durable side: DB hooks, media storage, reconciler, retention.

Used by `python -m app.cli worker` and by the API's embedded worker (EMBEDDED_WORKER=1).
"""

from __future__ import annotations

from app.api.notifications import EmailOutbox, Mailer, publication_outcome
from app.api.persistence import attach
from app.api.reconcile import Reconciler
from app.api.retention import prune_media
from app.publishing.crypto import Cipher
from app.publishing.delivery import Deliverer
from app.publishing.media import MediaResolver
from app.publishing.registry import build_providers
from app.storage import MediaStorage
from app.wiring import Runtime

RECONCILE_EVERY_S = 30.0
RETENTION_EVERY_S = 3600.0
DELIVERY_EVERY_S = 10.0
NOTIFY_EVERY_S = 15.0


def configure_worker(
    rt: Runtime,
    session_factory,
    storage: MediaStorage,
    *,
    retention_days: int,
    retention_max_days: int = 0,
    reconcile_every_s: float = RECONCILE_EVERY_S,
    retention_every_s: float = RETENTION_EVERY_S,
    publishing: tuple[tuple[str, ...], Cipher] | None = None,
    delivery_every_s: float = DELIVERY_EVERY_S,
    publish_signer=None,  # MediaLinkSigner(jwt_secret, PUBLISHING), for Instagram's media URLs
    notifications_every_s: float = NOTIFY_EVERY_S,
) -> Reconciler:
    attach(rt.worker, session_factory)
    rt.worker.materialize = storage.materialize
    reconciler = Reconciler(session_factory, rt.store, rt.queue, storage)
    rt.worker.every(reconcile_every_s, reconciler.run_once)

    def retention() -> dict:
        return prune_media(session_factory, storage, retention_days=retention_days,
                           max_days=retention_max_days)

    rt.worker.every(retention_every_s, retention)

    if publishing is not None and publishing[0]:
        names, cipher = publishing
        deliverer = Deliverer(
            session_factory, build_providers(names, session_factory), cipher,
            media=MediaResolver(storage, publish_signer), notify=publication_outcome,
        )
        rt.worker.every(delivery_every_s, deliverer.run_once)

    mailer = Mailer.from_env()
    if mailer is not None:
        outbox = EmailOutbox(session_factory, mailer, cipher=publishing[1] if publishing else None)
        rt.worker.every(notifications_every_s, outbox.run_once)
    return reconciler
