from __future__ import annotations

import json
import zipfile

import pytest

from tts_cli.alpha_store import AlphaError, AlphaStore
from tts_cli.corpus import (
    AzerothCoreCorpusExtractor,
    CorpusError,
    MappingCorpusSource,
    load_corpus_bundle,
    write_corpus_bundle,
)


def extract(tables: dict[str, list[dict]]):
    return AzerothCoreCorpusExtractor(
        MappingCorpusSource(tables),
        source_sha256="a" * 64,
        source_version="AzerothCore rev fixture",
        source_artifacts=[{"name": "dbc.sql.gz", "sha256": "b" * 64}],
    ).extract()


def test_extractor_reconciles_shared_quest_givers_and_nested_gossip(azerothcore_tables):
    bundle = extract(azerothcore_tables)

    accept = next(
        row for row in bundle.texts if row["stage"] == "accept" and row["quest_id"] == 100
    )
    accept_bindings = [row for row in bundle.bindings if row["content_id"] == accept["content_id"]]
    assert {row["addon_file_key"] for row in accept_bindings} == {
        "100-accept-c1",
        "100-accept-c2",
        "100-accept-g20",
        "100-accept-i30",
    }
    assert len([row for row in bundle.bindings if row["stage"] == "progress"]) == 3
    assert len([row for row in bundle.bindings if row["stage"] == "complete"]) == 3
    nested_trigger = next(row for row in bundle.triggers if row["menu_path"] == "10>11")
    nested_context = json.loads(nested_trigger["context_json"])
    assert nested_context["player_choice_path"] == ["Tell me more."]
    assert nested_context["player_choice_conditions"][0][0]["ConditionValue1"] == 100
    assert {row["variant"] for row in bundle.texts if row["source_table"] == "broadcast_text"} == {
        "male",
        "female",
    }
    assert {row["reason"] for row in bundle.quarantine} >= {
        "cyclic_gossip_menu",
        "disabled_quest",
        "unrooted_gossip",
    }
    rowan = next(row for row in bundle.entities if row["entity_key"] == "3.3.5:creature:1")
    assert rowan["race_name"] == "human"
    assert rowan["gender_name"] == "male"
    assert rowan["zone_name"] == "Elwynn Forest"
    assert rowan["faction_name"] == "Stormwind"
    assert bundle.manifest["source"]["database_version_rows"] == [
        {"required_rev": "2026_08_10_00", "sql_rev": "2026_08_11_00"}
    ]
    assert bundle.manifest["source"]["database_version_table"] == "version_db_world"
    assert bundle.manifest["source"]["additional_artifacts"] == [
        {"name": "dbc.sql.gz", "sha256": "b" * 64}
    ]
    assert bundle.manifest["tables"]["version_db_world"]["rows"] == 1
    assert bundle.manifest["counts"]["active_bindings"] == sum(
        row["active"] for row in bundle.bindings
    )
    disabled = next(row for row in bundle.bindings if row["quest_id"] == 200)
    assert disabled["active"] == 0


def test_current_azerothcore_version_table_is_recorded(azerothcore_tables):
    tables = dict(azerothcore_tables)
    tables["version"] = [
        {
            "core_version": "AzerothCore rev. cb999cf88954",
            "core_revision": "cb999cf88954",
            "db_version": "ACDB 335.16-dev",
            "cache_id": 16,
        }
    ]
    tables.pop("version_db_world")

    bundle = extract(tables)

    assert bundle.manifest["source"]["database_version_table"] == "version"
    assert bundle.manifest["source"]["database_version_rows"][0]["db_version"] == (
        "ACDB 335.16-dev"
    )
    assert bundle.manifest["tables"]["version"]["rows"] == 1


def test_stable_ids_ignore_source_row_order(azerothcore_tables):
    first = extract(azerothcore_tables)
    reversed_tables = {name: list(reversed(rows)) for name, rows in azerothcore_tables.items()}
    second = extract(reversed_tables)

    for attribute, key in (
        ("entities", "entity_key"),
        ("texts", "content_id"),
        ("bindings", "binding_id"),
        ("triggers", "trigger_id"),
        ("quarantine", "finding_id"),
    ):
        assert [row[key] for row in getattr(first, attribute)] == [
            row[key] for row in getattr(second, attribute)
        ]


def test_extractor_fails_closed_without_enrichment_or_recognized_npc_text(azerothcore_tables):
    missing_dbc = dict(azerothcore_tables)
    missing_dbc.pop("db_CreatureDisplayInfo")
    with pytest.raises(CorpusError, match="enrichment tables"):
        extract(missing_dbc)

    unknown_text = dict(azerothcore_tables)
    unknown_text["npc_text"] = [{"ID": 500, "UnknownText": "not supported"}]
    with pytest.raises(CorpusError, match="neither BroadcastTextID slots nor direct text slots"):
        extract(unknown_text)


def test_missing_delivery_entity_is_quarantined(azerothcore_tables):
    tables = {name: [dict(row) for row in rows] for name, rows in azerothcore_tables.items()}
    tables["gameobject_template"] = [
        {"entry": 21, "name": "Unrelated Fixture", "type": 10, "data19": 0}
    ]

    bundle = extract(tables)

    object_bindings = [row for row in bundle.bindings if row["entity_type"] == "gameobject"]
    assert object_bindings
    assert {row["active"] for row in object_bindings} == {0}
    assert "missing_entity" in {row["reason"] for row in bundle.quarantine}


def test_split_gossip_action_schema_is_supported(azerothcore_tables):
    tables = {name: [dict(row) for row in rows] for name, rows in azerothcore_tables.items()}
    actions = []
    for option in tables["gossip_menu_option"]:
        actions.append(
            {
                "MenuId": option["MenuID"],
                "OptionIndex": option["OptionID"],
                "ActionMenuId": option.pop("ActionMenuID"),
                "ActionPoiId": 0,
            }
        )
        option["OptionIndex"] = option.pop("OptionID")
    tables["gossip_menu_option_action"] = actions

    bundle = extract(tables)

    assert any(row["menu_path"] == "10>11" for row in bundle.triggers)


def test_multiple_item_starters_are_quarantined(azerothcore_tables):
    tables = {name: [dict(row) for row in rows] for name, rows in azerothcore_tables.items()}
    tables["item_template"].append({"entry": 31, "name": "Second Warning", "StartQuest": 100})
    bundle = extract(tables)

    item_accept = [
        row for row in bundle.bindings if row["stage"] == "accept" and row["entity_type"] == "item"
    ]
    assert len(item_accept) == 2
    assert {row["active"] for row in item_accept} == {0}
    assert {
        finding["binding_id"]
        for finding in bundle.quarantine
        if finding["reason"] == "ambiguous_item_starter"
    } == {row["binding_id"] for row in item_accept}


def test_bundle_hashes_and_relationships_are_verified(tmp_path, azerothcore_tables):
    path = write_corpus_bundle(extract(azerothcore_tables), tmp_path / "valid.zip")
    loaded = load_corpus_bundle(path)
    assert loaded.manifest["counts"]["bindings"] == len(loaded.bindings)

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(tampered, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "bindings.csv":
                content += b"tampered"
            target.writestr(name, content)
    with pytest.raises(CorpusError, match="hash does not match"):
        load_corpus_bundle(tampered)


def test_atomic_import_preserves_overrides_and_shares_spoken_text(
    tmp_path, corpus_bundle_path, azerothcore_tables
):
    store = AlphaStore(tmp_path / "production.sqlite3", tmp_path / "storage")
    store.initialize()
    with store.connect() as connection:
        dialogue_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(dialogue_entries)")
        }
    assert "dialogue_binding_idx" in dialogue_indexes

    dry_run = store.import_corpus_bundle(corpus_bundle_path, dry_run=True)
    assert dry_run["counts"]["added"] == dry_run["counts"]["active_bindings"]
    assert store.dashboard()["counts"]["dialogue"] == 0
    applied = store.import_corpus_bundle(corpus_bundle_path)
    assert applied["applied"] is True
    assert store.dashboard()["counts"]["dialogue"] == applied["counts"]["active_bindings"]

    rowan = store.get_speaker("creature-1")["speaker"]
    assert rowan["faction"] == "alliance"
    store.update_speaker(rowan["speaker_id"], {"zone": "Manual Testing Zone"})
    accept_rows = [
        row
        for row in store.list_dialogue(source="accept", page_size=50)["rows"]
        if row["quest_id"] == 100
    ]
    store.save_spoken_text(accept_rows[0]["dialogue_id"], "Shared reviewed wording.")
    assert {store.get_dialogue(row["dialogue_id"])["spoken_text"] for row in accept_rows} == {
        "Shared reviewed wording."
    }

    assert store.import_corpus_bundle(corpus_bundle_path)["counts"]["added"] == 0
    assert store.get_speaker(rowan["speaker_id"])["speaker"]["zone"] == "Manual Testing Zone"

    changed_tables = {
        name: [dict(row) for row in rows] for name, rows in azerothcore_tables.items()
    }
    changed_tables["quest_template"][0]["QuestDescription"] = "The warning has changed."
    changed_path = write_corpus_bundle(extract(changed_tables), tmp_path / "changed.zip")
    changed_plan = store.import_corpus_bundle(changed_path, dry_run=True)
    assert changed_plan["counts"]["source_changed"] == 4
    store.import_corpus_bundle(changed_path)
    changed_accept = store.get_dialogue(accept_rows[0]["dialogue_id"])
    assert changed_accept["production_state"] == "source_changed"
    assert changed_accept["spoken_text"] == "Shared reviewed wording."
    assert len(changed_accept["quest_phases"]) >= 9

    store.import_corpus_bundle(changed_path)
    assert store.get_dialogue(accept_rows[0]["dialogue_id"])["production_state"] == "source_changed"

    store.save_spoken_text(accept_rows[0]["dialogue_id"], "Reviewed after source change.")
    assert store.get_dialogue(accept_rows[0]["dialogue_id"])["production_state"] != "source_changed"
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dialogue_content_versions WHERE content_id=?",
                (accept_rows[0]["content_id"],),
            ).fetchone()[0]
            == 2
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failed_bundle_import_leaves_active_snapshot_unchanged(tmp_path, corpus_bundle_path):
    store = AlphaStore(tmp_path / "production.sqlite3", tmp_path / "storage")
    store.initialize()
    store.import_corpus_bundle(corpus_bundle_path)
    before = store.dashboard()["snapshot"]["snapshot_id"]

    bad = tmp_path / "partial.zip"
    with zipfile.ZipFile(corpus_bundle_path) as source, zipfile.ZipFile(bad, "w") as target:
        target.writestr("manifest.json", source.read("manifest.json"))
        target.writestr("entities.csv", source.read("entities.csv"))
    with pytest.raises(AlphaError, match="bundle contents are invalid"):
        store.import_corpus_bundle(bad)

    assert store.dashboard()["snapshot"]["snapshot_id"] == before


def test_legacy_projection_alias_preserves_voice_samples_generations_and_assets(
    tmp_path, corpus_bundle_path
):
    store = AlphaStore(tmp_path / "production.sqlite3", tmp_path / "storage")
    store.initialize()
    legacy_csv = tmp_path / "legacy.csv"
    legacy_csv.write_text(
        "source,quest,quest_title,text,DisplayRaceID,DisplaySexID,name,type,id,original_text\n"
        'accept,100,A Shared Duty,"Bring this warning to the pass, $N.",1,0,'
        'Marshal Rowan,creature,1,"Bring this warning to the pass, $N."\n',
        encoding="utf-8",
    )
    store.import_csv(legacy_csv)
    legacy = store.list_dialogue(source="accept", page_size=10)["rows"][0]
    store.update_speaker("creature-1", {"zone": "Owner Override"})
    timestamp = "2026-08-11T00:00:00+00:00"
    with store.connect() as connection:
        version_id = connection.execute(
            "SELECT version_id FROM voice_versions WHERE voice_id='baseline--human-male' "
            "AND is_current=1"
        ).fetchone()[0]
        revision_id = connection.execute(
            "SELECT revision_id FROM spoken_text_revisions WHERE dialogue_id=? AND is_current=1",
            (legacy["dialogue_id"],),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO voice_id_candidates(candidate_id, voice_id, provider_voice_id, "
            "generation_number, creation_method, creation_model_id, created_at) "
            "VALUES ('voice-candidate', 'baseline--human-male', 'provider-preserved', 1, "
            "'designed', 'eleven_ttv_v3', ?)",
            (timestamp,),
        )
        connection.execute(
            "INSERT INTO reference_clips(clip_id, voice_id, original_name, storage_path, sha256, "
            "provenance, created_at) VALUES ('clip-preserved', 'baseline--human-male', "
            "'fixture.wav', 'ignored/fixture.wav', ?, 'fixture', ?)",
            ("a" * 64, timestamp),
        )
        connection.execute(
            "INSERT INTO voice_delivery_previews(preview_id, voice_id, delivery, "
            "generation_number, storage_path, sha256, duration_seconds, sample_text, "
            "request_json, status, created_at) VALUES ('preset-preserved', "
            "'baseline--human-male', 'neutral', 1, 'ignored/preset.mp3', ?, 1.0, "
            "'sample', '{}', 'candidate', ?)",
            ("b" * 64, timestamp),
        )
        connection.execute(
            "INSERT INTO generations(generation_id, dialogue_id, revision_id, voice_id, "
            "voice_version_id, provider, model_id, delivery, request_text, request_json, "
            "character_count, status, created_at) VALUES ('generation-preserved', ?, ?, "
            "'baseline--human-male', ?, 'fixture', 'fixture', 'neutral', 'sample', '{}', 6, "
            "'complete', ?)",
            (legacy["dialogue_id"], revision_id, version_id, timestamp),
        )
        connection.execute(
            "INSERT INTO audio_candidates(candidate_id, generation_id, dialogue_id, "
            "storage_path, sha256, duration_seconds, mime_type, status, created_at) VALUES "
            "('audio-preserved', 'generation-preserved', ?, 'ignored/audio.mp3', ?, 1.0, "
            "'audio/mpeg', 'approved', ?)",
            (legacy["dialogue_id"], "c" * 64, timestamp),
        )
        connection.execute(
            "INSERT INTO production_assets(dialogue_id, candidate_id, addon_filename, sha256, "
            "duration_seconds, approved_at) VALUES (?, 'audio-preserved', "
            "'generated/sounds/quests/100-accept.mp3', ?, 1.0, ?)",
            (legacy["dialogue_id"], "c" * 64, timestamp),
        )

    store.import_corpus_bundle(corpus_bundle_path)

    with store.connect() as connection:
        binding = connection.execute(
            "SELECT binding_id, dialogue_id FROM dialogue_bindings WHERE entity_type='creature' "
            "AND entity_id=1 AND quest_id=100 AND stage='accept'"
        ).fetchone()
        alias = connection.execute(
            "SELECT binding_id FROM legacy_dialogue_aliases WHERE alias_id=?",
            (legacy["dialogue_id"],),
        ).fetchone()
        assert binding["dialogue_id"] == legacy["dialogue_id"]
        assert alias["binding_id"] == binding["binding_id"]
        for table in (
            "voice_id_candidates",
            "reference_clips",
            "voice_delivery_previews",
            "generations",
            "audio_candidates",
            "production_assets",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT addon_filename FROM production_assets WHERE dialogue_id=?",
                (legacy["dialogue_id"],),
            )
            .fetchone()[0]
            .endswith("100-accept-c1.mp3")
        )
    assert store.get_speaker("creature-1")["speaker"]["zone"] == "Owner Override"


def test_manifest_is_machine_readable(corpus_bundle_path):
    with zipfile.ZipFile(corpus_bundle_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["source"]["expansion"] == "3.3.5"
    assert manifest["source"]["locale"] == "enUS"
    assert manifest["source"]["sha256"] == "a" * 64
