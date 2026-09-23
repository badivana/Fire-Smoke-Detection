"""Application settings, loaded from environment variables / .env.

Secrets are typed as SecretStr so they never appear in repr(), logs, or /health.
Safety-relevant combinations are validated at startup (fail fast, fail closed).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]

# Gmail scopes are fixed in code, not configurable, so a .env edit can never widen access.
# readonly: fetch messages/attachments. send: send approved replies only.
GMAIL_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)


class AppEnv(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class LLMProviderName(StrEnum):
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


class SendMode(StrEnum):
    DISABLED = "disabled"  # every send attempt is refused
    SIMULATED = "simulated"  # "sent" to an in-process outbox; nothing leaves the machine
    GMAIL = "gmail"  # real Gmail API send (still requires APPROVED state)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_env: AppEnv = AppEnv.DEV
    demo_mode: bool = True
    log_level: str = "INFO"
    log_json: bool = False
    admin_api_key: SecretStr | None = None

    # --- Database ---
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"

    # --- LLM ---
    llm_provider: LLMProviderName = LLMProviderName.OLLAMA
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b"
    # qwen3 is a "thinking" model; reasoning text breaks strict JSON output. Keep off.
    llm_think: bool = False
    openai_compat_base_url: str | None = None
    openai_compat_api_key: SecretStr | None = None
    openai_compat_model: str | None = None
    llm_temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    # Spec: 1 retry with a stricter prompt, then ERROR. Capped so a bad .env can't loop.
    llm_max_retries: int = Field(default=1, ge=0, le=2)

    # --- Classification ---
    classification_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    categories_file: Path = BASE_DIR / "config" / "categories.yaml"

    # --- Input limits (truncate safely before prompting) ---
    max_email_chars: int = Field(default=12_000, ge=1_000)
    max_attachment_chars: int = Field(default=20_000, ge=1_000)
    max_attachment_bytes: int = Field(default=15 * 1024 * 1024, ge=1024)

    # --- Attachments / OCR ---
    attachments_dir: Path = BASE_DIR / "data" / "attachments"
    ocr_enabled: bool = True
    tesseract_cmd: str | None = None

    # --- Replies ---
    institution_name: str = Field(default="the Institute", max_length=200)
    reply_signature: str = Field(default="IT/Admin Office", max_length=500)

    # --- Sending ---
    send_mode: SendMode = SendMode.SIMULATED
    outbox_dir: Path = BASE_DIR / "data" / "outbox"  # SEND_MODE=simulated writes .eml here
    # Max automatic retries of processing (not sending) for one email.
    max_processing_attempts: int = Field(default=3, ge=1, le=10)

    # --- Gmail ---
    gmail_credentials_file: Path = BASE_DIR / "secrets" / "credentials.json"
    gmail_token_file: Path = BASE_DIR / "secrets" / "token.json"
    gmail_query: str = "in:inbox -in:spam newer_than:7d"

    @field_validator("admin_api_key", "openai_compat_api_key", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _check_safety(self) -> Settings:
        if self.demo_mode and self.send_mode == SendMode.GMAIL:
            raise ValueError("DEMO_MODE=true cannot be combined with SEND_MODE=gmail")
        if self.app_env == AppEnv.PROD:
            if self.admin_api_key is None or len(self.admin_api_key.get_secret_value()) < 32:
                raise ValueError("APP_ENV=prod requires ADMIN_API_KEY of at least 32 chars")
            if self.demo_mode:
                raise ValueError("APP_ENV=prod requires DEMO_MODE=false")
        if self.llm_provider == LLMProviderName.OPENAI_COMPATIBLE:
            missing = [
                name
                for name, val in (
                    ("OPENAI_COMPAT_BASE_URL", self.openai_compat_base_url),
                    ("OPENAI_COMPAT_MODEL", self.openai_compat_model),
                )
                if not val
            ]
            if missing:
                raise ValueError(f"LLM_PROVIDER=openai_compatible requires {', '.join(missing)}")
        return self

    @property
    def llm_model_name(self) -> str:
        if self.llm_provider == LLMProviderName.OLLAMA:
            return self.ollama_model
        return self.openai_compat_model or ""

    def public_summary(self) -> dict[str, object]:
        """Non-secret view of the config, safe for /health and logs."""
        return {
            "app_env": self.app_env.value,
            "demo_mode": self.demo_mode,
            "send_mode": self.send_mode.value,
            "llm_provider": self.llm_provider.value,
            "llm_model": self.llm_model_name,
            "confidence_threshold": self.classification_confidence_threshold,
            "ocr_enabled": self.ocr_enabled,
            "database": self.database_url.split(":", 1)[0],
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
