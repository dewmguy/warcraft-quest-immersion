from __future__ import annotations

import csv
import json
from pathlib import Path

from tts_cli.paths import PROJECT_ROOT

PROFILE_DIR = PROJECT_ROOT / "voice_profiles"


class VoiceProfileError(ValueError):
    pass


def _load_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as source:
            payload = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise VoiceProfileError(f"Could not load {path.name}: {error}") from error
    if payload.get("schema_version") != 1:
        raise VoiceProfileError(f"Unsupported schema version in {path.name}")
    return payload


def _load_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames or any(field is None for field in reader.fieldnames):
                raise VoiceProfileError(f"Invalid header in {path.name}")
            rows = list(reader)
    except OSError as error:
        raise VoiceProfileError(f"Could not load {path.name}: {error}") from error
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise VoiceProfileError(f"Malformed row in {path.name}")
    return rows


def load_phase2_review(profile_dir: Path = PROFILE_DIR) -> dict:
    manifest = _load_json(profile_dir / "phase2-manifest.json")
    preset_payload = _load_json(profile_dir / "delivery-presets.json")
    script_payload = _load_json(profile_dir / "evaluation-scripts.json")
    profiles = _load_csv(profile_dir / "baseline-voice-profiles.csv")
    previews = _load_csv(profile_dir / "placeholder-preview-matrix.csv")

    preset_ids = [preset["id"] for preset in preset_payload["presets"]]
    profile_ids = {profile["profile_id"] for profile in profiles}
    if len(profile_ids) != len(profiles):
        raise VoiceProfileError("Baseline profile IDs must be unique")
    if len(set(preset_ids)) != len(preset_ids):
        raise VoiceProfileError("Delivery preset IDs must be unique")
    if len(profiles) != manifest["profile_count"] or len(previews) != manifest["preview_count"]:
        raise VoiceProfileError("Phase 2 artifact counts do not match the manifest")
    if any(preview["profile_id"] not in profile_ids for preview in previews):
        raise VoiceProfileError("Preview matrix references an unknown baseline profile")
    if any(preview["delivery_preset_id"] not in preset_ids for preview in previews):
        raise VoiceProfileError("Preview matrix references an unknown delivery preset")

    preview_states: dict[str, int] = {}
    for preview in previews:
        state = preview["audio_status"]
        preview_states[state] = preview_states.get(state, 0) + 1

    for profile in profiles:
        try:
            profile["race_id"] = int(profile["race_id"])
            profile["gender_id"] = int(profile["gender_id"])
            profile["observed_display_count"] = int(profile["observed_display_count"])
        except ValueError as error:
            raise VoiceProfileError("Profile numeric fields must contain integers") from error

    scripts_by_preset = {script["preset_id"]: script for script in script_payload["scripts"]}
    return {
        "manifest": manifest,
        "profiles": profiles,
        "presets": preset_payload["presets"],
        "approval_order": preset_payload["approval_order"],
        "scripts_by_preset": scripts_by_preset,
        "preview_states": preview_states,
    }
