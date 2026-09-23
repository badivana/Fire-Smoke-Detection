import json

import httpx
import pytest

from app.core.errors import ErrorCode, PipelineError
from app.llm.factory import build_provider
from app.llm.ollama import OllamaProvider
from app.llm.openai_compat import OpenAICompatibleProvider
from app.llm.structured import extract_json

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def ollama(handler) -> OllamaProvider:
    return OllamaProvider(
        base_url="http://ollama.test",
        model="qwen3:4b",
        timeout=5,
        transport=httpx.MockTransport(handler),
    )


def test_ollama_request_uses_schema_and_disables_thinking():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "model": "qwen3:4b",
                "total_duration": 2_500_000_000,
                "message": {"content": '{"ok": true}'},
            },
        )

    r = ollama(handler).generate_json(system="S", user="U", schema=SCHEMA, schema_name="t")
    body = seen["body"]
    assert seen["url"] == "http://ollama.test/api/chat"
    assert body["format"] == SCHEMA and body["stream"] is False and body["think"] is False
    assert body["options"]["temperature"] == 0.0
    assert body["messages"] == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
    ]
    assert r.text == '{"ok": true}' and r.duration_ms == 2500


@pytest.mark.parametrize(
    "exc, code",
    [
        (httpx.ConnectError("refused"), ErrorCode.LLM_UNAVAILABLE),
        (httpx.ReadTimeout("slow"), ErrorCode.LLM_TIMEOUT),
    ],
)
def test_ollama_transport_errors_mapped(exc, code):
    def handler(req):
        raise exc

    with pytest.raises(PipelineError) as ei:
        ollama(handler).generate_json(system="S", user="U", schema=SCHEMA, schema_name="t")
    assert ei.value.code == code


def test_ollama_missing_model_is_unavailable():
    p = ollama(lambda req: httpx.Response(404, json={"error": "model 'qwen3:4b' not found"}))
    with pytest.raises(PipelineError) as ei:
        p.generate_json(system="S", user="U", schema=SCHEMA, schema_name="t")
    assert ei.value.code == ErrorCode.LLM_UNAVAILABLE and "pulled" in ei.value.detail


def test_ollama_empty_content_is_schema_error():
    p = ollama(lambda req: httpx.Response(200, json={"message": {}}))
    with pytest.raises(PipelineError) as ei:
        p.generate_json(system="S", user="U", schema=SCHEMA, schema_name="t")
    assert ei.value.code == ErrorCode.LLM_SCHEMA_INVALID


def test_openai_compatible_request_and_key_never_in_errors():
    key = "nvapi-SUPERSECRET1234567890"
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        if seen.get("fail"):
            return httpx.Response(401, json={"error": f"bad key {key}"})
        return httpx.Response(
            200, json={"model": "m", "choices": [{"message": {"content": '{"ok":true}'}}]}
        )

    p = OpenAICompatibleProvider(
        base_url="https://nim.test/v1",
        model="m",
        api_key=key,
        timeout=5,
        transport=httpx.MockTransport(handler),
    )
    r = p.generate_json(system="S", user="U", schema=SCHEMA, schema_name="task")
    assert seen["auth"] == f"Bearer {key}"
    rf = seen["body"]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["schema"] == SCHEMA
    assert rf["json_schema"]["strict"] is True
    assert r.text == '{"ok":true}'
    seen["fail"] = True
    with pytest.raises(PipelineError) as ei:
        p.generate_json(system="S", user="U", schema=SCHEMA, schema_name="task")
    assert ei.value.code == ErrorCode.LLM_UNAVAILABLE
    assert "SUPERSECRET" not in str(ei.value) and "authentication" in ei.value.detail


def test_factory_builds_configured_provider(monkeypatch):
    from app.core.config import Settings

    p = build_provider(Settings(_env_file=None))
    assert isinstance(p, OllamaProvider) and p.model == "qwen3:4b" and p.think is False
    s = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        openai_compat_base_url="https://x/v1",
        openai_compat_model="meta/llama",
    )
    assert isinstance(build_provider(s), OpenAICompatibleProvider)
    assert build_provider(s, model="other").model == "other"


@pytest.mark.parametrize(
    "text",
    [
        '{"ok": true}',
        '```json\n{"ok": true}\n```',
        '<think>hmm {"ok": false}</think>\n{"ok": true}',
        'Sure! Here is the JSON: {"ok": true} Hope that helps.',
    ],
)
def test_extract_json_tolerates_wrappers(text):
    assert extract_json(text) == {"ok": True}


def test_extract_json_rejects_garbage():
    with pytest.raises(json.JSONDecodeError):
        extract_json("no json here")
