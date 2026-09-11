from pathlib import Path

from tts_cli.alpha_store import AlphaStore
from tts_cli.npc_references import (
    NPCReferenceCatalog,
    NPCReferenceEntry,
    load_npc_reference_catalog,
)
from tts_cli.paths import ASSETS_DIR, SAMPLE_DATA_PATH


def _catalog(
    *entries: NPCReferenceEntry, matched_voice_scope: str = "unchanged"
) -> NPCReferenceCatalog:
    return NPCReferenceCatalog(
        catalog_id="test-cultural-references",
        catalog_version=1,
        expansion="3.3.5",
        locale="enUS",
        researched_at="2026-09-10",
        entries=entries,
        matched_voice_scope=matched_voice_scope,
    )


def _entry(name: str = "Marshal Rowan") -> NPCReferenceEntry:
    return NPCReferenceEntry(
        key="rowan-test-reference",
        npc_names=(name,),
        entity_ids=(),
        reference_title="A Useful Reference",
        reference_type="literature",
        summary="The name and quest form a direct reference to a fictional commander.",
        voice_direction="Measured command authority with restrained warmth.",
        confidence="high",
        source_title="Reference source",
        source_url="https://example.test/reference",
    )


def test_bundled_reference_catalog_is_valid_and_contains_requested_example():
    catalog = load_npc_reference_catalog(ASSETS_DIR / "npc-references" / "3.3.5-enUS.json")

    assert catalog.catalog_version == 2
    assert catalog.matched_voice_scope == "unique"
    assert len(catalog.entries) >= 60
    stonefield = next(entry for entry in catalog.entries if entry.key == "stonefield-maclure-feud")
    assert stonefield.npc_names == ("Tommy Joe Stonefield", "Maybell Maclure")
    assert "Hatfield-McCoy" in stonefield.reference_title
    assert "The Notebook" in stonefield.summary


def test_reference_import_preserves_context_and_supports_search_and_voice_direction(
    tmp_path: Path,
):
    store = AlphaStore(tmp_path / "alpha.sqlite3", tmp_path / "storage")
    store.initialize()
    store.import_csv(SAMPLE_DATA_PATH)
    row = store.list_npcs(page_size=10)["rows"][0]
    original_context = store.get_speaker(row["speaker_id"])["npc"]["context_summary"]
    catalog = _catalog(_entry(row["name"]), _entry("Missing NPC"))

    dry_run = store.import_npc_reference_catalog(catalog, dry_run=True)
    assert dry_run["reference_records"] == 1
    assert dry_run["unmatched_entries"] == [
        {"key": "rowan-test-reference", "npc_names": ["Missing NPC"]}
    ]

    applied = store.import_npc_reference_catalog(catalog)
    assert applied["applied"] is True
    assert Path(applied["backup"]["path"]).is_file()
    profile = store.get_speaker(row["speaker_id"])
    assert profile["npc"]["context_summary"] == original_context
    assert profile["references"][0]["reference_title"] == "A Useful Reference"
    assert store.list_npcs(query="fictional commander")["total"] == 1

    unique = store.create_unique_voice(row["speaker_id"])
    assert "Cultural reference:" in unique["description"]
    assert "Measured command authority" in unique["description"]


def test_reimport_updates_catalog_record_without_creating_a_duplicate(tmp_path: Path):
    store = AlphaStore(tmp_path / "alpha.sqlite3", tmp_path / "storage")
    store.initialize()
    store.import_csv(SAMPLE_DATA_PATH)
    npc_name = store.list_npcs(page_size=10)["rows"][0]["name"]
    first = _entry(npc_name)
    revised = NPCReferenceEntry(
        **{
            **first.__dict__,
            "summary": "Revised, more precise reference research.",
        }
    )

    store.import_npc_reference_catalog(_catalog(first))
    store.import_npc_reference_catalog(_catalog(revised))

    with store.connect() as connection:
        rows = connection.execute("SELECT * FROM speaker_references").fetchall()
    assert len(rows) == 1
    assert rows[0]["summary"] == "Revised, more precise reference research."


def test_reference_catalog_can_activate_every_matched_npc_as_unique(tmp_path: Path):
    store = AlphaStore(tmp_path / "alpha.sqlite3", tmp_path / "storage")
    store.initialize()
    store.import_csv(SAMPLE_DATA_PATH)
    npc = store.list_npcs(page_size=1)["rows"][0]
    catalog = _catalog(_entry(npc["name"]), matched_voice_scope="unique")

    dry_run = store.import_npc_reference_catalog(catalog, dry_run=True)
    assert dry_run["unique_profile_activations"] == 1

    applied = store.import_npc_reference_catalog(catalog)
    profile = store.get_speaker(npc["speaker_id"])
    assert applied["unique_profiles_activated"] == 1
    assert profile["npc"]["voice_scope"] == "unique"
    assert profile["npc"]["voice_id"] == f"unique--{npc['speaker_id']}"
    assert (
        store.import_npc_reference_catalog(catalog, dry_run=True)["unique_profile_activations"] == 0
    )
