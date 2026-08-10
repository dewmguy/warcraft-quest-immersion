from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile

from tts_cli.consts import GENDER_DICT, RACE_DICT
from tts_cli.data_sources import REQUIRED_COLUMNS, VALID_SOURCES, load_dialogue_csv
from tts_cli.voice_profiles import load_phase2_review

DELIVERIES = ("neutral", "angry", "sorrowful", "joyful", "proclaiming")
DELIVERY_DEFAULTS = {
    "neutral": {"prompt_tag": "", "stability": 0.5},
    "angry": {"prompt_tag": "angry", "stability": 0.5},
    "sorrowful": {"prompt_tag": "sad", "stability": 0.5},
    "joyful": {"prompt_tag": "cheerfully", "stability": 0.5},
    "proclaiming": {"prompt_tag": "projecting", "stability": 0.5},
}
ROLE_OPTIONS = (
    "default",
    "peasant",
    "artisan",
    "merchant",
    "innkeeper",
    "scholar",
    "spiritual_leader",
    "soldier",
    "officer",
    "highborn",
    "royalty",
    "outlaw",
    "other",
)
AFFILIATION_OPTIONS = (
    "unspecified",
    "alliance",
    "horde",
    "neutral",
    "kirin_tor",
    "argent_crusade",
    "cenarion_circle",
    "steamwheedle_cartel",
    "scarlet_crusade",
    "scourge",
    "burning_legion",
    "other",
)
IMPORTANCE_SCORES = {
    "pivotal": 100,
    "global": 85,
    "inter_zone": 70,
    "zone": 55,
    "subzone": 40,
    "stepping_stone": 25,
    "one_off": 10,
}
VOICE_METHODS = (
    "unselected",
    "library",
    "designed",
    "reference_design",
    "instant_clone",
    "external",
)
VOICE_CANDIDATE_METHOD_ORDER = (
    "designed",
    "reference_design",
    "instant_clone",
    "library",
    "external",
    "legacy_unknown",
)
DELIVERY_STATUSES = ("not_tested", "previewed", "approved")
PERFORMANCE_METHODS = {
    0.0: ("creative", "Creative"),
    0.5: ("natural", "Natural"),
    1.0: ("robust", "Robust"),
}
PRODUCTION_STATES = (
    "needs_text",
    "needs_voice",
    "ready_to_generate",
    "generation_failed",
    "audio_to_review",
    "approved",
)
MAX_REFERENCE_BYTES = 50 * 1024 * 1024
MAX_DISPLAY_NAME_LENGTH = 80
BASELINE_CONTEXT_REVISION = 2


class AlphaError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _with_voice_lifecycle(voice: dict[str, Any]) -> dict[str, Any]:
    """Derive workflow state from provider, preset, and production readiness."""
    approved = int(voice.get("approved_delivery_count") or 0)
    dialogue_count = int(voice.get("dialogue_count") or 0)
    missing = int(voice.get("missing_dialogue_count") or 0)
    deployed = max(dialogue_count - missing, 0)
    stored_status = str(voice.get("stored_status") or "draft")
    voice_id_candidate_count = int(voice.get("voice_id_candidate_count") or 0)
    if not voice_id_candidate_count and voice.get("voice_id_candidates"):
        voice_id_candidate_count = len(voice["voice_id_candidates"])

    if voice.get("scope") == "unique" and stored_status == "retired":
        status = "dormant"
        reason = (
            "Dormant because the NPC currently uses its race / gender baseline; "
            "all unique-profile work remains stored."
        )
    elif not voice_id_candidate_count or approved < len(DELIVERIES):
        requirements = []
        if not voice_id_candidate_count:
            requirements.append("generate a reusable ElevenLabs voice ID")
        if approved < len(DELIVERIES):
            requirements.append(
                f"approve {len(DELIVERIES) - approved} remaining emotional delivery "
                f"preset{'s' if len(DELIVERIES) - approved != 1 else ''}"
            )
        status = "draft"
        reason = "Draft until you " + " and ".join(requirements) + "."
    elif deployed == 0:
        status = "candidate"
        reason = (
            "The reusable voice and all five emotional delivery presets are approved, "
            "but no matching dialogue audio has been approved for production yet."
        )
    elif missing == 0:
        status = "completed"
        reason = (
            f"All {dialogue_count} matching dialogue record"
            f"{'s have' if dialogue_count != 1 else ' has'} approved production audio."
        )
    else:
        status = "active"
        reason = (
            f"{deployed} of {dialogue_count} matching dialogue records have approved "
            f"production audio; {missing} remain."
        )

    voice["status"] = status
    voice["lifecycle_reason"] = reason
    voice["deployed_dialogue_count"] = deployed
    return voice


def _normalize_voice_actor_notes(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).replace("[", "").replace("]", "")).strip()


def _normalize_display_name(value: Any) -> str:
    display_name = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        raise AlphaError(f"Names cannot exceed {MAX_DISPLAY_NAME_LENGTH} characters.")
    return display_name


def _delivery_request_text(notes: Any, spoken_text: str) -> str:
    direction = _normalize_voice_actor_notes(notes)
    text = spoken_text.strip()
    return f"[{direction}] {text}" if direction else text


def _candidate_groups(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(str(candidate.get("creation_method") or "legacy_unknown"), []).append(
            candidate
        )
    order = {method: index for index, method in enumerate(VOICE_CANDIDATE_METHOD_ORDER)}
    return [
        {"creation_method": method, "candidates": grouped[method]}
        for method in sorted(grouped, key=lambda method: (order.get(method, 999), method))
    ]


def _performance_method(value: Any) -> tuple[str, str, float]:
    stability = float(value)
    key, label = PERFORMANCE_METHODS.get(stability, ("custom", f"Custom ({stability:g})"))
    return key, label, stability


def _delivery_preview_metadata(request: dict[str, Any], sample_text: str) -> dict[str, str]:
    actor_notes = str(request.get("actor_notes") or "").strip()
    if "actor_notes" not in request:
        request_text = str(request.get("text") or "")
        if sample_text and request_text.endswith(sample_text):
            prefix = request_text[: -len(sample_text)].strip()
            match = re.fullmatch(r"\[(.*)]", prefix)
            actor_notes = match.group(1).strip() if match else ""

    method = str(request.get("performance_method") or "").strip()
    method_label = str(request.get("performance_method_label") or "").strip()
    if not method or not method_label:
        settings = request.get("voice_settings")
        stability = settings.get("stability", 0.5) if isinstance(settings, dict) else 0.5
        inferred_method, inferred_label, _ = _performance_method(stability)
        method = method or inferred_method
        method_label = method_label or inferred_label

    baseline_voice_id = str(
        request.get("baseline_voice_id")
        or request.get("provider_voice_id")
        or request.get("voice_id")
        or ""
    ).strip()
    return {
        "actor_notes": actor_notes,
        "performance_method": method,
        "performance_method_label": method_label,
        "baseline_voice_id": baseline_voice_id,
    }


def _baseline_description(profile: dict[str, Any]) -> str:
    """Build the provider prompt in the order recommended for Voice Design v3."""
    return " ".join(
        part.strip()
        for part in (
            profile["accent_target"],
            f"Adult {profile['gender_name'].lower()} voice. Perfect audio quality.",
            f"Persona: {profile['identity']}",
            f"Delivery: {profile['accent_or_cadence']}",
            profile["timbre"],
            profile["pacing"],
            profile["gender_guidance"],
            profile["guardrails"],
            f"Accent exclusions: {profile['accent_avoid']}",
        )
        if part.strip()
    )


def _audio_duration(path: Path) -> float | None:
    try:
        audio = MutagenFile(path)
        return round(float(audio.info.length), 3) if audio and audio.info else None
    except Exception:
        return None


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned[:120] or "audio.mp3"


def _first_metadata(metadata: dict[str, Any], *keys: str) -> str:
    lowered = {str(key).lower(): value for key, value in metadata.items()}
    for key in keys:
        value = str(lowered.get(key.lower(), "")).strip()
        if value and value.lower() not in {"none", "nan", "0"}:
            return value
    return ""


def _infer_role(name: str, metadata: dict[str, Any], entity_type: str) -> str:
    haystack = " ".join(
        [name, _first_metadata(metadata, "role", "occupation", "rank", "subname")]
    ).lower()
    groups = (
        ("royalty", ("king", "queen", "prince", "princess", "emperor", "empress", "warchief")),
        (
            "officer",
            ("captain", "commander", "general", "marshal", "sergeant", "lieutenant", "overlord"),
        ),
        ("soldier", ("guard", "sentinel", "soldier", "trooper", "grunt", "watcher", "defender")),
        ("highborn", ("lord", "lady", "baron", "duke", "noble", "highborn")),
        ("merchant", ("merchant", "vendor", "trader", "supplier", "dealer")),
        ("innkeeper", ("innkeeper", "barkeep", "bartender")),
        (
            "artisan",
            (
                "smith",
                "blacksmith",
                "alchemist",
                "engineer",
                "tailor",
                "leatherworker",
                "carpenter",
            ),
        ),
        ("scholar", ("scholar", "historian", "archivist", "librarian", "researcher", "mage")),
        ("spiritual_leader", ("priest", "shaman", "druid", "oracle", "seer", "bishop")),
        ("outlaw", ("bandit", "pirate", "thief", "assassin", "smuggler")),
        ("peasant", ("peasant", "farmer", "laborer", "worker", "villager")),
    )
    for role, terms in groups:
        if any(term in haystack for term in terms):
            return role
    return "other" if entity_type != "creature" else "default"


def _infer_affiliation(metadata: dict[str, Any]) -> str:
    value = _first_metadata(
        metadata,
        "affiliation",
        "faction_name",
        "faction",
        "organization",
        "team",
    )
    if not value:
        return "unspecified"
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    aliases = {
        "argent_dawn": "argent_crusade",
        "argent_crusade": "argent_crusade",
        "kirin_tor": "kirin_tor",
        "cenarion_circle": "cenarion_circle",
        "steamwheedle_cartel": "steamwheedle_cartel",
        "scarlet_crusade": "scarlet_crusade",
        "burning_legion": "burning_legion",
    }
    if normalized in AFFILIATION_OPTIONS:
        return normalized
    return aliases.get(normalized, "other")


class AlphaStore:
    def __init__(self, database_path: Path, storage_root: Path) -> None:
        self.database_path = database_path.resolve()
        self.storage_root = storage_root.resolve()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS source_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    source_hash TEXT NOT NULL UNIQUE,
                    expansion TEXT NOT NULL,
                    locale TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    imported_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS voices (
                    voice_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('baseline', 'unique')),
                    profile_id TEXT,
                    race_id INTEGER NOT NULL,
                    gender_id INTEGER NOT NULL,
                    parent_voice_id TEXT REFERENCES voices(voice_id),
                    npc_speaker_id TEXT,
                    candidate_sequence INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voice_versions (
                    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    voice_id TEXT NOT NULL REFERENCES voices(voice_id) ON DELETE CASCADE,
                    version_number INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    creation_method TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'elevenlabs',
                    provider_voice_id TEXT,
                    model_id TEXT NOT NULL DEFAULT 'eleven_v3',
                    settings_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(voice_id, version_number)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_current_voice_version
                    ON voice_versions(voice_id) WHERE is_current = 1;
                CREATE TABLE IF NOT EXISTS voice_delivery_presets (
                    voice_id TEXT NOT NULL REFERENCES voices(voice_id) ON DELETE CASCADE,
                    delivery TEXT NOT NULL,
                    provider_voice_id TEXT NOT NULL DEFAULT '',
                    sample_sequence INTEGER NOT NULL DEFAULT 0,
                    prompt_tag TEXT NOT NULL DEFAULT '',
                    stability REAL NOT NULL DEFAULT 0.5,
                    status TEXT NOT NULL DEFAULT 'not_tested',
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(voice_id, delivery)
                );
                CREATE TABLE IF NOT EXISTS app_settings (
                    setting_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS speakers (
                    speaker_id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    race_id INTEGER NOT NULL,
                    gender_id INTEGER NOT NULL,
                    race_name TEXT NOT NULL,
                    gender_name TEXT NOT NULL,
                    voice_id TEXT REFERENCES voices(voice_id),
                    role TEXT NOT NULL DEFAULT '',
                    faction TEXT NOT NULL DEFAULT '',
                    zone TEXT NOT NULL DEFAULT '',
                    context_summary TEXT NOT NULL DEFAULT '',
                    importance TEXT NOT NULL DEFAULT 'unassessed',
                    uniqueness TEXT NOT NULL DEFAULT 'unassessed',
                    source_snapshot_id TEXT REFERENCES source_snapshots(snapshot_id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, entity_id)
                );
                CREATE TABLE IF NOT EXISTS dialogue_entries (
                    dialogue_id TEXT PRIMARY KEY,
                    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
                    expansion TEXT NOT NULL,
                    locale TEXT NOT NULL,
                    source_record_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    quest_id INTEGER,
                    quest_title TEXT NOT NULL,
                    speaker_id TEXT NOT NULL REFERENCES speakers(speaker_id),
                    original_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    addon_file_key TEXT NOT NULL,
                    delivery TEXT NOT NULL DEFAULT 'neutral',
                    imported_audio_status TEXT NOT NULL DEFAULT 'unknown',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS dialogue_speaker_idx ON dialogue_entries(speaker_id);
                CREATE INDEX IF NOT EXISTS dialogue_quest_idx ON dialogue_entries(quest_id);
                CREATE INDEX IF NOT EXISTS dialogue_source_idx ON dialogue_entries(source);
                CREATE TABLE IF NOT EXISTS spoken_text_revisions (
                    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dialogue_id TEXT NOT NULL REFERENCES dialogue_entries(dialogue_id) ON DELETE CASCADE,
                    revision_number INTEGER NOT NULL,
                    spoken_text TEXT NOT NULL,
                    processor TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    warnings_json TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(dialogue_id, revision_number)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_current_spoken_revision
                    ON spoken_text_revisions(dialogue_id) WHERE is_current = 1;
                CREATE TABLE IF NOT EXISTS reference_clips (
                    clip_id TEXT PRIMARY KEY,
                    voice_id TEXT NOT NULL REFERENCES voices(voice_id) ON DELETE CASCADE,
                    original_name TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    duration_seconds REAL,
                    provenance TEXT NOT NULL,
                    provider_eligible INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voice_previews (
                    preview_id TEXT PRIMARY KEY,
                    voice_id TEXT NOT NULL REFERENCES voices(voice_id) ON DELETE CASCADE,
                    generated_voice_id TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    duration_seconds REAL,
                    prompt TEXT NOT NULL,
                    preview_text TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    creation_method TEXT NOT NULL DEFAULT 'legacy_unknown',
                    generation_number INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voice_id_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    voice_id TEXT NOT NULL REFERENCES voices(voice_id) ON DELETE CASCADE,
                    provider_voice_id TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    generation_number INTEGER NOT NULL,
                    creation_method TEXT NOT NULL,
                    creation_model_id TEXT NOT NULL,
                    sample_storage_path TEXT,
                    sample_sha256 TEXT NOT NULL DEFAULT '',
                    sample_duration_seconds REAL,
                    sample_text TEXT NOT NULL DEFAULT '',
                    sample_model_id TEXT NOT NULL DEFAULT '',
                    provider_request_id TEXT,
                    subscription_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(voice_id, generation_number)
                );
                CREATE TABLE IF NOT EXISTS voice_delivery_previews (
                    preview_id TEXT PRIMARY KEY,
                    voice_id TEXT NOT NULL REFERENCES voices(voice_id) ON DELETE CASCADE,
                    delivery TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    generation_number INTEGER NOT NULL DEFAULT 0,
                    storage_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    sample_text TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    provider_request_id TEXT,
                    subscription_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'candidate',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS generations (
                    generation_id TEXT PRIMARY KEY,
                    dialogue_id TEXT NOT NULL REFERENCES dialogue_entries(dialogue_id),
                    revision_id INTEGER NOT NULL REFERENCES spoken_text_revisions(revision_id),
                    voice_id TEXT NOT NULL REFERENCES voices(voice_id),
                    voice_version_id INTEGER NOT NULL REFERENCES voice_versions(version_id),
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    delivery TEXT NOT NULL,
                    request_text TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    provider_request_id TEXT,
                    character_count INTEGER NOT NULL,
                    subscription_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS generations_dialogue_idx ON generations(dialogue_id);
                CREATE TABLE IF NOT EXISTS provider_usage_events (
                    event_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    action TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    input_character_count INTEGER NOT NULL DEFAULT 0,
                    character_cost INTEGER,
                    provider_request_id TEXT,
                    subscription_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audio_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    dialogue_id TEXT NOT NULL REFERENCES dialogue_entries(dialogue_id),
                    storage_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    mime_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS production_assets (
                    dialogue_id TEXT PRIMARY KEY REFERENCES dialogue_entries(dialogue_id),
                    candidate_id TEXT NOT NULL REFERENCES audio_candidates(candidate_id),
                    addon_filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    approved_at TEXT NOT NULL
                );
                """
            )
            self._ensure_columns(connection)
            self._seed_app_settings(connection)
            self._seed_baseline_voices(connection)
            self._normalize_delivery_prompt_tags(connection)
            self._normalize_instant_clone_previews(connection)
            self._sync_baseline_contexts(connection)
            self._synchronize_candidate_sequences(connection)
            self._backfill_voice_id_candidates(connection)
            self._synchronize_candidate_sequences(connection)
            self._synchronize_delivery_sample_sequences(connection)
            self._pin_delivery_presets_to_current_voice(connection)

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection) -> None:
        """Apply additive migrations to Alpha databases created by earlier builds."""
        voice_columns = {row["name"] for row in connection.execute("PRAGMA table_info(voices)")}
        if "candidate_sequence" not in voice_columns:
            connection.execute(
                "ALTER TABLE voices ADD COLUMN candidate_sequence INTEGER NOT NULL DEFAULT 0"
            )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(dialogue_entries)")}
        if "source_record_id" not in columns:
            connection.execute(
                "ALTER TABLE dialogue_entries ADD COLUMN source_record_id TEXT NOT NULL DEFAULT ''"
            )
        if "metadata_json" not in columns:
            connection.execute(
                "ALTER TABLE dialogue_entries ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        preview_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(voice_previews)")
        }
        if "creation_method" not in preview_columns:
            connection.execute(
                "ALTER TABLE voice_previews ADD COLUMN creation_method TEXT NOT NULL "
                "DEFAULT 'legacy_unknown'"
            )
        if "generation_number" not in preview_columns:
            connection.execute(
                "ALTER TABLE voice_previews ADD COLUMN generation_number INTEGER NOT NULL DEFAULT 0"
            )
        preset_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(voice_delivery_presets)")
        }
        if "provider_voice_id" not in preset_columns:
            connection.execute(
                "ALTER TABLE voice_delivery_presets ADD COLUMN provider_voice_id "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "sample_sequence" not in preset_columns:
            connection.execute(
                "ALTER TABLE voice_delivery_presets ADD COLUMN sample_sequence "
                "INTEGER NOT NULL DEFAULT 0"
            )
        delivery_preview_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(voice_delivery_previews)")
        }
        voice_id_candidate_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(voice_id_candidates)")
        }
        if "display_name" not in voice_id_candidate_columns:
            connection.execute(
                "ALTER TABLE voice_id_candidates ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
            )
        if "display_name" not in delivery_preview_columns:
            connection.execute(
                "ALTER TABLE voice_delivery_previews ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
            )
        if "generation_number" not in delivery_preview_columns:
            connection.execute(
                "ALTER TABLE voice_delivery_previews ADD COLUMN generation_number "
                "INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _normalize_instant_clone_previews(connection: sqlite3.Connection) -> None:
        """Repair older clone records that still mark a Voice Design preview as selected."""
        connection.execute(
            "UPDATE voice_previews SET status='superseded' WHERE status='selected' "
            "AND EXISTS (SELECT 1 FROM voice_versions vv "
            "WHERE vv.voice_id=voice_previews.voice_id AND vv.is_current=1 "
            "AND vv.creation_method='instant_clone' "
            "AND COALESCE(vv.provider_voice_id, '')<>'')"
        )

    @staticmethod
    def _reserve_candidate_numbers(
        connection: sqlite3.Connection, voice_id: str, count: int = 1
    ) -> list[int]:
        if count < 1:
            raise AlphaError("At least one candidate number must be reserved.")
        row = connection.execute(
            "SELECT candidate_sequence FROM voices WHERE voice_id=?", (voice_id,)
        ).fetchone()
        if not row:
            raise AlphaError("Voice was not found.")
        first = int(row["candidate_sequence"] or 0) + 1
        last = first + count - 1
        connection.execute(
            "UPDATE voices SET candidate_sequence=? WHERE voice_id=?", (last, voice_id)
        )
        return list(range(first, last + 1))

    @classmethod
    def _synchronize_candidate_sequences(cls, connection: sqlite3.Connection) -> None:
        """Backfill stable production numbers and retain the highest number ever issued."""
        voice_rows = connection.execute(
            "SELECT voice_id, candidate_sequence FROM voices"
        ).fetchall()
        for voice in voice_rows:
            highest_candidate = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation_number), 0) FROM voice_id_candidates "
                    "WHERE voice_id=?",
                    (voice["voice_id"],),
                ).fetchone()[0]
            )
            current = max(int(voice["candidate_sequence"] or 0), highest_candidate)
            unnumbered = connection.execute(
                "SELECT preview_id FROM voice_previews WHERE voice_id=? "
                "AND generation_number=0 ORDER BY created_at, preview_id",
                (voice["voice_id"],),
            ).fetchall()
            for preview in unnumbered:
                current += 1
                connection.execute(
                    "UPDATE voice_previews SET generation_number=? WHERE preview_id=?",
                    (current, preview["preview_id"]),
                )
            highest_preview = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation_number), 0) FROM voice_previews "
                    "WHERE voice_id=?",
                    (voice["voice_id"],),
                ).fetchone()[0]
            )
            current = max(current, highest_preview)
            connection.execute(
                "UPDATE voices SET candidate_sequence=? WHERE voice_id=?",
                (current, voice["voice_id"]),
            )

    @staticmethod
    def _reserve_delivery_sample_number(
        connection: sqlite3.Connection, voice_id: str, delivery: str
    ) -> int:
        row = connection.execute(
            "SELECT sample_sequence FROM voice_delivery_presets WHERE voice_id=? AND delivery=?",
            (voice_id, delivery),
        ).fetchone()
        if not row:
            raise AlphaError("Delivery preset was not found.")
        generation_number = int(row["sample_sequence"] or 0) + 1
        connection.execute(
            "UPDATE voice_delivery_presets SET sample_sequence=? WHERE voice_id=? AND delivery=?",
            (generation_number, voice_id, delivery),
        )
        return generation_number

    @classmethod
    def _synchronize_delivery_sample_sequences(cls, connection: sqlite3.Connection) -> None:
        """Backfill stable per-preset sample numbers without reusing deleted numbers."""
        presets = connection.execute(
            "SELECT voice_id, delivery, sample_sequence FROM voice_delivery_presets"
        ).fetchall()
        for preset in presets:
            current = max(
                int(preset["sample_sequence"] or 0),
                int(
                    connection.execute(
                        "SELECT COALESCE(MAX(generation_number), 0) "
                        "FROM voice_delivery_previews WHERE voice_id=? AND delivery=?",
                        (preset["voice_id"], preset["delivery"]),
                    ).fetchone()[0]
                ),
            )
            unnumbered = connection.execute(
                "SELECT preview_id FROM voice_delivery_previews "
                "WHERE voice_id=? AND delivery=? AND generation_number=0 "
                "ORDER BY created_at, preview_id",
                (preset["voice_id"], preset["delivery"]),
            ).fetchall()
            for preview in unnumbered:
                current += 1
                connection.execute(
                    "UPDATE voice_delivery_previews SET generation_number=? WHERE preview_id=?",
                    (current, preview["preview_id"]),
                )
            connection.execute(
                "UPDATE voice_delivery_presets SET sample_sequence=? "
                "WHERE voice_id=? AND delivery=?",
                (current, preset["voice_id"], preset["delivery"]),
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS delivery_sample_generation_number "
            "ON voice_delivery_previews(voice_id, delivery, generation_number)"
        )

    def _backfill_voice_id_candidates(self, connection: sqlite3.Connection) -> None:
        """Register current provider IDs created before the candidate registry existed."""
        rows = connection.execute(
            "SELECT vv.voice_id, vv.provider_voice_id, vv.creation_method, vv.model_id, "
            "vv.created_at FROM voice_versions vv WHERE vv.is_current=1 "
            "AND COALESCE(vv.provider_voice_id, '')<>'' "
            "AND NOT EXISTS (SELECT 1 FROM voice_id_candidates vic "
            "WHERE vic.provider_voice_id=vv.provider_voice_id) ORDER BY vv.created_at"
        ).fetchall()
        for row in rows:
            candidate_id = uuid.uuid4().hex
            creation_model_id = (
                "instant_voice_clone"
                if row["creation_method"] == "instant_clone"
                else row["model_id"]
            )
            sample_path = None
            sample_sha256 = ""
            sample_duration = None
            sample_text = ""
            sample_model_id = ""
            preview = connection.execute(
                "SELECT * FROM voice_previews WHERE voice_id=? AND status='selected' "
                "ORDER BY created_at DESC LIMIT 1",
                (row["voice_id"],),
            ).fetchone()
            generation_number = (
                int(preview["generation_number"])
                if preview and int(preview["generation_number"] or 0) > 0
                else self._reserve_candidate_numbers(connection, row["voice_id"])[0]
            )
            if preview:
                source_path = Path(preview["storage_path"]).resolve()
                if self.storage_root in source_path.parents and source_path.is_file():
                    content = source_path.read_bytes()
                    folder = self.storage_root / "voice-id-candidates" / row["voice_id"]
                    folder.mkdir(parents=True, exist_ok=True)
                    destination = folder / f"{candidate_id}.mp3"
                    destination.write_bytes(content)
                    sample_path = str(destination)
                    sample_sha256 = sha256_bytes(content)
                    sample_duration = preview["duration_seconds"]
                    sample_text = preview["preview_text"]
                    sample_model_id = preview["model_id"]
            connection.execute(
                "INSERT INTO voice_id_candidates(candidate_id, voice_id, provider_voice_id, "
                "generation_number, creation_method, creation_model_id, sample_storage_path, "
                "sample_sha256, sample_duration_seconds, sample_text, sample_model_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    row["voice_id"],
                    row["provider_voice_id"],
                    generation_number,
                    row["creation_method"],
                    creation_model_id,
                    sample_path,
                    sample_sha256,
                    sample_duration,
                    sample_text,
                    sample_model_id,
                    row["created_at"],
                ),
            )

    @staticmethod
    def _pin_delivery_presets_to_current_voice(connection: sqlite3.Connection) -> None:
        """Make each preset's reusable voice choice explicit before the pool grows."""
        connection.execute(
            "UPDATE voice_delivery_presets SET provider_voice_id=("
            "SELECT vv.provider_voice_id FROM voice_versions vv "
            "WHERE vv.voice_id=voice_delivery_presets.voice_id AND vv.is_current=1) "
            "WHERE COALESCE(provider_voice_id, '')='' AND EXISTS ("
            "SELECT 1 FROM voice_versions vv JOIN voice_id_candidates vic "
            "ON vic.voice_id=vv.voice_id AND vic.provider_voice_id=vv.provider_voice_id "
            "WHERE vv.voice_id=voice_delivery_presets.voice_id AND vv.is_current=1 "
            "AND COALESCE(vv.provider_voice_id, '')<>'')"
        )

    def _seed_baseline_voices(self, connection: sqlite3.Connection) -> None:
        review = load_phase2_review()
        now = utc_now()
        default_settings = {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0,
            "use_speaker_boost": True,
            "speed": 1,
        }
        for profile in review["profiles"]:
            voice_id = f"baseline--{profile['profile_id']}"
            description = _baseline_description(profile)
            connection.execute(
                "INSERT OR IGNORE INTO voices(voice_id, name, scope, profile_id, race_id, "
                "gender_id, created_at, updated_at) VALUES (?, ?, 'baseline', ?, ?, ?, ?, ?)",
                (
                    voice_id,
                    f"{profile['race_name']} · {profile['gender_name']}",
                    profile["profile_id"],
                    profile["race_id"],
                    profile["gender_id"],
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO voice_versions(voice_id, version_number, description, "
                "creation_method, settings_json, status, created_at) VALUES (?, 1, ?, "
                "'unselected', ?, 'draft', ?)",
                (voice_id, description, _json(default_settings), now),
            )
            self._seed_delivery_presets(connection, voice_id)

    def _sync_baseline_contexts(self, connection: sqlite3.Connection) -> None:
        """Version baseline prompts once when the reviewed source context changes.

        This Alpha migration intentionally preserves the creation method, provider
        voice, lifecycle state, model, settings, reference clips, and previews.
        """
        row = connection.execute(
            "SELECT value_json FROM app_settings WHERE setting_key='baseline_context_revision'"
        ).fetchone()
        applied_revision = int(_loads(row["value_json"], 0)) if row else 0
        if applied_revision >= BASELINE_CONTEXT_REVISION:
            return

        now = utc_now()
        for profile in load_phase2_review()["profiles"]:
            voice_id = f"baseline--{profile['profile_id']}"
            current = connection.execute(
                "SELECT * FROM voice_versions WHERE voice_id=? AND is_current=1", (voice_id,)
            ).fetchone()
            if not current:
                continue
            description = _baseline_description(profile)
            if current["description"] == description:
                continue
            next_version = connection.execute(
                "SELECT COALESCE(MAX(version_number), 0)+1 FROM voice_versions WHERE voice_id=?",
                (voice_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE voice_versions SET is_current=0 WHERE voice_id=?", (voice_id,)
            )
            connection.execute(
                "INSERT INTO voice_versions(voice_id, version_number, description, "
                "creation_method, provider, provider_voice_id, model_id, settings_json, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    voice_id,
                    next_version,
                    description,
                    current["creation_method"],
                    current["provider"],
                    current["provider_voice_id"],
                    current["model_id"],
                    current["settings_json"],
                    current["status"],
                    now,
                ),
            )
            connection.execute("UPDATE voices SET updated_at=? WHERE voice_id=?", (now, voice_id))

        connection.execute(
            "INSERT INTO app_settings(setting_key, value_json, updated_at) VALUES "
            "('baseline_context_revision', ?, ?) ON CONFLICT(setting_key) DO UPDATE SET "
            "value_json=excluded.value_json, updated_at=excluded.updated_at",
            (_json(BASELINE_CONTEXT_REVISION), now),
        )

    @staticmethod
    def _seed_app_settings(connection: sqlite3.Connection) -> None:
        now = utc_now()
        defaults = {
            "tts_model_id": "eleven_v3",
            "voice_design_model_id": "eleven_ttv_v3",
            "output_format": "mp3_44100_128",
        }
        for key, value in defaults.items():
            connection.execute(
                "INSERT OR IGNORE INTO app_settings(setting_key, value_json, updated_at) "
                "VALUES (?, ?, ?)",
                (key, _json(value), now),
            )

    @staticmethod
    def _seed_delivery_presets(connection: sqlite3.Connection, voice_id: str) -> None:
        now = utc_now()
        for delivery, defaults in DELIVERY_DEFAULTS.items():
            connection.execute(
                "INSERT OR IGNORE INTO voice_delivery_presets(voice_id, delivery, prompt_tag, "
                "stability, status, updated_at) VALUES (?, ?, ?, ?, 'not_tested', ?)",
                (voice_id, delivery, defaults["prompt_tag"], defaults["stability"], now),
            )

    @staticmethod
    def _normalize_delivery_prompt_tags(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT voice_id, delivery, prompt_tag FROM voice_delivery_presets"
        ).fetchall()
        for row in rows:
            normalized = _normalize_voice_actor_notes(row["prompt_tag"])[:80]
            if normalized != row["prompt_tag"]:
                connection.execute(
                    "UPDATE voice_delivery_presets SET prompt_tag=? "
                    "WHERE voice_id=? AND delivery=?",
                    (normalized, row["voice_id"], row["delivery"]),
                )

    @staticmethod
    def _dialogue_id(row: dict[str, Any], expansion: str, locale: str) -> str:
        quest = str(row["quest"]).strip()
        identity = [expansion, locale, row["type"], str(row["id"]), row["source"]]
        source_record_id = next(
            (
                str(row.get(field, "")).strip()
                for field in ("source_record_id", "broadcast_text_id", "record_id")
                if str(row.get(field, "")).strip() not in {"", "0"}
            ),
            "",
        )
        if source_record_id:
            identity.extend(["source-record", source_record_id])
        elif quest:
            identity.extend(["quest", quest])
        else:
            identity.extend(["gossip", hashlib.sha256(row["original_text"].encode()).hexdigest()])
        return hashlib.sha256("|".join(identity).encode()).hexdigest()[:24]

    @staticmethod
    def _addon_key(row: dict[str, Any]) -> str:
        quest = str(row["quest"]).strip()
        if quest:
            return f"{int(float(quest))}-{row['source']}"
        race = RACE_DICT.get(int(row["DisplayRaceID"]), "unknown")
        gender = GENDER_DICT.get(int(row["DisplaySexID"]), "unknown")
        payload = f"{row['original_text']}{race}{gender}"
        return hashlib.md5(payload.encode()).hexdigest()  # noqa: S324 - addon compatibility key

    def import_csv(
        self,
        path: Path,
        *,
        source_name: str = "dialogue.csv",
        expansion: str = "3.3.5",
        locale: str = "enUS",
    ) -> dict[str, Any]:
        dataframe = load_dialogue_csv(path)
        content_hash = sha256_bytes(path.read_bytes())
        source_hash = sha256_bytes(f"{expansion}|{locale}|{content_hash}".encode())
        snapshot_id = source_hash[:24]
        now = utc_now()
        imported_ids: set[str] = set()
        imported_speaker_ids: set[str] = set()
        prepared_spoken_texts = 0
        with self.connect() as connection:
            connection.execute(
                "UPDATE source_snapshots SET is_active=0 WHERE expansion=? AND locale=?",
                (expansion, locale),
            )
            connection.execute(
                "UPDATE dialogue_entries SET active=0 WHERE expansion=? AND locale=?",
                (expansion, locale),
            )
            connection.execute(
                "INSERT INTO source_snapshots(snapshot_id, source_name, source_hash, expansion, "
                "locale, row_count, imported_at, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(source_hash) DO UPDATE SET source_name=excluded.source_name, "
                "row_count=excluded.row_count, imported_at=excluded.imported_at, is_active=1",
                (
                    snapshot_id,
                    source_name,
                    source_hash,
                    expansion,
                    locale,
                    len(dataframe),
                    now,
                ),
            )
            for row in dataframe.to_dict(orient="records"):
                row = {
                    key: value.item() if hasattr(value, "item") else value
                    for key, value in row.items()
                }
                metadata = {
                    key: value
                    for key, value in row.items()
                    if key not in REQUIRED_COLUMNS and str(value).strip()
                }
                entity_type = str(row["type"])
                entity_id = int(row["id"])
                speaker_id = f"{entity_type}-{entity_id}"
                imported_speaker_ids.add(speaker_id)
                race_id = int(row["DisplayRaceID"])
                gender_id = int(row["DisplaySexID"])
                race_name = RACE_DICT.get(race_id, f"race-{race_id}")
                gender_name = GENDER_DICT.get(gender_id, f"gender-{gender_id}")
                profile_id = f"baseline--{race_name}-{gender_name}"
                baseline_exists = connection.execute(
                    "SELECT 1 FROM voices WHERE voice_id = ?", (profile_id,)
                ).fetchone()
                inferred_role = _infer_role(str(row["name"]), metadata, entity_type)
                inferred_affiliation = _infer_affiliation(metadata)
                inferred_zone = _first_metadata(
                    metadata, "zone_name", "zone", "area_name", "area", "map_name"
                )
                connection.execute(
                    "INSERT INTO speakers(speaker_id, entity_type, entity_id, name, race_id, "
                    "gender_id, race_name, gender_name, voice_id, role, faction, zone, "
                    "source_snapshot_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(speaker_id) DO UPDATE SET name=excluded.name, race_id=excluded.race_id, "
                    "gender_id=excluded.gender_id, race_name=excluded.race_name, "
                    "gender_name=excluded.gender_name, source_snapshot_id=excluded.source_snapshot_id, "
                    "role=CASE WHEN speakers.role='' THEN excluded.role ELSE speakers.role END, "
                    "faction=CASE WHEN speakers.faction='' THEN excluded.faction ELSE speakers.faction END, "
                    "zone=CASE WHEN speakers.zone='' THEN excluded.zone ELSE speakers.zone END, "
                    "updated_at=excluded.updated_at",
                    (
                        speaker_id,
                        entity_type,
                        entity_id,
                        str(row["name"]),
                        race_id,
                        gender_id,
                        race_name,
                        gender_name,
                        profile_id if baseline_exists else None,
                        inferred_role,
                        inferred_affiliation,
                        inferred_zone,
                        snapshot_id,
                        now,
                        now,
                    ),
                )
                if baseline_exists:
                    connection.execute(
                        "UPDATE speakers SET voice_id = COALESCE(voice_id, ?) WHERE speaker_id = ?",
                        (profile_id, speaker_id),
                    )
                dialogue_id = self._dialogue_id(row, expansion, locale)
                imported_ids.add(dialogue_id)
                quest_raw = str(row["quest"]).strip()
                quest_id = int(float(quest_raw)) if quest_raw else None
                source_record_id = next(
                    (
                        str(row.get(field, "")).strip()
                        for field in ("source_record_id", "broadcast_text_id", "record_id")
                        if str(row.get(field, "")).strip() not in {"", "0"}
                    ),
                    "",
                )
                connection.execute(
                    "INSERT INTO dialogue_entries(dialogue_id, source_snapshot_id, expansion, "
                    "locale, source_record_id, source, quest_id, quest_title, speaker_id, original_text, "
                    "metadata_json, addon_file_key, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(dialogue_id) DO UPDATE SET source_snapshot_id=excluded.source_snapshot_id, "
                    "quest_title=excluded.quest_title, speaker_id=excluded.speaker_id, "
                    "original_text=excluded.original_text, source_record_id=excluded.source_record_id, "
                    "metadata_json=excluded.metadata_json, addon_file_key=excluded.addon_file_key, "
                    "active=1, updated_at=excluded.updated_at",
                    (
                        dialogue_id,
                        snapshot_id,
                        expansion,
                        locale,
                        source_record_id,
                        str(row["source"]),
                        quest_id,
                        str(row["quest_title"]),
                        speaker_id,
                        str(row["original_text"]),
                        _json(metadata),
                        self._addon_key(row),
                        now,
                        now,
                    ),
                )
                if str(row["source"]) != "gossip" and self._ensure_spoken_text_revision(
                    connection,
                    dialogue_id,
                    str(row["original_text"]),
                ):
                    prepared_spoken_texts += 1
            for speaker_id in imported_speaker_ids:
                self._refresh_speaker_inference(connection, speaker_id)
        return {
            "snapshot_id": snapshot_id,
            "source_hash": source_hash,
            "rows_received": len(dataframe),
            "dialogue_records": len(imported_ids),
            "prepared_spoken_texts": prepared_spoken_texts,
        }

    @staticmethod
    def _refresh_speaker_inference(connection: sqlite3.Connection, speaker_id: str) -> None:
        speaker = connection.execute(
            "SELECT * FROM speakers WHERE speaker_id=?", (speaker_id,)
        ).fetchone()
        if not speaker:
            return
        totals = connection.execute(
            "SELECT COUNT(*) AS line_count, COUNT(DISTINCT quest_id) AS quest_count, "
            "SUM(CASE WHEN source='gossip' THEN 1 ELSE 0 END) AS gossip_count "
            "FROM dialogue_entries WHERE speaker_id=? AND active=1",
            (speaker_id,),
        ).fetchone()
        line_count = int(totals["line_count"] or 0)
        quest_count = int(totals["quest_count"] or 0)
        name = str(speaker["name"]).lower()
        if any(term in name for term in ("king", "queen", "warchief", "lich king", "thrall")):
            importance = "pivotal"
        elif quest_count >= 10:
            importance = "global"
        elif quest_count >= 6:
            importance = "inter_zone"
        elif quest_count >= 3:
            importance = "zone"
        elif quest_count >= 2 or line_count >= 4:
            importance = "subzone"
        elif quest_count == 1:
            importance = "stepping_stone"
        else:
            importance = "one_off"
        context = (
            f"{speaker['name']} is a {speaker['race_name']} {speaker['gender_name']} "
            f"{str(speaker['role']).replace('_', ' ')}"
        )
        if speaker["faction"] and speaker["faction"] != "unspecified":
            context += f" associated with {str(speaker['faction']).replace('_', ' ')}"
        if speaker["zone"]:
            context += f" in {speaker['zone']}"
        context += f". This source currently assigns {line_count} spoken record(s) to this NPC."
        connection.execute(
            "UPDATE speakers SET importance=CASE WHEN importance='unassessed' THEN ? ELSE importance END, "
            "context_summary=CASE WHEN context_summary='' THEN ? ELSE context_summary END "
            "WHERE speaker_id=?",
            (importance, context, speaker_id),
        )

    @staticmethod
    def _status_expression() -> str:
        return """
            CASE
                WHEN pa.dialogue_id IS NOT NULL THEN 'approved'
                WHEN EXISTS (
                    SELECT 1 FROM audio_candidates ac
                    WHERE ac.dialogue_id = d.dialogue_id AND ac.status = 'pending_review'
                ) THEN 'audio_to_review'
                WHEN EXISTS (
                    SELECT 1 FROM generations gx
                    WHERE gx.dialogue_id = d.dialogue_id AND gx.status = 'failed'
                    AND gx.created_at = (SELECT MAX(created_at) FROM generations WHERE dialogue_id=d.dialogue_id)
                ) THEN 'generation_failed'
                WHEN tr.revision_id IS NULL THEN 'needs_text'
                WHEN s.voice_id IS NULL OR COALESCE(NULLIF(vdp.provider_voice_id, ''),
                    vv.provider_voice_id, '') = ''
                    THEN 'needs_voice'
                ELSE 'ready_to_generate'
            END
        """

    def _dialogue_select(self) -> str:
        return f"""
            SELECT d.*, s.name AS speaker_name, s.entity_type, s.entity_id, s.race_id,
                s.gender_id, s.race_name, s.gender_name, s.voice_id,
                v.name AS voice_name, v.scope AS voice_scope,
                vv.version_id AS voice_version_id, vv.version_number AS voice_version_number,
                COALESCE(NULLIF(vdp.provider_voice_id, ''), vv.provider_voice_id) AS provider_voice_id,
                vv.provider_voice_id AS default_provider_voice_id,
                vv.model_id, vv.description AS voice_description,
                vv.creation_method, vv.settings_json,
                tr.revision_id, tr.revision_number, tr.spoken_text, tr.changes_json,
                tr.warnings_json, pa.candidate_id AS production_candidate_id,
                pa.addon_filename, pa.sha256 AS production_sha256,
                pa.duration_seconds AS production_duration,
                {self._status_expression()} AS production_state
            FROM dialogue_entries d
            JOIN speakers s ON s.speaker_id = d.speaker_id
            LEFT JOIN voices v ON v.voice_id = s.voice_id
            LEFT JOIN voice_versions vv ON vv.voice_id = v.voice_id AND vv.is_current = 1
            LEFT JOIN voice_delivery_presets vdp ON vdp.voice_id=v.voice_id
                AND vdp.delivery=d.delivery
            LEFT JOIN spoken_text_revisions tr ON tr.dialogue_id = d.dialogue_id AND tr.is_current = 1
            LEFT JOIN production_assets pa ON pa.dialogue_id = d.dialogue_id
        """

    def dashboard(self) -> dict[str, Any]:
        with self.connect() as connection:
            snapshots = connection.execute(
                "SELECT * FROM source_snapshots WHERE is_active=1 "
                "ORDER BY imported_at DESC, expansion, locale"
            ).fetchall()
            counts = {
                "dialogue": connection.execute(
                    "SELECT COUNT(*) FROM dialogue_entries WHERE active=1"
                ).fetchone()[0],
                "speakers": connection.execute("SELECT COUNT(*) FROM speakers").fetchone()[0],
                "npcs": connection.execute(
                    "SELECT COUNT(*) FROM speakers WHERE entity_type='creature'"
                ).fetchone()[0],
                "baseline_voices": connection.execute(
                    "SELECT COUNT(*) FROM voices WHERE scope='baseline'"
                ).fetchone()[0],
                "unique_voices": connection.execute(
                    "SELECT COUNT(*) FROM voices v JOIN voice_versions vv ON vv.voice_id=v.voice_id "
                    "AND vv.is_current=1 WHERE v.scope='unique' AND vv.status<>'retired'"
                ).fetchone()[0],
            }
            rows = connection.execute(
                f"SELECT production_state, COUNT(*) AS total FROM ({self._dialogue_select()} "
                "WHERE d.active=1) GROUP BY production_state ORDER BY production_state"
            ).fetchall()
            source_rows = connection.execute(
                "SELECT source, COUNT(*) AS total FROM dialogue_entries WHERE active=1 "
                "GROUP BY source ORDER BY source"
            ).fetchall()
        return {
            "snapshot": dict(snapshots[0]) if snapshots else None,
            "snapshots": [dict(snapshot) for snapshot in snapshots],
            "counts": counts,
            "states": {row["production_state"]: row["total"] for row in rows},
            "sources": {row["source"]: row["total"] for row in source_rows},
        }

    def list_dialogue(
        self,
        *,
        query: str = "",
        state: str = "",
        source: str = "",
        expansion: str = "",
        race_id: str = "",
        gender_id: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        conditions = ["active = 1"]
        parameters: list[Any] = []
        if query.strip():
            conditions.append(
                "(speaker_name LIKE ? OR quest_title LIKE ? OR original_text LIKE ? OR "
                "CAST(quest_id AS TEXT) = ?)"
            )
            term = f"%{query.strip()}%"
            parameters.extend([term, term, term, query.strip()])
        if state:
            if state not in PRODUCTION_STATES:
                raise AlphaError("Unknown production state filter.")
            conditions.append("production_state = ?")
            parameters.append(state)
        if source:
            if source == "quest":
                conditions.append("source <> 'gossip'")
            elif source in VALID_SOURCES:
                conditions.append("source = ?")
                parameters.append(source)
            else:
                raise AlphaError("Unknown content filter.")
        if expansion:
            conditions.append("expansion = ?")
            parameters.append(expansion)
        if race_id:
            conditions.append("race_id = ?")
            parameters.append(int(race_id))
        if gender_id:
            conditions.append("gender_id = ?")
            parameters.append(int(gender_id))
        where = " AND ".join(conditions)
        base = self._dialogue_select()
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM ({base}) WHERE {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM ({base}) WHERE {where} ORDER BY speaker_name, quest_id, source "
                "LIMIT ? OFFSET ?",
                [*parameters, page_size, max(page - 1, 0) * page_size],
            ).fetchall()
            races = connection.execute(
                "SELECT DISTINCT race_id, race_name FROM speakers ORDER BY race_name"
            ).fetchall()
        return {
            "rows": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "page_count": max(1, (total + page_size - 1) // page_size),
            "races": [dict(row) for row in races],
        }

    def get_dialogue(self, dialogue_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                f"{self._dialogue_select()} WHERE d.dialogue_id = ?", (dialogue_id,)
            ).fetchone()
            if not row:
                raise AlphaError("Dialogue record was not found.")
            revisions = connection.execute(
                "SELECT * FROM spoken_text_revisions WHERE dialogue_id=? "
                "ORDER BY revision_number DESC",
                (dialogue_id,),
            ).fetchall()
            candidates = connection.execute(
                "SELECT ac.*, g.delivery, g.model_id, g.character_count, g.provider_request_id, "
                "g.created_at AS generated_at FROM audio_candidates ac "
                "JOIN generations g ON g.generation_id=ac.generation_id "
                "WHERE ac.dialogue_id=? ORDER BY ac.created_at DESC",
                (dialogue_id,),
            ).fetchall()
            generations = connection.execute(
                "SELECT * FROM generations WHERE dialogue_id=? ORDER BY created_at DESC LIMIT 20",
                (dialogue_id,),
            ).fetchall()
            quest_phases: list[sqlite3.Row] = []
            if row["quest_id"] is not None and row["source"] != "gossip":
                quest_phases = connection.execute(
                    f"{self._dialogue_select()} WHERE d.active=1 AND d.quest_id=? "
                    "AND d.expansion=? AND d.locale=? AND d.dialogue_id<>? "
                    "AND d.source<>'gossip' ORDER BY CASE d.source "
                    "WHEN 'accept' THEN 1 WHEN 'progress' THEN 2 "
                    "WHEN 'complete' THEN 3 ELSE 4 END, s.name",
                    (
                        row["quest_id"],
                        row["expansion"],
                        row["locale"],
                        dialogue_id,
                    ),
                ).fetchall()
            voices = connection.execute(
                "SELECT v.voice_id, v.name, v.scope, vv.provider_voice_id, vv.status, "
                "vv.version_number FROM voices v JOIN voice_versions vv ON vv.voice_id=v.voice_id "
                "AND vv.is_current=1 ORDER BY v.scope, v.name"
            ).fetchall()
        payload = dict(row)
        payload["metadata"] = _loads(payload.get("metadata_json"), {})
        payload["changes"] = _loads(payload.get("changes_json"), [])
        payload["warnings"] = _loads(payload.get("warnings_json"), [])
        payload["voice_settings"] = _loads(payload.get("settings_json"), {})
        payload["revisions"] = [dict(item) for item in revisions]
        payload["candidates"] = [dict(item) for item in candidates]
        payload["quest_phases"] = [dict(item) for item in quest_phases]
        payload["generations"] = []
        for item in generations:
            generation = dict(item)
            generation["subscription"] = _loads(generation.get("subscription_json"), {})
            payload["generations"].append(generation)
        payload["voices"] = [dict(item) for item in voices]
        payload["generation_text"] = self.generation_text(payload)
        return payload

    def list_npcs(
        self,
        *,
        query: str = "",
        race_id: str = "",
        gender_id: str = "",
        role: str = "",
        faction: str = "",
        importance: str = "",
        voice_approach: str = "",
        voice_state: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """List creature-backed NPCs independently from quest and gossip lines."""
        conditions = ["entity_type = 'creature'"]
        parameters: list[Any] = []
        if query.strip():
            conditions.append(
                "(name LIKE ? OR role LIKE ? OR faction LIKE ? OR zone LIKE ? "
                "OR context_summary LIKE ? OR CAST(entity_id AS TEXT) = ?)"
            )
            term = f"%{query.strip()}%"
            parameters.extend([term, term, term, term, term, query.strip()])
        if race_id:
            conditions.append("race_id = ?")
            parameters.append(int(race_id))
        if gender_id:
            conditions.append("gender_id = ?")
            parameters.append(int(gender_id))
        if role:
            if role not in ROLE_OPTIONS:
                raise AlphaError("Unknown NPC role filter.")
            conditions.append("role = ?")
            parameters.append(role)
        if faction:
            if faction not in AFFILIATION_OPTIONS:
                raise AlphaError("Unknown NPC faction filter.")
            conditions.append("faction = ?")
            parameters.append(faction)
        if importance:
            if importance not in IMPORTANCE_SCORES:
                raise AlphaError("Unknown story reach filter.")
            conditions.append("importance = ?")
            parameters.append(importance)
        if voice_approach:
            if voice_approach == "baseline":
                conditions.append("voice_scope = 'baseline'")
            elif voice_approach == "unique":
                conditions.append("voice_scope = 'unique'")
            elif voice_approach == "dormant":
                conditions.append("unique_voice_id IS NOT NULL AND voice_scope <> 'unique'")
            elif voice_approach == "unassigned":
                conditions.append("voice_id IS NULL")
            else:
                raise AlphaError("Unknown voice approach filter.")
        if voice_state:
            if voice_state == "needs_voice":
                conditions.append("(voice_id IS NULL OR COALESCE(provider_voice_id, '') = '')")
            elif voice_state == "ready":
                conditions.append("voice_id IS NOT NULL AND COALESCE(provider_voice_id, '') <> ''")
            else:
                raise AlphaError("Unknown voice readiness filter.")

        base = """
            SELECT s.*, v.name AS voice_name, v.scope AS voice_scope,
                vv.provider_voice_id, vv.status AS stored_voice_status,
                (SELECT COUNT(*) FROM dialogue_entries d
                    WHERE d.speaker_id=s.speaker_id AND d.active=1) AS dialogue_count,
                (SELECT COUNT(*) FROM dialogue_entries d
                    WHERE d.speaker_id=s.speaker_id AND d.active=1 AND d.source<>'gossip')
                    AS quest_count,
                (SELECT COUNT(*) FROM dialogue_entries d
                    WHERE d.speaker_id=s.speaker_id AND d.active=1 AND d.source='gossip')
                    AS gossip_count,
                (SELECT uv.voice_id FROM voices uv
                    WHERE uv.scope='unique' AND uv.npc_speaker_id=s.speaker_id LIMIT 1)
                    AS unique_voice_id,
                (SELECT uvv.status FROM voices uv
                    JOIN voice_versions uvv ON uvv.voice_id=uv.voice_id AND uvv.is_current=1
                    WHERE uv.scope='unique' AND uv.npc_speaker_id=s.speaker_id LIMIT 1)
                    AS unique_voice_status
            FROM speakers s
            LEFT JOIN voices v ON v.voice_id=s.voice_id
            LEFT JOIN voice_versions vv ON vv.voice_id=v.voice_id AND vv.is_current=1
        """
        where = " AND ".join(conditions)
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM ({base}) WHERE {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM ({base}) WHERE {where} ORDER BY name, entity_id LIMIT ? OFFSET ?",
                [*parameters, page_size, max(page - 1, 0) * page_size],
            ).fetchall()
            races = connection.execute(
                "SELECT DISTINCT race_id, race_name FROM speakers "
                "WHERE entity_type='creature' ORDER BY race_name"
            ).fetchall()
        npc_rows = []
        for row in rows:
            npc = dict(row)
            npc["importance_score"] = IMPORTANCE_SCORES.get(npc["importance"], 0)
            npc_rows.append(npc)
        return {
            "rows": npc_rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "page_count": max(1, (total + page_size - 1) // page_size),
            "races": [dict(row) for row in races],
        }

    @staticmethod
    def prepare_text(original_text: str) -> tuple[str, list[str], list[str]]:
        text = original_text
        changes: list[str] = []
        warnings: list[str] = []
        substitutions = {
            "$N": "Adventurer",
            "$n": "adventurer",
            "$C": "Adventurer",
            "$c": "adventurer",
            "$R": "Traveler",
            "$r": "traveler",
            "$B": "\n\n",
            "$b": "\n\n",
        }
        for source, replacement in substitutions.items():
            if source in text:
                text = text.replace(source, replacement)
                changes.append(f"Expanded {source} as {replacement!r}.")
        markup = re.findall(r"<[^>]+>", text)
        if markup:
            text = re.sub(r"<[^>]+>", " ", text)
            changes.append("Removed non-spoken markup.")
        if re.search(r"\$[Gg][^;]*;", text):
            warnings.append("Player-gender alternatives still require a deliberate spoken version.")
        unresolved = sorted(set(re.findall(r"\$[A-Za-z]+", text)))
        if unresolved:
            warnings.append(f"Unresolved game tokens remain: {', '.join(unresolved)}")
        paragraphs = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n+", text)]
        text = "\n\n".join(part for part in paragraphs if part)
        if text != original_text and not changes:
            changes.append("Normalized whitespace for speech.")
        return text, changes, warnings

    def _insert_revision(
        self,
        connection: sqlite3.Connection,
        dialogue_id: str,
        spoken_text: str,
        processor: str,
        changes: list[str],
        warnings: list[str],
    ) -> int:
        current = connection.execute(
            "SELECT COALESCE(MAX(revision_number), 0) FROM spoken_text_revisions "
            "WHERE dialogue_id=?",
            (dialogue_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE spoken_text_revisions SET is_current=0 WHERE dialogue_id=?",
            (dialogue_id,),
        )
        cursor = connection.execute(
            "INSERT INTO spoken_text_revisions(dialogue_id, revision_number, spoken_text, "
            "processor, changes_json, warnings_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                dialogue_id,
                current + 1,
                spoken_text,
                processor,
                _json(changes),
                _json(warnings),
                utc_now(),
            ),
        )
        return int(cursor.lastrowid)

    def _ensure_spoken_text_revision(
        self,
        connection: sqlite3.Connection,
        dialogue_id: str,
        original_text: str,
    ) -> bool:
        existing = connection.execute(
            "SELECT 1 FROM spoken_text_revisions WHERE dialogue_id=? AND is_current=1",
            (dialogue_id,),
        ).fetchone()
        if existing:
            return False
        text, changes, warnings = self.prepare_text(original_text)
        self._insert_revision(
            connection,
            dialogue_id,
            text,
            "deterministic-cleaner-v1",
            changes,
            warnings,
        )
        return True

    def ensure_spoken_text(self, dialogue_id: str) -> dict[str, Any]:
        """Create deterministic spoken text when a legacy quest record is missing it."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT source, original_text FROM dialogue_entries WHERE dialogue_id=?",
                (dialogue_id,),
            ).fetchone()
            if not row:
                raise AlphaError("Dialogue record was not found.")
            if row["source"] == "gossip":
                raise AlphaError("Gossip spoken text is prepared through its review workflow.")
            self._ensure_spoken_text_revision(connection, dialogue_id, row["original_text"])
        return self.get_dialogue(dialogue_id)

    def prepare_spoken_text(self, dialogue_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT original_text FROM dialogue_entries WHERE dialogue_id=?", (dialogue_id,)
            ).fetchone()
            if not row:
                raise AlphaError("Dialogue record was not found.")
            existing = connection.execute(
                "SELECT revision_id FROM spoken_text_revisions WHERE dialogue_id=? AND is_current=1",
                (dialogue_id,),
            ).fetchone()
            if existing:
                raise AlphaError("Spoken text has already been prepared; save a revision instead.")
            self._ensure_spoken_text_revision(connection, dialogue_id, row["original_text"])
        return self.get_dialogue(dialogue_id)

    def save_spoken_text(self, dialogue_id: str, spoken_text: str) -> dict[str, Any]:
        text = spoken_text.strip()
        if not text or len(text) > 20000:
            raise AlphaError("Spoken text must contain 1–20,000 characters.")
        warnings = []
        unresolved = sorted(set(re.findall(r"\$[A-Za-z]+|<[^>]+>", text)))
        if unresolved:
            warnings.append(f"Unresolved game tokens or markup remain: {', '.join(unresolved)}")
        with self.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM dialogue_entries WHERE dialogue_id=?", (dialogue_id,)
            ).fetchone():
                raise AlphaError("Dialogue record was not found.")
            self._insert_revision(
                connection,
                dialogue_id,
                text,
                "manual",
                ["Saved a manually reviewed spoken-text revision."],
                warnings,
            )
        return self.get_dialogue(dialogue_id)

    def set_delivery(self, dialogue_id: str, delivery: str) -> None:
        if delivery not in DELIVERIES:
            raise AlphaError("Unknown delivery selection.")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE dialogue_entries SET delivery=?, updated_at=? WHERE dialogue_id=?",
                (delivery, utc_now(), dialogue_id),
            )
            if not cursor.rowcount:
                raise AlphaError("Dialogue record was not found.")

    def update_speaker(self, speaker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM speakers WHERE speaker_id=?", (speaker_id,)
            ).fetchone()
        if not existing:
            raise AlphaError("NPC was not found.")
        importance = str(payload.get("importance", existing["importance"]))
        uniqueness = str(payload.get("uniqueness", existing["uniqueness"]))
        role = str(payload.get("role", existing["role"]))
        faction = str(payload.get("faction", existing["faction"]))
        if importance not in IMPORTANCE_SCORES:
            raise AlphaError("Unknown story importance.")
        if uniqueness not in {"unassessed", "baseline", "unique_candidate", "unique"}:
            raise AlphaError("Unknown uniqueness selection.")
        if role not in ROLE_OPTIONS:
            raise AlphaError("Unknown role or occupation.")
        if faction not in AFFILIATION_OPTIONS:
            raise AlphaError("Unknown faction.")
        voice_id = str(payload.get("voice_id", existing["voice_id"] or "")).strip() or None
        with self.connect() as connection:
            if (
                voice_id
                and not connection.execute(
                    "SELECT 1 FROM voices WHERE voice_id=?", (voice_id,)
                ).fetchone()
            ):
                raise AlphaError("Selected voice was not found.")
            cursor = connection.execute(
                "UPDATE speakers SET role=?, faction=?, zone=?, context_summary=?, importance=?, "
                "uniqueness=?, voice_id=?, updated_at=? WHERE speaker_id=?",
                (
                    role,
                    faction,
                    str(payload.get("zone", existing["zone"]))[:200].strip(),
                    str(payload.get("context_summary", existing["context_summary"]))[:4000].strip(),
                    importance,
                    uniqueness,
                    voice_id,
                    utc_now(),
                    speaker_id,
                ),
            )
            if not cursor.rowcount:
                raise AlphaError("NPC was not found.")
        return self.get_speaker(speaker_id)

    def get_speaker(self, speaker_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            speaker = connection.execute(
                "SELECT s.*, v.name AS voice_name, v.scope AS voice_scope, "
                "vv.provider_voice_id, vv.status AS voice_status FROM speakers s "
                "LEFT JOIN voices v ON v.voice_id=s.voice_id "
                "LEFT JOIN voice_versions vv ON vv.voice_id=v.voice_id AND vv.is_current=1 "
                "WHERE s.speaker_id=?",
                (speaker_id,),
            ).fetchone()
            if not speaker:
                raise AlphaError("NPC was not found.")
            lines = connection.execute(
                f"SELECT * FROM ({self._dialogue_select()}) WHERE speaker_id=? AND active=1 "
                "ORDER BY quest_id, source",
                (speaker_id,),
            ).fetchall()
            voices = connection.execute(
                "SELECT v.voice_id, v.name, v.scope, vv.provider_voice_id, vv.status "
                "FROM voices v JOIN voice_versions vv ON vv.voice_id=v.voice_id AND vv.is_current=1 "
                "WHERE (v.scope='baseline' AND v.race_id=? AND v.gender_id=?) OR "
                "(v.scope='unique' AND v.npc_speaker_id=?) "
                "ORDER BY v.scope, v.name",
                (speaker["race_id"], speaker["gender_id"], speaker_id),
            ).fetchall()
            baseline_voice = connection.execute(
                "SELECT v.voice_id, v.name, vv.provider_voice_id, vv.status AS stored_status "
                "FROM voices v JOIN voice_versions vv "
                "ON vv.voice_id=v.voice_id AND vv.is_current=1 WHERE v.scope='baseline' "
                "AND v.race_id=? AND v.gender_id=?",
                (speaker["race_id"], speaker["gender_id"]),
            ).fetchone()
            unique_voice = connection.execute(
                "SELECT v.voice_id, v.name, vv.provider_voice_id, vv.status AS stored_status "
                "FROM voices v JOIN voice_versions vv "
                "ON vv.voice_id=v.voice_id AND vv.is_current=1 WHERE v.scope='unique' "
                "AND v.npc_speaker_id=?",
                (speaker_id,),
            ).fetchone()
        speaker_payload = dict(speaker)
        speaker_payload["importance_score"] = IMPORTANCE_SCORES.get(
            speaker_payload["importance"], 0
        )
        if speaker_payload.get("voice_id"):
            voice_summary = next(
                (
                    voice
                    for voice in self.list_voices(include_retired=True)
                    if voice["voice_id"] == speaker_payload["voice_id"]
                ),
                None,
            )
            if voice_summary:
                speaker_payload["voice_status"] = voice_summary["status"]
        unique_payload = dict(unique_voice) if unique_voice else None
        if unique_payload:
            unique_summary = next(
                (
                    voice
                    for voice in self.list_voices(include_retired=True)
                    if voice["voice_id"] == unique_payload["voice_id"]
                ),
                None,
            )
            if unique_summary:
                unique_payload["status"] = unique_summary["status"]
            unique_payload["is_active"] = (
                speaker_payload.get("voice_id") == unique_payload["voice_id"]
            )
        record = {
            "speaker": speaker_payload,
            "npc": speaker_payload,
            "dialogue": [dict(row) for row in lines],
            "voices": [dict(row) for row in voices],
            "baseline_voice": dict(baseline_voice) if baseline_voice else None,
            "unique_voice": unique_payload,
        }
        return record

    def create_unique_voice(self, speaker_id: str) -> dict[str, Any]:
        now = utc_now()
        existing_voice_id: str | None = None
        with self.connect() as connection:
            speaker = connection.execute(
                "SELECT * FROM speakers WHERE speaker_id=?", (speaker_id,)
            ).fetchone()
            if not speaker:
                raise AlphaError("NPC was not found.")
            existing = connection.execute(
                "SELECT voice_id FROM voices WHERE npc_speaker_id=?", (speaker_id,)
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE speakers SET voice_id=?, uniqueness='unique', updated_at=? "
                    "WHERE speaker_id=?",
                    (existing["voice_id"], now, speaker_id),
                )
                existing_voice_id = str(existing["voice_id"])
            else:
                voice_id = f"unique--{speaker_id}"
                parent_voice_id = speaker["voice_id"]
                parent = connection.execute(
                    "SELECT description, settings_json FROM voice_versions "
                    "WHERE voice_id=? AND is_current=1",
                    (parent_voice_id,),
                ).fetchone()
                npc_context = (
                    f"{speaker['race_name']} {speaker['gender_name']}; "
                    f"role: {str(speaker['role']).replace('_', ' ')}; "
                    f"faction: {str(speaker['faction']).replace('_', ' ')}; "
                    f"zone: {speaker['zone'] or 'unspecified'}; "
                    f"story reach: {str(speaker['importance']).replace('_', ' ')}. "
                    f"{speaker['context_summary']}"
                ).strip()
                description = (
                    f"Unique voice for {speaker['name']}. NPC context: {npc_context} "
                    f"Baseline direction: "
                    f"{parent['description'] if parent else 'Context and direction need review.'}"
                )[:1000]
                settings = parent["settings_json"] if parent else _json({"stability": 0.5})
                connection.execute(
                    "INSERT INTO voices(voice_id, name, scope, race_id, gender_id, parent_voice_id, "
                    "npc_speaker_id, created_at, updated_at) VALUES (?, ?, 'unique', ?, ?, ?, ?, ?, ?)",
                    (
                        voice_id,
                        speaker["name"],
                        speaker["race_id"],
                        speaker["gender_id"],
                        parent_voice_id,
                        speaker_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO voice_versions(voice_id, version_number, description, "
                    "creation_method, settings_json, status, created_at) "
                    "VALUES (?, 1, ?, 'unselected', ?, 'draft', ?)",
                    (voice_id, description, settings, now),
                )
                self._seed_delivery_presets(connection, voice_id)
                connection.execute(
                    "UPDATE speakers SET voice_id=?, uniqueness='unique', updated_at=? "
                    "WHERE speaker_id=?",
                    (voice_id, now, speaker_id),
                )
        if not existing_voice_id:
            return self.get_voice(voice_id)
        voice = self.get_voice(existing_voice_id)
        if voice["stored_status"] != "retired":
            return voice
        return self.update_voice(existing_voice_id, {}, _stored_status="draft")

    def use_baseline_voice(self, speaker_id: str) -> dict[str, Any]:
        old_unique_voice_id: str | None = None
        with self.connect() as connection:
            speaker = connection.execute(
                "SELECT s.*, v.scope AS voice_scope FROM speakers s LEFT JOIN voices v "
                "ON v.voice_id=s.voice_id WHERE s.speaker_id=?",
                (speaker_id,),
            ).fetchone()
            if not speaker:
                raise AlphaError("NPC was not found.")
            baseline = connection.execute(
                "SELECT v.voice_id FROM voices v JOIN voice_versions vv ON vv.voice_id=v.voice_id "
                "AND vv.is_current=1 WHERE v.scope='baseline' AND v.race_id=? AND v.gender_id=?",
                (speaker["race_id"], speaker["gender_id"]),
            ).fetchone()
            if not baseline:
                raise AlphaError("A matching race and gender baseline voice was not found.")
            connection.execute(
                "UPDATE speakers SET voice_id=?, uniqueness='baseline', updated_at=? "
                "WHERE speaker_id=?",
                (baseline["voice_id"], utc_now(), speaker_id),
            )
            if speaker["voice_scope"] == "unique" and speaker["voice_id"]:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM speakers WHERE voice_id=?",
                    (speaker["voice_id"],),
                ).fetchone()[0]
                if not remaining:
                    old_unique_voice_id = str(speaker["voice_id"])

        if old_unique_voice_id:
            voice = self.get_voice(old_unique_voice_id)
            if voice["stored_status"] != "retired":
                self.update_voice(old_unique_voice_id, {}, _stored_status="retired")
        record = self.get_speaker(speaker_id)
        record["retired_voice_id"] = old_unique_voice_id
        return record

    def get_app_settings(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM app_settings").fetchall()
        return {row["setting_key"]: _loads(row["value_json"], None) for row in rows}

    def update_app_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_app_settings()
        updated = {
            "tts_model_id": str(payload.get("tts_model_id", current["tts_model_id"])),
            "voice_design_model_id": str(
                payload.get("voice_design_model_id", current["voice_design_model_id"])
            ),
            "output_format": str(payload.get("output_format", current["output_format"])),
        }
        if updated["tts_model_id"] not in {"eleven_v3", "eleven_multilingual_v2"}:
            raise AlphaError("Unknown speech model.")
        if updated["voice_design_model_id"] not in {
            "eleven_ttv_v3",
            "eleven_multilingual_ttv_v2",
        }:
            raise AlphaError("Unknown Voice Design model.")
        if updated["output_format"] != "mp3_44100_128":
            raise AlphaError("The Alpha currently supports MP3 44.1 kHz / 128 kbps output.")
        now = utc_now()
        with self.connect() as connection:
            for key, value in updated.items():
                connection.execute(
                    "INSERT INTO app_settings(setting_key, value_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(setting_key) DO UPDATE SET value_json=excluded.value_json, "
                    "updated_at=excluded.updated_at",
                    (key, _json(value), now),
                )
        return self.get_app_settings()

    def record_provider_usage(
        self,
        *,
        action: str,
        subject_id: str,
        input_character_count: int,
        character_cost: int | None,
        provider_request_id: str | None,
        subscription: dict[str, Any] | None,
    ) -> dict[str, Any]:
        event_id = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO provider_usage_events(event_id, provider, action, subject_id, "
                "input_character_count, character_cost, provider_request_id, subscription_json, "
                "created_at) VALUES (?, 'elevenlabs', ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    action[:80],
                    subject_id[:255],
                    max(int(input_character_count), 0),
                    character_cost,
                    provider_request_id,
                    _json(subscription or {}),
                    utc_now(),
                ),
            )
        return self.list_provider_usage(limit=1)[0]

    def list_provider_usage(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_usage_events ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        events = [dict(row) for row in rows]
        for event in events:
            event["subscription"] = _loads(event.get("subscription_json"), {})
        return events

    def progress(self) -> dict[str, dict[str, int | float | str]]:
        with self.connect() as connection:
            voice = connection.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) "
                "AS complete FROM voice_delivery_presets vdp JOIN voices v ON v.voice_id=vdp.voice_id "
                "WHERE v.scope='baseline'"
            ).fetchone()
            unique_npcs = connection.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN "
                "EXISTS (SELECT 1 FROM voice_id_candidates vic WHERE vic.voice_id=v.voice_id) "
                "AND (SELECT COUNT(*) FROM voice_delivery_presets vdp "
                "WHERE vdp.voice_id=v.voice_id AND vdp.status='approved')=? "
                "THEN 1 ELSE 0 END) AS complete FROM voices v "
                "JOIN voice_versions vv ON vv.voice_id=v.voice_id AND vv.is_current=1 "
                "JOIN speakers s ON s.speaker_id=v.npc_speaker_id AND s.voice_id=v.voice_id "
                "WHERE v.scope='unique' AND vv.status<>'retired'",
                (len(DELIVERIES),),
            ).fetchone()
            dialogue = {}
            for key, condition in (
                ("quests", "d.source<>'gossip'"),
                ("gossip", "d.source='gossip'"),
            ):
                dialogue[key] = connection.execute(
                    "SELECT COUNT(*) AS total, SUM(CASE WHEN pa.dialogue_id IS NOT NULL THEN 1 ELSE 0 "
                    f"END) AS complete FROM dialogue_entries d LEFT JOIN production_assets pa "
                    f"ON pa.dialogue_id=d.dialogue_id WHERE d.active=1 AND {condition}"
                ).fetchone()

        def item(label: str, row: sqlite3.Row, href: str) -> dict[str, int | float | str]:
            total = int(row["total"] or 0)
            complete = int(row["complete"] or 0)
            return {
                "label": label,
                "complete": complete,
                "total": total,
                "percent": round((complete / total * 100) if total else 0, 1),
                "href": href,
            }

        return {
            "voices": item(
                "Baseline deliveries",
                voice,
                "/alpha/races?completion=incomplete",
            ),
            "unique_npcs": item(
                "Unique NPCs",
                unique_npcs,
                "/alpha/npcs?voice_approach=unique",
            ),
            "quests": item("Quest audio", dialogue["quests"], "/alpha"),
            "gossip": item("Gossip audio", dialogue["gossip"], "/alpha/gossip"),
        }

    def update_delivery_preset(
        self, voice_id: str, delivery: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if delivery not in DELIVERIES:
            raise AlphaError("Unknown delivery preset.")
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM voice_delivery_presets WHERE voice_id=? AND delivery=?",
                (voice_id, delivery),
            ).fetchone()
        if not existing:
            raise AlphaError("Delivery preset was not found.")
        status = str(payload.get("status", existing["status"]))
        if status not in DELIVERY_STATUSES:
            raise AlphaError("Unknown delivery status.")
        prompt_tag = _normalize_voice_actor_notes(payload.get("prompt_tag", ""))[:80]
        stability = float(payload.get("stability", 0.5))
        if not 0 <= stability <= 1:
            raise AlphaError("Delivery stability must be between 0 and 1.")
        provider_voice_id = str(
            payload.get("provider_voice_id", existing["provider_voice_id"] or "")
        ).strip()
        if provider_voice_id:
            with self.connect() as connection:
                candidate = connection.execute(
                    "SELECT 1 FROM voice_id_candidates WHERE voice_id=? AND provider_voice_id=?",
                    (voice_id, provider_voice_id),
                ).fetchone()
            if not candidate:
                raise AlphaError("Select a reusable voice ID candidate from this voice profile.")
        settings_changed = (
            provider_voice_id != (existing["provider_voice_id"] or "")
            or prompt_tag != existing["prompt_tag"]
            or stability != float(existing["stability"])
        )
        if settings_changed and "status" not in payload:
            with self.connect() as connection:
                has_previews = connection.execute(
                    "SELECT 1 FROM voice_delivery_previews WHERE voice_id=? AND delivery=? LIMIT 1",
                    (voice_id, delivery),
                ).fetchone()
            status = "previewed" if has_previews else "not_tested"
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE voice_delivery_presets SET provider_voice_id=?, prompt_tag=?, stability=?, "
                "status=?, notes=?, updated_at=? WHERE voice_id=? AND delivery=?",
                (
                    provider_voice_id,
                    prompt_tag,
                    stability,
                    status,
                    str(payload.get("notes", existing["notes"])).strip()[:1000],
                    utc_now(),
                    voice_id,
                    delivery,
                ),
            )
            if not cursor.rowcount:
                raise AlphaError("Delivery preset was not found.")
        return self.get_voice(voice_id)

    def delivery_preview_request(
        self, voice_id: str, delivery: str, sample_text: str
    ) -> dict[str, Any]:
        if delivery not in DELIVERIES:
            raise AlphaError("Unknown delivery preset.")
        text = sample_text.strip()
        if not 100 <= len(text) <= 1000:
            raise AlphaError("Delivery sample text must contain 100–1,000 characters.")
        voice = self.get_voice(voice_id)
        preset = next(item for item in voice["delivery_presets"] if item["delivery"] == delivery)
        provider_voice_id = preset.get("provider_voice_id") or voice.get("provider_voice_id")
        if not provider_voice_id:
            raise AlphaError("Generate a reusable voice ID before testing delivery presets.")
        actor_notes = _normalize_voice_actor_notes(preset["prompt_tag"])
        request_text = _delivery_request_text(actor_notes, text)
        model_id = self.get_app_settings()["tts_model_id"]
        settings = voice["settings"].copy()
        method, method_label, stability = _performance_method(preset["stability"])
        if model_id == "eleven_v3":
            settings = {"stability": stability}
        return {
            "voice_id": provider_voice_id,
            "baseline_voice_id": provider_voice_id,
            "actor_notes": actor_notes,
            "performance_method": method,
            "performance_method_label": method_label,
            "text": request_text,
            "model_id": model_id,
            "voice_settings": settings,
            "sample_text": text,
        }

    def record_delivery_preview(
        self,
        voice_id: str,
        delivery: str,
        request: dict[str, Any],
        *,
        content: bytes,
        provider_request_id: str | None,
        subscription: dict[str, Any] | None,
    ) -> dict[str, Any]:
        preview_id = uuid.uuid4().hex
        folder = self.storage_root / "delivery-previews" / voice_id / delivery
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{preview_id}.mp3"
        path.write_bytes(content)
        duration = _audio_duration(path)
        if duration is None or duration <= 0:
            path.unlink(missing_ok=True)
            raise AlphaError("ElevenLabs returned audio whose duration could not be validated.")
        now = utc_now()
        with self.connect() as connection:
            generation_number = self._reserve_delivery_sample_number(connection, voice_id, delivery)
            connection.execute(
                "INSERT INTO voice_delivery_previews(preview_id, voice_id, delivery, "
                "generation_number, storage_path, sha256, duration_seconds, sample_text, "
                "request_json, provider_request_id, subscription_json, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)",
                (
                    preview_id,
                    voice_id,
                    delivery,
                    generation_number,
                    str(path),
                    sha256_bytes(content),
                    duration,
                    request["sample_text"],
                    _json(request),
                    provider_request_id,
                    _json(subscription or {}),
                    now,
                ),
            )
            connection.execute(
                "UPDATE voice_delivery_presets SET status='previewed', updated_at=? "
                "WHERE voice_id=? AND delivery=? AND status<>'approved'",
                (now, voice_id, delivery),
            )
        return {
            "preview_id": preview_id,
            "duration_seconds": duration,
            "generation_number": generation_number,
        }

    def approve_delivery_preview(self, preview_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            preview = connection.execute(
                "SELECT * FROM voice_delivery_previews WHERE preview_id=?", (preview_id,)
            ).fetchone()
            if not preview:
                raise AlphaError("Delivery preview was not found.")
            connection.execute(
                "UPDATE voice_delivery_previews SET status='rejected', reviewed_at=? "
                "WHERE voice_id=? AND delivery=? AND preview_id<>? AND status='candidate'",
                (now, preview["voice_id"], preview["delivery"], preview_id),
            )
            connection.execute(
                "UPDATE voice_delivery_previews SET status='approved', reviewed_at=? "
                "WHERE preview_id=?",
                (now, preview_id),
            )
            connection.execute(
                "UPDATE voice_delivery_presets SET status='approved', updated_at=? "
                "WHERE voice_id=? AND delivery=?",
                (now, preview["voice_id"], preview["delivery"]),
            )
        return self.get_voice(preview["voice_id"])

    def update_delivery_preview_name(self, preview_id: str, display_name: Any) -> dict[str, Any]:
        normalized = _normalize_display_name(display_name)
        with self.connect() as connection:
            preview = connection.execute(
                "SELECT voice_id FROM voice_delivery_previews WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
            if not preview:
                raise AlphaError("Delivery preview was not found.")
            connection.execute(
                "UPDATE voice_delivery_previews SET display_name=? WHERE preview_id=?",
                (normalized, preview_id),
            )
        return self.get_voice(preview["voice_id"])

    def delivery_preview_path(self, preview_id: str) -> Path:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT storage_path FROM voice_delivery_previews WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
        if not row:
            raise AlphaError("Delivery preview was not found.")
        path = Path(row["storage_path"]).resolve()
        if self.storage_root not in path.parents or not path.is_file():
            raise AlphaError("Delivery preview file is unavailable.")
        return path

    def delete_delivery_preview(self, preview_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT voice_id, delivery, storage_path FROM voice_delivery_previews "
                "WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
        if not row:
            raise AlphaError("Delivery preview was not found.")
        path = Path(row["storage_path"]).resolve()
        if self.storage_root not in path.parents:
            raise AlphaError("Delivery preview storage path is invalid.")
        temporary_path = path.with_name(f".{path.name}.deleting")
        if path.is_file():
            path.replace(temporary_path)
        try:
            with self.connect() as connection:
                connection.execute(
                    "DELETE FROM voice_delivery_previews WHERE preview_id=?", (preview_id,)
                )
                remaining = connection.execute(
                    "SELECT status FROM voice_delivery_previews WHERE voice_id=? AND delivery=?",
                    (row["voice_id"], row["delivery"]),
                ).fetchall()
                remaining_statuses = {item["status"] for item in remaining}
                preset_status = (
                    "approved"
                    if "approved" in remaining_statuses
                    else "previewed"
                    if remaining_statuses
                    else "not_tested"
                )
                connection.execute(
                    "UPDATE voice_delivery_presets SET status=?, updated_at=? "
                    "WHERE voice_id=? AND delivery=?",
                    (preset_status, utc_now(), row["voice_id"], row["delivery"]),
                )
        except Exception:
            if temporary_path.is_file():
                temporary_path.replace(path)
            raise
        temporary_path.unlink(missing_ok=True)
        return self.get_voice(row["voice_id"])

    def list_voices(
        self,
        scope: str = "",
        completion: str = "",
        *,
        include_retired: bool = False,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        conditions = []
        if scope:
            if scope not in {"baseline", "unique"}:
                raise AlphaError("Unknown voice scope.")
            conditions.append("v.scope=?")
            parameters.append(scope)
        if not include_retired:
            conditions.append("(v.scope<>'unique' OR vv.status<>'retired')")
        if completion:
            if completion not in {"incomplete", "complete"}:
                raise AlphaError("Unknown delivery completion filter.")
            operator = "<" if completion == "incomplete" else "="
            conditions.append(
                "(SELECT COUNT(*) FROM voice_delivery_presets cvdp "
                "WHERE cvdp.voice_id=v.voice_id AND cvdp.status='approved') "
                f"{operator} 5"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT v.*, vv.version_id, vv.version_number, vv.creation_method, vv.provider, "
                "vv.provider_voice_id, vv.model_id, vv.status AS stored_status, "
                "(SELECT COUNT(*) FROM speakers s WHERE s.voice_id=v.voice_id) AS speaker_count, "
                "(SELECT COUNT(*) FROM reference_clips rc WHERE rc.voice_id=v.voice_id) AS clip_count, "
                "(SELECT COUNT(*) FROM speakers ms WHERE "
                "ms.entity_type='creature' AND ((v.scope='baseline' AND ms.race_id=v.race_id "
                "AND ms.gender_id=v.gender_id) OR "
                "(v.scope='unique' AND ms.voice_id=v.voice_id))) AS matching_speaker_count, "
                "(SELECT COUNT(*) FROM dialogue_entries md JOIN speakers ms ON ms.speaker_id=md.speaker_id "
                "WHERE md.active=1 AND ms.voice_id=v.voice_id) "
                "AS dialogue_count, "
                "(SELECT COUNT(*) FROM dialogue_entries md JOIN speakers ms ON ms.speaker_id=md.speaker_id "
                "LEFT JOIN production_assets mpa ON mpa.dialogue_id=md.dialogue_id "
                "WHERE md.active=1 AND mpa.dialogue_id IS NULL AND ms.voice_id=v.voice_id) "
                "AS missing_dialogue_count, "
                "(SELECT COUNT(*) FROM voice_delivery_presets vdp WHERE vdp.voice_id=v.voice_id "
                "AND vdp.status='approved') AS approved_delivery_count "
                ", (SELECT COUNT(*) FROM voice_id_candidates vic WHERE vic.voice_id=v.voice_id) "
                "AS voice_id_candidate_count "
                "FROM voices v JOIN voice_versions vv ON vv.voice_id=v.voice_id AND vv.is_current=1 "
                f"{where} ORDER BY v.scope, v.name",
                parameters,
            ).fetchall()
        return [_with_voice_lifecycle(dict(row)) for row in rows]

    def get_voice(self, voice_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            voice = connection.execute(
                "SELECT v.*, vv.version_id, vv.version_number, vv.description, vv.creation_method, "
                "vv.provider, vv.provider_voice_id, vv.model_id, vv.settings_json, "
                "vv.status AS stored_status "
                "FROM voices v JOIN voice_versions vv ON vv.voice_id=v.voice_id AND vv.is_current=1 "
                "WHERE v.voice_id=?",
                (voice_id,),
            ).fetchone()
            if not voice:
                raise AlphaError("Voice was not found.")
            versions = connection.execute(
                "SELECT * FROM voice_versions WHERE voice_id=? ORDER BY version_number DESC",
                (voice_id,),
            ).fetchall()
            clips = connection.execute(
                "SELECT * FROM reference_clips WHERE voice_id=? ORDER BY created_at DESC",
                (voice_id,),
            ).fetchall()
            previews = connection.execute(
                "SELECT * FROM voice_previews WHERE voice_id=? ORDER BY generation_number DESC",
                (voice_id,),
            ).fetchall()
            voice_id_candidates = connection.execute(
                "SELECT * FROM voice_id_candidates WHERE voice_id=? "
                "ORDER BY generation_number DESC",
                (voice_id,),
            ).fetchall()
            speakers = connection.execute(
                "SELECT speaker_id, entity_type, entity_id, name FROM speakers "
                "WHERE voice_id=? AND entity_type='creature' "
                "ORDER BY name",
                (voice_id,),
            ).fetchall()
            npc = None
            baseline_voice = None
            if voice["npc_speaker_id"]:
                npc = connection.execute(
                    "SELECT * FROM speakers WHERE speaker_id=?",
                    (voice["npc_speaker_id"],),
                ).fetchone()
                if npc:
                    baseline_voice = connection.execute(
                        "SELECT v.voice_id, v.name FROM voices v WHERE v.scope='baseline' "
                        "AND v.race_id=? AND v.gender_id=?",
                        (npc["race_id"], npc["gender_id"]),
                    ).fetchone()
            delivery_presets = connection.execute(
                "SELECT * FROM voice_delivery_presets WHERE voice_id=? "
                "ORDER BY CASE delivery WHEN 'neutral' THEN 0 WHEN 'angry' THEN 1 "
                "WHEN 'sorrowful' THEN 2 WHEN 'joyful' THEN 3 ELSE 4 END",
                (voice_id,),
            ).fetchall()
            delivery_previews = connection.execute(
                "SELECT * FROM voice_delivery_previews WHERE voice_id=? "
                "ORDER BY delivery, generation_number DESC",
                (voice_id,),
            ).fetchall()
        payload = dict(voice)
        payload["settings"] = _loads(payload["settings_json"], {})
        version_payloads = [dict(row) for row in versions]
        ascending = list(reversed(version_payloads))
        for index, version in enumerate(ascending):
            version["settings"] = _loads(version["settings_json"], {})
            version["delta"] = (
                self._voice_version_delta(ascending[index - 1], version) if index else []
            )
        payload["versions"] = list(reversed(ascending))
        payload["prompt_versions"] = []
        seen_prompts = {payload["description"]}
        for version in payload["versions"]:
            description = str(version.get("description") or "").strip()
            if description and description not in seen_prompts:
                payload["prompt_versions"].append(version)
                seen_prompts.add(description)
        payload["clips"] = [dict(row) for row in clips]
        payload["previews"] = [dict(row) for row in previews]
        payload["proposed_voice_groups"] = _candidate_groups(payload["previews"])
        payload["voice_id_candidates"] = []
        for row in voice_id_candidates:
            candidate = dict(row)
            candidate["subscription"] = _loads(candidate.get("subscription_json"), {})
            candidate["credit_cost"] = candidate["subscription"].get("request_character_cost")
            payload["voice_id_candidates"].append(candidate)
        voice_names = {
            candidate["provider_voice_id"]: candidate["display_name"]
            for candidate in payload["voice_id_candidates"]
            if candidate.get("display_name")
        }
        payload["voice_id_candidate_groups"] = _candidate_groups(payload["voice_id_candidates"])
        payload["speakers"] = [dict(row) for row in speakers]
        payload["npcs"] = payload["speakers"]
        payload["npc"] = dict(npc) if npc else None
        if payload["npc"]:
            payload["npc"]["importance_score"] = IMPORTANCE_SCORES.get(
                payload["npc"]["importance"], 0
            )
            payload["npc"]["is_unique_voice_active"] = payload["npc"].get("voice_id") == voice_id
        payload["baseline_voice"] = dict(baseline_voice) if baseline_voice else None
        preview_payloads = [dict(row) for row in delivery_previews]
        for preview in preview_payloads:
            preview["subscription"] = _loads(preview.get("subscription_json"), {})
            request = _loads(preview.get("request_json"), {})
            preview["request"] = request
            preview.update(_delivery_preview_metadata(request, preview["sample_text"]))
            preview["baseline_voice_name"] = voice_names.get(preview["baseline_voice_id"], "")
        payload["delivery_presets"] = []
        for row in delivery_presets:
            preset = dict(row)
            preset["effective_provider_voice_id"] = (
                preset.get("provider_voice_id") or payload.get("provider_voice_id") or ""
            )
            preset["previews"] = [
                preview for preview in preview_payloads if preview["delivery"] == preset["delivery"]
            ]
            payload["delivery_presets"].append(preset)
        summary = next(
            item for item in self.list_voices(include_retired=True) if item["voice_id"] == voice_id
        )
        payload.update(
            {
                key: summary[key]
                for key in (
                    "matching_speaker_count",
                    "dialogue_count",
                    "missing_dialogue_count",
                    "approved_delivery_count",
                    "deployed_dialogue_count",
                    "status",
                    "lifecycle_reason",
                )
            }
        )
        return payload

    @staticmethod
    def _voice_version_delta(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
        delta = []
        labels = {
            "description": "Voice description",
            "creation_method": "Creation method",
            "provider_voice_id": "Provider voice",
            "model_id": "Speech model",
        }
        for key, label in labels.items():
            if previous.get(key) != current.get(key):
                delta.append(label)
        previous_settings = previous.get("settings") or _loads(previous.get("settings_json"), {})
        current_settings = current.get("settings") or _loads(current.get("settings_json"), {})
        for key in sorted(set(previous_settings) | set(current_settings)):
            if previous_settings.get(key) != current_settings.get(key):
                delta.append(key.replace("_", " ").title())
        return delta

    def update_voice(
        self,
        voice_id: str,
        payload: dict[str, Any],
        *,
        _stored_status: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_voice(voice_id)
        if "status" in payload:
            raise AlphaError("Voice lifecycle status is computed automatically.")
        method = str(payload.get("creation_method", current["creation_method"]))
        if method not in VOICE_METHODS:
            raise AlphaError("Unknown voice creation method.")
        stored_status = _stored_status or current["stored_status"]
        description = str(payload.get("description", current["description"])).strip()
        if not 20 <= len(description) <= 5000:
            raise AlphaError("Voice description must contain 20–5,000 characters.")
        model_id = (
            str(payload.get("model_id", self.get_app_settings()["tts_model_id"])).strip()
            or "eleven_v3"
        )
        provider_voice_id = str(
            payload.get("provider_voice_id", current["provider_voice_id"] or "")
        ).strip()
        settings = current["settings"].copy()
        for key in ("stability", "similarity_boost", "style", "speed"):
            if key in payload:
                settings[key] = float(payload[key])
        if "use_speaker_boost" in payload:
            settings["use_speaker_boost"] = bool(payload["use_speaker_boost"])
        unchanged = (
            description == current["description"]
            and method == current["creation_method"]
            and stored_status == current["stored_status"]
            and provider_voice_id == (current["provider_voice_id"] or "")
            and model_id == current["model_id"]
            and settings == current["settings"]
        )
        if unchanged:
            current["version_changed"] = False
            return current
        now = utc_now()
        with self.connect() as connection:
            next_version = connection.execute(
                "SELECT COALESCE(MAX(version_number), 0)+1 FROM voice_versions WHERE voice_id=?",
                (voice_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE voice_versions SET is_current=0 WHERE voice_id=?", (voice_id,)
            )
            connection.execute(
                "INSERT INTO voice_versions(voice_id, version_number, description, creation_method, "
                "provider, provider_voice_id, model_id, settings_json, status, created_at) "
                "VALUES (?, ?, ?, ?, 'elevenlabs', ?, ?, ?, ?, ?)",
                (
                    voice_id,
                    next_version,
                    description,
                    method,
                    provider_voice_id or None,
                    model_id,
                    _json(settings),
                    stored_status,
                    now,
                ),
            )
            connection.execute("UPDATE voices SET updated_at=? WHERE voice_id=?", (now, voice_id))
            existing_candidate = (
                connection.execute(
                    "SELECT 1 FROM voice_id_candidates WHERE provider_voice_id=?",
                    (provider_voice_id,),
                ).fetchone()
                if provider_voice_id
                else None
            )
            if provider_voice_id and not existing_candidate:
                generation_number = self._reserve_candidate_numbers(connection, voice_id)[0]
                creation_model_id = "instant_voice_clone" if method == "instant_clone" else model_id
                connection.execute(
                    "INSERT INTO voice_id_candidates(candidate_id, voice_id, "
                    "provider_voice_id, generation_number, creation_method, creation_model_id, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        voice_id,
                        provider_voice_id,
                        generation_number,
                        method,
                        creation_model_id,
                        now,
                    ),
                )
            self._pin_delivery_presets_to_current_voice(connection)
        updated = self.get_voice(voice_id)
        updated["version_changed"] = True
        return updated

    def save_reference_clip(
        self,
        voice_id: str,
        *,
        original_name: str,
        content: bytes,
        provenance: str,
        provider_eligible: bool,
    ) -> dict[str, Any]:
        if not content or len(content) > MAX_REFERENCE_BYTES:
            raise AlphaError("Reference audio must contain 1 byte–50 MB.")
        self.get_voice(voice_id)
        clip_id = uuid.uuid4().hex
        folder = self.storage_root / "reference-clips" / voice_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{clip_id}-{_safe_filename(original_name)}"
        path.write_bytes(content)
        duration = _audio_duration(path)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO reference_clips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    clip_id,
                    voice_id,
                    original_name[:255],
                    str(path),
                    sha256_bytes(content),
                    duration,
                    provenance[:2000].strip(),
                    int(provider_eligible),
                    utc_now(),
                ),
            )
        return self.get_voice(voice_id)

    def delete_reference_clip(self, clip_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT voice_id, storage_path FROM reference_clips WHERE clip_id=?", (clip_id,)
            ).fetchone()
        if not row:
            raise AlphaError("Reference clip was not found.")
        path = Path(row["storage_path"]).resolve()
        if self.storage_root not in path.parents:
            raise AlphaError("Reference clip storage path is invalid.")
        temporary_path = path.with_name(f".{path.name}.deleting")
        if path.is_file():
            path.replace(temporary_path)
        try:
            with self.connect() as connection:
                connection.execute("DELETE FROM reference_clips WHERE clip_id=?", (clip_id,))
        except Exception:
            if temporary_path.is_file():
                temporary_path.replace(path)
            raise
        temporary_path.unlink(missing_ok=True)
        return self.get_voice(row["voice_id"])

    def get_voice_id_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM voice_id_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        if not row:
            raise AlphaError("Voice ID candidate was not found.")
        candidate = dict(row)
        candidate["subscription"] = _loads(candidate.get("subscription_json"), {})
        return candidate

    def update_voice_id_candidate_name(
        self, candidate_id: str, display_name: Any
    ) -> dict[str, Any]:
        candidate = self.get_voice_id_candidate(candidate_id)
        normalized = _normalize_display_name(display_name)
        with self.connect() as connection:
            connection.execute(
                "UPDATE voice_id_candidates SET display_name=? WHERE candidate_id=?",
                (normalized, candidate_id),
            )
        return self.get_voice(candidate["voice_id"])

    def record_voice_id_candidate(
        self,
        voice_id: str,
        *,
        provider_voice_id: str,
        creation_method: str,
        creation_model_id: str,
        sample_text: str = "",
        sample_model_id: str = "",
        content: bytes | None = None,
        provider_request_id: str | None = None,
        subscription: dict[str, Any] | None = None,
        generation_number: int | None = None,
    ) -> dict[str, Any]:
        self.get_voice(voice_id)
        provider_voice_id = provider_voice_id.strip()
        if not provider_voice_id:
            raise AlphaError("A reusable provider voice ID is required.")
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT candidate_id FROM voice_id_candidates WHERE provider_voice_id=?",
                (provider_voice_id,),
            ).fetchone()
        if existing:
            return self.get_voice_id_candidate(existing["candidate_id"])

        candidate_id = uuid.uuid4().hex
        path: Path | None = None
        duration: float | None = None
        if content is not None:
            folder = self.storage_root / "voice-id-candidates" / voice_id
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{candidate_id}.mp3"
            path.write_bytes(content)
            duration = _audio_duration(path)
            if duration is not None and duration <= 0:
                path.unlink(missing_ok=True)
                raise AlphaError("The reusable voice audition audio could not be validated.")
        try:
            with self.connect() as connection:
                if generation_number is None:
                    assigned_number = self._reserve_candidate_numbers(connection, voice_id)[0]
                else:
                    assigned_number = int(generation_number)
                    if assigned_number < 1:
                        raise AlphaError("Candidate production number must be positive.")
                    connection.execute(
                        "UPDATE voices SET candidate_sequence=MAX(candidate_sequence, ?) "
                        "WHERE voice_id=?",
                        (assigned_number, voice_id),
                    )
                connection.execute(
                    "INSERT INTO voice_id_candidates(candidate_id, voice_id, provider_voice_id, "
                    "generation_number, creation_method, creation_model_id, sample_storage_path, "
                    "sample_sha256, sample_duration_seconds, sample_text, sample_model_id, "
                    "provider_request_id, subscription_json, created_at) VALUES (?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        voice_id,
                        provider_voice_id,
                        assigned_number,
                        creation_method,
                        creation_model_id,
                        str(path) if path else None,
                        sha256_bytes(content) if content is not None else "",
                        duration,
                        sample_text.strip() if content is not None else "",
                        sample_model_id.strip() if content is not None else "",
                        provider_request_id,
                        _json(subscription or {}),
                        utc_now(),
                    ),
                )
        except Exception:
            if path:
                path.unlink(missing_ok=True)
            raise
        return self.get_voice_id_candidate(candidate_id)

    def attach_voice_id_candidate_sample(
        self,
        candidate_id: str,
        *,
        sample_text: str,
        sample_model_id: str,
        content: bytes,
        provider_request_id: str | None,
        subscription: dict[str, Any] | None,
    ) -> dict[str, Any]:
        candidate = self.get_voice_id_candidate(candidate_id)
        folder = self.storage_root / "voice-id-candidates" / candidate["voice_id"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{candidate_id}.mp3"
        temporary_path = folder / f".{candidate_id}.{uuid.uuid4().hex}.mp3"
        temporary_path.write_bytes(content)
        duration = _audio_duration(temporary_path)
        if duration is not None and duration <= 0:
            temporary_path.unlink(missing_ok=True)
            raise AlphaError("The reusable voice audition audio could not be validated.")
        old_path = (
            Path(candidate["sample_storage_path"]).resolve()
            if candidate.get("sample_storage_path")
            else None
        )
        temporary_path.replace(path)
        with self.connect() as connection:
            connection.execute(
                "UPDATE voice_id_candidates SET sample_storage_path=?, sample_sha256=?, "
                "sample_duration_seconds=?, sample_text=?, sample_model_id=?, "
                "provider_request_id=?, subscription_json=? WHERE candidate_id=?",
                (
                    str(path),
                    sha256_bytes(content),
                    duration,
                    sample_text.strip(),
                    sample_model_id.strip(),
                    provider_request_id,
                    _json(subscription or {}),
                    candidate_id,
                ),
            )
        if old_path and old_path != path and self.storage_root in old_path.parents:
            old_path.unlink(missing_ok=True)
        return self.get_voice_id_candidate(candidate_id)

    def voice_id_candidate_path(self, candidate_id: str) -> Path:
        candidate = self.get_voice_id_candidate(candidate_id)
        if not candidate.get("sample_storage_path"):
            raise AlphaError("This voice ID candidate does not have an audition sample yet.")
        path = Path(candidate["sample_storage_path"]).resolve()
        if self.storage_root not in path.parents or not path.is_file():
            raise AlphaError("Voice ID candidate audition audio is unavailable.")
        return path

    def connect_voice_id_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Keep legacy voice metadata current without making the candidate a UI default."""
        candidate = self.get_voice_id_candidate(candidate_id)
        voice = self.get_voice(candidate["voice_id"])
        return self.update_voice(
            candidate["voice_id"],
            {
                "description": voice["description"],
                "creation_method": candidate["creation_method"],
                "provider_voice_id": candidate["provider_voice_id"],
                "model_id": voice["model_id"],
            },
        )

    def restore_voice_prompt(self, voice_id: str, version_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            version = connection.execute(
                "SELECT description FROM voice_versions WHERE voice_id=? AND version_id=?",
                (voice_id, version_id),
            ).fetchone()
        if not version:
            raise AlphaError("Voice prompt version was not found.")
        current = self.get_voice(voice_id)
        return self.update_voice(
            voice_id,
            {
                "description": version["description"],
                "creation_method": current["creation_method"],
                "provider_voice_id": current["provider_voice_id"] or "",
                "model_id": current["model_id"],
            },
        )

    def delete_voice_id_candidate(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.get_voice_id_candidate(candidate_id)
        voice = self.get_voice(candidate["voice_id"])
        provider_voice_id = candidate["provider_voice_id"]
        is_current_legacy_link = provider_voice_id == (voice.get("provider_voice_id") or "")
        affected_deliveries = [
            preset["delivery"]
            for preset in voice["delivery_presets"]
            if preset.get("provider_voice_id") == provider_voice_id
            or (is_current_legacy_link and not preset.get("provider_voice_id"))
        ]
        path = (
            Path(candidate["sample_storage_path"]).resolve()
            if candidate.get("sample_storage_path")
            else None
        )
        if path and self.storage_root not in path.parents:
            raise AlphaError("Voice ID candidate storage path is invalid.")
        temporary_path = path.with_name(f".{path.name}.deleting") if path else None
        if path and path.is_file():
            path.replace(temporary_path)
        try:
            if is_current_legacy_link:
                self.update_voice(
                    candidate["voice_id"],
                    {
                        "description": voice["description"],
                        "creation_method": voice["creation_method"],
                        "provider_voice_id": "",
                        "model_id": voice["model_id"],
                    },
                )
            with self.connect() as connection:
                if affected_deliveries:
                    placeholders = ",".join("?" for _ in affected_deliveries)
                    connection.execute(
                        "UPDATE voice_delivery_presets SET provider_voice_id='', "
                        "status=CASE WHEN EXISTS (SELECT 1 FROM voice_delivery_previews vdpv "
                        "WHERE vdpv.voice_id=voice_delivery_presets.voice_id "
                        "AND vdpv.delivery=voice_delivery_presets.delivery) "
                        "THEN 'previewed' ELSE 'not_tested' END, updated_at=? "
                        f"WHERE voice_id=? AND delivery IN ({placeholders})",
                        (utc_now(), candidate["voice_id"], *affected_deliveries),
                    )
                connection.execute(
                    "DELETE FROM voice_id_candidates WHERE candidate_id=?", (candidate_id,)
                )
        except Exception:
            if temporary_path and temporary_path.is_file():
                temporary_path.replace(path)
            raise
        if temporary_path:
            temporary_path.unlink(missing_ok=True)
        revised = self.get_voice(candidate["voice_id"])
        revised["affected_delivery_count"] = len(affected_deliveries)
        revised["cleared_legacy_voice_link"] = is_current_legacy_link
        return revised

    def delete_voice_preview(self, preview_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT voice_id, storage_path, status FROM voice_previews WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
        if not row:
            raise AlphaError("Voice candidate was not found.")
        path = Path(row["storage_path"]).resolve()
        if self.storage_root not in path.parents:
            raise AlphaError("Voice candidate storage path is invalid.")
        temporary_path = path.with_name(f".{path.name}.deleting")
        if path.is_file():
            path.replace(temporary_path)
        try:
            with self.connect() as connection:
                connection.execute("DELETE FROM voice_previews WHERE preview_id=?", (preview_id,))
        except Exception:
            if temporary_path.is_file():
                temporary_path.replace(path)
            raise
        temporary_path.unlink(missing_ok=True)
        return self.get_voice(row["voice_id"])

    def record_voice_previews(
        self,
        voice_id: str,
        *,
        prompt: str,
        preview_text: str,
        model_id: str,
        previews: list[dict[str, Any]],
        creation_method: str = "legacy_unknown",
        replace_existing: bool = False,
    ) -> list[str]:
        if not previews:
            raise AlphaError("At least one generated voice candidate is required.")
        if creation_method not in {"designed", "reference_design", "legacy_unknown"}:
            raise AlphaError("Unknown Voice Design candidate method.")
        self.get_voice(voice_id)
        folder = self.storage_root / "voice-previews" / voice_id
        folder.mkdir(parents=True, exist_ok=True)
        preview_ids = []
        new_paths: list[Path] = []
        replaced_rows: list[sqlite3.Row] = []
        now = utc_now()
        try:
            with self.connect() as connection:
                generation_numbers = self._reserve_candidate_numbers(
                    connection, voice_id, len(previews)
                )
                if replace_existing:
                    replaced_rows = list(
                        connection.execute(
                            "SELECT preview_id, storage_path FROM voice_previews WHERE voice_id=?",
                            (voice_id,),
                        )
                    )
                for preview, generation_number in zip(previews, generation_numbers, strict=True):
                    content = preview["content"]
                    preview_id = uuid.uuid4().hex
                    path = folder / f"{preview_id}.mp3"
                    path.write_bytes(content)
                    new_paths.append(path)
                    connection.execute(
                        "INSERT INTO voice_previews(preview_id, voice_id, generated_voice_id, "
                        "storage_path, sha256, duration_seconds, prompt, preview_text, model_id, "
                        "creation_method, generation_number, status, created_at) VALUES (?, ?, ?, "
                        "?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)",
                        (
                            preview_id,
                            voice_id,
                            preview["generated_voice_id"],
                            str(path),
                            sha256_bytes(content),
                            _audio_duration(path),
                            prompt,
                            preview_text,
                            model_id,
                            creation_method,
                            generation_number,
                            now,
                        ),
                    )
                    preview_ids.append(preview_id)
                if replaced_rows:
                    connection.executemany(
                        "DELETE FROM voice_previews WHERE preview_id=?",
                        ((row["preview_id"],) for row in replaced_rows),
                    )
        except Exception:
            for path in new_paths:
                path.unlink(missing_ok=True)
            raise

        for row in replaced_rows:
            path = Path(row["storage_path"]).resolve()
            if self.storage_root in path.parents:
                path.unlink(missing_ok=True)
        return preview_ids

    def get_reference_clip(self, clip_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reference_clips WHERE clip_id=?", (clip_id,)
            ).fetchone()
        if not row:
            raise AlphaError("Reference clip was not found.")
        payload = dict(row)
        path = Path(payload["storage_path"]).resolve()
        if self.storage_root not in path.parents or not path.is_file():
            raise AlphaError("Reference clip file is unavailable.")
        payload["path"] = path
        return payload

    def reference_path(self, clip_id: str) -> Path:
        return self.get_reference_clip(clip_id)["path"]

    def get_voice_preview(self, preview_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT vp.*, v.name AS voice_name, vv.description FROM voice_previews vp "
                "JOIN voices v ON v.voice_id=vp.voice_id "
                "JOIN voice_versions vv ON vv.voice_id=v.voice_id AND vv.is_current=1 "
                "WHERE vp.preview_id=?",
                (preview_id,),
            ).fetchone()
        if not row:
            raise AlphaError("Voice preview was not found.")
        return dict(row)

    def activate_voice_preview(self, preview_id: str, provider_voice_id: str) -> dict[str, Any]:
        preview = self.get_voice_preview(preview_id)
        creation_method = (
            preview["creation_method"]
            if preview["creation_method"] in {"designed", "reference_design"}
            else "designed"
        )
        candidate = self.record_voice_id_candidate(
            preview["voice_id"],
            provider_voice_id=provider_voice_id,
            creation_method=creation_method,
            creation_model_id=preview["model_id"],
            sample_text=preview["preview_text"],
            sample_model_id=preview["model_id"],
            content=Path(preview["storage_path"]).read_bytes(),
            generation_number=preview["generation_number"],
        )
        self.connect_voice_id_candidate(candidate["candidate_id"])
        with self.connect() as connection:
            discarded_rows = connection.execute(
                "SELECT storage_path FROM voice_previews WHERE voice_id=?", (preview["voice_id"],)
            ).fetchall()
            connection.execute(
                "DELETE FROM voice_previews WHERE voice_id=?", (preview["voice_id"],)
            )
        for row in discarded_rows:
            path = Path(row["storage_path"]).resolve()
            if self.storage_root in path.parents:
                path.unlink(missing_ok=True)
        voice = self.get_voice(preview["voice_id"])
        voice["discarded_preview_count"] = len(discarded_rows)
        voice["created_voice_id_candidate"] = candidate["candidate_id"]
        return voice

    def supersede_selected_voice_previews(self, voice_id: str) -> dict[str, Any]:
        """Keep prior design audio for comparison without presenting it as the active voice."""
        self.get_voice(voice_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE voice_previews SET status='superseded' "
                "WHERE voice_id=? AND status='selected'",
                (voice_id,),
            )
        return self.get_voice(voice_id)

    def retain_voice_preview(self, preview_id: str) -> dict[str, Any]:
        """Keep one selected preview and remove every other local candidate for its voice."""
        preview = self.get_voice_preview(preview_id)
        if preview["status"] != "selected":
            raise AlphaError("Only a selected voice candidate can be retained.")
        with self.connect() as connection:
            discarded_rows = list(
                connection.execute(
                    "SELECT preview_id, storage_path FROM voice_previews "
                    "WHERE voice_id=? AND preview_id<>?",
                    (preview["voice_id"], preview_id),
                )
            )
        temporary_paths: list[tuple[Path, Path]] = []
        try:
            for row in discarded_rows:
                path = Path(row["storage_path"]).resolve()
                if self.storage_root not in path.parents:
                    raise AlphaError("Voice candidate storage path is invalid.")
                if not path.is_file():
                    continue
                temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.deleting")
                path.replace(temporary_path)
                temporary_paths.append((path, temporary_path))
            with self.connect() as connection:
                connection.execute(
                    "DELETE FROM voice_previews WHERE voice_id=? AND preview_id<>?",
                    (preview["voice_id"], preview_id),
                )
        except Exception:
            for path, temporary_path in reversed(temporary_paths):
                if temporary_path.is_file():
                    temporary_path.replace(path)
            raise
        for _, temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)
        voice = self.get_voice(preview["voice_id"])
        voice["discarded_preview_count"] = len(discarded_rows)
        return voice

    def generation_text(self, dialogue: dict[str, Any]) -> str:
        text = str(dialogue.get("spoken_text") or "")
        delivery = str(dialogue.get("delivery") or "neutral")
        voice_id = str(dialogue.get("voice_id") or "")
        with self.connect() as connection:
            preset = connection.execute(
                "SELECT prompt_tag FROM voice_delivery_presets WHERE voice_id=? AND delivery=?",
                (voice_id, delivery),
            ).fetchone()
        prompt_tag = preset["prompt_tag"] if preset else ""
        return _delivery_request_text(prompt_tag, text)

    def begin_generation(self, dialogue_id: str) -> dict[str, Any]:
        dialogue = self.get_dialogue(dialogue_id)
        if not dialogue.get("revision_id"):
            raise AlphaError("Prepare and review spoken text before generating audio.")
        if dialogue.get("warnings"):
            raise AlphaError("Resolve the spoken-text warnings before generating audio.")
        if not dialogue.get("voice_id") or not dialogue.get("provider_voice_id"):
            raise AlphaError("Assign an active provider voice before generating audio.")
        request_text = self.generation_text(dialogue)
        app_settings = self.get_app_settings()
        model_id = app_settings["tts_model_id"]
        voice_settings = dialogue["voice_settings"].copy()
        provider_voice_id = dialogue["provider_voice_id"]
        with self.connect() as connection:
            preset = connection.execute(
                "SELECT stability, provider_voice_id FROM voice_delivery_presets "
                "WHERE voice_id=? AND delivery=?",
                (dialogue["voice_id"], dialogue["delivery"]),
            ).fetchone()
        if preset and preset["provider_voice_id"]:
            provider_voice_id = preset["provider_voice_id"]
        if model_id == "eleven_v3":
            voice_settings = {"stability": float(preset["stability"] if preset else 0.5)}
        request_payload = {
            "text": request_text,
            "voice_id": provider_voice_id,
            "model_id": model_id,
            "voice_settings": voice_settings,
        }
        generation_id = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO generations(generation_id, dialogue_id, revision_id, voice_id, "
                "voice_version_id, provider, model_id, delivery, request_text, request_json, "
                "character_count, status, created_at) VALUES (?, ?, ?, ?, ?, 'elevenlabs', ?, ?, ?, ?, ?, "
                "'requested', ?)",
                (
                    generation_id,
                    dialogue_id,
                    dialogue["revision_id"],
                    dialogue["voice_id"],
                    dialogue["voice_version_id"],
                    model_id,
                    dialogue["delivery"],
                    request_text,
                    _json(request_payload),
                    len(request_text),
                    utc_now(),
                ),
            )
        return {"generation_id": generation_id, **request_payload}

    def fail_generation(self, generation_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE generations SET status='failed', error=?, completed_at=? WHERE generation_id=?",
                (error[:2000], utc_now(), generation_id),
            )

    def complete_generation(
        self,
        generation_id: str,
        *,
        content: bytes,
        mime_type: str,
        provider_request_id: str | None,
        subscription: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            generation = connection.execute(
                "SELECT * FROM generations WHERE generation_id=?", (generation_id,)
            ).fetchone()
            if not generation:
                raise AlphaError("Generation record was not found.")
        candidate_id = uuid.uuid4().hex
        folder = self.storage_root / "audio" / "candidates" / generation["dialogue_id"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{candidate_id}.mp3"
        path.write_bytes(content)
        duration = _audio_duration(path)
        if duration is None or duration <= 0:
            path.unlink(missing_ok=True)
            raise AlphaError("ElevenLabs returned audio whose duration could not be validated.")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE generations SET status='complete', provider_request_id=?, "
                "subscription_json=?, completed_at=? WHERE generation_id=?",
                (provider_request_id, _json(subscription or {}), now, generation_id),
            )
            connection.execute(
                "INSERT INTO audio_candidates VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, NULL)",
                (
                    candidate_id,
                    generation_id,
                    generation["dialogue_id"],
                    str(path),
                    sha256_bytes(content),
                    duration,
                    mime_type,
                    now,
                ),
            )
        return {"candidate_id": candidate_id, "duration_seconds": duration}

    def candidate_path(self, candidate_id: str) -> Path:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT storage_path FROM audio_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        if not row:
            raise AlphaError("Audio candidate was not found.")
        path = Path(row["storage_path"]).resolve()
        if self.storage_root not in path.parents or not path.is_file():
            raise AlphaError("Audio candidate file is unavailable.")
        return path

    def preview_path(self, preview_id: str) -> Path:
        preview = self.get_voice_preview(preview_id)
        path = Path(preview["storage_path"]).resolve()
        if self.storage_root not in path.parents or not path.is_file():
            raise AlphaError("Voice preview file is unavailable.")
        return path

    def approve_candidate(self, candidate_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT ac.*, d.addon_file_key, d.source FROM audio_candidates ac "
                "JOIN dialogue_entries d ON d.dialogue_id=ac.dialogue_id "
                "WHERE ac.candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if not row:
                raise AlphaError("Audio candidate was not found.")
            connection.execute(
                "UPDATE audio_candidates SET status='rejected', reviewed_at=? "
                "WHERE dialogue_id=? AND candidate_id<>? AND status='pending_review'",
                (now, row["dialogue_id"], candidate_id),
            )
            connection.execute(
                "UPDATE audio_candidates SET status='approved', reviewed_at=? WHERE candidate_id=?",
                (now, candidate_id),
            )
            folder = "quests" if row["source"] != "gossip" else "gossip"
            addon_filename = f"generated/sounds/{folder}/{row['addon_file_key']}.mp3"
            connection.execute(
                "INSERT INTO production_assets(dialogue_id, candidate_id, addon_filename, sha256, "
                "duration_seconds, approved_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(dialogue_id) DO UPDATE SET candidate_id=excluded.candidate_id, "
                "addon_filename=excluded.addon_filename, sha256=excluded.sha256, "
                "duration_seconds=excluded.duration_seconds, approved_at=excluded.approved_at",
                (
                    row["dialogue_id"],
                    candidate_id,
                    addon_filename,
                    row["sha256"],
                    row["duration_seconds"],
                    now,
                ),
            )
        return self.get_dialogue(row["dialogue_id"])

    def export_manifest(self) -> dict[str, Any]:
        with self.connect() as connection:
            snapshots = connection.execute(
                "SELECT * FROM source_snapshots WHERE is_active=1 "
                "ORDER BY imported_at DESC, expansion, locale"
            ).fetchall()
            rows = connection.execute(
                "SELECT pa.*, d.expansion, d.locale, d.source, d.quest_id, d.quest_title, "
                "d.original_text, d.speaker_id, s.name AS speaker_name, ac.storage_path "
                "FROM production_assets pa "
                "JOIN dialogue_entries d ON d.dialogue_id=pa.dialogue_id "
                "JOIN speakers s ON s.speaker_id=d.speaker_id "
                "JOIN audio_candidates ac ON ac.candidate_id=pa.candidate_id "
                "ORDER BY pa.addon_filename"
            ).fetchall()
        assets = [dict(row) for row in rows]
        for asset in assets:
            asset["package_path"] = (
                f"{asset['expansion']}/{asset['locale']}/{asset['addon_filename']}"
            )
        return {
            "schema_version": 1,
            "generated_at": utc_now(),
            "source_snapshot": dict(snapshots[0]) if snapshots else None,
            "source_snapshots": [dict(snapshot) for snapshot in snapshots],
            "asset_count": len(rows),
            "assets": assets,
        }
