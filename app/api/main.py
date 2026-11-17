from __future__ import annotations

import logging
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.api.audit_api import router as audit_router
from app.api.config import ApiSettings
from app.api.database import (
    ApiJob,
    Base,
    ScheduledPost,
    User,
    Workspace,
    create_database,
)
from app.api.drafts import router as drafts_router
from app.api.publishing import router as publishing_router
from app.api.routes import router
from app.api.worker_setup import configure_worker
from app.api.workspaces import router as workspaces_router
from app.config import Settings
from app.obs import metrics
from app.obs.http import ObservabilityMiddleware
from app.obs.logs import configure_logging
from app.obs.tracing import configure_tracing, instrument_app, instrument_engine
from app.publishing.registry import build_providers
from app.wiring import Runtime, build_runtime

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
log = logging.getLogger("api")


def create_app(
    *,
    api_settings: ApiSettings | None = None,
    app_settings: Settings | None = None,
    runtime: Runtime | None = None,
    engine=None,
    session_factory=None,
    storage=None,
    frontend_dir: Path | None = FRONTEND_DIST,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        load_dotenv()
        configure_logging("mediaflow-api")
        configure_tracing("mediaflow-api")
        cfg = api_settings or ApiSettings.from_env()
        rt = runtime or build_runtime(app_settings or Settings.from_env())
        db_engine = engine
        db_sessions = session_factory
        if db_engine is None or db_sessions is None:
            db_engine, db_sessions = create_database(cfg.database_url)
            instrument_engine(db_engine)
        metrics.watch_queue(rt)
        metrics.serve(metrics.metrics_port())
        if cfg.demo_mode:
            if cfg.database_url.startswith("sqlite"):
                Base.metadata.create_all(db_engine)  # zero-setup demo database
            from app.demo import demo_banner, seed

            seed(db_sessions)
            print(demo_banner(), file=sys.stderr, flush=True)
        app.state.api_settings = cfg
        app.state.runtime = rt
        app.state.engine = db_engine
        app.state.session_factory = db_sessions
        app.state.storage = storage or cfg.build_storage()
        app.state.cipher = cfg.build_cipher()
        app.state.providers = build_providers(cfg.publishing_providers, db_sessions)
        app.state.owns_runtime = runtime is None
        app.state.owns_engine = engine is None

        stop, worker_thread = threading.Event(), None
        if cfg.embedded_worker:
            configure_worker(
                rt, db_sessions, app.state.storage, retention_days=cfg.media_retention_days,
                publishing=(cfg.publishing_providers, app.state.cipher),
            )
            worker_thread = threading.Thread(
                target=rt.worker.run, args=(stop,), name="embedded-worker", daemon=True
            )
            worker_thread.start()
            log.info("embedded queue worker started (EMBEDDED_WORKER=1)")
        try:
            yield
        finally:
            stop.set()
            if worker_thread is not None:
                worker_thread.join(timeout=10)
            if app.state.owns_runtime:
                rt.r.close()
            if app.state.owns_engine:
                db_engine.dispose()

    application = FastAPI(
        title="Media Queue API",
        description=(
            "Authenticated media analysis, review, and post scheduling APIs."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    application.include_router(router)
    application.include_router(workspaces_router)
    application.include_router(audit_router)
    application.include_router(drafts_router)
    application.include_router(publishing_router)
    # Order matters: tracing is added last so it is outermost, and the access log written by
    # ObservabilityMiddleware happens inside the request span (and so carries its trace_id).
    application.add_middleware(ObservabilityMiddleware)
    instrument_app(application)

    @application.get("/health/live", tags=["health"])
    def liveness():
        return {"status": "alive"}

    @application.get("/health/ready", tags=["health"])
    def readiness(request: Request):
        try:
            with request.app.state.session_factory() as session:
                session.execute(text("SELECT 1"))
                session.execute(select(User.id).limit(0))
                session.execute(select(ApiJob.id).limit(0))
                session.execute(select(ScheduledPost.id).limit(0))
                session.execute(select(Workspace.id).limit(0))
            request.app.state.runtime.r.ping()
        except Exception:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Database or Redis is unavailable",
            ) from None
        return {"status": "ready"}

    # Serve the built web app (frontend/dist) from the same origin when it exists.
    # Mounted last so API, docs, and health routes always win.
    if frontend_dir is not None and (frontend_dir / "index.html").is_file():
        application.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return application


app = create_app()
