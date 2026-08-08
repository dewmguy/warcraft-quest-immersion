from fastapi.testclient import TestClient

from tts_cli import web


def test_health_is_public():
    with TestClient(web.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_is_open_when_authentication_is_delegated(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Application login is off" in response.text


def test_voice_review_is_read_only_and_complete(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        page = client.get("/voices")
        payload = client.get("/api/phase2")

    assert page.status_code == 200
    assert "All 230 planned previews remain ungenerated" in page.text
    assert "No source voice · no audio" in page.text
    assert payload.status_code == 200
    assert payload.json()["manifest"]["profile_count"] == 46
    assert payload.json()["preview_states"] == {"ungenerated": 230}


def test_dashboard_requires_authentication(monkeypatch):
    monkeypatch.setenv("WQI_ADMIN_PASSWORD", "test-password")
    with TestClient(web.app) as client:
        unauthorized = client.get("/")
        authorized = client.get("/", auth=("admin", "test-password"))

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert "Warcraft Quest Immersion" in authorized.text


def test_mutations_require_confirmation_header(monkeypatch):
    monkeypatch.setenv("WQI_ADMIN_PASSWORD", "test-password")
    with TestClient(web.app) as client:
        response = client.post("/api/generate-lookups", auth=("admin", "test-password"))

    assert response.status_code == 400
