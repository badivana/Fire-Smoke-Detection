import os

import pytest

from app.core.categories import get_categories
from app.core.config import Settings, get_settings

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
    get_settings.cache_clear()
    get_categories.cache_clear()
    yield
    get_settings.cache_clear()
    get_categories.cache_clear()
