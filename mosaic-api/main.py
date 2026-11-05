"""Muse Mosaic public API.

Implemented (fail-closed where noted):
- GET /health — mosaic-api status + trust-engine reachability + active policy
  versions (read from the bundled policy mirror, and from trust-engine when up).
- GET /fragments/{entity_type}/{entity_id} — labelled synthetic fixtures.
- POST /resolve — ingest + mock propose + trust-engine derive + gated publish.
  Engine unreachable -> 503 with steward escalation, no golden, no decision row.
- GET /golden/{entity_type}/{entity_id} — latest golden or 404.
- GET /explain/{decision_id} — replay from the ledger (never vector search).
- POST /steward/{id}/approve|reject — steward decision as a new fact.
- POST /steward/{id}/split — restore a prior golden version from history.
- GET /steward/pending — steward queue rebuilt from SQLite on each request.
- GET /similar — structured similar search (vector recall skipped in v1).
- GET / — static two-column UI shell.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

import ledger
from config import Settings, get_settings
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from policies import PolicyError, load_policies
from routes import autonomy as autonomy_routes
from routes import build as build_routes
from routes import demo as demo_routes
from routes import downstream as downstream_routes
from routes import entities as entities_routes
from routes import explain as explain_routes
from routes import export as export_routes
from routes import golden as golden_routes
from routes import graph as graph_routes
from routes import health as health_routes
from routes import ingest as ingest_routes
from routes import jobs as jobs_routes
from routes import pipeline as pipeline_routes
from routes import reference as reference_routes
from routes import resolve as resolve_routes
from routes import similar as similar_routes
from routes import steward as steward_routes
from routes import upload as upload_routes
from routes.deps import (
    _load_expected_structures,
)
from taxonomies import load_taxonomies

logger = logging.getLogger("mosaic-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    ledger.init_db(settings.database_path)
    try:
        app.state.policies = load_policies()
    except PolicyError as exc:
        raise RuntimeError(f"cannot start without policies: {exc}") from exc
    try:

        app.state.taxonomies = load_taxonomies()
    except Exception as exc:
        raise RuntimeError(f"cannot start without taxonomies: {exc}") from exc
    tasks: list[asyncio.Task] = []
    if settings.enable_background_tasks:
        import worker as worker_module

        tasks.append(asyncio.create_task(worker_module.run_jobs_loop(app), name="job-worker"))
        if settings.inbox_dir:
            tasks.append(asyncio.create_task(worker_module.inbox_loop(app), name="inbox-poller"))
        if settings.analyser_interval_seconds > 0:
            tasks.append(asyncio.create_task(worker_module.analyser_loop(app), name="analyser"))
    yield
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    app_settings.validate_startup()

    app = FastAPI(
        title="Muse Mosaic",
        version="0.2.0",
        description=(
            "Many fragments, one trusted picture, with proof for every piece. "
            "Muse proposes; the manglekit trust-engine derives; "
            "the publisher writes only derived values."
        ),
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    # Demo-only engine kill switch (Act 5). Process-local, defaults off, a
    # restart always recovers. See routes/demo.py.
    app.state.engine_killed = False
    # Autonomy freeze (M3). Process-local, defaults off; a restart recovers.
    app.state.autonomy_frozen = False
    # Active policy profile for the dashboard; starts at the default.
    from policy_profiles import default_profile_id

    app.state.active_policy = default_profile_id()
    try:
        app.state.policies = load_policies()
    except PolicyError as exc:
        raise RuntimeError(f"cannot start without policies: {exc}") from exc
    try:

        app.state.taxonomies = load_taxonomies()
        app.state.expected_structures = _load_expected_structures()
    except Exception as exc:
        raise RuntimeError(f"cannot start without taxonomies: {exc}") from exc
    for routes_module in (
        health_routes,
        autonomy_routes,
        entities_routes,
        reference_routes,
        demo_routes,
        resolve_routes,
        jobs_routes,
        ingest_routes,
        golden_routes,
        explain_routes,
        export_routes,
        steward_routes,
        similar_routes,
        build_routes,
        upload_routes,
        graph_routes,
        downstream_routes,
        pipeline_routes,
    ):
        app.include_router(routes_module.router)

    if app_settings.api_token:

        @app.middleware("http")
        async def require_token(request: Request, call_next):
            """Gate every state-changing call (steward approve/reject, publish,
            jobs, uploads, policy apply). Reads stay open so the cockpit loads."""
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                supplied = request.headers.get("authorization", "")
                expected = f"Bearer {app_settings.api_token}"
                if not hmac.compare_digest(supplied.encode(), expected.encode()):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Missing or invalid API token."},
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            return await call_next(request)

    else:
        logging.getLogger(__name__).warning(
            "MOSAIC_API_TOKEN is not set: steward and publish routes are unauthenticated "
            "(acceptable for local dev only)."
        )

    app.mount(
        "/",
        StaticFiles(directory=str(app_settings.static_dir), html=True),
        name="static",
    )
    return app


app = create_app()
