"""Validate every imported MP3 against the legacy sound-length lookup."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from mutagen.mp3 import MP3

DEFAULT_ROOT = Path("data/imported/mrthinger-vanilla-v1.0.0/AI_VoiceOverData_Vanilla")
DEFAULT_REPORT = Path("imports/reports/legacy-import-validation.json")
LENGTH_PATTERN = re.compile(r'\["([^"]+)"\]\s*=\s*([0-9.]+)')


def parse_length_table(path: Path) -> dict[str, float]:
    text = path.read_text(encoding="utf-8")
    return {stem: float(duration) for stem, duration in LENGTH_PATTERN.findall(text)}


def validate_import(root: Path) -> dict:
    expected = parse_length_table(root / "generated/sound_length_table.lua")
    files = sorted((root / "generated/sounds").rglob("*.mp3"))
    errors = []
    duration_deltas = []
    missing_lookup = []

    for path in files:
        try:
            actual_duration = MP3(path).info.length
        except Exception as error:  # mutagen exposes several format-specific exceptions
            errors.append({"file": path.relative_to(root).as_posix(), "error": str(error)})
            continue
        if path.stem not in expected:
            missing_lookup.append(path.relative_to(root).as_posix())
            continue
        duration_deltas.append(abs(actual_duration - expected[path.stem]))

    audio_stems = {path.stem for path in files}
    return {
        "schema_version": 1,
        "import_root": root.as_posix(),
        "mp3_files": len(files),
        "decode_error_count": len(errors),
        "decode_errors": errors,
        "missing_length_lookup_count": len(missing_lookup),
        "missing_length_lookups": missing_lookup,
        "length_without_audio_count": len(expected.keys() - audio_stems),
        "duration_comparisons": len(duration_deltas),
        "maximum_duration_delta_seconds": max(duration_deltas) if duration_deltas else None,
        "mean_duration_delta_seconds": (
            sum(duration_deltas) / len(duration_deltas) if duration_deltas else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    report = validate_import(args.root)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report["decode_error_count"] or report["missing_length_lookup_count"]:
        raise SystemExit("Imported audio validation failed")
    print(f"Validated {report['mp3_files']} imported MP3 files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
