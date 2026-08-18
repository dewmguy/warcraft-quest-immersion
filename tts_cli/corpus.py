from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
import zipfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pymysql

from tts_cli.consts import GENDER_DICT, RACE_DICT
from tts_cli.npc_identity import infer_creature_identity

CORPUS_SCHEMA_VERSION = 2
EXTRACTOR_VERSION = "1.1"
CORPUS_FILES = (
    "locations.csv",
    "entities.csv",
    "texts.csv",
    "bindings.csv",
    "triggers.csv",
    "quarantine.csv",
)

LOCATION_FIELDS = (
    "location_key",
    "expansion",
    "location_type",
    "source_id",
    "map_id",
    "parent_zone_id",
    "name",
    "display_name",
)

ENTITY_FIELDS = (
    "entity_key",
    "expansion",
    "entity_type",
    "entity_id",
    "name",
    "subname",
    "race_id",
    "gender_id",
    "race_name",
    "gender_name",
    "model_ids",
    "race_candidates",
    "gender_candidates",
    "faction_id",
    "faction_name",
    "zone_id",
    "zone_name",
    "zone_location_key",
    "zone_ids",
    "map_ids",
    "role",
    "story_reach",
    "inference_json",
    "status",
)
TEXT_FIELDS = (
    "content_id",
    "expansion",
    "locale",
    "kind",
    "quest_id",
    "stage",
    "quest_title",
    "variant",
    "source_table",
    "source_record_id",
    "original_text",
    "context_json",
    "source_text_sha256",
)
BINDING_FIELDS = (
    "binding_id",
    "content_id",
    "entity_key",
    "expansion",
    "locale",
    "entity_type",
    "entity_id",
    "quest_id",
    "stage",
    "addon_file_key",
    "active",
    "status",
)
TRIGGER_FIELDS = (
    "trigger_id",
    "binding_id",
    "trigger_type",
    "source_table",
    "source_record_id",
    "menu_path",
    "context_json",
)
QUARANTINE_FIELDS = (
    "finding_id",
    "content_id",
    "binding_id",
    "entity_key",
    "reason",
    "details",
    "severity",
)
FIELD_MAP = {
    "locations.csv": LOCATION_FIELDS,
    "entities.csv": ENTITY_FIELDS,
    "texts.csv": TEXT_FIELDS,
    "bindings.csv": BINDING_FIELDS,
    "triggers.csv": TRIGGER_FIELDS,
    "quarantine.csv": QUARANTINE_FIELDS,
}

# Map.dbc entrance coordinates are insufficient for a few overlapping or zero-coordinate
# instances. These build-12340 corrections anchor each shipped dungeon/raid to the outdoor
# zone containing its entrance. Unknown custom maps still use coordinate inference.
INSTANCE_PARENT_ZONE_IDS = {
    33: 130,
    34: 1519,
    36: 40,
    43: 17,
    47: 17,
    48: 331,
    70: 3,
    90: 1,
    109: 8,
    129: 17,
    189: 85,
    209: 440,
    229: 25,
    230: 25,
    249: 15,
    269: 440,
    289: 28,
    309: 33,
    329: 139,
    349: 405,
    389: 1637,
    409: 25,
    429: 357,
    469: 25,
    509: 1377,
    531: 1377,
    532: 41,
    533: 65,
    534: 440,
    540: 3483,
    542: 3483,
    543: 3483,
    544: 3483,
    545: 3521,
    546: 3521,
    547: 3521,
    548: 3521,
    550: 3523,
    552: 3523,
    553: 3523,
    554: 3523,
    555: 3519,
    556: 3519,
    557: 3519,
    558: 3519,
    560: 440,
    564: 3520,
    565: 3522,
    568: 3433,
    574: 495,
    575: 495,
    576: 3537,
    578: 3537,
    580: 4080,
    585: 4080,
    595: 440,
    599: 67,
    600: 66,
    601: 65,
    602: 67,
    603: 67,
    604: 66,
    608: 4395,
    615: 65,
    616: 3537,
    619: 65,
    624: 4197,
    631: 210,
    632: 210,
    649: 210,
    650: 210,
    658: 210,
    668: 210,
    724: 65,
}


class CorpusError(ValueError):
    """Raised when an AzerothCore source or corpus bundle is invalid."""


class CorpusSource(Protocol):
    def has_table(self, table: str) -> bool: ...

    def columns(self, table: str) -> set[str]: ...

    def rows(self, table: str) -> list[dict[str, Any]]: ...


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _row_value(row: dict[str, Any], *names: str, default: Any = "") -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return default


def _integer(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _location_key(expansion: str, location_type: str, source_id: int) -> str:
    family = "instance" if location_type in {"dungeon", "raid"} else "zone"
    return f"{expansion}:{family}:{source_id}"


def _csv_bytes(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _read_csv_bytes(content: bytes, fields: tuple[str, ...], name: str) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CorpusError(f"{name} must be UTF-8.") from error
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != fields:
        raise CorpusError(f"{name} does not match corpus schema version {CORPUS_SCHEMA_VERSION}.")
    return list(reader)


class MappingCorpusSource:
    """In-memory source used by deterministic extractor fixtures and adapters."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = {name.lower(): [dict(row) for row in rows] for name, rows in tables.items()}

    def has_table(self, table: str) -> bool:
        return table.lower() in self._tables

    def columns(self, table: str) -> set[str]:
        rows = self._tables.get(table.lower(), [])
        return {str(key) for row in rows for key in row}

    def rows(self, table: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._tables.get(table.lower(), [])]


class MySQLCorpusSource:
    """Read-only table adapter for a restored AzerothCore world snapshot."""

    def __init__(self, connection: pymysql.Connection, database: str) -> None:
        self.connection = connection
        self.database = database
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
                (database,),
            )
            self._tables = {str(row[0]).lower(): str(row[0]) for row in cursor.fetchall()}
        self._row_cache: dict[str, list[dict[str, Any]]] = {}
        self._column_cache: dict[str, set[str]] = {}

    def has_table(self, table: str) -> bool:
        return table.lower() in self._tables

    def columns(self, table: str) -> set[str]:
        key = table.lower()
        if key not in self._column_cache:
            actual = self._tables.get(key)
            if not actual:
                return set()
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s",
                    (self.database, actual),
                )
                self._column_cache[key] = {str(row[0]) for row in cursor.fetchall()}
        return set(self._column_cache[key])

    def rows(self, table: str) -> list[dict[str, Any]]:
        key = table.lower()
        if key not in self._row_cache:
            actual = self._tables.get(key)
            if not actual:
                return []
            safe_name = actual.replace("`", "``")
            with self.connection.cursor() as cursor:
                cursor.execute(f"SELECT * FROM `{safe_name}`")  # noqa: S608 - introspected identifier
                columns = [str(item[0]) for item in cursor.description]
                self._row_cache[key] = [
                    dict(zip(columns, values, strict=True)) for values in cursor
                ]
        return [dict(row) for row in self._row_cache[key]]


@dataclass
class CorpusBundle:
    manifest: dict[str, Any]
    locations: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    texts: list[dict[str, Any]]
    bindings: list[dict[str, Any]]
    triggers: list[dict[str, Any]]
    quarantine: list[dict[str, Any]]

    def as_files(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "locations.csv": self.locations,
            "entities.csv": self.entities,
            "texts.csv": self.texts,
            "bindings.csv": self.bindings,
            "triggers.csv": self.triggers,
            "quarantine.csv": self.quarantine,
        }


class AzerothCoreCorpusExtractor:
    REQUIRED_TABLES = {
        "quest_template",
        "quest_request_items",
        "quest_offer_reward",
        "creature_template",
        "creature_model_info",
        "creature_queststarter",
        "creature_questender",
        "gameobject_template",
        "gameobject_queststarter",
        "gameobject_questender",
        "gossip_menu",
        "gossip_menu_option",
        "npc_text",
        "instance_template",
    }
    REQUIRED_TABLE_VARIANTS = {
        "creature display DBC": ("db_CreatureDisplayInfo", "creaturedisplayinfo_dbc"),
        "creature display-extra DBC": (
            "db_CreatureDisplayInfoExtra",
            "creaturedisplayinfoextra_dbc",
        ),
        "creature model-data DBC": ("db_CreatureModelData", "creaturemodeldata_dbc"),
        "faction-template DBC": ("db_FactionTemplate", "factiontemplate_dbc"),
        "faction DBC": ("db_Faction", "faction_dbc"),
        "area-table DBC": ("db_AreaTable", "areatable_dbc"),
        "map DBC": ("db_Map", "map_dbc"),
        "world-map-area DBC": ("db_WorldMapArea", "worldmaparea_dbc"),
    }

    def __init__(
        self,
        source: CorpusSource,
        *,
        expansion: str = "3.3.5",
        locale: str = "enUS",
        source_name: str = "azerothcore-world",
        source_sha256: str = "",
        source_version: str = "",
        source_artifacts: list[dict[str, str]] | None = None,
    ) -> None:
        self.source = source
        self.expansion = expansion
        self.locale = locale
        self.source_name = source_name
        self.source_sha256 = source_sha256
        self.source_version = source_version
        self.source_artifacts = source_artifacts or []
        self._table_cache: dict[str, list[dict[str, Any]]] = {}

    def _rows(self, table: str) -> list[dict[str, Any]]:
        if table not in self._table_cache:
            self._table_cache[table] = self.source.rows(table)
        return self._table_cache[table]

    def _validate_schema(self) -> None:
        missing = sorted(
            table for table in self.REQUIRED_TABLES if not self.source.has_table(table)
        )
        if missing:
            raise CorpusError(f"AzerothCore source is missing tables: {', '.join(missing)}")
        missing_variants = [
            label
            for label, tables in self.REQUIRED_TABLE_VARIANTS.items()
            if not any(self.source.has_table(table) for table in tables)
        ]
        if missing_variants:
            raise CorpusError(
                "AzerothCore source is missing provenance-matched enrichment tables: "
                + ", ".join(missing_variants)
            )

        def require_columns(table: str, *groups: tuple[str, ...]) -> None:
            available = {value.lower() for value in self.source.columns(table)}
            missing_groups = [
                group for group in groups if not ({v.lower() for v in group} & available)
            ]
            if missing_groups:
                labels = ["/".join(group) for group in missing_groups]
                raise CorpusError(f"{table} has no recognized column for: {', '.join(labels)}.")

        base_columns = {
            "quest_template": (("ID", "entry"), ("QuestDescription", "Details")),
            "quest_request_items": (("ID", "entry"), ("CompletionText",)),
            "quest_offer_reward": (("ID", "entry"), ("RewardText",)),
            "creature_template": (("entry", "id"), ("name", "Name")),
            "creature_queststarter": (("id",), ("quest",)),
            "creature_questender": (("id",), ("quest",)),
            "gameobject_template": (("entry", "id"), ("name", "Name"), ("type",)),
            "gameobject_queststarter": (("id",), ("quest",)),
            "gameobject_questender": (("id",), ("quest",)),
            "gossip_menu": (("MenuID", "entry", "menu_id"), ("TextID", "text_id")),
            "gossip_menu_option": (
                ("MenuID", "menu_id"),
                ("OptionID", "OptionIndex", "option_id", "option_index"),
            ),
            "npc_text": (("ID",),),
            "instance_template": (("map", "Map"), ("script", "ScriptName")),
        }
        for table, groups in base_columns.items():
            require_columns(table, *groups)

        quest_columns = {value.lower() for value in self.source.columns("quest_template")}
        if not ({"questdescription", "details"} & quest_columns):
            raise CorpusError("quest_template has no recognized quest-description column.")
        npc_columns = {value.lower() for value in self.source.columns("npc_text")}
        has_broadcast = any(re.fullmatch(r"broadcasttextid[0-7]", value) for value in npc_columns)
        has_direct = any(re.fullmatch(r"text[0-7]_[01]", value) for value in npc_columns)
        if not has_broadcast and not has_direct:
            raise CorpusError("npc_text has neither BroadcastTextID slots nor direct text slots.")
        if has_broadcast and not self.source.has_table("broadcast_text"):
            raise CorpusError("npc_text references broadcast text, but broadcast_text is absent.")
        if has_broadcast:
            require_columns("broadcast_text", ("ID", "entry"), ("MaleText",), ("FemaleText",))

        optional_columns = {
            "quest_greeting": (("ID", "entry"), ("Type", "type"), ("Greeting", "content_default")),
            "item_template": (("entry", "id"), ("name", "Name"), ("StartQuest", "start_quest")),
            "creature_template_model": (
                ("CreatureID", "entry"),
                ("CreatureDisplayID", "DisplayID", "displayid"),
            ),
            "creature_model_info": (("DisplayID", "modelid"), ("Gender", "gender")),
            "creature": (
                ("id1", "id", "entry"),
                ("zoneId", "zoneid", "areaId", "areaid"),
                ("map", "mapId", "mapid"),
            ),
            "conditions": (
                ("SourceTypeOrReferenceId", "source_type"),
                ("SourceGroup", "source_group"),
                ("SourceEntry", "source_entry"),
            ),
            "disables": (("sourceType", "source_type"), ("entry", "Entry")),
        }
        for table, groups in optional_columns.items():
            if self.source.has_table(table):
                require_columns(table, *groups)

        for label, variants in self.REQUIRED_TABLE_VARIANTS.items():
            table = next(table for table in variants if self.source.has_table(table))
            groups = {
                "creature display DBC": (("ID",), ("ExtendedDisplayInfoID",)),
                "creature display-extra DBC": (
                    ("ID",),
                    ("DisplayRaceID",),
                    ("DisplaySexID",),
                ),
                "creature model-data DBC": (("ID",), ("ModelPath",)),
                "faction-template DBC": (("ID",), ("Faction", "FactionID", "faction_id")),
                "faction DBC": (("ID",), ("Name_Lang_enUS", "Name_enUS", "Name", "name")),
                "area-table DBC": (
                    ("ID", "AreaID"),
                    ("AreaName_Lang_enUS", "Name_Lang_enUS", "Name", "name"),
                ),
                "map DBC": (
                    ("ID",),
                    ("MapType",),
                    ("MapName_Lang_enUS", "MapName", "Name", "name"),
                ),
                "world-map-area DBC": (
                    ("ID",),
                    ("MapID",),
                    ("AreaID",),
                ),
            }[label]
            require_columns(table, *groups)

        gossip_option_columns = {
            value.lower() for value in self.source.columns("gossip_menu_option")
        }
        if "actionmenuid" not in gossip_option_columns and not self.source.has_table(
            "gossip_menu_option_action"
        ):
            raise CorpusError(
                "gossip_menu_option has no ActionMenuID and the split action table is absent."
            )
        if self.source.has_table("gossip_menu_option_action"):
            require_columns(
                "gossip_menu_option_action",
                ("MenuID", "menu_id"),
                ("OptionID", "OptionIndex", "option_id", "option_index"),
                ("ActionMenuID", "action_menu_id"),
            )

    def _table_manifest(self) -> dict[str, dict[str, Any]]:
        tables: dict[str, dict[str, Any]] = {}
        for table in sorted(self._table_cache):
            rows = self._table_cache[table]
            row_hashes = sorted(
                hashlib.sha256(_json(row).encode("utf-8")).hexdigest() for row in rows
            )
            tables[table] = {
                "rows": len(rows),
                "columns": sorted(self.source.columns(table), key=str.lower),
                "fingerprint": hashlib.sha256("".join(row_hashes).encode("ascii")).hexdigest(),
            }
        return tables

    def extract(self) -> CorpusBundle:
        self._validate_schema()
        entities: dict[str, dict[str, Any]] = {}
        texts: dict[str, dict[str, Any]] = {}
        bindings: dict[str, dict[str, Any]] = {}
        triggers: dict[str, dict[str, Any]] = {}
        findings: dict[str, dict[str, Any]] = {}

        creature_templates = {
            _integer(_row_value(row, "entry", "id")): row for row in self._rows("creature_template")
        }
        object_templates = {
            _integer(_row_value(row, "entry", "id")): row
            for row in self._rows("gameobject_template")
        }
        item_templates = {
            _integer(_row_value(row, "entry", "id")): row
            for row in (
                self._rows("item_template") if self.source.has_table("item_template") else []
            )
        }
        model_rows = (
            self._rows("creature_template_model")
            if self.source.has_table("creature_template_model")
            else []
        )
        models_by_creature: dict[int, set[int]] = defaultdict(set)
        for row in model_rows:
            models_by_creature[_integer(_row_value(row, "CreatureID", "entry"))].add(
                _integer(_row_value(row, "CreatureDisplayID", "DisplayID", "displayid"))
            )
        display_rows = self._optional_rows("db_CreatureDisplayInfo", "creaturedisplayinfo_dbc")
        extra_rows = self._optional_rows(
            "db_CreatureDisplayInfoExtra", "creaturedisplayinfoextra_dbc"
        )
        model_data_rows = self._optional_rows("db_CreatureModelData", "creaturemodeldata_dbc")
        model_info_rows = (
            self._rows("creature_model_info")
            if self.source.has_table("creature_model_info")
            else []
        )
        displays = {_integer(_row_value(row, "ID")): row for row in display_rows}
        extras = {_integer(_row_value(row, "ID")): row for row in extra_rows}
        model_data = {_integer(_row_value(row, "ID")): row for row in model_data_rows}
        model_info = {
            _integer(_row_value(row, "DisplayID", "modelid")): row for row in model_info_rows
        }
        faction_template_rows = self._optional_rows("db_FactionTemplate", "factiontemplate_dbc")
        faction_rows = self._optional_rows("db_Faction", "faction_dbc")
        area_rows = self._optional_rows("db_AreaTable", "areatable_dbc")
        map_rows = self._optional_rows("db_Map", "map_dbc")
        world_map_area_rows = self._optional_rows("db_WorldMapArea", "worldmaparea_dbc")
        faction_templates = {_integer(_row_value(row, "ID")): row for row in faction_template_rows}
        factions = {_integer(_row_value(row, "ID")): row for row in faction_rows}
        areas = {_integer(_row_value(row, "ID", "AreaID")): row for row in area_rows}

        zone_counts: dict[int, Counter[int]] = defaultdict(Counter)
        map_counts: dict[int, Counter[int]] = defaultdict(Counter)
        if self.source.has_table("creature"):
            for row in self._rows("creature"):
                creature_id = _integer(_row_value(row, "id1", "id", "entry"))
                zone_id = _integer(_row_value(row, "zoneId", "zoneid", "areaId", "areaid"))
                map_id = _integer(_row_value(row, "map", "mapId", "mapid"), -1)
                if creature_id and zone_id:
                    zone_counts[creature_id][zone_id] += 1
                if creature_id and map_id >= 0:
                    map_counts[creature_id][map_id] += 1
        locations, zones_by_id, instances_by_map = self._build_locations(
            area_rows,
            map_rows,
            world_map_area_rows,
            {zone_id for counts in zone_counts.values() for zone_id in counts},
        )

        def add_finding(
            reason: str,
            details: str,
            *,
            content_id: str = "",
            binding_id: str = "",
            entity_key: str = "",
            severity: str = "warning",
        ) -> None:
            finding_id = _stable_id("finding", reason, content_id, binding_id, entity_key, details)
            findings[finding_id] = {
                "finding_id": finding_id,
                "content_id": content_id,
                "binding_id": binding_id,
                "entity_key": entity_key,
                "reason": reason,
                "details": details,
                "severity": severity,
            }

        def entity(entity_type: str, entity_id: int) -> dict[str, Any]:
            entity_key = f"{self.expansion}:{entity_type}:{entity_id}"
            if entity_key in entities:
                return entities[entity_key]
            template = (
                creature_templates.get(entity_id, {})
                if entity_type == "creature"
                else object_templates.get(entity_id, {})
                if entity_type == "gameobject"
                else item_templates.get(entity_id, {})
            )
            missing_entity = not template
            name = _clean_text(_row_value(template, "name", "Name")) or f"{entity_type} {entity_id}"
            subname = _clean_text(_row_value(template, "subname", "SubName"))
            model_ids = set(models_by_creature.get(entity_id, set()))
            if entity_type == "creature" and not model_ids:
                for index in range(1, 5):
                    model = _integer(_row_value(template, f"modelid{index}", f"display_id{index}"))
                    if model:
                        model_ids.add(model)
            race_candidates: set[int] = set()
            gender_candidates: set[int] = set()
            model_genders: set[int] = set()
            model_paths: set[str] = set()
            for model_id in model_ids:
                display = displays.get(model_id, {})
                model_data_id = _integer(_row_value(display, "ModelID", "model_id"))
                model_path = _clean_text(
                    _row_value(model_data.get(model_data_id, {}), "ModelPath", "model_path")
                )
                if model_path:
                    model_paths.add(model_path)
                model_gender = _integer(
                    _row_value(model_info.get(model_id, {}), "Gender", "gender"), -1
                )
                if model_gender in GENDER_DICT:
                    model_genders.add(model_gender)
                extra_id = _integer(
                    _row_value(display, "ExtendedDisplayInfoID", "extendeddisplayinfoid")
                )
                extra = extras.get(extra_id, {})
                if extra:
                    race_candidates.add(_integer(_row_value(extra, "DisplayRaceID"), -1))
                    gender_candidates.add(_integer(_row_value(extra, "DisplaySexID"), -1))
            race_candidates.discard(-1)
            gender_candidates.discard(-1)
            identity = (
                infer_creature_identity(
                    name=name,
                    creature_type=_integer(_row_value(template, "type")),
                    race_candidates=race_candidates,
                    gender_candidates=gender_candidates,
                    model_paths=model_paths,
                    model_genders=model_genders,
                )
                if entity_type == "creature"
                else None
            )
            ambiguous = bool(identity and identity.ambiguous)
            missing_model = entity_type == "creature" and not model_ids
            race_id = identity.race_id if identity else -1
            gender_id = identity.gender_id if identity else 0
            zones = zone_counts.get(entity_id, Counter())
            maps_for_entity = map_counts.get(entity_id, Counter())
            primary_zone = (
                sorted(zones.items(), key=lambda item: (-item[1], item[0]))[0][0] if zones else 0
            )
            primary_map = (
                sorted(maps_for_entity.items(), key=lambda item: (-item[1], item[0]))[0][0]
                if maps_for_entity
                else -1
            )
            exclusive_instance = bool(maps_for_entity) and set(maps_for_entity).issubset(
                instances_by_map
            )
            location = (
                instances_by_map.get(primary_map)
                if exclusive_instance
                else zones_by_id.get(primary_zone)
            )
            faction_template_id = _integer(_row_value(template, "faction"))
            faction_template = faction_templates.get(faction_template_id, {})
            faction_id = _integer(
                _row_value(faction_template, "Faction", "FactionID", "faction_id"),
                faction_template_id,
            )
            faction = factions.get(faction_id, {})
            faction_name = _clean_text(
                _row_value(
                    faction,
                    "Name_Lang_enUS",
                    "Name_enUS",
                    "Name",
                    "name",
                )
            )
            area = areas.get(primary_zone, {})
            zone_name = (
                str(location["display_name"])
                if location
                else _clean_text(
                    _row_value(area, "AreaName_Lang_enUS", "Name_Lang_enUS", "Name", "name")
                )
            )
            role = self._infer_role(name, subname, template, entity_type)
            status = (
                "missing_entity"
                if missing_entity
                else "ambiguous_model"
                if ambiguous
                else "missing_model"
                if missing_model
                else "active"
            )
            payload = {
                "entity_key": entity_key,
                "expansion": self.expansion,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "name": name,
                "subname": subname,
                "race_id": race_id,
                "gender_id": gender_id,
                "race_name": RACE_DICT.get(race_id, f"race-{race_id}"),
                "gender_name": GENDER_DICT.get(gender_id, f"gender-{gender_id}"),
                "model_ids": _json(sorted(model_ids)),
                "race_candidates": _json(sorted(race_candidates)),
                "gender_candidates": _json(sorted(gender_candidates)),
                "faction_id": faction_id,
                "faction_name": faction_name,
                "zone_id": primary_zone,
                "zone_name": zone_name or (str(primary_zone) if primary_zone else ""),
                "zone_location_key": str(location["location_key"]) if location else "",
                "zone_ids": _json(sorted(zones)),
                "map_ids": _json(sorted(maps_for_entity)),
                "role": role,
                "story_reach": "one_off",
                "inference_json": _json(
                    {
                        "role_basis": "name, subname, and NPC service flags",
                        "race_basis": identity.race_basis if identity else "delivery endpoint",
                        "gender_basis": identity.gender_basis if identity else "delivery endpoint",
                        "model_paths": sorted(model_paths),
                        "model_genders": sorted(model_genders),
                        "creature_type": _integer(_row_value(template, "type")),
                        "zone_spawn_counts": dict(sorted(zones.items())),
                        "map_spawn_counts": dict(sorted(maps_for_entity.items())),
                        "location_basis": (
                            "exclusive instance map"
                            if exclusive_instance
                            else "most common spawn zone"
                            if zones
                            else "no spawn location"
                        ),
                    }
                ),
                "status": status,
            }
            entities[entity_key] = payload
            if missing_entity:
                add_finding(
                    "missing_entity",
                    f"{entity_type} {entity_id} has no matching template row.",
                    entity_key=entity_key,
                )
            elif ambiguous:
                add_finding(
                    "ambiguous_model",
                    f"{name} resolves to multiple race or gender candidates.",
                    entity_key=entity_key,
                )
            elif missing_model:
                add_finding(
                    "missing_model",
                    f"{name} has no unambiguous race and gender in the supplied DBC metadata.",
                    entity_key=entity_key,
                )
            return payload

        def add_text(
            *,
            identity: tuple[Any, ...],
            kind: str,
            original_text: str,
            source_table: str,
            source_record_id: str,
            quest_id: int | None = None,
            stage: str = "",
            quest_title: str = "",
            variant: str = "default",
            context: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            content_id = _stable_id("content", self.expansion, self.locale, *identity)
            text = _clean_text(original_text)
            payload = {
                "content_id": content_id,
                "expansion": self.expansion,
                "locale": self.locale,
                "kind": kind,
                "quest_id": quest_id or "",
                "stage": stage,
                "quest_title": quest_title,
                "variant": variant,
                "source_table": source_table,
                "source_record_id": source_record_id,
                "original_text": text,
                "context_json": _json(context or {}),
                "source_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            existing = texts.get(content_id)
            if existing and existing["source_text_sha256"] != payload["source_text_sha256"]:
                raise CorpusError(f"Stable content ID {content_id} resolved to conflicting text.")
            texts[content_id] = payload
            return payload

        def add_binding(
            content: dict[str, Any],
            entity_type: str,
            entity_id: int,
            trigger_family: str,
            *,
            trigger_type: str,
            source_table: str,
            source_record_id: str,
            menu_path: str = "",
            context: dict[str, Any] | None = None,
            forced_quarantine: str = "",
        ) -> dict[str, Any]:
            speaker = entity(entity_type, entity_id)
            binding_id = _stable_id(
                "binding",
                content["content_id"],
                speaker["entity_key"],
                trigger_family,
            )
            reason = forced_quarantine or (
                str(speaker["status"]) if speaker["status"] != "active" else ""
            )
            active = not reason
            prefix = {"creature": "c", "gameobject": "g", "item": "i"}[entity_type]
            if content["quest_id"]:
                addon_key = f"{content['quest_id']}-{content['stage']}-{prefix}{entity_id}"
            else:
                addon_key = hashlib.md5(  # noqa: S324 - inherited addon filename contract
                    f"{content['content_id']}|{prefix}{entity_id}".encode()
                ).hexdigest()
            bindings[binding_id] = {
                "binding_id": binding_id,
                "content_id": content["content_id"],
                "entity_key": speaker["entity_key"],
                "expansion": self.expansion,
                "locale": self.locale,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "quest_id": content["quest_id"],
                "stage": content["stage"],
                "addon_file_key": addon_key,
                "active": 1 if active else 0,
                "status": "active" if active else "quarantined",
            }
            trigger_id = _stable_id(
                "trigger",
                binding_id,
                trigger_type,
                source_table,
                source_record_id,
                menu_path,
            )
            triggers[trigger_id] = {
                "trigger_id": trigger_id,
                "binding_id": binding_id,
                "trigger_type": trigger_type,
                "source_table": source_table,
                "source_record_id": source_record_id,
                "menu_path": menu_path,
                "context_json": _json(context or {}),
            }
            if reason:
                add_finding(
                    reason,
                    f"Binding {binding_id} is retained outside the production queue.",
                    content_id=content["content_id"],
                    binding_id=binding_id,
                    entity_key=speaker["entity_key"],
                )
            return bindings[binding_id]

        quest_rows = {
            _integer(_row_value(row, "ID", "entry")): row for row in self._rows("quest_template")
        }
        request_rows = {
            _integer(_row_value(row, "ID", "entry")): row
            for row in self._rows("quest_request_items")
        }
        reward_rows = {
            _integer(_row_value(row, "ID", "entry")): row
            for row in self._rows("quest_offer_reward")
        }
        starters = self._quest_relations("starter")
        enders = self._quest_relations("ender")
        disabled_quests = {
            _integer(_row_value(row, "entry", "Entry"))
            for row in (self._rows("disables") if self.source.has_table("disables") else [])
            if _integer(_row_value(row, "sourceType", "source_type")) == 1
        }
        for item_id, row in item_templates.items():
            quest_id = _integer(_row_value(row, "StartQuest", "start_quest"))
            if quest_id:
                starters[quest_id].add(("item", item_id, "item_template"))

        for quest_id, quest in sorted(quest_rows.items()):
            title = _clean_text(_row_value(quest, "LogTitle", "Title"))
            disabled = quest_id in disabled_quests
            stages = (
                (
                    "accept",
                    _clean_text(_row_value(quest, "QuestDescription", "Details")),
                    "quest_template",
                    starters.get(quest_id, set()),
                ),
                (
                    "progress",
                    _clean_text(_row_value(request_rows.get(quest_id, {}), "CompletionText")),
                    "quest_request_items",
                    enders.get(quest_id, set()),
                ),
                (
                    "complete",
                    _clean_text(_row_value(reward_rows.get(quest_id, {}), "RewardText")),
                    "quest_offer_reward",
                    enders.get(quest_id, set()),
                ),
            )
            for stage, spoken_text, source_table, endpoints in stages:
                if not spoken_text:
                    continue
                content = add_text(
                    identity=("quest", quest_id, stage),
                    kind="quest",
                    original_text=spoken_text,
                    source_table=source_table,
                    source_record_id=str(quest_id),
                    quest_id=quest_id,
                    stage=stage,
                    quest_title=title,
                    context={
                        "log_description": _clean_text(
                            _row_value(quest, "LogDescription", "Objectives")
                        ),
                        "quest_completion_log": _clean_text(
                            _row_value(quest, "QuestCompletionLog", "EndText")
                        ),
                        "flags": _integer(_row_value(quest, "Flags")),
                        "quest_type": _integer(_row_value(quest, "QuestType"), 2),
                    },
                )
                if not endpoints:
                    add_finding(
                        "missing_delivery_endpoint",
                        f"Quest {quest_id} {stage} has text but no supported delivery endpoint.",
                        content_id=content["content_id"],
                    )
                    continue
                item_endpoint_count = sum(
                    1 for entity_type, _entity_id, _table in endpoints if entity_type == "item"
                )
                for entity_type, entity_id, relation_table in sorted(endpoints):
                    quarantine_reason = "disabled_quest" if disabled else ""
                    if entity_type == "item" and item_endpoint_count > 1:
                        quarantine_reason = "ambiguous_item_starter"
                    add_binding(
                        content,
                        entity_type,
                        entity_id,
                        f"quest-{stage}",
                        trigger_type=f"quest_{stage}",
                        source_table=relation_table,
                        source_record_id=f"{entity_id}:{quest_id}",
                        forced_quarantine=quarantine_reason,
                    )

        if self.source.has_table("quest_greeting"):
            for row in self._rows("quest_greeting"):
                entity_id = _integer(_row_value(row, "ID", "entry"))
                greeting = _clean_text(_row_value(row, "Greeting", "content_default"))
                raw_type = _integer(_row_value(row, "Type", "type"))
                entity_type = "creature" if raw_type == 0 else "gameobject"
                if not entity_id or not greeting:
                    continue
                content = add_text(
                    identity=("quest-greeting", entity_type, entity_id),
                    kind="gossip",
                    original_text=greeting,
                    source_table="quest_greeting",
                    source_record_id=f"{raw_type}:{entity_id}",
                )
                add_binding(
                    content,
                    entity_type,
                    entity_id,
                    "quest-greeting",
                    trigger_type="quest_greeting",
                    source_table="quest_greeting",
                    source_record_id=f"{raw_type}:{entity_id}",
                )

        reached_npc_slots: set[str] = set()
        npc_rows = {_integer(_row_value(row, "ID")): row for row in self._rows("npc_text")}
        broadcast_rows = {
            _integer(_row_value(row, "ID", "entry")): row
            for row in (
                self._rows("broadcast_text") if self.source.has_table("broadcast_text") else []
            )
        }
        menus: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in self._rows("gossip_menu"):
            menus[_integer(_row_value(row, "MenuID", "entry", "menu_id"))].append(row)
        menu_options: dict[int, list[dict[str, Any]]] = defaultdict(list)
        option_actions: dict[tuple[int, int], dict[str, Any]] = {}
        if self.source.has_table("gossip_menu_option_action"):
            for action in self._rows("gossip_menu_option_action"):
                key = (
                    _integer(_row_value(action, "MenuID", "menu_id")),
                    _integer(
                        _row_value(action, "OptionID", "OptionIndex", "option_id", "option_index")
                    ),
                )
                option_actions[key] = action
        for row in self._rows("gossip_menu_option"):
            menu_id = _integer(_row_value(row, "MenuID", "menu_id"))
            option_id = _integer(
                _row_value(row, "OptionID", "OptionIndex", "option_id", "option_index")
            )
            action = option_actions.get((menu_id, option_id), {})
            enriched = dict(row)
            if action:
                enriched["ActionMenuID"] = _row_value(action, "ActionMenuID", "action_menu_id")
                enriched["ActionPoiID"] = _row_value(action, "ActionPoiID", "action_poi_id")
            menu_options[menu_id].append(enriched)
        menu_conditions: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        option_conditions: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        if self.source.has_table("conditions"):
            for row in self._rows("conditions"):
                source_type = _integer(_row_value(row, "SourceTypeOrReferenceId", "source_type"))
                source_group = _integer(_row_value(row, "SourceGroup", "source_group"))
                source_entry = _integer(_row_value(row, "SourceEntry", "source_entry"))
                if source_type == 14:
                    menu_conditions[(source_group, source_entry)].append(row)
                elif source_type == 15:
                    option_conditions[(source_group, source_entry)].append(row)

        roots: list[tuple[str, int, int]] = []
        for creature_id, row in creature_templates.items():
            menu_id = _integer(_row_value(row, "gossip_menu_id", "GossipMenuId"))
            if menu_id:
                roots.append(("creature", creature_id, menu_id))
        for object_id, row in object_templates.items():
            object_type = _integer(_row_value(row, "type"))
            field = {2: "data3", 10: "data19"}.get(object_type)
            menu_id = _integer(_row_value(row, field)) if field else 0
            if menu_id:
                roots.append(("gameobject", object_id, menu_id))

        for entity_type, entity_id, root_menu in sorted(roots):
            queue: deque[
                tuple[int, tuple[int, ...], tuple[str, ...], tuple[list[dict[str, Any]], ...]]
            ] = deque([(root_menu, (root_menu,), (), ())])
            while queue:
                menu_id, path, choice_path, choice_condition_path = queue.popleft()
                for menu_row in menus.get(menu_id, []):
                    text_id = _integer(_row_value(menu_row, "TextID", "text_id"))
                    npc_row = npc_rows.get(text_id)
                    if not npc_row:
                        add_finding(
                            "missing_npc_text",
                            f"Gossip menu {menu_id} references missing npc_text {text_id}.",
                            entity_key=f"{self.expansion}:{entity_type}:{entity_id}",
                        )
                        continue
                    for resolved in self._resolve_npc_text(npc_row, broadcast_rows):
                        source_record = f"{text_id}:{resolved['slot']}:{resolved['variant']}"
                        reached_npc_slots.add(source_record)
                        broadcast_id = _integer(resolved["context"]["broadcast_text_id"])
                        text_identity = (
                            ("broadcast-text", broadcast_id, resolved["variant"])
                            if broadcast_id
                            else ("npc-text", text_id, resolved["slot"], resolved["variant"])
                        )
                        text_source_table = "broadcast_text" if broadcast_id else "npc_text"
                        text_source_record = (
                            f"{broadcast_id}:{resolved['variant']}"
                            if broadcast_id
                            else source_record
                        )
                        content = add_text(
                            identity=text_identity,
                            kind="gossip",
                            original_text=resolved["text"],
                            source_table=text_source_table,
                            source_record_id=text_source_record,
                            variant=resolved["variant"],
                            context=resolved["context"],
                        )
                        add_binding(
                            content,
                            entity_type,
                            entity_id,
                            "gossip",
                            trigger_type="gossip_menu",
                            source_table="gossip_menu",
                            source_record_id=f"{menu_id}:{text_id}",
                            menu_path=">".join(str(value) for value in path),
                            context={
                                "menu_id": menu_id,
                                "text_id": text_id,
                                "player_choice_path": list(choice_path),
                                "player_choice_conditions": list(choice_condition_path),
                                "menu_conditions": menu_conditions.get((menu_id, text_id), []),
                            },
                        )
                for option in menu_options.get(menu_id, []):
                    action_menu = _integer(_row_value(option, "ActionMenuID", "action_menu_id"))
                    option_text = _clean_text(_row_value(option, "OptionText", "option_text"))
                    option_broadcast_id = _integer(
                        _row_value(option, "OptionBroadcastTextID", "option_broadcast_text_id")
                    )
                    if not option_text and option_broadcast_id:
                        option_broadcast = broadcast_rows.get(option_broadcast_id, {})
                        option_text = _clean_text(
                            _row_value(option_broadcast, "MaleText", "FemaleText")
                        )
                    next_choices = (*choice_path, option_text) if option_text else choice_path
                    next_choice_conditions = (
                        *choice_condition_path,
                        option_conditions.get(
                            (
                                menu_id,
                                _integer(
                                    _row_value(
                                        option,
                                        "OptionID",
                                        "OptionIndex",
                                        "option_id",
                                        "option_index",
                                    )
                                ),
                            ),
                            [],
                        ),
                    )
                    if action_menu and action_menu not in path:
                        queue.append(
                            (
                                action_menu,
                                (*path, action_menu),
                                next_choices,
                                next_choice_conditions,
                            )
                        )
                    elif action_menu and action_menu in path:
                        add_finding(
                            "cyclic_gossip_menu",
                            f"Gossip menu path {'>'.join(map(str, (*path, action_menu)))} is cyclic.",
                            entity_key=f"{self.expansion}:{entity_type}:{entity_id}",
                            severity="info",
                        )

        for text_id, npc_row in sorted(npc_rows.items()):
            for resolved in self._resolve_npc_text(npc_row, broadcast_rows):
                source_record = f"{text_id}:{resolved['slot']}:{resolved['variant']}"
                if source_record in reached_npc_slots:
                    continue
                broadcast_id = _integer(resolved["context"]["broadcast_text_id"])
                text_identity = (
                    ("broadcast-text", broadcast_id, resolved["variant"])
                    if broadcast_id
                    else ("npc-text", text_id, resolved["slot"], resolved["variant"])
                )
                text_source_table = "broadcast_text" if broadcast_id else "npc_text"
                text_source_record = (
                    f"{broadcast_id}:{resolved['variant']}" if broadcast_id else source_record
                )
                content = add_text(
                    identity=text_identity,
                    kind="gossip",
                    original_text=resolved["text"],
                    source_table=text_source_table,
                    source_record_id=text_source_record,
                    variant=resolved["variant"],
                    context=resolved["context"],
                )
                add_finding(
                    "unrooted_gossip",
                    f"npc_text {source_record} is not reachable from a supported interaction root.",
                    content_id=content["content_id"],
                )

        quest_counts: Counter[str] = Counter()
        line_counts: Counter[str] = Counter()
        entity_quests: dict[str, set[int]] = defaultdict(set)
        for binding in bindings.values():
            line_counts[binding["entity_key"]] += 1
            if binding["quest_id"]:
                entity_quests[binding["entity_key"]].add(_integer(binding["quest_id"]))
        for entity_key, quest_ids in entity_quests.items():
            quest_counts[entity_key] = len(quest_ids)
        for entity_key, payload in entities.items():
            quest_count = quest_counts[entity_key]
            line_count = line_counts[entity_key]
            zone_count = len(json.loads(str(payload["zone_ids"])))
            name = str(payload["name"]).lower()
            if any(term in name for term in ("king", "queen", "warchief", "lich king", "thrall")):
                reach = "pivotal"
            elif quest_count >= 10 or zone_count >= 3:
                reach = "global"
            elif quest_count >= 6 or zone_count >= 2:
                reach = "inter_zone"
            elif quest_count >= 3:
                reach = "zone"
            elif quest_count >= 2 or line_count >= 4:
                reach = "subzone"
            elif quest_count == 1:
                reach = "stepping_stone"
            else:
                reach = "one_off"
            payload["story_reach"] = reach
            inference = json.loads(str(payload["inference_json"]))
            inference.update(
                {
                    "distinct_quests": quest_count,
                    "voiceable_bindings": line_count,
                    "zone_count": zone_count,
                }
            )
            payload["inference_json"] = _json(inference)

        active_bindings = sum(_integer(row["active"]) for row in bindings.values())
        shared_quest_contents = sum(
            1
            for content_id, total in Counter(
                row["content_id"] for row in bindings.values() if row["quest_id"]
            ).items()
            if total > 1 and content_id
        )
        database_version_table = next(
            (table for table in ("version_db_world", "version") if self.source.has_table(table)),
            "",
        )
        database_version_rows = (
            json.loads(_json(self._rows(database_version_table))) if database_version_table else []
        )
        manifest = {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "source": {
                "name": self.source_name,
                "sha256": self.source_sha256,
                "version": self.source_version,
                "database_version_table": database_version_table,
                "database_version_rows": database_version_rows,
                "additional_artifacts": json.loads(_json(self.source_artifacts)),
                "expansion": self.expansion,
                "locale": self.locale,
            },
            "extracted_at": _utc_now(),
            "tables": self._table_manifest(),
            "counts": {
                "locations": len(locations),
                "entities": len(entities),
                "texts": len(texts),
                "bindings": len(bindings),
                "active_bindings": active_bindings,
                "quarantined_bindings": len(bindings) - active_bindings,
                "triggers": len(triggers),
                "findings": len(findings),
                "shared_quest_contents": shared_quest_contents,
                "addon_conflicts": 0,
            },
            "artifacts": {},
        }
        return CorpusBundle(
            manifest=manifest,
            locations=locations,
            entities=sorted(entities.values(), key=lambda row: str(row["entity_key"])),
            texts=sorted(texts.values(), key=lambda row: str(row["content_id"])),
            bindings=sorted(bindings.values(), key=lambda row: str(row["binding_id"])),
            triggers=sorted(triggers.values(), key=lambda row: str(row["trigger_id"])),
            quarantine=sorted(findings.values(), key=lambda row: str(row["finding_id"])),
        )

    def _optional_rows(self, *tables: str) -> list[dict[str, Any]]:
        for table in tables:
            if self.source.has_table(table):
                return self._rows(table)
        return []

    def _build_locations(
        self,
        area_rows: list[dict[str, Any]],
        map_rows: list[dict[str, Any]],
        world_map_area_rows: list[dict[str, Any]],
        spawn_zone_ids: set[int],
    ) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
        areas = {_integer(_row_value(row, "ID", "AreaID")): row for row in area_rows}
        maps = {_integer(_row_value(row, "ID")): row for row in map_rows}
        world_zone_ids = {
            _integer(_row_value(row, "AreaID"))
            for row in world_map_area_rows
            if _integer(_row_value(row, "AreaID"))
        }
        instance_template_maps = {
            _integer(_row_value(row, "map"))
            for row in self._rows("instance_template")
            if _clean_text(_row_value(row, "script"))
        }
        instance_maps = {
            map_id
            for map_id, row in maps.items()
            if map_id in instance_template_maps
            and _integer(_row_value(row, "MapType", "InstanceType")) in {1, 2}
        }

        outdoor_map_ids = {
            map_id
            for map_id, row in maps.items()
            if _integer(_row_value(row, "MapType", "InstanceType")) == 0
        }
        for zone_id in spawn_zone_ids:
            area = areas.get(zone_id, {})
            if _integer(_row_value(area, "ContinentID", "MapID")) in outdoor_map_ids:
                world_zone_ids.add(zone_id)
        world_zone_ids.update(INSTANCE_PARENT_ZONE_IDS.get(map_id, 0) for map_id in instance_maps)
        world_zone_ids.discard(0)

        locations: list[dict[str, Any]] = []
        zones_by_id: dict[int, dict[str, Any]] = {}
        for zone_id in sorted(world_zone_ids):
            area = areas.get(zone_id, {})
            name = _clean_text(
                _row_value(area, "AreaName_Lang_enUS", "Name_Lang_enUS", "Name", "name")
            )
            if not name:
                continue
            location = {
                "location_key": _location_key(self.expansion, "zone", zone_id),
                "expansion": self.expansion,
                "location_type": "zone",
                "source_id": zone_id,
                "map_id": _integer(_row_value(area, "ContinentID", "MapID")),
                "parent_zone_id": 0,
                "name": name,
                "display_name": name,
            }
            locations.append(location)
            zones_by_id[zone_id] = location

        boxes = []
        for row in world_map_area_rows:
            boxes.append(
                {
                    "map_id": _integer(_row_value(row, "MapID")),
                    "area_id": _integer(_row_value(row, "AreaID")),
                    "y1": _float(_row_value(row, "Y1")),
                    "y2": _float(_row_value(row, "Y2")),
                    "x1": _float(_row_value(row, "X1")),
                    "x2": _float(_row_value(row, "X2")),
                }
            )

        instances_by_map: dict[int, dict[str, Any]] = {}
        for map_id in sorted(instance_maps):
            row = maps[map_id]
            map_type = _integer(_row_value(row, "MapType", "InstanceType"))
            location_type = "raid" if map_type == 2 else "dungeon"
            name = _clean_text(
                _row_value(row, "MapName_Lang_enUS", "Name_Lang_enUS", "Name", "name")
            )
            if not name:
                continue
            parent_zone_id = INSTANCE_PARENT_ZONE_IDS.get(map_id, 0)
            if not parent_zone_id:
                entrance_map = _integer(_row_value(row, "EntranceMap"), -1)
                entrance_x = _float(_row_value(row, "EntranceX"))
                entrance_y = _float(_row_value(row, "EntranceY"))
                candidates = []
                if entrance_map >= 0 and (entrance_x or entrance_y):
                    for box in boxes:
                        if box["map_id"] != entrance_map:
                            continue
                        if min(box["y1"], box["y2"]) <= entrance_y <= max(
                            box["y1"], box["y2"]
                        ) and min(box["x1"], box["x2"]) <= entrance_x <= max(box["x1"], box["x2"]):
                            size = abs((box["y1"] - box["y2"]) * (box["x1"] - box["x2"]))
                            candidates.append((size, box["area_id"]))
                if candidates:
                    parent_zone_id = min(candidates)[1]
            parent = zones_by_id.get(parent_zone_id)
            display_name = f"{parent['name']} - {name}" if parent else name
            location = {
                "location_key": _location_key(self.expansion, location_type, map_id),
                "expansion": self.expansion,
                "location_type": location_type,
                "source_id": map_id,
                "map_id": map_id,
                "parent_zone_id": parent_zone_id,
                "name": name,
                "display_name": display_name,
            }
            locations.append(location)
            instances_by_map[map_id] = location

        return (
            sorted(locations, key=lambda row: (str(row["display_name"]), str(row["location_key"]))),
            zones_by_id,
            instances_by_map,
        )

    def _quest_relations(self, relation: str) -> dict[int, set[tuple[str, int, str]]]:
        relationships: dict[int, set[tuple[str, int, str]]] = defaultdict(set)
        for entity_type, table in (
            ("creature", f"creature_quest{relation}"),
            ("gameobject", f"gameobject_quest{relation}"),
        ):
            for row in self._rows(table):
                quest_id = _integer(_row_value(row, "quest"))
                entity_id = _integer(_row_value(row, "id"))
                if quest_id and entity_id:
                    relationships[quest_id].add((entity_type, entity_id, table))
        return relationships

    @staticmethod
    def _infer_role(name: str, subname: str, template: dict[str, Any], entity_type: str) -> str:
        haystack = f"{name} {subname}".lower()
        groups = (
            ("royalty", ("king", "queen", "prince", "princess", "warchief")),
            ("officer", ("captain", "commander", "general", "marshal", "sergeant")),
            ("soldier", ("guard", "sentinel", "soldier", "trooper", "grunt")),
            ("highborn", ("lord", "lady", "baron", "duke", "noble", "highborn")),
            ("merchant", ("merchant", "vendor", "trader", "supplier")),
            ("innkeeper", ("innkeeper", "barkeep", "bartender")),
            ("artisan", ("smith", "alchemist", "engineer", "tailor", "carpenter")),
            ("scholar", ("scholar", "historian", "archivist", "librarian", "mage")),
            ("spiritual_leader", ("priest", "shaman", "druid", "oracle", "seer")),
            ("outlaw", ("bandit", "pirate", "thief", "assassin", "smuggler")),
            ("peasant", ("peasant", "farmer", "laborer", "worker", "villager")),
        )
        for role, terms in groups:
            if any(term in haystack for term in terms):
                return role
        npc_flags = _integer(_row_value(template, "npcflag"))
        if npc_flags & 128:
            return "merchant"
        if npc_flags & (16 | 32 | 64):
            return "scholar"
        return "default" if entity_type == "creature" else "other"

    @staticmethod
    def _resolve_npc_text(
        npc_row: dict[str, Any], broadcast_rows: dict[int, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for slot in range(8):
            broadcast_id = _integer(_row_value(npc_row, f"BroadcastTextID{slot}"))
            broadcast = broadcast_rows.get(broadcast_id, {})
            values = (
                (
                    ("male", _clean_text(_row_value(broadcast, "MaleText"))),
                    ("female", _clean_text(_row_value(broadcast, "FemaleText"))),
                )
                if broadcast
                else (
                    ("male", _clean_text(_row_value(npc_row, f"text{slot}_0"))),
                    ("female", _clean_text(_row_value(npc_row, f"text{slot}_1"))),
                )
            )
            for variant, text in values:
                if not text:
                    continue
                results.append(
                    {
                        "slot": slot,
                        "variant": variant,
                        "text": text,
                        "context": {
                            "broadcast_text_id": broadcast_id,
                            "probability": _row_value(npc_row, f"Probability{slot}", default=0),
                            "language_id": _integer(_row_value(broadcast, "LanguageID")),
                            "sound_entries_id": _integer(
                                _row_value(broadcast, "SoundEntriesID", "SoundEntriesId")
                            ),
                        },
                    }
                )
        return results


def write_corpus_bundle(bundle: CorpusBundle, path: Path) -> Path:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    artifact_bytes = {
        name: _csv_bytes(rows, FIELD_MAP[name]) for name, rows in bundle.as_files().items()
    }
    manifest = json.loads(json.dumps(bundle.manifest))
    manifest["artifacts"] = {
        name: {
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "rows": len(bundle.as_files()[name]),
        }
        for name, content in artifact_bytes.items()
    }
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        for name in CORPUS_FILES:
            archive.writestr(name, artifact_bytes[name])
    return destination


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != CORPUS_SCHEMA_VERSION:
        raise CorpusError(f"Unsupported corpus schema version: {manifest.get('schema_version')!r}.")
    if not str(manifest.get("extractor_version", "")).strip():
        raise CorpusError("Corpus manifest has no extractor version.")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise CorpusError("Corpus manifest has no source provenance.")
    for field in ("name", "version", "expansion", "locale"):
        if not str(source.get(field, "")).strip():
            raise CorpusError(f"Corpus source provenance is missing {field}.")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(source.get("sha256", ""))):
        raise CorpusError("Corpus source provenance has no valid SHA-256.")
    if not re.fullmatch(r"[A-Za-z]{4}", str(source["locale"])):
        raise CorpusError("Corpus source locale must look like enUS.")
    additional_artifacts = source.get("additional_artifacts", [])
    if not isinstance(additional_artifacts, list):
        raise CorpusError("Corpus source artifact provenance is invalid.")
    for artifact in additional_artifacts:
        if (
            not isinstance(artifact, dict)
            or not str(artifact.get("name", "")).strip()
            or not re.fullmatch(r"[0-9a-fA-F]{64}", str(artifact.get("sha256", "")))
        ):
            raise CorpusError("Corpus source artifact provenance is invalid.")
    if not isinstance(manifest.get("tables"), dict) or not manifest["tables"]:
        raise CorpusError("Corpus manifest has no source-table fingerprints.")
    for table, fingerprint in manifest["tables"].items():
        if not isinstance(fingerprint, dict):
            raise CorpusError(f"Corpus table fingerprint is invalid: {table}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint.get("fingerprint", ""))):
            raise CorpusError(f"Corpus table fingerprint is invalid: {table}")
        if not isinstance(fingerprint.get("columns"), list):
            raise CorpusError(f"Corpus table schema is invalid: {table}")
        if _integer(fingerprint.get("rows"), -1) < 0:
            raise CorpusError(f"Corpus table count is invalid: {table}")


def load_corpus_bundle(path: Path) -> CorpusBundle:
    source_path = path.expanduser().resolve()
    if not source_path.is_file():
        raise CorpusError(f"Corpus bundle was not found: {source_path}")
    try:
        with zipfile.ZipFile(source_path) as archive:
            names = set(archive.namelist())
            expected = {"manifest.json", *CORPUS_FILES}
            if names != expected:
                missing = sorted(expected - names)
                extra = sorted(names - expected)
                details = []
                if missing:
                    details.append(f"missing {', '.join(missing)}")
                if extra:
                    details.append(f"unexpected {', '.join(extra)}")
                raise CorpusError(f"Corpus bundle contents are invalid: {'; '.join(details)}.")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            _validate_manifest(manifest)
            parsed: dict[str, list[dict[str, str]]] = {}
            for name in CORPUS_FILES:
                content = archive.read(name)
                expected_hash = str(manifest.get("artifacts", {}).get(name, {}).get("sha256", ""))
                actual_hash = hashlib.sha256(content).hexdigest()
                if not expected_hash or not hmac.compare_digest(expected_hash, actual_hash):
                    raise CorpusError(f"Corpus artifact hash does not match: {name}")
                parsed[name] = _read_csv_bytes(content, FIELD_MAP[name], name)
                artifact = manifest.get("artifacts", {}).get(name, {})
                if _integer(artifact.get("bytes"), -1) != len(content):
                    raise CorpusError(f"Corpus artifact byte count does not match: {name}")
                if _integer(artifact.get("rows"), -1) != len(parsed[name]):
                    raise CorpusError(f"Corpus artifact row count does not match: {name}")
    except (zipfile.BadZipFile, OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"Could not read corpus bundle: {error}") from error

    _validate_relationships(parsed)
    counts = manifest.get("counts", {})
    expected_counts = {
        "locations": len(parsed["locations.csv"]),
        "entities": len(parsed["entities.csv"]),
        "texts": len(parsed["texts.csv"]),
        "bindings": len(parsed["bindings.csv"]),
        "triggers": len(parsed["triggers.csv"]),
        "findings": len(parsed["quarantine.csv"]),
    }
    for key, actual in expected_counts.items():
        if _integer(counts.get(key), -1) != actual:
            raise CorpusError(f"Manifest count does not match {key}.")
    return CorpusBundle(
        manifest=manifest,
        locations=parsed["locations.csv"],
        entities=parsed["entities.csv"],
        texts=parsed["texts.csv"],
        bindings=parsed["bindings.csv"],
        triggers=parsed["triggers.csv"],
        quarantine=parsed["quarantine.csv"],
    )


def _validate_relationships(files: dict[str, list[dict[str, str]]]) -> None:
    def unique(rows: list[dict[str, str]], field: str, label: str) -> set[str]:
        values = [row[field] for row in rows]
        if any(not value for value in values):
            raise CorpusError(f"{label} contains an empty {field}.")
        if len(values) != len(set(values)):
            raise CorpusError(f"{label} contains duplicate {field} values.")
        return set(values)

    location_ids = unique(files["locations.csv"], "location_key", "locations.csv")
    zone_source_ids = {
        (row["expansion"], row["source_id"])
        for row in files["locations.csv"]
        if row["location_type"] == "zone"
    }
    for row in files["locations.csv"]:
        if row["location_type"] not in {"zone", "dungeon", "raid"}:
            raise CorpusError(f"Location {row['location_key']} has an invalid type.")
        if not row["display_name"]:
            raise CorpusError(f"Location {row['location_key']} has no display name.")
        if (
            row["parent_zone_id"] != "0"
            and (row["expansion"], row["parent_zone_id"]) not in zone_source_ids
        ):
            raise CorpusError(f"Location {row['location_key']} has a missing parent zone.")
    entity_ids = unique(files["entities.csv"], "entity_key", "entities.csv")
    content_ids = unique(files["texts.csv"], "content_id", "texts.csv")
    binding_ids = unique(files["bindings.csv"], "binding_id", "bindings.csv")
    unique(files["triggers.csv"], "trigger_id", "triggers.csv")
    unique(files["quarantine.csv"], "finding_id", "quarantine.csv")
    finding_content_ids = {
        row["content_id"] for row in files["quarantine.csv"] if row["content_id"]
    }
    finding_binding_ids = {
        row["binding_id"] for row in files["quarantine.csv"] if row["binding_id"]
    }
    bound_content_ids: set[str] = set()
    for row in files["bindings.csv"]:
        if row["content_id"] not in content_ids:
            raise CorpusError(f"Binding {row['binding_id']} references missing content.")
        if row["entity_key"] not in entity_ids:
            raise CorpusError(f"Binding {row['binding_id']} references missing entity.")
        bound_content_ids.add(row["content_id"])
        if row["active"] not in {"0", "1"}:
            raise CorpusError(f"Binding {row['binding_id']} has an invalid active value.")
        if row["active"] == "0" and row["binding_id"] not in finding_binding_ids:
            raise CorpusError(f"Quarantined binding {row['binding_id']} has no documented finding.")
    for row in files["entities.csv"]:
        if row["zone_location_key"] and row["zone_location_key"] not in location_ids:
            raise CorpusError(f"Entity {row['entity_key']} references a missing corpus location.")
    for row in files["triggers.csv"]:
        if row["binding_id"] not in binding_ids:
            raise CorpusError(f"Trigger {row['trigger_id']} references missing binding.")
    for row in files["quarantine.csv"]:
        if row["content_id"] and row["content_id"] not in content_ids:
            raise CorpusError(f"Finding {row['finding_id']} references missing content.")
        if row["binding_id"] and row["binding_id"] not in binding_ids:
            raise CorpusError(f"Finding {row['finding_id']} references missing binding.")
        if row["entity_key"] and row["entity_key"] not in entity_ids:
            raise CorpusError(f"Finding {row['finding_id']} references missing entity.")
    unreconciled = content_ids - bound_content_ids - finding_content_ids
    if unreconciled:
        raise CorpusError(
            "Corpus contains text with neither a binding nor a quarantine finding: "
            + ", ".join(sorted(unreconciled)[:5])
        )


def corpus_bundle_summary(bundle: CorpusBundle) -> dict[str, Any]:
    counts = dict(bundle.manifest.get("counts", {}))
    counts["source_changed"] = 0
    counts["added"] = len(bundle.bindings)
    counts["changed"] = 0
    counts["removed"] = 0
    counts["ambiguous_models"] = sum(
        1 for row in bundle.quarantine if row.get("reason") == "ambiguous_model"
    )
    return {
        "schema_version": bundle.manifest["schema_version"],
        "source": bundle.manifest["source"],
        "counts": counts,
    }
