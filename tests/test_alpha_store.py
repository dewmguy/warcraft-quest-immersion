import sqlite3
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


def test_initialize_adds_new_columns_to_existing_alpha_database(tmp_path: Path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE voice_previews (preview_id TEXT PRIMARY KEY, voice_id TEXT NOT NULL, "
            "generated_voice_id TEXT NOT NULL, storage_path TEXT NOT NULL, sha256 TEXT NOT NULL, "
            "duration_seconds REAL, prompt TEXT NOT NULL, preview_text TEXT NOT NULL, "
            "model_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate', "
            "created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE voice_delivery_presets (voice_id TEXT NOT NULL, delivery TEXT NOT NULL, "
            "provider_voice_id TEXT NOT NULL DEFAULT '', prompt_tag TEXT NOT NULL DEFAULT '', "
            "stability REAL NOT NULL DEFAULT 0.5, status TEXT NOT NULL DEFAULT 'not_tested', "
            "notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, "
            "PRIMARY KEY(voice_id, delivery))"
        )
        connection.execute(
            "CREATE TABLE voice_delivery_previews (preview_id TEXT PRIMARY KEY, "
            "voice_id TEXT NOT NULL, delivery TEXT NOT NULL, storage_path TEXT NOT NULL, "
            "sha256 TEXT NOT NULL, duration_seconds REAL NOT NULL, sample_text TEXT NOT NULL, "
            "request_json TEXT NOT NULL, provider_request_id TEXT, "
            "subscription_json TEXT NOT NULL DEFAULT '{}', "
            "status TEXT NOT NULL DEFAULT 'candidate', created_at TEXT NOT NULL, reviewed_at TEXT)"
        )

    legacy_store = AlphaStore(database, tmp_path / "storage")
    legacy_store.initialize()
    with legacy_store.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(voice_previews)")}
        voice_columns = {row["name"] for row in connection.execute("PRAGMA table_info(voices)")}
        preset_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(voice_delivery_presets)")
        }
        delivery_preview_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(voice_delivery_previews)")
        }
        voice_id_candidate_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(voice_id_candidates)")
        }

    assert "creation_method" in columns
    assert "generation_number" in columns
    assert "candidate_sequence" in voice_columns
    assert "sample_sequence" in preset_columns
    assert "generation_number" in delivery_preview_columns
    assert "display_name" in delivery_preview_columns
    assert "display_name" in voice_id_candidate_columns


def test_initialize_backfills_current_provider_voice_into_candidate_registry(store: AlphaStore):
    voice_id = "baseline--bloodelf-female"
    current = store.get_voice(voice_id)
    store.update_voice(
        voice_id,
        {
            "description": current["description"],
            "creation_method": "instant_clone",
            "provider_voice_id": "active-instant-clone",
        },
    )
    with store.connect() as connection:
        connection.execute(
            "DELETE FROM voice_id_candidates WHERE provider_voice_id='active-instant-clone'"
        )

    store.initialize()

    candidate = store.get_voice(voice_id)["voice_id_candidates"][0]
    assert candidate["provider_voice_id"] == "active-instant-clone"
    assert candidate["creation_method"] == "instant_clone"
    assert candidate["creation_model_id"] == "instant_voice_clone"
    assert "is_default" not in candidate
    assert {
        preset["provider_voice_id"] for preset in store.get_voice(voice_id)["delivery_presets"]
    } == {"active-instant-clone"}


def test_import_creates_full_scope_records_without_spoken_text(store: AlphaStore):
    dashboard = store.dashboard()
    listing = store.list_dialogue(page_size=10)

    assert dashboard["counts"]["dialogue"] == 4
    assert dashboard["counts"]["speakers"] == 3
    assert dashboard["counts"]["npcs"] == 2
    assert dashboard["counts"]["baseline_voices"] == 46
    assert dashboard["states"] == {"needs_text": 4}
    assert all(row["revision_id"] is None for row in listing["rows"])
    assert store.progress() == {
        "voices": {
            "label": "Baseline deliveries",
            "complete": 0,
            "total": 230,
            "percent": 0.0,
            "href": "/alpha/races?completion=incomplete",
        },
        "unique_npcs": {
            "label": "Unique NPCs",
            "complete": 0,
            "total": 0,
            "percent": 0.0,
            "href": "/alpha/npcs?voice_approach=unique",
        },
        "quests": {
            "label": "Quest audio",
            "complete": 0,
            "total": 3,
            "percent": 0.0,
            "href": "/alpha",
        },
        "gossip": {
            "label": "Gossip audio",
            "complete": 0,
            "total": 1,
            "percent": 0.0,
            "href": "/alpha/gossip",
        },
    }


def test_unique_npc_progress_tracks_ready_active_profiles(store: AlphaStore):
    speaker = next(
        row
        for row in store.list_dialogue(page_size=10)["rows"]
        if row["speaker_name"] == "Sentinel Amara"
    )
    voice = store.create_unique_voice(speaker["speaker_id"])

    assert store.progress()["unique_npcs"] == {
        "label": "Unique NPCs",
        "complete": 0,
        "total": 1,
        "percent": 0.0,
        "href": "/alpha/npcs?voice_approach=unique",
    }

    store.record_voice_id_candidate(
        voice["voice_id"],
        provider_voice_id="unique-progress-voice",
        creation_method="external",
        creation_model_id="external",
    )
    for delivery in alpha_module.DELIVERIES:
        store.update_delivery_preset(voice["voice_id"], delivery, {"status": "approved"})

    assert store.progress()["unique_npcs"]["complete"] == 1
    assert store.progress()["unique_npcs"]["percent"] == 100.0

    store.use_baseline_voice(speaker["speaker_id"])
    assert store.progress()["unique_npcs"]["total"] == 0


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


def test_quest_detail_lists_only_other_phases_of_the_same_quest(store: AlphaStore):
    rows = store.list_dialogue(source="quest", page_size=10)["rows"]
    accept = next(row for row in rows if row["quest_id"] == 101 and row["source"] == "accept")
    complete = next(
        row for row in rows if row["quest_id"] == 101 and row["source"] == "complete"
    )
    single_phase = next(row for row in rows if row["quest_id"] == 102)
    gossip = store.list_dialogue(source="gossip", page_size=10)["rows"][0]

    related = store.get_dialogue(accept["dialogue_id"])["quest_phases"]

    assert [phase["dialogue_id"] for phase in related] == [complete["dialogue_id"]]
    assert related[0]["source"] == "complete"
    assert store.get_dialogue(single_phase["dialogue_id"])["quest_phases"] == []
    assert store.get_dialogue(gossip["dialogue_id"])["quest_phases"] == []


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
    assert "NPC context:" in unique["description"]
    assert "story reach:" in unique["description"]
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
    dormant = store.get_voice(unique["voice_id"])
    assert dormant["status"] == "dormant"
    assert dormant["stored_status"] == "retired"
    assert unique["voice_id"] not in {voice["voice_id"] for voice in store.list_voices("unique")}
    assert store.dashboard()["counts"]["unique_voices"] == 0

    restored = store.create_unique_voice(speaker["speaker_id"])
    assert restored["voice_id"] == unique["voice_id"]
    assert restored["status"] == "draft"
    assert restored["version_number"] == unique["version_number"] + 2


def test_voice_lifecycle_is_derived_from_readiness_and_production(
    store: AlphaStore, monkeypatch: pytest.MonkeyPatch
):
    dialogue = [
        row
        for row in store.list_dialogue(page_size=10)["rows"]
        if row["speaker_name"] == "Marshal Rowan"
    ]
    voice_id = dialogue[0]["voice_id"]

    draft = store.get_voice(voice_id)
    assert draft["status"] == "draft"
    assert "generate a reusable ElevenLabs voice ID" in draft["lifecycle_reason"]

    store.update_voice(
        voice_id,
        {
            "description": draft["description"],
            "creation_method": "external",
            "provider_voice_id": "provider-voice-test",
        },
    )
    for delivery in alpha_module.DELIVERIES:
        store.update_delivery_preset(voice_id, delivery, {"status": "approved"})

    candidate = store.get_voice(voice_id)
    assert candidate["status"] == "candidate"
    assert candidate["deployed_dialogue_count"] == 0

    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.25)
    for index, row in enumerate(dialogue, start=1):
        store.prepare_spoken_text(row["dialogue_id"])
        generation = store.begin_generation(row["dialogue_id"])
        audio = store.complete_generation(
            generation["generation_id"],
            content=f"audio-{index}".encode(),
            mime_type="audio/mpeg",
            provider_request_id=None,
            subscription=None,
        )
        store.approve_candidate(audio["candidate_id"])
        lifecycle = store.get_voice(voice_id)
        assert lifecycle["status"] == ("active" if index == 1 else "completed")

    assert lifecycle["missing_dialogue_count"] == 0
    assert lifecycle["deployed_dialogue_count"] == 2


def test_voice_lifecycle_cannot_be_set_manually(store: AlphaStore):
    voice = store.list_voices("baseline")[0]

    with pytest.raises(AlphaError, match="computed automatically"):
        store.update_voice(voice["voice_id"], {"status": "active"})

    with store.connect() as connection:
        connection.execute(
            "UPDATE voice_versions SET status='retired' WHERE voice_id=? AND is_current=1",
            (voice["voice_id"],),
        )
    reloaded = store.get_voice(voice["voice_id"])
    assert reloaded["status"] == "draft"
    assert voice["voice_id"] in {item["voice_id"] for item in store.list_voices("baseline")}


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
    assert "2 spoken record(s)" in record["npc"]["context_summary"]
    assert "this NPC" in record["npc"]["context_summary"]

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


def test_npc_directory_excludes_objects_and_filters_voice_approach(store: AlphaStore):
    directory = store.list_npcs(page_size=10)

    assert directory["total"] == 2
    assert {npc["name"] for npc in directory["rows"]} == {
        "Marshal Rowan",
        "Sentinel Amara",
    }
    assert "Weathered Tablet" not in {npc["name"] for npc in directory["rows"]}
    assert store.list_npcs(query="Amara")["total"] == 1
    assert store.list_npcs(role="officer")["rows"][0]["name"] == "Marshal Rowan"

    store.create_unique_voice("creature-90002")
    assert store.list_npcs(voice_approach="unique")["rows"][0]["name"] == "Sentinel Amara"
    store.use_baseline_voice("creature-90002")
    dormant = store.list_npcs(voice_approach="dormant")
    assert dormant["rows"][0]["name"] == "Sentinel Amara"
    assert dormant["rows"][0]["unique_voice_id"] == "unique--creature-90002"


def test_voice_versions_only_change_on_delta_and_prompt_can_be_restored(store: AlphaStore):
    voice = store.list_voices("baseline")[0]
    current = store.get_voice(voice["voice_id"])

    unchanged = store.update_voice(
        voice["voice_id"],
        {
            "description": current["description"],
            "creation_method": current["creation_method"],
            **current["settings"],
        },
    )
    changed = store.update_voice(
        voice["voice_id"],
        {
            "description": current["description"] + " Reviewed for Alpha.",
            "creation_method": "designed",
        },
    )
    restored = store.restore_voice_prompt(voice["voice_id"], current["version_id"])

    assert unchanged["version_changed"] is False
    assert changed["version_number"] == current["version_number"] + 1
    assert {"Voice description", "Creation method"}.issubset(set(changed["versions"][0]["delta"]))
    assert restored["version_number"] == changed["version_number"] + 1
    assert restored["description"] == current["description"]
    assert restored["creation_method"] == "designed"
    assert changed["description"] in {
        version["description"] for version in restored["prompt_versions"]
    }


def test_baseline_context_revision_preserves_creation_work(store: AlphaStore, monkeypatch):
    voice_id = "baseline--bloodelf-female"
    current = store.get_voice(voice_id)
    configured = store.update_voice(
        voice_id,
        {
            "description": "Legacy baseline context that is long enough for an Alpha voice record.",
            "creation_method": "reference_design",
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


def test_voice_candidate_regeneration_replaces_files_only_after_new_set_is_ready(
    store: AlphaStore, monkeypatch
):
    voice_id = "baseline--bloodelf-female"
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.25)
    former_ids = store.record_voice_previews(
        voice_id,
        prompt="Former prompt",
        preview_text="Former comparison text",
        model_id="eleven_ttv_v3",
        previews=[
            {"content": f"former-{index}".encode(), "generated_voice_id": f"former-{index}"}
            for index in range(3)
        ],
    )
    former_paths = [Path(item["storage_path"]) for item in store.get_voice(voice_id)["previews"]]

    with pytest.raises(AlphaError, match="At least one generated voice candidate"):
        store.record_voice_previews(
            voice_id,
            prompt="Failed prompt",
            preview_text="Failed comparison text",
            model_id="eleven_ttv_v3",
            previews=[],
            replace_existing=True,
        )
    assert {item["preview_id"] for item in store.get_voice(voice_id)["previews"]} == set(former_ids)
    assert all(path.is_file() for path in former_paths)

    replacement_ids = store.record_voice_previews(
        voice_id,
        prompt="Replacement prompt",
        preview_text="Replacement comparison text",
        model_id="eleven_ttv_v3",
        previews=[
            {
                "content": f"replacement-{index}".encode(),
                "generated_voice_id": f"replacement-{index}",
            }
            for index in range(3)
        ],
        replace_existing=True,
    )
    revised = store.get_voice(voice_id)

    assert {item["preview_id"] for item in revised["previews"]} == set(replacement_ids)
    assert {item["generation_number"] for item in revised["previews"]} == {4, 5, 6}
    assert not any(path.exists() for path in former_paths)
    assert all(Path(item["storage_path"]).is_file() for item in revised["previews"])

    newest_id = store.record_voice_previews(
        voice_id,
        prompt="Later prompt",
        preview_text="Later comparison text",
        model_id="eleven_ttv_v3",
        previews=[{"content": b"later", "generated_voice_id": "later-candidate"}],
        replace_existing=True,
    )[0]
    assert store.get_voice_preview(newest_id)["generation_number"] == 7


def test_voice_candidate_can_be_deleted_without_changing_other_voice_data(
    store: AlphaStore, monkeypatch
):
    voice_id = "baseline--bloodelf-female"
    before = store.get_voice(voice_id)
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.25)
    preview_id = store.record_voice_previews(
        voice_id,
        prompt="Candidate prompt",
        preview_text="Candidate comparison text",
        model_id="eleven_ttv_v3",
        previews=[{"content": b"candidate", "generated_voice_id": "generated-candidate"}],
    )[0]
    preview_path = Path(store.get_voice_preview(preview_id)["storage_path"])

    revised = store.delete_voice_preview(preview_id)

    assert revised["previews"] == []
    assert revised["version_number"] == before["version_number"]
    assert revised["clips"] == before["clips"]
    assert not preview_path.exists()
    with pytest.raises(AlphaError, match="Voice preview was not found"):
        store.get_voice_preview(preview_id)
    later_id = store.record_voice_previews(
        voice_id,
        prompt="Later prompt",
        preview_text="Later comparison text",
        model_id="eleven_ttv_v3",
        previews=[{"content": b"later", "generated_voice_id": "later-generated"}],
    )[0]
    assert store.get_voice_preview(later_id)["generation_number"] == 2


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
    voice = store.get_voice(prepared["voice_id"])
    angry = next(item for item in voice["delivery_presets"] if item["delivery"] == "angry")
    assert angry["prompt_tag"] == "furious"
    assert dialogue["generation_text"].startswith("[furious]")
    assert store.progress()["voices"]["complete"] == 1


def test_each_delivery_preset_can_target_a_different_voice_id_candidate(store: AlphaStore):
    voice_id = "baseline--bloodelf-female"
    voice = store.get_voice(voice_id)
    store.update_voice(
        voice_id,
        {
            "description": voice["description"],
            "creation_method": "external",
            "provider_voice_id": "provider-default",
        },
    )
    alternate = store.record_voice_id_candidate(
        voice_id,
        provider_voice_id="provider-sorrowful",
        creation_method="designed",
        creation_model_id="eleven_ttv_v3",
    )
    store.update_voice_id_candidate_name(alternate["candidate_id"], "  Grieving   Noble  ")
    store.update_delivery_preset(
        voice_id,
        "sorrowful",
        {
            "provider_voice_id": alternate["provider_voice_id"],
            "prompt_tag": "quietly grieving",
            "stability": 0.5,
        },
    )

    request = store.delivery_preview_request(
        voice_id,
        "sorrowful",
        "This fixed passage is deliberately long enough to test the selected reusable voice "
        "candidate for a restrained and sorrowful emotional delivery.",
    )
    revised = store.get_voice(voice_id)
    preset = next(item for item in revised["delivery_presets"] if item["delivery"] == "sorrowful")

    assert request["voice_id"] == "provider-sorrowful"
    assert request["baseline_voice_id"] == "provider-sorrowful"
    assert preset["provider_voice_id"] == "provider-sorrowful"
    assert preset["effective_provider_voice_id"] == "provider-sorrowful"
    assert next(
        item
        for item in revised["voice_id_candidates"]
        if item["provider_voice_id"] == "provider-sorrowful"
    )["display_name"] == "Grieving Noble"
    assert len(revised["voice_id_candidates"]) == 2
    assert {item["provider_voice_id"] for item in revised["voice_id_candidates"]} == {
        "provider-default",
        "provider-sorrowful",
    }
    with pytest.raises(AlphaError, match="cannot exceed 80 characters"):
        store.update_voice_id_candidate_name(alternate["candidate_id"], "x" * 81)


def test_initialize_removes_legacy_brackets_from_saved_voice_actor_notes(store: AlphaStore):
    voice_id = store.list_voices("baseline")[0]["voice_id"]
    with store.connect() as connection:
        connection.execute(
            "UPDATE voice_delivery_presets SET prompt_tag='[[sharply]]' "
            "WHERE voice_id=? AND delivery='angry'",
            (voice_id,),
        )

    store.initialize()

    voice = store.get_voice(voice_id)
    angry = next(item for item in voice["delivery_presets"] if item["delivery"] == "angry")
    assert angry["prompt_tag"] == "sharply"


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
        },
    )
    store.update_delivery_preset(
        voice["voice_id"],
        "angry",
        {"prompt_tag": "angry", "stability": 0, "notes": "Comparison candidate"},
    )
    sample_text = (
        "The road ahead is dangerous, but our purpose remains clear. Stay close, listen "
        "carefully, and remember why we began this journey."
    )

    request = store.delivery_preview_request(voice["voice_id"], "angry", sample_text)
    assert request["text"].startswith("[angry]")
    assert request["voice_settings"] == {"stability": 0.0}
    assert request["actor_notes"] == "angry"
    assert request["performance_method"] == "creative"
    assert request["performance_method_label"] == "Creative"
    assert request["baseline_voice_id"] == "provider-voice-test"
    assert "notes" not in request
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
    assert preview["generation_number"] == 1
    store.update_delivery_preview_name(preview["preview_id"], "  Controlled   Anger  ")
    assert store.delivery_preview_path(preview["preview_id"]).is_file()
    stored = store.get_voice(voice["voice_id"])["delivery_presets"][1]["previews"][0]
    assert stored["actor_notes"] == "angry"
    assert stored["performance_method"] == "creative"
    assert stored["performance_method_label"] == "Creative"
    assert stored["baseline_voice_id"] == "provider-voice-test"
    assert stored["generation_number"] == 1
    assert stored["display_name"] == "Controlled Anger"
    assert store.progress()["voices"]["complete"] == 0

    approved = store.approve_delivery_preview(preview["preview_id"])
    angry = next(item for item in approved["delivery_presets"] if item["delivery"] == "angry")
    assert angry["status"] == "approved"
    assert angry["previews"][0]["status"] == "approved"
    assert store.progress()["voices"]["complete"] == 1


def test_existing_delivery_samples_recover_metadata_from_their_saved_request(
    store: AlphaStore, monkeypatch: pytest.MonkeyPatch
):
    voice_id = "baseline--bloodelf-female"
    voice = store.get_voice(voice_id)
    store.update_voice(
        voice_id,
        {
            "description": voice["description"],
            "creation_method": "external",
            "provider_voice_id": "legacy-provider-voice",
        },
    )
    sample_text = (
        "The road ahead is dangerous, but our purpose remains clear. Stay close, listen "
        "carefully, and remember why we began this journey."
    )
    request = store.delivery_preview_request(voice_id, "angry", sample_text)
    legacy_request = {
        key: value
        for key, value in request.items()
        if key
        not in {
            "actor_notes",
            "baseline_voice_id",
            "performance_method",
            "performance_method_label",
        }
    }
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.25)
    store.record_delivery_preview(
        voice_id,
        "angry",
        legacy_request,
        content=b"legacy-delivery-audio",
        provider_request_id="legacy-request",
        subscription={},
    )

    stored = store.get_voice(voice_id)["delivery_presets"][1]["previews"][0]

    assert stored["actor_notes"] == "angry"
    assert stored["performance_method"] == "natural"
    assert stored["performance_method_label"] == "Natural"
    assert stored["baseline_voice_id"] == "legacy-provider-voice"


def test_existing_delivery_samples_receive_stable_numbers_during_migration(
    store: AlphaStore, monkeypatch: pytest.MonkeyPatch
):
    voice_id = "baseline--bloodelf-female"
    voice = store.get_voice(voice_id)
    store.update_voice(
        voice_id,
        {
            "description": voice["description"],
            "creation_method": "external",
            "provider_voice_id": "provider-voice-test",
        },
    )
    request = store.delivery_preview_request(
        voice_id,
        "angry",
        "The road ahead is dangerous, but our purpose remains clear. Stay close, listen "
        "carefully, and remember why we began this journey.",
    )
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.25)
    first = store.record_delivery_preview(
        voice_id,
        "angry",
        request,
        content=b"legacy-first",
        provider_request_id="legacy-first",
        subscription={},
    )
    second = store.record_delivery_preview(
        voice_id,
        "angry",
        request,
        content=b"legacy-second",
        provider_request_id="legacy-second",
        subscription={},
    )
    with store.connect() as connection:
        connection.execute("DROP INDEX delivery_sample_generation_number")
        connection.execute(
            "UPDATE voice_delivery_previews SET created_at='2026-01-01T00:00:01+00:00', "
            "generation_number=0 WHERE preview_id=?",
            (first["preview_id"],),
        )
        connection.execute(
            "UPDATE voice_delivery_previews SET created_at='2026-01-01T00:00:02+00:00', "
            "generation_number=0 WHERE preview_id=?",
            (second["preview_id"],),
        )
        connection.execute(
            "UPDATE voice_delivery_presets SET sample_sequence=0 "
            "WHERE voice_id=? AND delivery='angry'",
            (voice_id,),
        )

    store.initialize()

    angry = next(
        item for item in store.get_voice(voice_id)["delivery_presets"] if item["delivery"] == "angry"
    )
    assert [
        (preview["preview_id"], preview["generation_number"]) for preview in angry["previews"]
    ] == [(second["preview_id"], 2), (first["preview_id"], 1)]
    store.delete_delivery_preview(second["preview_id"])
    third = store.record_delivery_preview(
        voice_id,
        "angry",
        request,
        content=b"post-migration-third",
        provider_request_id="post-migration-third",
        subscription={},
    )
    assert third["generation_number"] == 3


def test_delivery_comparisons_can_be_deleted_and_recalculate_preset_status(
    store: AlphaStore, monkeypatch: pytest.MonkeyPatch
):
    voice_id = "baseline--bloodelf-female"
    voice = store.get_voice(voice_id)
    store.update_voice(
        voice_id,
        {
            "description": voice["description"],
            "creation_method": "external",
            "provider_voice_id": "provider-voice-test",
        },
    )
    sample_text = (
        "The road ahead is dangerous, but our purpose remains clear. Stay close, listen "
        "carefully, and remember why we began this journey."
    )
    request = store.delivery_preview_request(voice_id, "angry", sample_text)
    monkeypatch.setattr(alpha_module, "_audio_duration", lambda _path: 1.25)
    first = store.record_delivery_preview(
        voice_id,
        "angry",
        request,
        content=b"first-delivery-audio",
        provider_request_id="request-first",
        subscription={},
    )
    store.approve_delivery_preview(first["preview_id"])
    second = store.record_delivery_preview(
        voice_id,
        "angry",
        request,
        content=b"second-delivery-audio",
        provider_request_id="request-second",
        subscription={},
    )
    assert first["generation_number"] == 1
    assert second["generation_number"] == 2
    first_path = store.delivery_preview_path(first["preview_id"])
    second_path = store.delivery_preview_path(second["preview_id"])

    with_candidate = store.delete_delivery_preview(first["preview_id"])
    angry = next(item for item in with_candidate["delivery_presets"] if item["delivery"] == "angry")
    assert angry["status"] == "previewed"
    assert [item["preview_id"] for item in angry["previews"]] == [second["preview_id"]]
    assert angry["previews"][0]["generation_number"] == 2
    assert not first_path.exists()

    without_previews = store.delete_delivery_preview(second["preview_id"])
    angry = next(
        item for item in without_previews["delivery_presets"] if item["delivery"] == "angry"
    )
    assert angry["status"] == "not_tested"
    assert angry["previews"] == []
    assert not second_path.exists()

    third = store.record_delivery_preview(
        voice_id,
        "angry",
        request,
        content=b"third-delivery-audio",
        provider_request_id="request-third",
        subscription={},
    )
    assert third["generation_number"] == 3
    after_third_delete = store.delete_delivery_preview(third["preview_id"])
    angry = next(
        item for item in after_third_delete["delivery_presets"] if item["delivery"] == "angry"
    )
    assert angry["status"] == "not_tested"
    assert angry["previews"] == []
    with pytest.raises(AlphaError, match="Delivery preview was not found"):
        store.delete_delivery_preview(second["preview_id"])


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
