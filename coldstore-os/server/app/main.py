"""ColdStore OS server — app factory. Static PWA at '/', versioned API at /api/v1."""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import idempotency as idem
from .api import (routes_admin, routes_auth, routes_billing, routes_dispatch,
                  routes_intake, routes_inventory, routes_masters, routes_parties,
                  routes_reports)
from .config import API_V1, ensure_dirs
from .db import engine
from .domain.inventory import DomainError

logging.basicConfig(level=logging.INFO,
                    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("coldstore")


def create_app() -> FastAPI:
    ensure_dirs()
    app = FastAPI(title="ColdStore OS — CA Apple Edition", version="1.0.0-m1",
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    # Token-in-header auth (no cookies) ⇒ CORS cannot be abused for CSRF; LAN clients
    # may be served from any host — keep permissive, restrict via env at hardening time.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"], allow_credentials=False)

    @app.exception_handler(DomainError)
    async def _domain(_req: Request, exc: DomainError):
        status = {"capacity": 409, "insufficient_stock": 409, "duplicate": 409,
                  "merge_incompatible": 409, "conflict": 409,
                  "forbidden": 403, "not_found": 404, "ownership": 409}.get(exc.code, 422)
        return JSONResponse(status_code=status,
                            content={"detail": exc.detail, "code": exc.code})

    @app.exception_handler(idem.Replay)
    async def _replay(_req: Request, exc: idem.Replay):
        return JSONResponse(status_code=exc.status_code, content=exc.payload,
                            headers={"Idempotent-Replay": "true"})

    @app.middleware("http")
    async def _access_log(request: Request, call_next):
        rid = str(uuid.uuid4())[:8]
        start = time.time()
        resp = await call_next(request)
        if request.url.path.startswith(API_V1):
            log.info("req %s %s %s -> %s (%.0fms)", rid, request.method,
                     request.url.path, resp.status_code, (time.time() - start) * 1000)
        resp.headers["X-Request-Id"] = rid
        return resp

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}

    for r in (routes_auth.router, routes_masters.router, routes_parties.router,
              routes_intake.router, routes_inventory.router, routes_dispatch.router,
              routes_billing.router, routes_reports.router, routes_admin.router):
        app.include_router(r, prefix=API_V1)

    # ---------------- Phase 2 telemetry (isolated, monitoring-only) ----------------
    from telemetry import api as tapi
    from telemetry.tdb import init_schema
    init_schema()
    app.include_router(tapi.router, prefix=API_V1)

    # Server-owned staleness sweep: silence → STALE / gateway-down detection runs
    # even with no browser open and no frames arriving.
    from .config import TELEMETRY_SWEEP_INTERVAL_S

    @app.on_event("startup")
    async def _telemetry_sweep():
        import asyncio
        from datetime import datetime
        from telemetry.service import evaluate_staleness
        from telemetry.tdb import TSessionLocal

        async def loop() -> None:
            while True:
                await asyncio.sleep(TELEMETRY_SWEEP_INTERVAL_S)
                try:
                    with TSessionLocal() as s:
                        evaluate_staleness(s, datetime.utcnow())
                        s.commit()
                except Exception as exc:  # sweep must never crash the app
                    log.warning("telemetry sweep failed: %s", exc)

        app.state.telemetry_sweeper = asyncio.create_task(loop())

    @app.on_event("shutdown")
    async def _telemetry_sweep_stop():
        task = getattr(app.state, "telemetry_sweeper", None)
        if task is not None:
            task.cancel()

    from .config import TELEMETRY_SIM, TELEMETRY_SIM_INTERVAL_S, TELEMETRY_TOKEN
    if TELEMETRY_SIM:
        import asyncio
        import threading
        from telemetry.sim import sim_loop
        # The demo simulator runs in-process as a background thread and POSTs
        # through the REAL ingest path (validation/quarantine included) using the
        # configured device token. Disabled unless COLDSTORE_TELEMETRY_SIM=1 AND a
        # token is set — production deployments must run a real (or bench) adapter.
        if TELEMETRY_TOKEN:
            @app.on_event("startup")
            async def _start_sim():
                stop = threading.Event()
                t = threading.Thread(target=sim_loop, args=(stop,), daemon=True,
                                     name="ca-sim")
                t.start()
                app.state.telemetry_sim_stop = stop
                log.warning("COLDSTORE_TELEMETRY_SIM=1 — telemetry data is SIMULATED")

            @app.on_event("shutdown")
            async def _stop_sim():
                app.state.telemetry_sim_stop.set()
        else:
            log.warning("TELEMETRY_SIM requested but COLDSTORE_TELEMETRY_TOKEN unset — sim not started")

    from pathlib import Path
    static_dir = Path(__file__).resolve().parent.parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    return app


app = create_app()
