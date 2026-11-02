from __future__ import annotations

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from sqlalchemy import select, text

from app.api.config import ApiSettings
from app.api.database import (
    ApiJob,
    ScheduledPost,
    User,
    create_database,
)
from app.api.routes import router
from app.config import Settings
from app.wiring import Runtime, build_runtime


def create_app(
    *,
    api_settings: ApiSettings | None = None,
    app_settings: Settings | None = None,
    runtime: Runtime | None = None,
    engine=None,
    session_factory=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        load_dotenv()
        cfg = api_settings or ApiSettings.from_env()
        rt = runtime or build_runtime(app_settings or Settings.from_env())
        db_engine = engine
        db_sessions = session_factory
        if db_engine is None or db_sessions is None:
            db_engine, db_sessions = create_database(cfg.database_url)
        app.state.api_settings = cfg
        app.state.runtime = rt
        app.state.engine = db_engine
        app.state.session_factory = db_sessions
        app.state.owns_runtime = runtime is None
        app.state.owns_engine = engine is None
        try:
            yield
        finally:
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
            request.app.state.runtime.r.ping()
        except Exception:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Database or Redis is unavailable",
            ) from None
        return {"status": "ready"}

    return application


app = create_app()
