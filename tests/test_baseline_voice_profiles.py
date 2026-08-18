import csv
import hashlib
import json
from pathlib import Path

from scripts import build_baseline_voice_profiles
from tts_cli.voice_profiles import load_phase2_review

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = PROJECT_ROOT / "voice_profiles"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_baseline_builder_is_deterministic_and_complete():
    profile_count, preview_count = build_baseline_voice_profiles.build()
    paths = [
        PROFILE_DIR / "baseline-voice-profiles.csv",
        PROFILE_DIR / "placeholder-preview-matrix.csv",
        PROFILE_DIR / "phase2-manifest.json",
    ]
    first_hashes = [_digest(path) for path in paths]

    assert build_baseline_voice_profiles.build() == (profile_count, preview_count)
    assert [_digest(path) for path in paths] == first_hashes
    assert (profile_count, preview_count) == (98, 490)


def test_profile_matrix_covers_every_legacy_race_and_gender():
    review = load_phase2_review()
    combinations = {(profile["race_id"], profile["gender_id"]) for profile in review["profiles"]}

    race_payload = json.loads((PROFILE_DIR / "race-archetypes.json").read_text(encoding="utf-8"))
    assert combinations == {
        (race["race_id"], gender_id) for race in race_payload["archetypes"] for gender_id in [0, 1]
    }
    assert review["manifest"]["observed_profile_count"] == 33
    assert review["manifest"]["planned_fallback_count"] == 63
    assert review["manifest"]["system_profile_count"] == 2
    assert review["preview_states"] == {"ungenerated": 490}


def test_every_race_has_an_explicit_auditable_accent_contract():
    payload = json.loads((PROFILE_DIR / "race-archetypes.json").read_text(encoding="utf-8"))

    assert len(payload["archetypes"]) == 49
    for race in payload["archetypes"]:
        assert race["accent_target"].startswith("Native English")
        assert len(race["accent_avoid"]) >= 40
        assert len(race["accent_basis"]) >= 30

    blood_elf = next(race for race in payload["archetypes"] if race["slug"] == "bloodelf")
    assert "General American" in blood_elf["accent_target"]
    assert "fully rhotic" in blood_elf["accent_target"]
    assert "No British influence" in blood_elf["accent_avoid"]
    assert "Mid-Atlantic" in blood_elf["accent_avoid"]


def test_provider_prompts_name_dialect_gender_and_exclusions():
    review = load_phase2_review()

    for profile in review["profiles"]:
        assert profile["accent_target"].startswith("Native English")
        assert profile["gender_name"] in {"Male", "Female"}
        assert profile["accent_avoid"]


def test_known_display_counts_come_from_the_pinned_export():
    review = load_phase2_review()
    profiles = {profile["profile_id"]: profile for profile in review["profiles"]}

    assert profiles["human-male"]["observed_display_count"] == 2265
    assert profiles["human-female"]["observed_display_count"] == 1246
    assert profiles["felorc-female"]["coverage_state"] == "planned_fallback"
    assert profiles["worgen-male"]["coverage_state"] == "planned_fallback"


def test_source_library_manifest_is_well_formed_and_empty():
    with (PROFILE_DIR / "source-library/manifest.csv").open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        rows = list(reader)

    assert rows == []
    assert "sha256" in reader.fieldnames
    assert "rights_basis" in reader.fieldnames


def test_phase2_manifest_records_pinned_source_hashes():
    manifest = json.loads((PROFILE_DIR / "phase2-manifest.json").read_text(encoding="utf-8"))

    assert manifest["source_hashes"]["assets/sql/exported/CreatureDisplayInfoExtra.sql"] == (
        "69d1d2d37c63dce3c0f4fd7ae054f9dd2c727b4ba62abe1d0857fff8d95b15d4"
    )
