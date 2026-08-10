import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tts_cli import alpha_store as alpha_module
from tts_cli import web
from tts_cli.alpha_store import AlphaStore


class UnconfiguredElevenLabs:
    configured = False


class ConfiguredElevenLabs:
    configured = True

    def text_to_speech(self, **_kwargs):
        return SimpleNamespace(
            content=b"voice-id-audition",
            request_id="audition-request-test",
            character_cost=240,
        )

    def delete_voice(self, _voice_id):
        return {"status": "ok"}

    def subscription(self):
        return {
            "tier": "creator",
            "status": "active",
            "character_count": 2500,
            "character_limit": 10000,
            "next_character_count_reset_unix": 1_800_000_000,
            "character_refresh_period": "monthly_period",
            "voice_limit": 30,
            "can_use_instant_voice_cloning": True,
        }


class DesigningElevenLabs(ConfiguredElevenLabs):
    def design_voice(self, **_kwargs):
        return SimpleNamespace(
            payload={
                "previews": [
                    {
                        "audio_base_64": base64.b64encode(f"new-{index}".encode()).decode(),
                        "generated_voice_id": f"generated-{index}",
                    }
                    for index in range(3)
                ]
            },
            request_id="design-request-test",
            character_cost=242,
        )

    def create_designed_voice(self, **_kwargs):
        return {"voice_id": "provider-voice-selected"}


class CloningElevenLabs(ConfiguredElevenLabs):
    def __init__(self):
        self.clone_kwargs = None
        self.tts_kwargs = None

    def clone_voice(self, **kwargs):
        self.clone_kwargs = kwargs
        return {"voice_id": "provider-instant-clone"}

    def text_to_speech(self, **kwargs):
        self.tts_kwargs = kwargs
        return super().text_to_speech(**kwargs)


class CloningWithFailedAuditionElevenLabs(CloningElevenLabs):
    def text_to_speech(self, **kwargs):
        self.tts_kwargs = kwargs
        raise web.ElevenLabsError("Audition synthesis failed after clone creation.")


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
    assert "Lifecycle · Draft" in page.text
    assert "Automatic lifecycle" not in page.text
    assert 'class="lifecycle-explanation"' not in page.text
    assert 'class="provider-id-card"' not in page.text
    assert 'class="voice-settings"' not in page.text
    assert "Voice Design Prompt Context" in page.text
    assert "Context sent to Voice Design" not in page.text
    assert "This exact prompt will be sent to Voice Design" not in page.text
    assert "data-prompt-context" not in page.text
    assert "Save changed voice settings" not in page.text
    assert "Audition Script" in page.text
    assert page.text.count(web.VOICE_ID_AUDITION_TEXT) == 3
    assert 'name="status"' not in page.text
    assert "Lifecycle status</span><select" not in page.text
    assert "Voice ID needed" in page.text
    assert "Reusable ElevenLabs voice</span><strong>Connected" not in page.text
    assert "Reference-guided Voice Design" in page.text
    assert "Instant Voice Clone" in page.text
    assert "Emotional Delivery Presets" in page.text
    assert "Define and test emotional performances" in page.text
    assert "selected candidate defines who is speaking" not in page.text
    assert "One comparison per paid click" not in page.text
    assert "Review notes · local only" not in page.text
    assert "Never sent to ElevenLabs" not in page.text
    assert page.text.count("Voice Actor Notes (Optional)") == 5
    assert page.text.count("Performance Method") == 5
    assert "Sent in brackets immediately before the spoken text" not in page.text
    assert "Creative follows emotion more strongly" not in page.text
    assert 'value="[angry]"' not in page.text
    assert 'value="angry"' in page.text
    assert "Creative · expressive, least predictable" in page.text
    assert "Natural · balanced and closest to the voice" in page.text
    assert "Robust · consistent, least responsive to direction" in page.text
    assert "Save preset settings" not in page.text
    assert page.text.count("data-auto-save-form") == 5
    assert "data-dirty-submit" not in page.text
    assert 'class="fa-solid fa-floppy-disk"' not in page.text
    assert page.text.count("Generate sample") == 5
    assert "Preset Settings" in page.text
    assert "Sample" in page.text
    assert "Generate all samples" in page.text
    assert "Sample Script" in page.text
    assert "<blockquote>" in page.text
    assert "<details><summary>Fixed comparison script" not in page.text
    assert page.text.count("data-delivery-generation") == 5
    assert "data-delivery-batch" in page.text
    assert 'type="range"' in page.text
    assert 'href="#provider-creation"' in page.text
    assert 'id="provider-creation"' in page.text
    assert "multiple" in page.text
    assert "MP3, WAV, M4A, OGG, or FLAC" in page.text
    assert "Stored Reference Clips" in page.text
    assert '<details class="reference-upload">' in page.text
    assert "Add Reference Clips" in page.text
    assert "https://kit.fontawesome.com/666b0b7246.js" in page.text
    assert "MP3 should be 192 kbps or higher" in page.text
    assert "candidate-generation-note" not in page.text

    stylesheet = (web.WEB_DIR / "static" / "app.css").read_text(encoding="utf-8")
    assert 'grid-template-areas: "heading review" "settings review"' in stylesheet
    assert (
        ".alpha-panel .delivery-preset-settings { grid-area: settings; grid-template-columns: 1fr;"
        in stylesheet
    )
    assert stylesheet.count(".alpha-panel .delivery-preset-settings {") == 1
    assert ".delivery-preset-grid { display: grid; grid-template-columns: 1fr;" in stylesheet
    template = (web.WEB_DIR / "templates" / "alpha-voice.html").read_text(encoding="utf-8")
    assert (
        '<div class="creation-controls">\n'
        '        <div class="panel-heading"><div><span class="panel-step">Provider Creation</span>'
        in template
    )


def test_voice_candidates_have_confirmed_manual_deletion(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        voice_id = "baseline--bloodelf-female"
        preview_id = web.alpha_store.record_voice_previews(
            voice_id,
            prompt="Candidate prompt",
            preview_text="Candidate comparison text",
            model_id="eleven_ttv_v3",
            creation_method="designed",
            previews=[{"content": b"candidate-audio", "generated_voice_id": "candidate-one"}],
        )[0]
        preview_path = Path(web.alpha_store.get_voice_preview(preview_id)["storage_path"])
        page = client.get(f"/alpha/voices/{voice_id}")
        deleted = client.delete(
            f"/api/alpha/voice-previews/{preview_id}",
            headers={"X-WQI-Action": "confirmed"},
        )

    assert page.status_code == 200
    assert "Proposed Voices" in page.text
    assert "Incoming" not in page.text
    assert "Temporary output" not in page.text
    assert f'data-url="/api/alpha/voice-previews/{preview_id}"' in page.text
    assert 'data-method="DELETE"' in page.text
    assert "Permanently delete proposed Voice Design #1" in page.text
    assert 'class="reference-player candidate-player"' in page.text
    assert 'data-audio-name="Proposed Voice Design 1"' in page.text
    assert "Voice Design #1" in page.text
    assert "Voice ID: <strong>Proposed</strong>" in page.text
    assert (
        f'<audio hidden preload="metadata" src="/api/alpha/voice-previews/{preview_id}/audio">'
        in page.text
    )
    assert deleted.status_code == 200
    assert deleted.json()["message"] == "Voice Design preview was deleted from local storage."
    assert not preview_path.exists()
    assert web.alpha_store.get_voice(voice_id)["previews"] == []


def test_prompt_history_restores_only_the_voice_design_prompt(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        voice_id = "baseline--bloodelf-female"
        original = web.alpha_store.get_voice(voice_id)
        changed = web.alpha_store.update_voice(
            voice_id,
            {
                "description": original["description"] + " Keep every consonant precise.",
                "creation_method": "designed",
            },
        )
        page = client.get(f"/alpha/voices/{voice_id}")
        restored = client.post(
            f"/api/alpha/voices/{voice_id}/prompts/{original['version_id']}/restore",
            headers={"X-WQI-Action": "confirmed"},
        )
        revised = web.alpha_store.get_voice(voice_id)

    assert page.status_code == 200
    assert page.text.count("Prompt History") == 2
    assert f"/prompts/{original['version_id']}/restore" in page.text
    assert restored.status_code == 200
    assert "Restored the prompt as version" in restored.json()["message"]
    assert revised["description"] == original["description"]
    assert revised["creation_method"] == "designed"
    assert changed["description"] in {
        version["description"] for version in revised["prompt_versions"]
    }


def test_delivery_samples_use_compact_players_and_confirm_review_actions(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.5)
    voice_id = "baseline--bloodelf-female"
    with TestClient(web.app) as client:
        voice = web.alpha_store.get_voice(voice_id)
        web.alpha_store.update_voice(
            voice_id,
            {
                "description": voice["description"],
                "creation_method": "external",
                "provider_voice_id": "provider-voice-test",
            },
        )
        web.alpha_store.update_delivery_preset(
            voice_id,
            "neutral",
            {"prompt_tag": "with restrained warmth", "stability": 1},
        )
        request = web.alpha_store.delivery_preview_request(
            voice_id,
            "neutral",
            "This fixed comparison passage is intentionally long enough to produce a useful "
            "delivery test while keeping every generated preset directly comparable.",
        )
        preview = web.alpha_store.record_delivery_preview(
            voice_id,
            "neutral",
            request,
            content=b"delivery-audio",
            provider_request_id="delivery-request-test",
            subscription={"request_character_cost": 80},
        )
        for index in range(2):
            web.alpha_store.record_delivery_preview(
                voice_id,
                "neutral",
                request,
                content=f"additional-delivery-audio-{index}".encode(),
                provider_request_id=f"additional-delivery-request-{index}",
                subscription={"request_character_cost": 80},
            )
        web.alpha_store.update_delivery_preset(
            voice_id,
            "neutral",
            {"prompt_tag": "more brightly", "stability": 0.5},
        )
        preview_path = web.alpha_store.delivery_preview_path(preview["preview_id"])
        page = client.get(f"/alpha/voices/{voice_id}")
        deleted = client.delete(
            f"/api/alpha/delivery-previews/{preview['preview_id']}",
            headers={"X-WQI-Action": "confirmed"},
        )

    assert page.status_code == 200
    assert 'class="reference-player delivery-player"' in page.text
    assert page.text.count('class="delivery-preview"') == 3
    assert page.text.count('data-audio-disclosure="delivery-samples"') == 3
    assert page.text.count("<summary>") >= 3
    assert page.text.count('class="delivery-preview-chevron"') == 3
    assert 'data-audio-name="Neutral sample 1"' in page.text
    assert (
        f'<audio hidden preload="metadata" src="/api/alpha/delivery-previews/{preview["preview_id"]}/audio">'
        in page.text
    )
    assert f'data-url="/api/alpha/delivery-previews/{preview["preview_id"]}"' in page.text
    assert page.text.count('class="secondary icon-button delivery-preview-delete"') == 3
    assert page.text.count('class="icon-button delivery-sample-approve"') == 3
    assert page.text.count("Approve this neutral sample?") == 3
    assert page.text.count("Permanently delete this neutral sample") == 3
    assert page.text.count("Stored Samples") == 1
    assert page.text.count('class="reference-player delivery-player"') == 3
    assert page.text.count('class="delivery-preview-metadata"') == 3
    assert page.text.count("Voice actor notes") == 3
    assert page.text.count(">with restrained warmth</dd>") == 3
    assert 'value="more brightly"' in page.text
    assert page.text.count("Performance method") == 3
    assert page.text.count(">Robust</dd>") == 3
    assert page.text.count("<dt>ElevenLabs voice ID</dt>") == 3
    assert page.text.count("provider-voice-test") >= 3
    assert "ElevenLabs usage cannot be refunded" in page.text
    assert "<audio controls" not in page.text
    stylesheet = (web.WEB_DIR / "static" / "app.css").read_text(encoding="utf-8")
    assert (
        ".alpha-body .delivery-sample-approve, .alpha-body .delivery-preview-delete.secondary { "
        "flex: 0 0 30px; border-color: #66542e; background: #100f12; "
        "color: var(--gold-bright); }" in stylesheet
    )
    assert ".delivery-sample-approve:hover" in stylesheet
    assert "background: var(--success); color: #100f12; filter: none;" in stylesheet
    assert ".delivery-preview-delete:hover" in stylesheet
    assert "background: var(--danger); color: #100f12; filter: none;" in stylesheet
    assert deleted.status_code == 200
    assert deleted.json()["message"] == "Delivery sample was deleted from local storage."
    assert not preview_path.exists()


def test_delivery_presets_offer_every_retained_voice_id(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        voice_id = "baseline--bloodelf-female"
        voice = web.alpha_store.get_voice(voice_id)
        web.alpha_store.update_voice(
            voice_id,
            {
                "description": voice["description"],
                "creation_method": "external",
                "provider_voice_id": "provider-default",
            },
        )
        alternate = web.alpha_store.record_voice_id_candidate(
            voice_id,
            provider_voice_id="provider-sorrowful",
            creation_method="designed",
            creation_model_id="eleven_ttv_v3",
        )
        web.alpha_store.update_delivery_preset(
            voice_id,
            "sorrowful",
            {
                "provider_voice_id": alternate["provider_voice_id"],
                "prompt_tag": "quietly grieving",
                "stability": 0.5,
            },
        )
        page = client.get(f"/alpha/voices/{voice_id}")

    assert page.status_code == 200
    assert page.text.count('<select name="provider_voice_id"') == 5
    assert page.text.count("provider-default") >= 5
    assert page.text.count("provider-sorrowful") >= 5
    assert (
        '<option value="provider-sorrowful" selected>#2 · Voice Design · '
        "provider-sorrowful</option>" in page.text
    )
    assert "Each emotional preset can use a different retained voice ID" in page.text


def test_delivery_preset_settings_auto_save_and_build_actions_skip_confirmations():
    script = (web.WEB_DIR / "static" / "alpha.js").read_text(encoding="utf-8")

    assert 'document.querySelectorAll("[data-auto-save-form]")' in script
    assert "const autoSaveStates = new WeakMap()" in script
    assert "function queueAutoSave(form, delay = 500)" in script
    assert "async function saveAutoForm(form)" in script
    assert "const requestBody = jsonFromForm(form)" in script
    assert "state.saved = serialized" in script
    assert "updatePresetStatus(form, payload.voice)" in script
    assert "if (form.dataset.autoSaveForm !== undefined) continue" in script
    assert "data-dirty-form" not in script
    assert 'deliveryBatchButton.textContent = "Generate all samples"' in script
    assert "deliveryBatchConfirmation" not in script
    assert "paidConfirmation" not in script
    assert "const shouldConfirm = paid" not in script
    assert (
        'if (confirmRequired && !window.confirm(confirmText || "Confirm this action?"))' in script
    )
    assert "for (const form of forms) launchDeliveryGeneration(form);" in script


def test_provider_creation_forms_follow_the_unsaved_creation_path():
    script = (web.WEB_DIR / "static" / "alpha.js").read_text(encoding="utf-8")

    assert 'const panels = [...document.querySelectorAll("[data-method-panel]")]' in script
    assert "panel.hidden = !active" in script
    assert "promptContext" not in script
    assert "selected !== savedMethod" not in script
    assert 'field.disabled = field.dataset.serverDisabled === "true" || !active' in script


def test_every_voice_page_delete_control_requires_confirmation():
    template = (web.WEB_DIR / "templates" / "alpha-voice.html").read_text(encoding="utf-8")
    opening_tags = [chunk.split(">", 1)[0] for chunk in template.split("<button")[1:]]
    delete_controls = [tag for tag in opening_tags if 'data-method="DELETE"' in tag]

    assert len(delete_controls) == 4
    assert all("data-confirm-required" in tag for tag in delete_controls)
    assert all("data-confirm=" in tag for tag in delete_controls)


def test_build_actions_skip_confirmation_while_teardown_keeps_it():
    script = (web.WEB_DIR / "static" / "alpha.js").read_text(encoding="utf-8")
    voice_template = (web.WEB_DIR / "templates" / "alpha-voice.html").read_text(encoding="utf-8")
    dialogue_template = (web.WEB_DIR / "templates" / "alpha-dialogue.html").read_text(
        encoding="utf-8"
    )
    voice_design_forms = [
        chunk.split(">", 1)[0]
        for chunk in voice_template.split("<form")[1:]
        if 'data-provider-operation="Generating three' in chunk.split(">", 1)[0]
    ]
    instant_clone_form = next(
        chunk.split(">", 1)[0]
        for chunk in voice_template.split("<form")[1:]
        if 'data-provider-operation="Generating an instant-clone voice ID and sample"'
        in chunk.split(">", 1)[0]
    )
    reusable_voice_button = next(
        chunk.split(">", 1)[0]
        for chunk in voice_template.split("<button")[1:]
        if 'data-provider-operation="Generating a Voice ID"' in chunk.split(">", 1)[0]
    )

    assert len(voice_design_forms) == 2
    assert all("data-confirm=" not in tag for tag in voice_design_forms)
    assert all("data-confirm-required" not in tag for tag in voice_design_forms)
    assert 'data-provider-operation="Generating a dialogue candidate"' in dialogue_template
    assert "data-confirm=" not in instant_clone_form
    assert "data-confirm-required" not in instant_clone_form
    assert "data-confirm=" not in reusable_voice_button
    assert "data-confirm-required" not in reusable_voice_button
    assert "paidConfirmation" not in script


def test_alpha_message_tracks_every_elevenlabs_request(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        voice_id = web.alpha_store.list_voices("baseline")[0]["voice_id"]
        dialogue_id = web.alpha_store.list_dialogue(page_size=1)["rows"][0]["dialogue_id"]
        voice_page = client.get(f"/alpha/voices/{voice_id}")
        dialogue_page = client.get(f"/alpha/dialogue/{dialogue_id}")

    script = (web.WEB_DIR / "static" / "alpha.js").read_text(encoding="utf-8")
    assert "data-alpha-message-title" in voice_page.text
    assert "data-alpha-message-elapsed" in voice_page.text
    assert "data-alpha-message-progress" in voice_page.text
    assert "data-alpha-message-close" in voice_page.text
    assert 'aria-live="polite"' in voice_page.text
    assert voice_page.text.count("data-paid") == voice_page.text.count("data-provider-operation")
    assert dialogue_page.text.count("data-paid") == dialogue_page.text.count(
        "data-provider-operation"
    )
    assert "function startElevenLabsRequest" in script
    assert "function finishElevenLabsRequest" in script
    assert "const alphaProviderRequests = new Map()" in script
    assert "const activeDeliveryGenerations = new Map()" in script
    assert "for (const form of forms) launchDeliveryGeneration(form);" in script
    assert "if (!activeDeliveryGenerations.size) finishDeliveryGenerationRun();" in script
    assert "Completed audio is stored even if another request fails." in script
    assert "window.setInterval(renderElevenLabsProgress, 1000)" in script
    assert "the request remains active" in script
    assert 'startElevenLabsRequest("Checking ElevenLabs account", null, true)' in script
    assert 'alphaMessageClose.hidden = state !== "failed"' in script
    assert 'alphaMessageClose.addEventListener("click"' in script
    assert script.count("alphaMessage.hidden = true") == 1


def test_successful_voice_regeneration_replaces_former_candidates(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(web, "elevenlabs", DesigningElevenLabs())
    with TestClient(web.app) as client:
        voice_id = "baseline--bloodelf-female"
        current = web.alpha_store.get_voice(voice_id)
        web.alpha_store.update_voice(
            voice_id,
            {
                "description": current["description"],
                "creation_method": "designed",
            },
        )
        former_id = web.alpha_store.record_voice_previews(
            voice_id,
            prompt="Former prompt",
            preview_text="Former comparison text",
            model_id="eleven_ttv_v3",
            previews=[{"content": b"former-audio", "generated_voice_id": "former-one"}],
        )[0]
        former_path = Path(web.alpha_store.get_voice_preview(former_id)["storage_path"])
        response = client.post(
            f"/api/alpha/voices/{voice_id}/design",
            headers={
                "X-WQI-Action": "confirmed",
                "X-WQI-Paid-Action": "confirmed",
            },
            json={
                "creation_method": "designed",
                "preview_text": (
                    "The road ahead is dangerous, but our purpose remains clear. Stay close, "
                    "listen carefully, and remember why we began this journey together."
                ),
            },
        )
        revised = web.alpha_store.get_voice(voice_id)

    assert response.status_code == 200
    assert "Replaced 1 former temporary Voice Design preview" in response.json()["message"]
    assert len(revised["previews"]) == 3
    assert former_id not in {item["preview_id"] for item in revised["previews"]}
    assert not former_path.exists()


def test_reusable_voice_id_candidate_survives_new_design_previews(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(web, "elevenlabs", DesigningElevenLabs())
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.5)
    with TestClient(web.app) as client:
        voice_id = "baseline--bloodelf-female"
        current = web.alpha_store.get_voice(voice_id)
        web.alpha_store.update_voice(
            voice_id,
            {
                "description": current["description"],
                "creation_method": "designed",
            },
        )
        preview_ids = web.alpha_store.record_voice_previews(
            voice_id,
            prompt="Candidate prompt",
            preview_text="Candidate comparison text",
            model_id="eleven_ttv_v3",
            previews=[
                {
                    "content": f"candidate-{index}".encode(),
                    "generated_voice_id": f"candidate-{index}",
                }
                for index in range(3)
            ],
        )
        selected_id = preview_ids[1]
        original_paths = {
            preview["preview_id"]: Path(preview["storage_path"])
            for preview in web.alpha_store.get_voice(voice_id)["previews"]
        }
        activated = client.post(
            f"/api/alpha/voice-previews/{selected_id}/activate",
            headers={
                "X-WQI-Action": "confirmed",
                "X-WQI-Paid-Action": "confirmed",
            },
        )
        activated_voice = web.alpha_store.get_voice(voice_id)
        candidate = activated_voice["voice_id_candidates"][0]
        candidate_path = web.alpha_store.voice_id_candidate_path(candidate["candidate_id"])
        selected_page = client.get(f"/alpha/voices/{voice_id}")
        regenerated = client.post(
            f"/api/alpha/voices/{voice_id}/design",
            headers={
                "X-WQI-Action": "confirmed",
                "X-WQI-Paid-Action": "confirmed",
            },
            json={
                "creation_method": "designed",
                "preview_text": (
                    "The road ahead is dangerous, but our purpose remains clear. Stay close, "
                    "listen carefully, and remember why we began this journey together."
                ),
            },
        )
        revised = web.alpha_store.get_voice(voice_id)

    assert activated.status_code == 200
    assert "Generated voice ID candidate #2" in activated.json()["message"]
    assert "Deleted 3 temporary Voice Design previews" in activated.json()["message"]
    assert selected_page.status_code == 200
    assert 'class="voice-id-candidate"' in selected_page.text
    assert "candidate-selected" not in selected_page.text
    assert "Generated Voice IDs" in selected_page.text
    assert "Voice Design #2" in selected_page.text
    assert "Profile default" not in selected_page.text
    assert "Use as default" not in selected_page.text
    assert "Voice Design · eleven_ttv_v3" not in selected_page.text
    assert candidate["provider_voice_id"] in selected_page.text
    assert regenerated.status_code == 200
    assert revised["provider_voice_id"] == "provider-voice-selected"
    assert len(revised["voice_id_candidates"]) == 1
    assert len(revised["previews"]) == 3
    assert {preview["generation_number"] for preview in revised["previews"]} == {4, 5, 6}
    assert {preview["preview_id"] for preview in revised["previews"]} == set(
        regenerated.json()["preview_ids"]
    )
    assert candidate_path.is_file()
    assert all(not path.exists() for path in original_paths.values())


def test_voice_id_candidate_can_be_deleted_from_provider_and_local_registry(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(web, "elevenlabs", DesigningElevenLabs())
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.5)
    with TestClient(web.app) as client:
        voice_id = "baseline--bloodelf-female"
        preview_id = web.alpha_store.record_voice_previews(
            voice_id,
            prompt="Candidate prompt",
            preview_text="Candidate comparison text",
            model_id="eleven_ttv_v3",
            creation_method="reference_design",
            previews=[{"content": b"candidate", "generated_voice_id": "candidate-selected"}],
        )[0]
        activated = client.post(
            f"/api/alpha/voice-previews/{preview_id}/activate",
            headers={
                "X-WQI-Action": "confirmed",
                "X-WQI-Paid-Action": "confirmed",
            },
        )
        candidate = web.alpha_store.get_voice(voice_id)["voice_id_candidates"][0]
        candidate_path = web.alpha_store.voice_id_candidate_path(candidate["candidate_id"])
        deleted = client.delete(
            f"/api/alpha/voice-id-candidates/{candidate['candidate_id']}",
            headers={"X-WQI-Action": "confirmed"},
        )
        revised = web.alpha_store.get_voice(voice_id)

    assert activated.status_code == 200
    assert deleted.status_code == 200
    assert (
        "Deleted voice ID candidate #1 from ElevenLabs and local storage"
        in (deleted.json()["message"])
    )
    assert "profile default" not in deleted.json()["message"]
    assert "cleared it from 5 delivery presets" in deleted.json()["message"]
    assert revised["provider_voice_id"] is None
    assert revised["voice_id_candidates"] == []
    assert revised["previews"] == []
    assert not candidate_path.exists()


def test_provider_creation_accepts_new_unsaved_path_and_records_candidate_method(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(web, "elevenlabs", DesigningElevenLabs())
    with TestClient(web.app) as client:
        voice_id = "baseline--bloodelf-female"
        current = web.alpha_store.get_voice(voice_id)
        web.alpha_store.update_voice(
            voice_id,
            {
                "description": current["description"],
                "creation_method": "reference_design",
            },
        )
        generated = client.post(
            f"/api/alpha/voices/{voice_id}/design",
            headers={
                "X-WQI-Action": "confirmed",
                "X-WQI-Paid-Action": "confirmed",
            },
            json={
                "creation_method": "designed",
                "description": "A newly edited provider-creation prompt for this voice.",
                "preview_text": (
                    "The road ahead is dangerous, but our purpose remains clear. Stay close, "
                    "listen carefully, and remember why we began this journey together."
                ),
            },
        )
        revised = web.alpha_store.get_voice(voice_id)
        page = client.get(f"/alpha/voices/{voice_id}")

    assert generated.status_code == 200
    assert revised["creation_method"] == "designed"
    assert revised["description"] == "A newly edited provider-creation prompt for this voice."
    assert {preview["creation_method"] for preview in revised["previews"]} == {"designed"}
    assert all(f"Voice Design #{number}" in page.text for number in (1, 2, 3))
    assert "Voice Design · eleven_ttv_v3" not in page.text
    assert "Proposed Voices" in page.text
    assert "Save changed voice settings" not in page.text


def test_instant_clone_accepts_new_unsaved_path(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    provider = CloningElevenLabs()
    monkeypatch.setattr(web, "elevenlabs", provider)
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 65.0)
    with TestClient(web.app) as client:
        voice_id = "baseline--bloodelf-female"
        current = web.alpha_store.get_voice(voice_id)
        full_description = (current["description"] + " Preserve this additional context.") * 2
        web.alpha_store.update_voice(
            voice_id,
            {
                "description": full_description,
                "creation_method": current["creation_method"],
            },
        )
        preview_id = web.alpha_store.record_voice_previews(
            voice_id,
            prompt=full_description,
            preview_text="A previous Voice Design sample retained for comparison.",
            model_id="eleven_ttv_v3",
            creation_method="designed",
            previews=[{"content": b"former-design-audio", "generated_voice_id": "former-design"}],
        )[0]
        web.alpha_store.activate_voice_preview(preview_id, "provider-before-clone")
        clip = web.alpha_store.save_reference_clip(
            voice_id,
            original_name="clone-source.wav",
            content=b"reference-audio",
            provenance="Test fixture",
            provider_eligible=True,
        )["clips"][0]
        cloned = client.post(
            f"/api/alpha/voices/{voice_id}/clone",
            headers={
                "X-WQI-Action": "confirmed",
                "X-WQI-Paid-Action": "confirmed",
            },
            json={
                "creation_method": "instant_clone",
                "clip_ids": [clip["clip_id"]],
            },
        )
        revised = web.alpha_store.get_voice(voice_id)
        page = client.get(f"/alpha/voices/{voice_id}")

    assert cloned.status_code == 200
    assert revised["creation_method"] == "instant_clone"
    assert revised["provider_voice_id"] == "provider-instant-clone"
    assert revised["description"] == full_description
    assert revised["previews"] == []
    assert len(revised["voice_id_candidates"]) == 2
    instant_candidate = revised["voice_id_candidates"][0]
    assert instant_candidate["provider_voice_id"] == "provider-instant-clone"
    assert instant_candidate["generation_number"] == 2
    assert instant_candidate["creation_method"] == "instant_clone"
    assert instant_candidate["creation_model_id"] == "instant_voice_clone"
    assert instant_candidate["sample_text"] == web.VOICE_ID_AUDITION_TEXT
    assert instant_candidate["sample_model_id"] == "eleven_v3"
    assert provider.clone_kwargs is not None
    assert provider.tts_kwargs is not None
    assert provider.tts_kwargs["voice_id"] == "provider-instant-clone"
    assert provider.tts_kwargs["text"] == web.VOICE_ID_AUDITION_TEXT
    assert len(provider.clone_kwargs["description"]) <= 500
    assert provider.clone_kwargs["description"].endswith("…")

    assert "The selected clips define the clone" in page.text
    assert "attached only as provider metadata" not in page.text
    assert 'data-method-panel="designed" hidden' in page.text
    assert "Generated Voice IDs" in page.text
    assert "Voice Design #1" in page.text
    assert "Instant Voice Clone #2" in page.text
    assert "Instant Voice Clone · instant_voice_clone" not in page.text
    assert "provider-instant-clone" in page.text
    assert "standardized sample is ready" in cloned.json()["message"]
    assert "240 credits" in page.text
    assert "Audition · eleven_v3" not in page.text


def test_instant_clone_keeps_voice_id_when_automatic_audition_fails(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    provider = CloningWithFailedAuditionElevenLabs()
    monkeypatch.setattr(web, "elevenlabs", provider)
    with TestClient(web.app) as client:
        voice_id = "baseline--bloodelf-female"
        clip = web.alpha_store.save_reference_clip(
            voice_id,
            original_name="clone-source.wav",
            content=b"reference-audio",
            provenance="Test fixture",
            provider_eligible=True,
        )["clips"][0]
        cloned = client.post(
            f"/api/alpha/voices/{voice_id}/clone",
            headers={
                "X-WQI-Action": "confirmed",
                "X-WQI-Paid-Action": "confirmed",
            },
            json={
                "creation_method": "instant_clone",
                "clip_ids": [clip["clip_id"]],
            },
        )
        revised = web.alpha_store.get_voice(voice_id)
        page = client.get(f"/alpha/voices/{voice_id}")

    assert cloned.status_code == 200
    assert cloned.json()["audition_error"] == "Audition synthesis failed after clone creation."
    assert "voice ID is safely tracked" in cloned.json()["message"]
    assert revised["provider_voice_id"] == "provider-instant-clone"
    assert len(revised["voice_id_candidates"]) == 1
    assert revised["voice_id_candidates"][0]["sample_storage_path"] is None
    assert "Generate Sample" in page.text
    assert "Audition sample missing" not in page.text


def test_reference_library_accepts_several_audio_files_at_once(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        voice_id = web.alpha_store.list_voices("baseline")[0]["voice_id"]
        response = client.post(
            f"/api/alpha/voices/{voice_id}/reference-clips",
            headers={"X-WQI-Action": "confirmed"},
            data={"provenance": "Two clean test excerpts from the same speaker."},
            files=[
                ("file", ("sample-one.mp3", b"first-audio", "audio/mpeg")),
                ("file", ("sample-two.wav", b"second-audio", "audio/wav")),
            ],
        )
        clips = web.alpha_store.get_voice(voice_id)["clips"]
        stored_content = {Path(clip["storage_path"]).read_bytes() for clip in clips}
        page = client.get(f"/alpha/voices/{voice_id}")
        deleted_path = Path(clips[0]["storage_path"])
        deleted = client.delete(
            f"/api/alpha/reference-clips/{clips[0]['clip_id']}",
            headers={"X-WQI-Action": "confirmed"},
        )

    assert response.status_code == 200
    assert response.json()["message"] == "Stored 2 reference clips outside Git."
    assert {clip["original_name"] for clip in clips} == {"sample-one.mp3", "sample-two.wav"}
    assert all(clip["provenance"].startswith("Two clean") for clip in clips)
    assert {Path(clip["storage_path"]).suffix for clip in clips} == {".mp3", ".wav"}
    assert stored_content == {b"first-audio", b"second-audio"}
    assert page.text.count('class="reference-clip"') == 2
    assert '<details class="reference-upload">' in page.text
    assert "Description" in page.text
    assert "Provenance" not in page.text
    assert 'class="fa-solid fa-trash-can"' in page.text
    assert page.text.count("data-compact-audio") == 2
    assert page.text.count("data-audio-name") == 2
    assert page.text.count("data-audio-toggle") == 2
    assert page.text.count("data-audio-progress") == 2
    assert page.text.count("<audio hidden") == 2
    assert deleted.status_code == 200
    assert deleted.json()["message"] == "Reference clip was deleted from local storage."
    assert not deleted_path.exists()
    assert len(web.alpha_store.get_voice(voice_id)["clips"]) == 1


def test_audio_disclosures_play_on_expand_and_collapse_the_previous_item():
    script = (web.WEB_DIR / "static" / "alpha.js").read_text(encoding="utf-8")

    assert 'document.querySelectorAll("[data-audio-disclosure]")' in script
    assert 'disclosure.addEventListener("toggle"' in script
    assert "otherDisclosure.open = false" in script
    assert (
        "otherDisclosure.dataset.audioDisclosure !== disclosure.dataset.audioDisclosure" in script
    )
    assert "audio.play()" in script
    assert "audio.pause()" in script
    assert "syncCompactPlayer" in script
    assert 'document.querySelectorAll("[data-compact-audio]")' in script
    assert "otherAudio && !otherAudio.paused" in script
    assert 'progress.addEventListener("input"' in script


def test_actionable_statuses_link_to_the_work_that_resolves_them(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        row = web.alpha_store.list_dialogue(page_size=10)["rows"][0]
        queue = client.get("/alpha")
        dialogue = client.get(f"/alpha/dialogue/{row['dialogue_id']}")
        speaker = client.get(f"/alpha/speakers/{row['entity_type']}/{row['entity_id']}")
        voices = client.get("/alpha/voices")

    spoken_text_target = f"/alpha/dialogue/{row['dialogue_id']}#spoken-text"
    provider_target = f"/alpha/voices/{row['voice_id']}#provider-creation"
    assert f'href="{spoken_text_target}"' in queue.text
    assert 'href="#spoken-text"' not in dialogue.text
    assert f'href="{spoken_text_target}"' in dialogue.text
    assert 'id="spoken-text"' in dialogue.text
    assert f'href="{provider_target}"' in speaker.text
    assert f'href="{provider_target}"' in voices.text


def test_progress_cards_open_prefiltered_quest_gossip_and_baseline_queues(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    with TestClient(web.app) as client:
        dashboard = client.get("/alpha")
        quests = client.get("/alpha?source=quest")
        gossip = client.get("/alpha?source=gossip")
        baselines = client.get("/alpha/voices?scope=baseline&completion=incomplete")

    assert 'href="/alpha?source=quest"' in dashboard.text
    assert 'href="/alpha?source=gossip"' in dashboard.text
    assert 'href="/alpha/voices?scope=baseline&amp;completion=incomplete"' in dashboard.text
    assert "3 matching records" in quests.text
    assert '<option value="quest" selected>Quest · all stages</option>' in quests.text
    assert "1 matching records" in gossip.text
    assert "Incomplete baseline profiles" in baselines.text
    assert "46 profiles" in baselines.text


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
    assert "How subscription credits work" in page.text
    assert "Cash or overage charges are not estimated here" in page.text
    assert "List-rate equivalent" not in page.text
    assert "configure-elevenlabs.cmd" in page.text
    assert saved.status_code == 200
    assert saved.json()["settings"]["tts_model_id"] == "eleven_multilingual_v2"


def test_provider_status_verifies_account_without_a_paid_action(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(web, "elevenlabs", ConfiguredElevenLabs())
    with TestClient(web.app) as client:
        response = client.get("/api/alpha/provider-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert payload["account"]["tier"] == "creator"
    assert payload["account"]["credits_used"] == 2500
    assert payload["account"]["credits_limit"] == 10000
    assert payload["account"]["credits_remaining"] == 7500
    assert payload["account"]["percent_used"] == 25.0
    assert payload["account"]["refresh_period"] == "monthly_period"
    assert "did not generate audio" in payload["message"]


def test_alpha_header_keeps_subscription_credit_usage_at_eye_level(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(web, "elevenlabs", ConfiguredElevenLabs())
    with TestClient(web.app) as client:
        voice_id = web.alpha_store.list_voices("baseline")[0]["voice_id"]
        page = client.get(f"/alpha/voices/{voice_id}")

    script = (web.WEB_DIR / "static" / "alpha.js").read_text(encoding="utf-8")
    stylesheet = (web.WEB_DIR / "static" / "app.css").read_text(encoding="utf-8")
    template = (web.WEB_DIR / "templates" / "alpha-voice.html").read_text(encoding="utf-8")
    assert page.status_code == 200
    assert "data-provider-usage-indicator" in page.text
    assert 'data-provider-url="/api/alpha/provider-status"' in page.text
    assert "Checking credits..." in page.text
    assert 'document.querySelector("[data-provider-usage-indicator]")' in script
    assert "queueProviderUsageRefresh()" in script
    assert "if (paid) queueProviderUsageRefresh();" in script
    assert "account.credits_used" in script
    assert "data-estimate-credits" in template
    assert "data-estimate-dollars" not in template
    assert "estimate.dollars" not in script
    assert ".provider-state-group { grid-column: 1 / -1; }" in stylesheet


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
