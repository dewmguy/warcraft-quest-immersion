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


def test_filters_isolate_work_by_status_and_content(store: AlphaStore):
    row = _first_dialogue(store)
    store.prepare_spoken_text(row["dialogue_id"])

    needs_voice = store.list_dialogue(state="needs_voice", page_size=10)
    gossip = store.list_dialogue(source="gossip", page_size=10)

    assert needs_voice["total"] == 1
    assert gossip["total"] == 1
    assert gossip["rows"][0]["source"] == "gossip"


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
