"""FastAPI entry point. Run: uvicorn app.main:app --reload"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import dashboard, demo, emails
from app.api import errors as api_errors
from app.core.categories import get_categories
from app.core.config import get_settings
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


def create_app() -> FastAPI:
    app = FastAPI(title="Institutional Email AI", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            **get_settings().public_summary(),
            "database_ok": db_ping(),
            "categories": get_categories().names,
        }

    api_errors.install(app)
    app.include_router(emails.router)
    app.include_router(dashboard.router)
    app.include_router(demo.router)
    return app


app = create_app()
