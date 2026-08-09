from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tts_cli import web
from tts_cli.alpha_store import AlphaStore


class UnconfiguredElevenLabs:
    configured = False


@pytest.fixture(autouse=True)
def isolated_alpha(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(web, "DATA_DIR", data_dir)
    monkeypatch.setattr(web, "DIALOGUE_PATH", data_dir / "dialogue.csv")
    monkeypatch.setattr(web, "SOURCE_DIR", data_dir / "sources")
    monkeypatch.setattr(
        web,
        "alpha_store",
        AlphaStore(tmp_path / "alpha.sqlite3", tmp_path / "alpha-storage"),
    )
    monkeypatch.setattr(web, "elevenlabs", UnconfiguredElevenLabs())


def test_health_is_public():
    with TestClient(web.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_opens_full_scope_alpha_when_authentication_is_delegated(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.url.path == "/alpha"
    assert "Alpha production database" in response.text
    assert "Work queue" in response.text
    assert "4 matching records" in response.text


def test_alpha_starts_with_empty_spoken_text_and_prepares_only_on_click(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        row = web.alpha_store.list_dialogue(page_size=10)["rows"][0]
        page = client.get(f"/alpha/dialogue/{row['dialogue_id']}")
        missing_confirmation = client.post(
            f"/api/alpha/dialogue/{row['dialogue_id']}/prepare-spoken-text"
        )
        prepared = client.post(
            f"/api/alpha/dialogue/{row['dialogue_id']}/prepare-spoken-text",
            headers={"X-WQI-Action": "confirmed"},
        )

    assert page.status_code == 200
    assert "No spoken text exists" in page.text
    assert missing_confirmation.status_code == 400
    assert prepared.status_code == 200
    assert prepared.json()["dialogue"]["revision_number"] == 1


def test_paid_generation_requires_separate_confirmation_and_configuration(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        row = web.alpha_store.list_dialogue(page_size=10)["rows"][0]
        missing_paid_confirmation = client.post(
            f"/api/alpha/dialogue/{row['dialogue_id']}/generate",
            headers={"X-WQI-Action": "confirmed"},
        )
        unavailable_provider = client.post(
            f"/api/alpha/dialogue/{row['dialogue_id']}/generate",
            headers={
                "X-WQI-Action": "confirmed",
                "X-WQI-Paid-Action": "confirmed",
            },
        )

    assert missing_paid_confirmation.status_code == 400
    assert unavailable_provider.status_code == 503
    assert "No paid request was made" in unavailable_provider.json()["detail"]


def test_upload_adds_an_expansion_without_replacing_other_sources(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        uploaded = client.post(
            "/api/data",
            headers={"X-WQI-Action": "confirmed"},
            data={"expansion": "1.12.1", "locale": "enUS"},
            files={
                "file": (
                    "vanilla-dialogue.csv",
                    web.SAMPLE_DATA_PATH.read_bytes(),
                    "text/csv",
                )
            },
        )
        dashboard = client.get("/api/alpha").json()
        vanilla = client.get("/alpha?expansion=1.12.1")

    assert uploaded.status_code == 200
    assert dashboard["counts"]["dialogue"] == 8
    assert len(dashboard["snapshots"]) == 2
    assert "4 matching records" in vanilla.text


def test_import_export_explains_demo_data_and_exact_schema(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        page = client.get("/alpha/import-export")
        old_export = client.get("/alpha/export", follow_redirects=False)

    assert page.status_code == 200
    assert "demonstration rows" in page.text
    assert "3.3.5 AzerothCore" in page.text
    assert "Exact CSV contract" in page.text
    assert all(column in page.text for column in web.REQUIRED_COLUMNS)
    assert old_export.status_code == 307
    assert old_export.headers["location"] == "/alpha/import-export"


def test_voice_page_hides_missing_provider_id_and_explains_creation_paths(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        voice_id = web.alpha_store.list_voices("baseline")[0]["voice_id"]
        page = client.get(f"/alpha/voices/{voice_id}")

    assert page.status_code == 200
    assert "Provider voice missing" in page.text
    assert "ElevenLabs voice ID" not in page.text
    assert "Reference-guided Voice Design" in page.text
    assert "Instant Voice Clone" in page.text
    assert "Delivery presets" in page.text
    assert 'type="range"' in page.text


def test_settings_owns_provider_model_selection(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        page = client.get("/alpha/settings")
        saved = client.patch(
            "/api/alpha/settings",
            headers={"X-WQI-Action": "confirmed"},
            json={
                "tts_model_id": "eleven_multilingual_v2",
                "voice_design_model_id": "eleven_multilingual_ttv_v2",
                "output_format": "mp3_44100_128",
            },
        )

    assert page.status_code == 200
    assert "What is reusable" in page.text
    assert saved.status_code == 200
    assert saved.json()["settings"]["tts_model_id"] == "eleven_multilingual_v2"


def test_npc_can_leave_unique_queue_and_return_to_baseline(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        speaker_id = "creature-90002"
        unique = web.alpha_store.create_unique_voice(speaker_id)
        page = client.get("/alpha/speakers/creature/90002")
        reset = client.post(
            f"/api/alpha/speakers/{speaker_id}/baseline-voice",
            headers={"X-WQI-Action": "confirmed"},
        )
        unique_queue = client.get("/alpha/voices?scope=unique")

    assert page.status_code == 200
    assert "Return to the race/gender baseline" in page.text
    assert "Use Night Elf" in page.text
    assert reset.status_code == 200
    assert reset.json()["speaker"]["speaker"]["voice_scope"] == "baseline"
    assert unique["name"] not in unique_queue.text


def test_failed_dwarf_poc_and_old_voice_page_redirect_to_alpha(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app, follow_redirects=False) as client:
        dwarf = client.get("/poc/dwarves")
        voices = client.get("/voices")

    assert dwarf.status_code == 307
    assert dwarf.headers["location"] == "/alpha"
    assert voices.status_code == 307
    assert voices.headers["location"] == "/alpha/voices"


def test_phase2_source_artifact_remains_available(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        payload = client.get("/api/phase2")

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
    assert "Alpha production database" in authorized.text


def test_mutations_require_confirmation_header(monkeypatch):
    monkeypatch.setenv("WQI_ADMIN_PASSWORD", "test-password")
    with TestClient(web.app) as client:
        response = client.post("/api/generate-lookups", auth=("admin", "test-password"))

    assert response.status_code == 400
