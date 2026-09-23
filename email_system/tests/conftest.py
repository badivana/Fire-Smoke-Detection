import os

import pytest

from app.core.categories import get_categories
from app.core.config import Settings, get_settings
from app.db.session import get_engine, get_sessionmaker

_APP_ENV_PREFIXES = (
    "APP_",
    "DEMO_",
    "LOG_",
    "ADMIN_",
    "DATABASE_",
    "LLM_",
    "OLLAMA_",
    "OPENAI_COMPAT_",
    "CLASSIFICATION_",
    "CATEGORIES_",
    "MAX_",
    "OCR_",
    "TESSERACT_",
    "SEND_",
    "GMAIL_",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Tests never read the developer's real .env or inherited app env vars."""
    for key in list(os.environ):
        if key.upper().startswith(_APP_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")  # never touch data/app.db
    caches = (get_settings, get_categories, get_engine, get_sessionmaker)
    for c in caches:
        c.cache_clear()
    yield
    for c in caches:
        c.cache_clear()


# ---------------------------------------------------------------- database fixtures
from pathlib import Path  # noqa: E402

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.db.session import make_engine  # noqa: E402

EMAIL_SYSTEM_DIR = Path(__file__).resolve().parents[1]


def alembic_config(url: str) -> Config:
    cfg = Config(str(EMAIL_SYSTEM_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(EMAIL_SYSTEM_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(db_url):
    """Schema built by running the real Alembic migrations, not create_all()."""
    command.upgrade(alembic_config(db_url), "head")
    eng = make_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db(session_factory) -> Session:
    with session_factory() as s:
        yield s
