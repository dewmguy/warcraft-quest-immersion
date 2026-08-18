from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = PROJECT_ROOT / "voice_profiles"
DISPLAY_EXTRA_PATH = PROJECT_ROOT / "assets/sql/exported/CreatureDisplayInfoExtra.sql"
DISPLAY_INFO_PATH = PROJECT_ROOT / "assets/sql/exported/CreatureDisplayInfo.sql"

PINNED_SOURCE_HASHES = {
    DISPLAY_EXTRA_PATH: "69d1d2d37c63dce3c0f4fd7ae054f9dd2c727b4ba62abe1d0857fff8d95b15d4",
    DISPLAY_INFO_PATH: "48086305bbc84d0af8928c37cb74bcdc95229918b91f00e1c88b74563427c51a",
}

PROFILE_FIELDS = [
    "profile_id",
    "race_id",
    "race_slug",
    "race_name",
    "gender_id",
    "gender_slug",
    "gender_name",
    "observed_display_count",
    "coverage_state",
    "identity",
    "accent_target",
    "accent_or_cadence",
    "accent_avoid",
    "accent_basis",
    "timbre",
    "pacing",
    "gender_guidance",
    "guardrails",
    "approval_status",
    "source_voice_id",
]

PREVIEW_FIELDS = [
    "preview_id",
    "profile_id",
    "delivery_preset_id",
    "evaluation_script_id",
    "profile_approval_status",
    "delivery_approval_status",
    "source_voice_id",
    "audio_status",
    "audio_path",
    "owner_decision",
    "notes",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(name: str) -> dict:
    path = PROFILE_DIR / name
    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    if payload.get("schema_version") != 1:
        raise ValueError(f"{name} must use schema_version 1")
    return payload


def _unique_records(records: list[dict], key: str, label: str) -> dict:
    indexed = {}
    for record in records:
        value = record.get(key)
        if value in indexed:
            raise ValueError(f"Duplicate {label}: {value}")
        indexed[value] = record
    return indexed


def _validate_pinned_sources() -> dict[str, str]:
    actual_hashes = {}
    for path, expected_hash in PINNED_SOURCE_HASHES.items():
        actual_hash = _sha256(path)
        if actual_hash != expected_hash:
            raise ValueError(
                f"Pinned source changed: {path.name} expected {expected_hash}, got {actual_hash}"
            )
        actual_hashes[str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")] = actual_hash
    return actual_hashes


def _observed_race_gender_counts() -> Counter[tuple[int, int]]:
    pattern = re.compile(
        r"INSERT INTO `db_CreatureDisplayInfoExtra` VALUES \(\s*-?\d+\s*,\s*(-?\d+)\s*,\s*(\d+)\s*,"
    )
    counts: Counter[tuple[int, int]] = Counter()
    with DISPLAY_EXTRA_PATH.open(encoding="utf-8", errors="strict") as source:
        for line in source:
            if match := pattern.search(line):
                counts[(int(match.group(1)), int(match.group(2)))] += 1
    if not counts:
        raise ValueError("No race/gender rows were parsed from CreatureDisplayInfoExtra.sql")
    unsupported_genders = sorted(gender_id for _, gender_id in counts if gender_id not in {0, 1})
    if unsupported_genders:
        raise ValueError(f"Unsupported gender IDs in pinned source: {unsupported_genders}")
    return counts


def build() -> tuple[int, int]:
    source_hashes = _validate_pinned_sources()
    race_payload = _load_json("race-archetypes.json")
    gender_payload = _load_json("gender-presentations.json")
    preset_payload = _load_json("delivery-presets.json")
    script_payload = _load_json("evaluation-scripts.json")

    races = _unique_records(race_payload["archetypes"], "race_id", "race ID")
    genders = _unique_records(gender_payload["presentations"], "gender_id", "gender ID")
    presets = _unique_records(preset_payload["presets"], "id", "delivery preset")
    scripts = _unique_records(script_payload["scripts"], "preset_id", "evaluation preset")

    if set(genders) != {0, 1}:
        raise ValueError("Phase 2 requires male and female presentation records")
    required_legacy_races = set(range(-1, 23)) - {0}
    if not required_legacy_races.issubset(races):
        raise ValueError(
            f"Race registry must retain narrator and legacy IDs 1-22; got {sorted(races)}"
        )
    if set(presets) != set(scripts):
        raise ValueError("Every delivery preset must have exactly one evaluation script")
    if preset_payload.get("approval_order") != [
        "neutral",
        "angry",
        "sorrowful",
        "joyful",
        "proclaiming",
    ]:
        raise ValueError("Delivery approval order must begin with neutral and retain five presets")

    observed_counts = _observed_race_gender_counts()
    unsupported_races = sorted(race_id for race_id, _ in observed_counts if race_id not in races)
    if unsupported_races:
        raise ValueError(f"Pinned source contains unregistered race IDs: {unsupported_races}")

    profiles = []
    previews = []
    for race_id, race in sorted(races.items()):
        for gender_id, gender in sorted(genders.items()):
            profile_id = f"{race['slug']}-{gender['slug']}"
            observed_count = observed_counts.get((race_id, gender_id), 0)
            if race_id == -1:
                coverage_state = "required_system_profile"
            elif observed_count:
                coverage_state = "observed_in_pinned_export"
            else:
                coverage_state = "planned_fallback"
            profiles.append(
                {
                    "profile_id": profile_id,
                    "race_id": race_id,
                    "race_slug": race["slug"],
                    "race_name": race["name"],
                    "gender_id": gender_id,
                    "gender_slug": gender["slug"],
                    "gender_name": gender["name"],
                    "observed_display_count": observed_count,
                    "coverage_state": coverage_state,
                    "identity": race["identity"],
                    "accent_target": race["accent_target"],
                    "accent_or_cadence": race["accent_or_cadence"],
                    "accent_avoid": race["accent_avoid"],
                    "accent_basis": race["accent_basis"],
                    "timbre": race["timbre"],
                    "pacing": race["pacing"],
                    "gender_guidance": gender["guidance"],
                    "guardrails": f"{race['guardrail']} {gender['guardrail']}",
                    "approval_status": "draft",
                    "source_voice_id": "",
                }
            )
            for preset_id in preset_payload["approval_order"]:
                previews.append(
                    {
                        "preview_id": f"{profile_id}--{preset_id}",
                        "profile_id": profile_id,
                        "delivery_preset_id": preset_id,
                        "evaluation_script_id": scripts[preset_id]["id"],
                        "profile_approval_status": "draft",
                        "delivery_approval_status": "draft",
                        "source_voice_id": "",
                        "audio_status": "ungenerated",
                        "audio_path": "",
                        "owner_decision": "pending",
                        "notes": "",
                    }
                )

    profile_path = PROFILE_DIR / "baseline-voice-profiles.csv"
    with profile_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=PROFILE_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(profiles)

    preview_path = PROFILE_DIR / "placeholder-preview-matrix.csv"
    with preview_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=PREVIEW_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(previews)

    manifest = {
        "schema_version": 1,
        "phase": 2,
        "phase_name": "Voice Workbench",
        "scope": race_payload["scope"],
        "profile_count": len(profiles),
        "preview_count": len(previews),
        "race_count": len(races),
        "gender_presentation_count": len(genders),
        "delivery_preset_count": len(presets),
        "observed_profile_count": sum(
            profile["coverage_state"] == "observed_in_pinned_export" for profile in profiles
        ),
        "planned_fallback_count": sum(
            profile["coverage_state"] == "planned_fallback" for profile in profiles
        ),
        "system_profile_count": sum(
            profile["coverage_state"] == "required_system_profile" for profile in profiles
        ),
        "generation_state": "No preview audio has been generated and no paid API call is authorized.",
        "source_hashes": source_hashes,
        "registry_hashes": {
            name: _sha256(PROFILE_DIR / name)
            for name in [
                "race-archetypes.json",
                "gender-presentations.json",
                "delivery-presets.json",
                "evaluation-scripts.json",
            ]
        },
    }
    manifest_path = PROFILE_DIR / "phase2-manifest.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(manifest, output, indent=2)
        output.write("\n")

    return len(profiles), len(previews)


def main() -> None:
    profile_count, preview_count = build()
    print(f"Built {profile_count} baseline profiles and {preview_count} preview records")


if __name__ == "__main__":
    main()
