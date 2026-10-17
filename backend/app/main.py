"""FastAPI + Socket.IO application — Flowmentor CRM."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.core.errors import install_error_handlers
from app.core.events import get_bus
from app.core.middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from app.core.scheduler import Scheduler
from app.db import init_models

logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("flowmentor")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
scheduler = Scheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_prod:
        problems = []
        if settings.secret_key.startswith("change-me") or len(settings.secret_key) < 32:
            problems.append("SECRET_KEY padrão/curta")
        if not settings.kek_keys:
            problems.append("KEK_KEYS ausente (envelope encryption)")
        if settings.meta_mode == "live" and not settings.meta_app_secret:
            problems.append("META_APP_SECRET ausente (assinatura de webhooks)")
        if settings.is_sqlite:
            problems.append("SQLite não é permitido em produção")
        if not settings.bootstrap_admin_password and not settings.seed_demo_data:
            problems.append("BOOTSTRAP_ADMIN_PASSWORD ausente — defina uma senha forte para o primeiro acesso")
        if problems:
            raise RuntimeError("Configuração insegura para produção: " + "; ".join(problems))
    if settings.is_sqlite or settings.environment != "production":
        await init_models()
    from app.registry import register_all
    from app.seed import bootstrap

    await bootstrap()
    bus = get_bus()
    register_all(bus)
    if settings.run_workers_in_process:
        await bus.start()
        await scheduler.start()
    log.info("Flowmentor CRM pronto (%s, Meta=%s, bus=%s)", settings.environment, settings.meta_mode, type(bus).__name__)
    yield
    await scheduler.stop()
    await bus.stop()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan, docs_url="/docs", redoc_url=None,
                  description="CRM B2B de saúde com BSP de WhatsApp (Cloud API), pipeline comercial, casos operacionais, contratos e LGPD.")
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False, allow_methods=["*"], allow_headers=["*"], max_age=600)
    install_error_handlers(app)

    from app.modules.auth.router import router as auth_router
    from app.modules.core.router import router as core_router
    from app.modules.dashboard.router import router as dashboard_router
    from app.modules.lgpd.router import router as lgpd_router
    from app.modules.openapi.router import router as openapi_router
    from app.modules.ops.router import router as ops_router
    from app.modules.pipeline.router import public_router as proposals_public, router as pipeline_router
    from app.modules.webhooks.router import router as webhooks_out_router
    from app.modules.whatsapp import kpis, media, supervisor, templates, tickets_router, webhook
    from app.modules.whatsapp.accounts_router import router as wa_accounts_router

    for r in (auth_router, core_router, pipeline_router, proposals_public, ops_router, tickets_router.router, templates.router, wa_accounts_router, supervisor.router, kpis.router,
              lgpd_router, webhooks_out_router, openapi_router, dashboard_router, webhook.router, media.router):
        app.include_router(r)

    @app.get("/api/health", tags=["Infra"])
    async def health():
        return {"status": "ok", "env": settings.environment, "meta_mode": settings.meta_mode}

    @app.get("/api/v1/meta", tags=["Infra"])
    async def public_meta():
        return {"name": settings.app_name, "meta_mode": settings.meta_mode, "meta_app_id": settings.meta_app_id or None}

    if FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")

        @app.get("/", include_in_schema=False)
        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str = ""):
            if path.startswith(("api/", "webhooks/", "media/", "socket.io", "p/")):
                return JSONResponse({"error": {"code": "not_found", "message": "Rota não encontrada"}}, status_code=404)
            return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


fastapi_app = create_app()

from app.modules.whatsapp.realtime import sio  # noqa: E402

# root ASGI: Socket.IO on /socket.io, everything else goes to FastAPI
app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app, socketio_path="/socket.io")
