from fastapi.testclient import TestClient

from app.main import create_app


def test_health_reports_safe_config():
    with TestClient(create_app()) as client:
        r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["demo_mode"] is True
    assert data["send_mode"] == "simulated"
    assert data["llm_model"] == "qwen3:4b"
    assert "IRRELEVANT" in data["categories"]
    assert not any("key" in k.lower() or "secret" in k.lower() for k in data)
