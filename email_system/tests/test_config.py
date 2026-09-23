import pytest
from pydantic import ValidationError

from app.core.config import BASE_DIR, GMAIL_SCOPES, SendMode, Settings


def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_defaults_are_safe():
    s = make()
    assert s.demo_mode is True
    assert s.send_mode == SendMode.SIMULATED
    assert s.ollama_model == "qwen3:4b"
    assert s.llm_think is False
    assert s.llm_max_retries == 1
    assert s.classification_confidence_threshold == 0.6


def test_demo_mode_cannot_send_real_gmail():
    with pytest.raises(ValidationError, match="DEMO_MODE=true cannot be combined"):
        make(demo_mode=True, send_mode="gmail")


def test_prod_requires_strong_admin_key_and_no_demo():
    with pytest.raises(ValidationError, match="ADMIN_API_KEY"):
        make(app_env="prod", demo_mode=False)
    with pytest.raises(ValidationError, match="ADMIN_API_KEY"):
        make(app_env="prod", demo_mode=False, admin_api_key="short")
    with pytest.raises(ValidationError, match="DEMO_MODE=false"):
        make(app_env="prod", demo_mode=True, admin_api_key="x" * 40)
    ok = make(app_env="prod", demo_mode=False, admin_api_key="x" * 40, send_mode="gmail")
    assert ok.send_mode == SendMode.GMAIL


def test_openai_compatible_requires_url_and_model():
    with pytest.raises(ValidationError, match="OPENAI_COMPAT_BASE_URL"):
        make(llm_provider="openai_compatible")
    s = make(
        llm_provider="openai_compatible",
        openai_compat_base_url="http://x/v1",
        openai_compat_model="m",
    )
    assert s.llm_model_name == "m"


def test_retry_count_is_capped():
    with pytest.raises(ValidationError):
        make(llm_max_retries=10)


def test_empty_secret_becomes_none():
    assert make(admin_api_key="  ").admin_api_key is None


def test_secrets_not_in_repr_or_summary():
    s = make(openai_compat_api_key="nvapi-SUPERSECRETVALUE123")
    assert "SUPERSECRET" not in repr(s)
    assert "SUPERSECRET" not in str(s.public_summary())


def test_gmail_scopes_are_least_privilege():
    assert set(GMAIL_SCOPES) == {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    }


def test_env_example_parses_and_is_safe(monkeypatch):
    """.env.example (with inline comments) must load to a valid, safe config."""
    s = Settings(_env_file=BASE_DIR / ".env.example")
    assert s.demo_mode is True
    assert s.send_mode == SendMode.SIMULATED
    assert s.ollama_model == "qwen3:4b"
    assert s.admin_api_key is None


def test_env_var_overrides(monkeypatch):
    monkeypatch.setenv("SEND_MODE", "disabled")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    s = make()
    assert s.send_mode == SendMode.DISABLED
    assert s.llm_model_name == "qwen3:8b"
