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

    def subscription(self):
        return {
            "tier": "creator",
            "status": "active",
            "character_count": 2500,
            "character_limit": 10000,
            "next_character_count_reset_unix": 1_800_000_000,
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
    def clone_voice(self, **_kwargs):
        return {"voice_id": "provider-instant-clone"}


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
    assert "Automatic lifecycle" in page.text
    assert "Draft until you" in page.text
    assert "Voice Design Prompt Context" in page.text
    assert "Context sent to Voice Design" not in page.text
    assert "This prompt is sent only by Voice Design" in page.text
    assert "data-prompt-context hidden" in page.text
    assert 'name="status"' not in page.text
    assert "Lifecycle status</span><select" not in page.text
    assert "Provider voice missing" in page.text
    assert "ElevenLabs voice ID" not in page.text
    assert "Reference-guided Voice Design" in page.text
    assert "Instant Voice Clone" in page.text
    assert "Emotional delivery presets" in page.text
    assert "Shape how this voice performs common emotions" in page.text
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
    assert page.text.count("data-dirty-submit hidden") == 5
    assert page.text.count('class="fa-solid fa-floppy-disk"') == 5
    assert page.text.count("Generate sample") == 5
    assert "Preset settings" in page.text
    assert "Sample" in page.text
    assert "Generate all samples" in page.text
    assert "Fixed sample script" in page.text
    assert "<blockquote>" in page.text
    assert "<details><summary>Fixed comparison script" not in page.text
    assert page.text.count("data-delivery-generation") == 5
    assert "data-delivery-batch" in page.text
    assert 'type="range"' in page.text
    assert 'href="#provider-creation"' in page.text
    assert 'id="provider-creation"' in page.text
    assert "multiple" in page.text
    assert "MP3, WAV, M4A, OGG, or FLAC" in page.text
    assert "Stored reference clips" in page.text
    assert "https://kit.fontawesome.com/666b0b7246.js" in page.text
    assert "Files are preserved exactly as uploaded" in page.text
    assert "No stored candidates will be removed" in page.text

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
        '        <div class="panel-heading"><div><span class="panel-step">Provider creation</span>'
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
            previews=[{"content": b"candidate-audio", "generated_voice_id": "candidate-one"}],
        )[0]
        preview_path = Path(web.alpha_store.get_voice_preview(preview_id)["storage_path"])
        page = client.get(f"/alpha/voices/{voice_id}")
        deleted = client.delete(
            f"/api/alpha/voice-previews/{preview_id}",
            headers={"X-WQI-Action": "confirmed"},
        )

    assert page.status_code == 200
    assert "permanently replace and delete all 1 stored candidate" in page.text
    assert f'data-url="/api/alpha/voice-previews/{preview_id}"' in page.text
    assert 'data-method="DELETE"' in page.text
    assert "This does not affect reference clips" in page.text
    assert 'class="reference-player candidate-player"' in page.text
    assert 'data-audio-name="Candidate 1"' in page.text
    assert (
        f'<audio hidden preload="metadata" src="/api/alpha/voice-previews/{preview_id}/audio">'
        in page.text
    )
    assert deleted.status_code == 200
    assert deleted.json()["message"] == "Voice candidate was deleted from local storage."
    assert not preview_path.exists()
    assert web.alpha_store.get_voice(voice_id)["previews"] == []


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
    assert page.text.count('class="fa-solid fa-check"') == 3
    assert page.text.count("Approve this neutral sample?") == 3
    assert page.text.count("Permanently delete this neutral sample") == 3
    assert page.text.count("Stored samples") == 1
    assert page.text.count('class="reference-player delivery-player"') == 3
    assert page.text.count('class="delivery-preview-metadata"') == 3
    assert page.text.count("Voice actor notes") == 3
    assert page.text.count(">with restrained warmth</dd>") == 3
    assert 'value="more brightly"' in page.text
    assert page.text.count("Performance method") == 3
    assert page.text.count(">Robust</dd>") == 3
    assert page.text.count("ElevenLabs voice ID") == 3
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


def test_delivery_sample_forms_show_save_only_when_dirty_and_skip_all_confirmations():
    script = (web.WEB_DIR / "static" / "alpha.js").read_text(encoding="utf-8")

    assert 'document.querySelectorAll("[data-dirty-form]")' in script
    assert "const dirtyFormSnapshots = new WeakMap()" in script
    assert "function syncDirtyForm(form)" in script
    assert "const requestBody = jsonFromForm(form)" in script
    assert "dirtyFormSnapshots.set(form, JSON.stringify(requestBody))" in script
    assert "syncDirtyForm(form)" in script
    assert "if (form.dataset.dirtyForm !== undefined)" in script
    assert "if (conditionalSubmit) conditionalSubmit.disabled = true" in script
    assert "if (conditionalSubmit) conditionalSubmit.disabled = false" in script
    assert "if (conditionalSubmit?.hidden) return" in script
    assert 'deliveryBatchButton.textContent = "Generate all samples"' in script
    assert "deliveryBatchConfirmation" not in script
    assert "paidConfirmation" not in script
    assert "const shouldConfirm = paid" not in script
    assert (
        'if (confirmRequired && !window.confirm(confirmText || "Confirm this action?"))' in script
    )
    assert "for (const form of forms) launchDeliveryGeneration(form);" in script


def test_voice_design_prompt_visibility_follows_creation_method():
    script = (web.WEB_DIR / "static" / "alpha.js").read_text(encoding="utf-8")

    assert 'selected === "designed" || selected === "reference_design"' in script
    assert 'selected === "instant_clone"' in script
    assert "promptContext.hidden = !usesVoiceDesign && !showsDisabledPrompt" in script
    assert "promptField.disabled = !usesVoiceDesign" in script
    assert "selected !== savedMethod" not in script
    assert 'field.disabled = field.dataset.serverDisabled === "true" || !active' in script


def test_every_voice_page_delete_control_requires_confirmation():
    template = (web.WEB_DIR / "templates" / "alpha-voice.html").read_text(encoding="utf-8")
    opening_tags = [chunk.split(">", 1)[0] for chunk in template.split("<button")[1:]]
    delete_controls = [tag for tag in opening_tags if 'data-method="DELETE"' in tag]

    assert len(delete_controls) == 3
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
        if 'data-provider-operation="Creating an instant voice clone"' in chunk.split(">", 1)[0]
    )
    reusable_voice_button = next(
        chunk.split(">", 1)[0]
        for chunk in voice_template.split("<button")[1:]
        if 'data-provider-operation="Creating a reusable ElevenLabs voice"'
        in chunk.split(">", 1)[0]
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
    assert "Replaced 1 former unselected candidate" in response.json()["message"]
    assert len(revised["previews"]) == 3
    assert former_id not in {item["preview_id"] for item in revised["previews"]}
    assert not former_path.exists()


def test_selected_voice_candidate_survives_regeneration(monkeypatch):
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
    assert "Deleted 2 other local candidates" in activated.json()["message"]
    assert selected_page.status_code == 200
    assert 'class="candidate-selected"' in selected_page.text
    assert "Selected candidate" in selected_page.text
    assert "disconnect its reusable voice ID from this profile" in selected_page.text
    assert "The selected candidate and reusable ElevenLabs voice will be preserved" in (
        selected_page.text
    )
    assert f'data-url="/api/alpha/voice-previews/{selected_id}"' in selected_page.text
    assert regenerated.status_code == 200
    assert (
        "Preserved the selected candidate and reusable ElevenLabs voice"
        in (regenerated.json()["message"])
    )
    assert revised["provider_voice_id"] == "provider-voice-selected"
    assert len(revised["previews"]) == 4
    assert sum(preview["status"] == "selected" for preview in revised["previews"]) == 1
    assert {preview["preview_id"] for preview in revised["previews"]} == {
        selected_id,
        *regenerated.json()["preview_ids"],
    }
    assert original_paths[selected_id].is_file()
    assert all(
        not path.exists()
        for preview_id, path in original_paths.items()
        if preview_id != selected_id
    )


def test_selected_voice_candidate_can_be_deleted_and_disconnects_reusable_id(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(web, "elevenlabs", DesigningElevenLabs())
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
        selected_path = Path(web.alpha_store.get_voice_preview(preview_id)["storage_path"])
        deleted = client.delete(
            f"/api/alpha/voice-previews/{preview_id}",
            headers={"X-WQI-Action": "confirmed"},
        )
        revised = web.alpha_store.get_voice(voice_id)

    assert activated.status_code == 200
    assert deleted.status_code == 200
    assert "reusable voice ID was disconnected" in deleted.json()["message"]
    assert "remains in your provider account" in deleted.json()["message"]
    assert revised["provider_voice_id"] is None
    assert revised["previews"] == []
    assert not selected_path.exists()


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
    assert {preview["creation_method"] for preview in revised["previews"]} == {"designed"}
    assert page.text.count("Created with Voice Design") == 3


def test_instant_clone_accepts_new_unsaved_path(monkeypatch):
    monkeypatch.delenv("WQI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(web, "elevenlabs", CloningElevenLabs())
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 65.0)
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

    assert cloned.status_code == 200
    assert revised["creation_method"] == "instant_clone"
    assert revised["provider_voice_id"] == "provider-instant-clone"


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
    assert "characters, not LLM tokens" in page.text
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
    assert payload["account"]["remaining_characters"] == 7500
    assert payload["account"]["percent_used"] == 25.0
    assert "did not generate audio" in payload["message"]


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
