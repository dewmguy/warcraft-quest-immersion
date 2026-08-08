from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

PROFILE_STAGES = [
    {
        "id": "identity_defined",
        "name": "Identity defined",
        "description": "The race/gender identity, cadence, timbre, pacing, and guardrails are scoped.",
    },
    {
        "id": "source_strategy",
        "name": "Source strategy selected",
        "description": "Choose designed voice, reference-assisted design, instant clone, or existing voice.",
    },
    {
        "id": "neutral_candidate",
        "name": "Neutral candidate prepared",
        "description": "A neutral comparison candidate is attached. No emotional tuning occurs yet.",
    },
    {
        "id": "neutral_approved",
        "name": "Neutral identity approved",
        "description": "The underlying speaker is accepted before delivery variants are evaluated.",
    },
    {
        "id": "delivery_calibrated",
        "name": "Delivery presets calibrated",
        "description": "Angry, sorrowful, joyful, and proclaiming retain the approved identity.",
    },
    {
        "id": "baseline_ready",
        "name": "Baseline ready",
        "description": "The versioned baseline is eligible for later line-level generation.",
    },
]

LINE_STAGES = [
    {
        "id": "imported_checked",
        "name": "Imported sample checked",
        "description": "Confirm whether inherited audio exists and whether it remains usable.",
    },
    {
        "id": "text_processed",
        "name": "Text processed",
        "description": "Review a speech-safe suggestion beside the immutable original.",
    },
    {
        "id": "short_baseline",
        "name": "Short baseline preview",
        "description": "Review a bounded sample using the approved race/gender baseline.",
    },
    {
        "id": "complete_baseline",
        "name": "Complete baseline preview",
        "description": "Review the full line using the baseline voice.",
    },
    {
        "id": "short_unique",
        "name": "Short unique preview",
        "description": "Conditional NPC-specific voice fork with explicit parent lineage.",
    },
    {
        "id": "complete_unique",
        "name": "Complete unique preview",
        "description": "Conditional full line using the accepted unique fork.",
    },
    {
        "id": "production_approved",
        "name": "Production approved",
        "description": "Promote the exact reviewed file without another generation.",
    },
]

SOURCE_STRATEGIES = {
    "undecided": "Undecided",
    "voice_design": "Voice Design",
    "reference_design": "Reference-assisted Voice Design",
    "instant_clone": "Instant Voice Clone",
    "existing_voice": "Existing ElevenLabs voice",
}
DELIVERY_PRESETS = {"neutral", "angry", "sorrowful", "joyful", "proclaiming"}
ROUTES = {"baseline", "unique"}

DEFAULT_DEMO_TEXT = (
    "The mountain road is dangerous after nightfall. Stay near the lanterns, traveler."
)


class WorkflowError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class WorkflowPoc:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS profile_settings (
                    profile_id TEXT PRIMARY KEY,
                    settings_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS demo_lines (
                    line_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL UNIQUE,
                    original_text TEXT NOT NULL,
                    processed_text TEXT NOT NULL,
                    delivery_preset TEXT NOT NULL,
                    route TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stage_states (
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    stage_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(entity_type, entity_id, stage_id)
                );
                CREATE TABLE IF NOT EXISTS stage_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    stage_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            for profile_id in ("dwarf-male", "dwarf-female"):
                self._seed_profile(connection, profile_id)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _seed_profile(self, connection: sqlite3.Connection, profile_id: str) -> None:
        default_settings = {
            "source_strategy": "undecided",
            "reference_label": "",
            "model_target": "eleven_v3",
            "stability_mode": "natural",
            "output_format": "mp3_44100_128",
            "neutral_script": (
                "The quick brown fox jumps over the lazy dog. Beyond the old road, a traveler "
                "waits beside the quiet fire."
            ),
            "enabled_deliveries": [
                "neutral",
                "angry",
                "sorrowful",
                "joyful",
                "proclaiming",
            ],
            "design_notes": "",
        }
        now = utc_now()
        connection.execute(
            "INSERT OR IGNORE INTO profile_settings VALUES (?, ?, ?)",
            (profile_id, json.dumps(default_settings, sort_keys=True), now),
        )
        line_id = f"{profile_id}-proof-line"
        connection.execute(
            "INSERT OR IGNORE INTO demo_lines VALUES (?, ?, ?, ?, 'neutral', 'baseline', ?)",
            (line_id, profile_id, DEFAULT_DEMO_TEXT, DEFAULT_DEMO_TEXT, now),
        )
        self._seed_stages(connection, "profile", profile_id, PROFILE_STAGES, now)
        self._seed_stages(connection, "line", line_id, LINE_STAGES, now)

    @staticmethod
    def _seed_stages(
        connection: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        stages: list[dict],
        now: str,
    ) -> None:
        for index, stage in enumerate(stages):
            connection.execute(
                "INSERT OR IGNORE INTO stage_states VALUES (?, ?, ?, ?, '', ?)",
                (
                    entity_type,
                    entity_id,
                    stage["id"],
                    "current" if index == 0 else "not_started",
                    now,
                ),
            )

    @staticmethod
    def _stage_definitions(entity_type: str) -> list[dict]:
        if entity_type == "profile":
            return PROFILE_STAGES
        if entity_type == "line":
            return LINE_STAGES
        raise WorkflowError("Unknown workflow entity type.")

    @staticmethod
    def _profile_map(review: dict) -> dict[str, dict]:
        return {
            profile["profile_id"]: profile
            for profile in review["profiles"]
            if profile["profile_id"] in {"dwarf-male", "dwarf-female"}
        }

    def bundle(self, review: dict) -> dict:
        profiles = self._profile_map(review)
        with self.connect() as connection:
            settings_rows = connection.execute("SELECT * FROM profile_settings").fetchall()
            line_rows = connection.execute("SELECT * FROM demo_lines").fetchall()
            state_rows = connection.execute("SELECT * FROM stage_states").fetchall()
            event_rows = connection.execute(
                "SELECT * FROM stage_events ORDER BY event_id DESC LIMIT 100"
            ).fetchall()
        settings = {
            row["profile_id"]: {**json.loads(row["settings_json"]), "updated_at": row["updated_at"]}
            for row in settings_rows
        }
        lines = {row["profile_id"]: dict(row) for row in line_rows}
        states: dict[tuple[str, str], dict[str, dict]] = {}
        for row in state_rows:
            states.setdefault((row["entity_type"], row["entity_id"]), {})[row["stage_id"]] = dict(
                row
            )
        events_by_profile = {profile_id: [] for profile_id in profiles}
        line_to_profile = {line["line_id"]: profile_id for profile_id, line in lines.items()}
        for row in event_rows:
            event = dict(row)
            profile_id = (
                event["entity_id"]
                if event["entity_type"] == "profile"
                else line_to_profile.get(event["entity_id"])
            )
            if profile_id in events_by_profile:
                events_by_profile[profile_id].append(event)

        output = []
        for profile_id, profile in profiles.items():
            line = lines[profile_id]
            profile_stages = [
                {**definition, **states[("profile", profile_id)][definition["id"]]}
                for definition in PROFILE_STAGES
            ]
            line_stages = [
                {**definition, **states[("line", line["line_id"])][definition["id"]]}
                for definition in LINE_STAGES
            ]
            profile_settings = settings[profile_id]
            character_estimate = len(profile_settings["neutral_script"]) + sum(
                len(review["scripts_by_preset"][preset]["text"])
                for preset in profile_settings["enabled_deliveries"]
                if preset != "neutral"
            )
            output.append(
                {
                    "profile": profile,
                    "settings": profile_settings,
                    "profile_stages": profile_stages,
                    "demo_line": line,
                    "line_stages": line_stages,
                    "history": events_by_profile[profile_id],
                    "character_estimate": character_estimate,
                    "source_strategy_name": SOURCE_STRATEGIES[profile_settings["source_strategy"]],
                }
            )
        return {
            "project_phase": {
                "number": 2,
                "name": "Voice Workbench",
                "status": "proof_of_concept",
                "scope": "Dwarf Male and Dwarf Female",
            },
            "profiles": sorted(output, key=lambda item: item["profile"]["gender_id"]),
            "source_strategies": SOURCE_STRATEGIES,
            "no_audio_mode": True,
        }

    def save_settings(self, profile_id: str, payload: dict) -> dict:
        if profile_id not in {"dwarf-male", "dwarf-female"}:
            raise WorkflowError("The proof of concept is limited to Dwarf profiles.")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT settings_json FROM profile_settings WHERE profile_id = ?", (profile_id,)
            ).fetchone()
            if not row:
                raise WorkflowError("Unknown proof-of-concept profile.")
            settings = json.loads(row[0])
            strategy = str(payload.get("source_strategy", settings["source_strategy"]))
            if strategy not in SOURCE_STRATEGIES:
                raise WorkflowError("Unknown source strategy.")
            neutral_script = str(payload.get("neutral_script", settings["neutral_script"])).strip()
            if not 20 <= len(neutral_script) <= 1000:
                raise WorkflowError("Neutral comparison text must be 20–1,000 characters.")
            deliveries = payload.get("enabled_deliveries", settings["enabled_deliveries"])
            if (
                not isinstance(deliveries, list)
                or not deliveries
                or not set(deliveries) <= DELIVERY_PRESETS
            ):
                raise WorkflowError("Choose at least one recognized delivery preset.")
            if "neutral" not in deliveries:
                raise WorkflowError("Neutral must remain enabled as the identity gate.")
            settings.update(
                {
                    "source_strategy": strategy,
                    "reference_label": str(
                        payload.get("reference_label", settings["reference_label"])
                    )[:200].strip(),
                    "model_target": "eleven_v3",
                    "stability_mode": str(
                        payload.get("stability_mode", settings["stability_mode"])
                    ),
                    "output_format": "mp3_44100_128",
                    "neutral_script": neutral_script,
                    "enabled_deliveries": list(dict.fromkeys(deliveries)),
                    "design_notes": str(payload.get("design_notes", settings["design_notes"]))[
                        :2000
                    ].strip(),
                }
            )
            if settings["stability_mode"] not in {"creative", "natural", "robust"}:
                raise WorkflowError("Unknown stability mode.")
            now = utc_now()
            connection.execute(
                "UPDATE profile_settings SET settings_json = ?, updated_at = ? WHERE profile_id = ?",
                (json.dumps(settings, sort_keys=True), now, profile_id),
            )
        return {**settings, "updated_at": now}

    def save_demo_line(self, profile_id: str, payload: dict) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM demo_lines WHERE profile_id = ?", (profile_id,)
            ).fetchone()
            if not row:
                raise WorkflowError("Unknown demonstration line.")
            processed_text = str(payload.get("processed_text", row["processed_text"])).strip()
            if not processed_text or len(processed_text) > 5000:
                raise WorkflowError("Processed demonstration text must be 1–5,000 characters.")
            delivery = str(payload.get("delivery_preset", row["delivery_preset"]))
            route = str(payload.get("route", row["route"]))
            if delivery not in DELIVERY_PRESETS:
                raise WorkflowError("Unknown delivery preset.")
            if route not in ROUTES:
                raise WorkflowError("Unknown audio-production route.")
            now = utc_now()
            connection.execute(
                "UPDATE demo_lines SET processed_text = ?, delivery_preset = ?, route = ?, "
                "updated_at = ? WHERE profile_id = ?",
                (processed_text, delivery, route, now, profile_id),
            )
            updated = connection.execute(
                "SELECT * FROM demo_lines WHERE profile_id = ?", (profile_id,)
            ).fetchone()
        return dict(updated)

    def transition(
        self,
        *,
        entity_type: str,
        entity_id: str,
        stage_id: str,
        action: str,
        note: str = "",
    ) -> dict:
        stages = self._stage_definitions(entity_type)
        stage_ids = [stage["id"] for stage in stages]
        if stage_id not in stage_ids:
            raise WorkflowError("Unknown workflow stage.")
        if action not in {"approve", "request_changes", "reopen", "skip_unique"}:
            raise WorkflowError("Unknown workflow action.")
        if action == "skip_unique" and entity_type != "line":
            raise WorkflowError("Only per-line unique stages can be skipped.")
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stage_states WHERE entity_type = ? AND entity_id = ?",
                (entity_type, entity_id),
            ).fetchall()
            state = {row["stage_id"]: dict(row) for row in rows}
            if len(state) != len(stages):
                raise WorkflowError("Workflow state is incomplete.")
            current = state[stage_id]["status"]
            stage_index = stage_ids.index(stage_id)
            if action == "approve":
                if current not in {"current", "needs_changes"}:
                    raise WorkflowError("Only the active stage can be approved.")
                new_status = "approved"
                connection.execute(
                    "UPDATE stage_states SET status = 'approved', note = ?, updated_at = ? "
                    "WHERE entity_type = ? AND entity_id = ? AND stage_id = ?",
                    (note.strip(), now, entity_type, entity_id, stage_id),
                )
                if stage_index + 1 < len(stage_ids):
                    connection.execute(
                        "UPDATE stage_states SET status = 'current', updated_at = ? "
                        "WHERE entity_type = ? AND entity_id = ? AND stage_id = ?",
                        (now, entity_type, entity_id, stage_ids[stage_index + 1]),
                    )
            elif action == "skip_unique":
                if stage_id not in {"short_unique", "complete_unique"} or current != "current":
                    raise WorkflowError("Only the active unique-voice stages can be skipped.")
                new_status = "not_required"
                connection.execute(
                    "UPDATE stage_states SET status = 'not_required', note = ?, updated_at = ? "
                    "WHERE entity_type = ? AND entity_id = ? AND stage_id = ?",
                    (note.strip(), now, entity_type, entity_id, stage_id),
                )
                if stage_index + 1 < len(stage_ids):
                    connection.execute(
                        "UPDATE stage_states SET status = 'current', updated_at = ? "
                        "WHERE entity_type = ? AND entity_id = ? AND stage_id = ?",
                        (now, entity_type, entity_id, stage_ids[stage_index + 1]),
                    )
            else:
                if current == "not_started":
                    raise WorkflowError("A future stage cannot be changed before earlier gates.")
                if action == "reopen" and current not in {"approved", "not_required"}:
                    raise WorkflowError("Only a completed stage can be reopened.")
                new_status = "current" if action == "reopen" else "needs_changes"
                connection.execute(
                    "UPDATE stage_states SET status = ?, note = ?, updated_at = ? "
                    "WHERE entity_type = ? AND entity_id = ? AND stage_id = ?",
                    (new_status, note.strip(), now, entity_type, entity_id, stage_id),
                )
                for later_id in stage_ids[stage_index + 1 :]:
                    connection.execute(
                        "UPDATE stage_states SET status = 'not_started', note = '', updated_at = ? "
                        "WHERE entity_type = ? AND entity_id = ? AND stage_id = ?",
                        (now, entity_type, entity_id, later_id),
                    )
            connection.execute(
                "INSERT INTO stage_events(entity_type, entity_id, stage_id, action, "
                "previous_status, new_status, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entity_type, entity_id, stage_id, action, current, new_status, note.strip(), now),
            )
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "stage_id": stage_id,
            "action": action,
            "previous_status": current,
            "new_status": new_status,
            "created_at": now,
        }
