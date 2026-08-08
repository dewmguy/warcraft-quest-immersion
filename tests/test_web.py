from fastapi.testclient import TestClient

from tts_cli import web
from tts_cli.workflow_poc import WorkflowPoc


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


def test_dwarf_poc_is_no_audio_and_persists_workflow_decisions(monkeypatch, tmp_path):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(web, "workflow_poc", WorkflowPoc(tmp_path / "dwarf-poc.sqlite3"))
    with TestClient(web.app) as client:
        page = client.get("/poc/dwarves")
        payload = client.get("/api/poc/dwarves")
        mutation = client.post(
            "/api/poc/dwarves/dwarf-male/profile-stages/identity_defined",
            headers={"X-WQI-Action": "confirmed"},
            json={"action": "approve", "note": "POC accepted."},
        )

    assert page.status_code == 200
    assert "No-spend mode is active" in page.text
    assert payload.json()["no_audio_mode"] is True
    assert len(payload.json()["profiles"]) == 2
    assert mutation.status_code == 200
    male = next(
        item
        for item in mutation.json()["profiles"]
        if item["profile"]["profile_id"] == "dwarf-male"
    )
    assert male["profile_stages"][0]["status"] == "approved"
    assert male["profile_stages"][1]["status"] == "current"


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
