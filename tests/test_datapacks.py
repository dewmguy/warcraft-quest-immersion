from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tts_cli.alpha_store import AlphaError, AlphaStore
from tts_cli.datapacks import inspect_datapack_archive, inspect_datapack_directory


def build_datapack(
    path: Path,
    *,
    module: str,
    title: str,
    version: str,
    priority: int,
    gossip_key: str,
) -> Path:
    sounds = {
        "100-accept": b"generic-accept-audio",
        "f-100-complete": b"female-complete-audio",
        "m-100-complete": b"male-complete-audio",
        gossip_key: b"gossip-audio",
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{module}/{module}.toc",
            "\n".join(
                (
                    "## Interface: 30300",
                    f"## Title: VoiceOver Data - {title}",
                    f"## Version: {version}",
                    f"## X-VoiceOver-DataModule-Priority: {priority}",
                    "",
                )
            ),
        )
        archive.writestr(
            f"{module}/generated/sound_length_table.lua",
            "\n".join(f'["{stem}"] = {index + 1}.25,' for index, stem in enumerate(sounds)),
        )
        archive.writestr(
            f"{module}/generated/npc_gossip_file_lookups.lua",
            f'[1] = {{ ["Fixture line"] = "{gossip_key}" }},\n',
        )
        for stem, content in sounds.items():
            folder = "gossip" if stem == gossip_key else "quests"
            archive.writestr(
                f"{module}/generated/sounds/{folder}/{stem}.mp3",
                content,
            )
    return path


def test_datapack_inspection_preserves_gender_variants_and_ignores_addon_archives(
    tmp_path: Path,
):
    gossip_key = "a" * 32
    archive = build_datapack(
        tmp_path / "FixtureData_v1.2.zip",
        module="FixtureData",
        title="Fixture",
        version="1.2",
        priority=105,
        gossip_key=gossip_key,
    )
    with zipfile.ZipFile(tmp_path / "AddonOnly_v1.0.zip", "w") as addon:
        addon.writestr("AddonOnly/AddonOnly.toc", "## Title: Addon Only\n")

    inspected = inspect_datapack_archive(archive)
    packs, ignored = inspect_datapack_directory(tmp_path)

    assert inspected is not None
    assert inspected.title == "Fixture"
    assert inspected.archive_version == "1.2"
    assert inspected.priority == 105
    assert len(inspected.assets) == 4
    complete = [asset for asset in inspected.assets if asset.logical_key == "quest:100:complete"]
    assert {asset.player_gender_variant for asset in complete} == {"f", "m"}
    assert all(asset.lookup_referenced for asset in inspected.assets)
    assert [pack.pack_id for pack in packs] == [inspected.pack_id]
    assert ignored == ["AddonOnly_v1.0.zip"]


def test_datapack_import_selects_highest_priority_and_keeps_versions_comparable(
    tmp_path: Path,
    corpus_bundle_path: Path,
):
    store = AlphaStore(tmp_path / "production.sqlite3", tmp_path / "storage")
    store.initialize()
    store.import_corpus_bundle(corpus_bundle_path)
    gossip = store.list_dialogue(source="gossip", page_size=100)["rows"][0]
    gossip_key = gossip["addon_file_key"]
    low_path = build_datapack(
        tmp_path / "LowData_v1.0.zip",
        module="LowData",
        title="Low Priority",
        version="1.0",
        priority=100,
        gossip_key=gossip_key,
    )
    high_path = build_datapack(
        tmp_path / "HighData_v2.0.zip",
        module="HighData",
        title="High Priority",
        version="2.0",
        priority=110,
        gossip_key=gossip_key,
    )
    packs = [inspect_datapack_archive(low_path), inspect_datapack_archive(high_path)]
    typed_packs = [pack for pack in packs if pack is not None]

    dry_run = store.import_datapacks(typed_packs, dry_run=True)
    applied = store.import_datapacks(typed_packs)
    accept = next(
        row
        for row in store.list_dialogue(source="accept", page_size=100)["rows"]
        if row["quest_id"] == 100
    )
    complete = next(
        row
        for row in store.list_dialogue(source="complete", page_size=100)["rows"]
        if row["quest_id"] == 100
    )
    accept_record = store.get_dialogue(accept["dialogue_id"])
    complete_record = store.get_dialogue(complete["dialogue_id"])

    assert dry_run["applied"] is False
    assert dry_run["counts"]["audio_assets"] == 8
    assert dry_run["counts"]["unmatched_assets"] == 0
    assert applied["applied"] is True
    assert accept_record["production_state"] == "preproduced_selected"
    assert accept_record["preproduced_candidate_count"] == 2
    assert len(accept_record["candidates"]) == 2
    selected = next(
        candidate for candidate in accept_record["candidates"] if candidate["is_selected"]
    )
    assert selected["pack_title"] == "High Priority"
    assert len(complete_record["production_files"]) == 2
    assert {Path(item["filename"]).name[:2] for item in complete_record["production_files"]} == {
        "f-",
        "m-",
    }

    low = next(
        candidate
        for candidate in accept_record["candidates"]
        if candidate["pack_title"] == "Low Priority"
    )
    switched = store.select_candidate(low["candidate_id"])
    assert (
        next(candidate for candidate in switched["candidates"] if candidate["is_selected"])[
            "pack_title"
        ]
        == "Low Priority"
    )
    repeated = store.import_datapacks(typed_packs)
    preserved = store.get_dialogue(accept["dialogue_id"])
    assert repeated["counts"]["default_selections"] == 0
    assert (
        next(candidate for candidate in preserved["candidates"] if candidate["is_selected"])[
            "pack_title"
        ]
        == "Low Priority"
    )

    gendered = next(
        candidate
        for candidate in complete_record["candidates"]
        if candidate["pack_title"] == "High Priority"
    )
    with pytest.raises(AlphaError, match="Choose a player-text variant"):
        store.candidate_path(gendered["candidate_id"])
    for asset in gendered["assets"]:
        assert store.candidate_path(gendered["candidate_id"], asset["asset_id"]).is_file()

    manifest = store.export_manifest()
    assert manifest["schema_version"] == 3
    assert (
        manifest["selection_count"]
        == store.progress()["quests"]["complete"] + store.progress()["gossip"]["complete"]
    )
    assert all(asset["candidate_origin"] == "preproduced" for asset in manifest["assets"])
