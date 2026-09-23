"""FastAPI entry point. Run: uvicorn app.main:app --reload"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import dashboard, demo, emails, gmail
from app.api import errors as api_errors
from app.core.categories import get_categories
from app.core.config import AppEnv, get_settings
from app.core.logging import get_logger, setup_logging
from app.db.session import db_ping


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    categories = get_categories()  # fail fast on a bad categories.yaml
    summary = {**settings.public_summary(), "categories": len(categories.names)}
    get_logger().info("startup", extra={"fields": summary})
    yield


STATIC_DIR = Path(__file__).resolve().parent / "static"

# Strict policy: no inline scripts/styles, no third-party origins, no framing. Email
# content is rendered with textContent; this is the second line of defence.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def create_app() -> FastAPI:
    prod = get_settings().app_env == AppEnv.PROD
    # In production the interactive API docs are not exposed.
    app = FastAPI(
        title="Institutional Email AI",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if prod else "/docs",
        redoc_url=None if prod else "/redoc",
        openapi_url=None if prod else "/openapi.json",
    )

    @app.get("/health")
    def health() -> dict[str, object]:
        if get_settings().app_env == AppEnv.PROD:  # unauthenticated: no config details
            return {"status": "ok", "database_ok": db_ping()}
        return {
            "status": "ok",
            **get_settings().public_summary(),
            "database_ok": db_ping(),
            "categories": get_categories().names,
        }

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    @app.get("/", include_in_schema=False)
    def dashboard_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    api_errors.install(app)
    app.include_router(emails.router)
    app.include_router(dashboard.router)
    app.include_router(gmail.router)
    app.include_router(demo.router)
    return app


app = create_app()
