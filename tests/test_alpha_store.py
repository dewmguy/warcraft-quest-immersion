from pathlib import Path

import pytest

from tts_cli import alpha_store as alpha_module
from tts_cli.alpha_store import AlphaError, AlphaStore
from tts_cli.paths import SAMPLE_DATA_PATH


@pytest.fixture
def store(tmp_path: Path) -> AlphaStore:
    instance = AlphaStore(tmp_path / "alpha.sqlite3", tmp_path / "storage")
    instance.initialize()
    instance.import_csv(SAMPLE_DATA_PATH, source_name="sample.csv")
    return instance


def _first_dialogue(store: AlphaStore) -> dict:
    return store.list_dialogue(page_size=10)["rows"][0]


def test_import_creates_full_scope_records_without_spoken_text(store: AlphaStore):
    dashboard = store.dashboard()
    listing = store.list_dialogue(page_size=10)

    assert dashboard["counts"]["dialogue"] == 4
    assert dashboard["counts"]["speakers"] == 3
    assert dashboard["counts"]["baseline_voices"] == 46
    assert dashboard["states"] == {"needs_text": 4}
    assert all(row["revision_id"] is None for row in listing["rows"])
    assert store.progress() == {
        "voices": {
            "label": "Baseline deliveries",
            "complete": 0,
            "total": 230,
            "percent": 0.0,
            "href": "/alpha/voices?scope=baseline&completion=incomplete",
        },
        "quests": {
            "label": "Quest audio",
            "complete": 0,
            "total": 3,
            "percent": 0.0,
            "href": "/alpha?source=quest",
        },
        "gossip": {
            "label": "Gossip audio",
            "complete": 0,
            "total": 1,
            "percent": 0.0,
            "href": "/alpha?source=gossip",
        },
    }


def test_spoken_text_is_created_only_by_explicit_action(store: AlphaStore):
    row = _first_dialogue(store)
    original = row["original_text"]

    prepared = store.prepare_spoken_text(row["dialogue_id"])

    assert prepared["original_text"] == original
    assert prepared["revision_number"] == 1
    assert "$N" not in prepared["spoken_text"]
    assert "Adventurer" in prepared["spoken_text"]
    with pytest.raises(AlphaError, match="already been prepared"):
        store.prepare_spoken_text(row["dialogue_id"])


def test_provider_usage_ledger_records_exact_reported_cost(store: AlphaStore):
    event = store.record_provider_usage(
        action="dialogue_tts",
        subject_id="dialogue-test",
        input_character_count=118,
        character_cost=120,
        provider_request_id="request-test",
        subscription={"character_count": 2500, "character_limit": 10000},
    )

    assert event["action"] == "dialogue_tts"
    assert event["input_character_count"] == 118
    assert event["character_cost"] == 120
    assert event["subscription"]["character_count"] == 2500
    assert store.list_provider_usage()[0]["provider_request_id"] == "request-test"


def test_text_voice_generation_and_approval_are_separate_records(
    store: AlphaStore, monkeypatch: pytest.MonkeyPatch
):
    row = _first_dialogue(store)
    dialogue_id = row["dialogue_id"]
    prepared = store.prepare_spoken_text(dialogue_id)
    voice = store.get_voice(prepared["voice_id"])
    store.update_voice(
        voice["voice_id"],
        {
            "description": voice["description"],
            "creation_method": "external",
            "provider_voice_id": "provider-voice-test",
            "status": "active",
        },
    )

    ready = store.get_dialogue(dialogue_id)
    assert ready["production_state"] == "ready_to_generate"
    generation = store.begin_generation(dialogue_id)
    assert generation["text"] == ready["spoken_text"]

    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.25)
    candidate = store.complete_generation(
        generation["generation_id"],
        content=b"test-audio",
        mime_type="audio/mpeg",
        provider_request_id="request-test",
        subscription={"character_count": 123},
    )
    reviewed = store.get_dialogue(dialogue_id)
    assert reviewed["production_state"] == "audio_to_review"

    approved = store.approve_candidate(candidate["candidate_id"])
    manifest = store.export_manifest()
    assert approved["production_state"] == "approved"
    assert approved["addon_filename"].endswith(f"{approved['addon_file_key']}.mp3")
    assert manifest["asset_count"] == 1
    assert manifest["assets"][0]["candidate_id"] == candidate["candidate_id"]
    assert manifest["assets"][0]["package_path"].startswith("3.3.5/enUS/")


def test_unique_voice_inherits_baseline_and_is_assigned(store: AlphaStore):
    row = _first_dialogue(store)
    speaker = store.get_speaker(row["speaker_id"])["speaker"]

    unique = store.create_unique_voice(speaker["speaker_id"])
    updated = store.get_speaker(speaker["speaker_id"])["speaker"]

    assert unique["scope"] == "unique"
    assert unique["parent_voice_id"] == speaker["voice_id"]
    assert updated["voice_id"] == unique["voice_id"]
    assert updated["uniqueness"] == "unique"


def test_unique_voice_can_return_to_baseline_without_losing_history(store: AlphaStore):
    row = next(
        item
        for item in store.list_dialogue(page_size=10)["rows"]
        if item["speaker_name"] == "Sentinel Amara"
    )
    speaker = store.get_speaker(row["speaker_id"])["speaker"]
    baseline_voice_id = speaker["voice_id"]
    unique = store.create_unique_voice(speaker["speaker_id"])

    reset = store.use_baseline_voice(speaker["speaker_id"])

    assert reset["speaker"]["voice_id"] == baseline_voice_id
    assert reset["speaker"]["uniqueness"] == "baseline"
    assert reset["retired_voice_id"] == unique["voice_id"]
    assert store.get_voice(unique["voice_id"])["status"] == "retired"
    assert unique["voice_id"] not in {voice["voice_id"] for voice in store.list_voices("unique")}
    assert store.dashboard()["counts"]["unique_voices"] == 0

    restored = store.create_unique_voice(speaker["speaker_id"])
    assert restored["voice_id"] == unique["voice_id"]
    assert restored["status"] == "draft"
    assert restored["version_number"] == unique["version_number"] + 2


def test_speaker_context_is_inferred_but_remains_editable(store: AlphaStore):
    row = next(
        item
        for item in store.list_dialogue(page_size=10)["rows"]
        if item["speaker_name"] == "Marshal Rowan"
    )
    record = store.get_speaker(row["speaker_id"])

    assert record["speaker"]["role"] == "officer"
    assert record["speaker"]["importance"] == "stepping_stone"
    assert record["speaker"]["importance_score"] == 25
    assert "2 spoken record(s)" in record["speaker"]["context_summary"]

    updated = store.update_speaker(
        row["speaker_id"],
        {
            "role": "soldier",
            "faction": "alliance",
            "zone": "Elwynn Forest",
            "importance": "zone",
            "context_summary": "Reviewed context.",
            "voice_id": record["speaker"]["voice_id"],
        },
    )
    assert updated["speaker"]["role"] == "soldier"
    assert updated["speaker"]["importance_score"] == 55


def test_voice_versions_only_change_on_delta_and_can_be_restored(store: AlphaStore):
    voice = store.list_voices("baseline")[0]
    current = store.get_voice(voice["voice_id"])

    unchanged = store.update_voice(
        voice["voice_id"],
        {
            "description": current["description"],
            "creation_method": current["creation_method"],
            "status": current["status"],
            **current["settings"],
        },
    )
    changed = store.update_voice(
        voice["voice_id"],
        {
            "description": current["description"] + " Reviewed for Alpha.",
            "creation_method": "designed",
            "status": "candidate",
        },
    )
    restored = store.restore_voice_version(voice["voice_id"], current["version_id"])

    assert unchanged["version_changed"] is False
    assert changed["version_number"] == current["version_number"] + 1
    assert {"Voice description", "Creation method", "Lifecycle status"}.issubset(
        set(changed["versions"][0]["delta"])
    )
    assert restored["version_number"] == changed["version_number"] + 1
    assert restored["description"] == current["description"]


def test_baseline_context_revision_preserves_creation_work(store: AlphaStore, monkeypatch):
    voice_id = "baseline--bloodelf-female"
    current = store.get_voice(voice_id)
    configured = store.update_voice(
        voice_id,
        {
            "description": "Legacy baseline context that is long enough for an Alpha voice record.",
            "creation_method": "reference_design",
            "status": "draft",
            "stability": 1,
        },
    )
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.25)
    store.save_reference_clip(
        voice_id,
        original_name="blood-elf-reference.ogg",
        content=b"reference-audio",
        provenance="Test fixture",
        provider_eligible=True,
    )
    store.record_voice_previews(
        voice_id,
        prompt=configured["description"],
        preview_text="A sufficiently long fixed comparison script for the saved Alpha preview record.",
        model_id="eleven_ttv_v3",
        previews=[{"content": b"candidate-audio", "generated_voice_id": "generated-test"}],
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE app_settings SET value_json='1' WHERE setting_key='baseline_context_revision'"
        )

    store.initialize()
    revised = store.get_voice(voice_id)

    assert revised["version_number"] == configured["version_number"] + 1
    assert revised["creation_method"] == "reference_design"
    assert revised["status"] == "draft"
    assert revised["settings"]["stability"] == 1
    assert len(revised["clips"]) == 1
    assert len(revised["previews"]) == 1
    assert "modern General American" in revised["description"]
    assert "Adult female voice" in revised["description"]
    assert "No British influence" in revised["description"]
    assert current["version_number"] < revised["version_number"]


def test_delivery_presets_are_voice_metadata_used_for_dialogue(store: AlphaStore):
    row = _first_dialogue(store)
    prepared = store.prepare_spoken_text(row["dialogue_id"])
    store.set_delivery(row["dialogue_id"], "angry")
    store.update_delivery_preset(
        prepared["voice_id"],
        "angry",
        {"prompt_tag": "[furious]", "stability": 0, "status": "approved", "notes": "Tested"},
    )

    dialogue = store.get_dialogue(row["dialogue_id"])
    assert dialogue["generation_text"].startswith("[furious]")
    assert store.progress()["voices"]["complete"] == 1


def test_incomplete_baseline_filter_requires_all_five_delivery_presets(store: AlphaStore):
    voice = store.get_voice(store.list_voices("baseline")[0]["voice_id"])
    assert len(store.list_voices("baseline", "incomplete")) == 46

    for preset in voice["delivery_presets"]:
        store.update_delivery_preset(
            voice["voice_id"],
            preset["delivery"],
            {
                "prompt_tag": preset["prompt_tag"],
                "stability": preset["stability"],
                "status": "approved",
                "notes": "Filter test",
            },
        )

    assert len(store.list_voices("baseline", "incomplete")) == 45
    assert [item["voice_id"] for item in store.list_voices("baseline", "complete")] == [
        voice["voice_id"]
    ]


def test_delivery_progress_requires_an_approved_generated_comparison(
    store: AlphaStore, monkeypatch: pytest.MonkeyPatch
):
    voice = store.get_voice(store.list_voices("baseline")[0]["voice_id"])
    store.update_voice(
        voice["voice_id"],
        {
            "description": voice["description"],
            "creation_method": "external",
            "provider_voice_id": "provider-voice-test",
            "status": "active",
        },
    )
    store.update_delivery_preset(
        voice["voice_id"],
        "angry",
        {"prompt_tag": "[angry]", "stability": 0, "notes": "Comparison candidate"},
    )
    sample_text = (
        "The road ahead is dangerous, but our purpose remains clear. Stay close, listen "
        "carefully, and remember why we began this journey."
    )

    request = store.delivery_preview_request(voice["voice_id"], "angry", sample_text)
    assert request["text"].startswith("[angry]")
    assert request["voice_settings"] == {"stability": 0.0}
    assert store.progress()["voices"]["complete"] == 0

    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.25)
    preview = store.record_delivery_preview(
        voice["voice_id"],
        "angry",
        request,
        content=b"test-audio",
        provider_request_id="request-test",
        subscription={"character_count": len(request["text"])},
    )
    assert store.delivery_preview_path(preview["preview_id"]).is_file()
    assert store.progress()["voices"]["complete"] == 0

    approved = store.approve_delivery_preview(preview["preview_id"])
    angry = next(item for item in approved["delivery_presets"] if item["delivery"] == "angry")
    assert angry["status"] == "approved"
    assert angry["previews"][0]["status"] == "approved"
    assert store.progress()["voices"]["complete"] == 1


def test_filters_isolate_work_by_status_and_content(store: AlphaStore):
    row = _first_dialogue(store)
    store.prepare_spoken_text(row["dialogue_id"])

    needs_voice = store.list_dialogue(state="needs_voice", page_size=10)
    quests = store.list_dialogue(source="quest", page_size=10)
    gossip = store.list_dialogue(source="gossip", page_size=10)

    assert needs_voice["total"] == 1
    assert quests["total"] == 3
    assert all(row["source"] != "gossip" for row in quests["rows"])
    assert gossip["total"] == 1
    assert gossip["rows"][0]["source"] == "gossip"
    with pytest.raises(AlphaError, match="Unknown content filter"):
        store.list_dialogue(source="not-a-source")


def test_expansion_sources_coexist_in_one_production_database(tmp_path: Path):
    instance = AlphaStore(tmp_path / "alpha.sqlite3", tmp_path / "storage")
    instance.initialize()

    instance.import_csv(SAMPLE_DATA_PATH, expansion="1.12.1", locale="enUS")
    instance.import_csv(SAMPLE_DATA_PATH, expansion="3.3.5", locale="enUS")

    dashboard = instance.dashboard()
    assert dashboard["counts"]["dialogue"] == 8
    assert len(dashboard["snapshots"]) == 2
    assert instance.list_dialogue(expansion="1.12.1")["total"] == 4
    assert instance.list_dialogue(expansion="3.3.5")["total"] == 4
