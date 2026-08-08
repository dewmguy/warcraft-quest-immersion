"""Inventory a legacy VoiceOver ZIP without extracting its audio payload."""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import re
import statistics
import zipfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

DEFAULT_ARCHIVE = Path(
    "imports/source-archives/AI-VoiceOver-1.4.1-plus-VanillaData-0.1-WoW-3.3.5a-Load-Outdated.zip"
)
DEFAULT_REPORT = Path("imports/reports/legacy-audio-inventory.json")
DEFAULT_CANDIDATES = Path("pronunciation/legacy-term-candidates.csv")

NPC_NAMES_PATH = "AI_VoiceOverData_Vanilla/generated/npc_name_lookups.lua"
NPC_GOSSIP_PATH = "AI_VoiceOverData_Vanilla/generated/npc_name_gossip_file_lookups.lua"
QUEST_LOOKUPS_PATH = "AI_VoiceOverData_Vanilla/generated/quest_id_lookups.lua"
LENGTHS_PATH = "AI_VoiceOverData_Vanilla/generated/sound_length_table.lua"

QUEST_AUDIO_PATTERN = re.compile(
    r"(?:(?P<player_gender>male|female)-)?"
    r"(?P<quest_id>\d+)-(?P<source>accept|complete|progress)\.mp3$",
    re.IGNORECASE,
)
NPC_NAME_PATTERN = re.compile(r'\[\d+\]\s*=\s*"((?:\\.|[^"\\])*)"')
GOSSIP_TEXT_PATTERN = re.compile(r'\["((?:\\.|[^"\\])*)"\]\s*=\s*"[0-9a-f]{32}"', re.IGNORECASE)
LUA_KEY_PATTERN = re.compile(r'\["((?:\\.|[^"\\])*)"\]\s*=')
LENGTH_PATTERN = re.compile(r'\["([^"]+)"\]\s*=\s*([0-9.]+)')
TERM_PATTERN = re.compile(r"\b[A-Z][A-Za-z]*(?:['’][A-Za-z]+)?\b")
WOW_MARKUP_PATTERN = re.compile(r"\$[A-Za-z]")

TERM_STOPWORDS = {
    "A",
    "All",
    "And",
    "Are",
    "As",
    "At",
    "Be",
    "But",
    "By",
    "Do",
    "For",
    "From",
    "Good",
    "Have",
    "He",
    "Here",
    "How",
    "I",
    "I'd",
    "I'll",
    "I'm",
    "I've",
    "If",
    "In",
    "It",
    "It's",
    "Look",
    "May",
    "More",
    "My",
    "No",
    "Not",
    "Now",
    "Of",
    "On",
    "Or",
    "Our",
    "Return",
    "She",
    "She's",
    "So",
    "Take",
    "That",
    "The",
    "Then",
    "There",
    "There's",
    "These",
    "They",
    "This",
    "Those",
    "Through",
    "To",
    "Very",
    "We",
    "We'll",
    "We're",
    "We've",
    "Welcome",
    "Well",
    "What",
    "When",
    "Where",
    "Which",
    "While",
    "Who",
    "Why",
    "With",
    "You",
    "You'd",
    "You'll",
    "You're",
    "You've",
    "Your",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_unsafe_archive_path(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return path.is_absolute() or ".." in path.parts or bool(re.match(r"^[A-Za-z]:", normalized))


def decode_lua_string(value: str) -> str:
    try:
        return ast.literal_eval(f'"{value}"')
    except (SyntaxError, ValueError):
        return value


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int(fraction * (len(ordered) - 1))]


def extract_terms(sources: dict[str, list[str]]) -> list[dict[str, object]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, str] = {}
    for source_name, values in sources.items():
        for value in values:
            value = WOW_MARKUP_PATTERN.sub(" ", value)
            for term in TERM_PATTERN.findall(value):
                if term.endswith(("'s", "’s")):
                    term = term[:-2]
                if term in TERM_STOPWORDS or len(term) < 3:
                    continue
                counts[term][source_name] += 1
                examples.setdefault(term, value)

    rows = []
    for term, source_counts in counts.items():
        rows.append(
            {
                "term": term,
                "occurrences": sum(source_counts.values()),
                "npc_name_occurrences": source_counts["npc_name"],
                "gossip_occurrences": source_counts["gossip"],
                "quest_lookup_occurrences": source_counts["quest_lookup"],
                "example": examples[term],
                "proposed_ipa": "",
                "proposed_alias": "",
                "review_status": "unreviewed",
                "notes": "",
            }
        )
    return sorted(rows, key=lambda row: (-int(row["occurrences"]), str(row["term"])))


def analyze_archive(archive: Path, verify_crc: bool = False) -> tuple[dict, list[dict]]:
    with zipfile.ZipFile(archive) as package:
        infos = package.infolist()
        files = [info for info in infos if not info.is_dir()]
        unsafe = [info.filename for info in files if is_unsafe_archive_path(info.filename)]
        if unsafe:
            raise ValueError(f"Unsafe archive paths detected: {unsafe[:5]}")

        bad_crc_file = package.testzip() if verify_crc else None
        if bad_crc_file:
            raise ValueError(f"CRC verification failed for {bad_crc_file}")

        audio = [info for info in files if info.filename.lower().endswith(".mp3")]
        quest_audio = [info for info in audio if "/generated/sounds/quests/" in info.filename]
        gossip_audio = [info for info in audio if "/generated/sounds/gossip/" in info.filename]

        quest_matches = [
            QUEST_AUDIO_PATTERN.search(Path(info.filename).name) for info in quest_audio
        ]
        unmatched_quests = [
            info.filename
            for info, match in zip(quest_audio, quest_matches, strict=True)
            if not match
        ]
        quest_sources = Counter(
            match.group("source").lower() for match in quest_matches if match is not None
        )
        quest_ids = {int(match.group("quest_id")) for match in quest_matches if match is not None}

        npc_names_text = package.read(NPC_NAMES_PATH).decode("utf-8")
        gossip_text = package.read(NPC_GOSSIP_PATH).decode("utf-8")
        quest_lookup_text = package.read(QUEST_LOOKUPS_PATH).decode("utf-8")
        lengths_text = package.read(LENGTHS_PATH).decode("utf-8")

        npc_names = [decode_lua_string(value) for value in NPC_NAME_PATTERN.findall(npc_names_text)]
        gossip_lines = [
            decode_lua_string(value) for value in GOSSIP_TEXT_PATTERN.findall(gossip_text)
        ]
        quest_lookup_keys = [
            decode_lua_string(value) for value in LUA_KEY_PATTERN.findall(quest_lookup_text)
        ]
        duration_by_stem = {
            stem: float(duration) for stem, duration in LENGTH_PATTERN.findall(lengths_text)
        }
        audio_stems = {Path(info.filename).stem for info in audio}
        durations = [duration_by_stem[stem] for stem in audio_stems if stem in duration_by_stem]

        top_level = Counter(info.filename.replace("\\", "/").split("/")[0] for info in files)
        extension_counts = Counter(Path(info.filename).suffix.lower() for info in files)

    report = {
        "schema_version": 1,
        "archive": {
            "file": archive.name,
            "size_bytes": archive.stat().st_size,
            "sha256": sha256_file(archive),
            "entries": len(infos),
            "files": len(files),
            "uncompressed_bytes": sum(info.file_size for info in files),
            "unsafe_path_count": 0,
            "crc_verified": verify_crc,
            "top_level_file_counts": dict(sorted(top_level.items())),
            "extension_counts": dict(sorted(extension_counts.items())),
        },
        "audio": {
            "mp3_files": len(audio),
            "quest_files": len(quest_audio),
            "gossip_files": len(gossip_audio),
            "other_files": len(audio) - len(quest_audio) - len(gossip_audio),
            "unique_quest_ids": len(quest_ids),
            "quest_source_counts": dict(sorted(quest_sources.items())),
            "unmatched_quest_filenames": unmatched_quests,
            "duration_entries": len(duration_by_stem),
            "audio_missing_duration": len(audio_stems - duration_by_stem.keys()),
            "duration_without_audio": len(duration_by_stem.keys() - audio_stems),
            "total_duration_hours": round(sum(durations) / 3600, 6),
            "minimum_duration_seconds": min(durations) if durations else None,
            "median_duration_seconds": statistics.median(durations) if durations else None,
            "p95_duration_seconds": percentile(durations, 0.95),
            "maximum_duration_seconds": max(durations) if durations else None,
        },
        "lookups": {
            "npc_name_records": len(npc_names),
            "gossip_text_mappings": len(gossip_lines),
            "quest_lookup_key_occurrences": len(quest_lookup_keys),
        },
    }
    terms = extract_terms(
        {
            "npc_name": npc_names,
            "gossip": gossip_lines,
            "quest_lookup": quest_lookup_keys,
        }
    )
    return report, terms


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_candidates(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]) if rows else ["term"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--verify-crc", action="store_true")
    args = parser.parse_args()

    report, terms = analyze_archive(args.archive, verify_crc=args.verify_crc)
    write_report(args.report, report)
    write_candidates(args.candidates, terms)
    print(f"Wrote {args.report} and {args.candidates}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
