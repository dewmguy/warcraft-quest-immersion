from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DBCError(ValueError):
    """Raised when a client DBC artifact does not match the 3.3.5a contract."""


@dataclass(frozen=True)
class DBCDefinition:
    filename: str
    table: str
    field_count: int
    columns: tuple[tuple[str, str, int], ...]


DBC_DEFINITIONS = (
    DBCDefinition(
        "CreatureDisplayInfo.dbc",
        "db_CreatureDisplayInfo",
        16,
        (
            ("ID", "integer", 0),
            ("ModelID", "integer", 1),
            ("ExtendedDisplayInfoID", "integer", 3),
        ),
    ),
    DBCDefinition(
        "CreatureDisplayInfoExtra.dbc",
        "db_CreatureDisplayInfoExtra",
        21,
        (
            ("ID", "integer", 0),
            ("DisplayRaceID", "integer", 1),
            ("DisplaySexID", "integer", 2),
        ),
    ),
    DBCDefinition(
        "CreatureModelData.dbc",
        "db_CreatureModelData",
        28,
        (("ID", "integer", 0), ("ModelPath", "string", 2)),
    ),
    DBCDefinition(
        "FactionTemplate.dbc",
        "db_FactionTemplate",
        14,
        (("ID", "integer", 0), ("Faction", "integer", 1)),
    ),
    DBCDefinition(
        "Faction.dbc",
        "db_Faction",
        57,
        (("ID", "integer", 0), ("Name_Lang_enUS", "string", 23)),
    ),
    DBCDefinition(
        "AreaTable.dbc",
        "db_AreaTable",
        36,
        (
            ("ID", "integer", 0),
            ("ContinentID", "integer", 1),
            ("ParentAreaID", "integer", 2),
            ("Flags", "integer", 4),
            ("AreaName_Lang_enUS", "string", 11),
        ),
    ),
    DBCDefinition(
        "Map.dbc",
        "db_Map",
        66,
        (
            ("ID", "integer", 0),
            ("InternalName", "string", 1),
            ("MapType", "integer", 2),
            ("Flags", "integer", 3),
            ("MapName_Lang_enUS", "string", 5),
            ("LinkedZone", "integer", 22),
            ("EntranceMap", "signed", 59),
            ("EntranceX", "float", 60),
            ("EntranceY", "float", 61),
            ("ExpansionID", "integer", 63),
            ("MaxPlayers", "integer", 65),
        ),
    ),
    DBCDefinition(
        "WorldMapArea.dbc",
        "db_WorldMapArea",
        11,
        (
            ("ID", "integer", 0),
            ("MapID", "integer", 1),
            ("AreaID", "integer", 2),
            ("Y1", "float", 4),
            ("Y2", "float", 5),
            ("X1", "float", 6),
            ("X2", "float", 7),
            ("VirtualMapID", "signed", 8),
        ),
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class WDBCFile:
    HEADER = struct.Struct("<4s4I")

    def __init__(self, path: Path) -> None:
        self.path = path
        data = path.read_bytes()
        if len(data) < self.HEADER.size:
            raise DBCError(f"{path.name} is shorter than a WDBC header.")
        magic, self.record_count, self.field_count, self.record_size, string_size = (
            self.HEADER.unpack_from(data)
        )
        if magic != b"WDBC":
            raise DBCError(f"{path.name} has unsupported magic {magic!r}; expected WDBC.")
        if self.record_size != self.field_count * 4:
            raise DBCError(
                f"{path.name} uses a {self.record_size}-byte record for "
                f"{self.field_count} fields; expected four-byte WDBC fields."
            )
        records_end = self.HEADER.size + self.record_count * self.record_size
        expected_size = records_end + string_size
        if len(data) != expected_size:
            raise DBCError(
                f"{path.name} is {len(data)} bytes; its header describes {expected_size} bytes."
            )
        self._records = memoryview(data)[self.HEADER.size : records_end]
        self._strings = memoryview(data)[records_end:expected_size]

    def values(self, index: int) -> tuple[int, ...]:
        if not 0 <= index < self.record_count:
            raise IndexError(index)
        offset = index * self.record_size
        return struct.unpack_from(f"<{self.field_count}I", self._records, offset)

    def string(self, offset: int) -> str:
        if not 0 <= offset < len(self._strings):
            raise DBCError(
                f"{self.path.name} contains string offset {offset}, outside its string block."
            )
        remaining = self._strings[offset:].tobytes()
        terminator = remaining.find(b"\0")
        if terminator < 0:
            raise DBCError(f"{self.path.name} contains an unterminated string at offset {offset}.")
        try:
            return remaining[:terminator].decode("utf-8")
        except UnicodeDecodeError as error:
            raise DBCError(
                f"{self.path.name} contains non-UTF-8 enUS text at offset {offset}."
            ) from error


def _resolve_dbc(directory: Path, filename: str) -> Path:
    candidates = (directory / filename, directory / "DBFilesClient" / filename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise DBCError(
        f"Required DBC is missing: {filename}. Looked in {directory} and "
        f"{directory / 'DBFilesClient'}."
    )


def load_dbc_tables(directory: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise DBCError(f"DBC directory was not found: {directory}")

    tables: dict[str, list[dict[str, Any]]] = {}
    artifacts: list[dict[str, Any]] = []
    for definition in DBC_DEFINITIONS:
        path = _resolve_dbc(directory, definition.filename)
        dbc = WDBCFile(path)
        if dbc.field_count != definition.field_count:
            raise DBCError(
                f"{definition.filename} has {dbc.field_count} fields; "
                f"3.3.5a build 12340 requires {definition.field_count}."
            )

        rows: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for record_index in range(dbc.record_count):
            values = dbc.values(record_index)
            row: dict[str, Any] = {}
            for name, field_type, field_index in definition.columns:
                raw_value = values[field_index]
                if field_type == "string":
                    row[name] = dbc.string(raw_value)
                elif field_type == "float":
                    row[name] = struct.unpack("<f", struct.pack("<I", raw_value))[0]
                elif field_type == "signed":
                    row[name] = struct.unpack("<i", struct.pack("<I", raw_value))[0]
                else:
                    row[name] = raw_value
            row_id = int(row["ID"])
            if row_id in seen_ids:
                raise DBCError(f"{definition.filename} contains duplicate ID {row_id}.")
            seen_ids.add(row_id)
            rows.append(row)
        rows.sort(key=lambda row: int(row["ID"]))
        tables[definition.table] = rows
        artifacts.append(
            {
                "name": definition.filename,
                "path": str(path),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
                "records": dbc.record_count,
                "fields": dbc.field_count,
                "record_size": dbc.record_size,
            }
        )
    return tables, {"format": "WDBC", "build": 12340, "locale": "enUS", "artifacts": artifacts}


def _sql_string(value: str) -> str:
    replacements = {
        "\\": "\\\\",
        "\0": "\\0",
        "\n": "\\n",
        "\r": "\\r",
        "\x1a": "\\Z",
        "'": "\\'",
    }
    return "'" + "".join(replacements.get(character, character) for character in value) + "'"


def _table_sql(definition: DBCDefinition, rows: list[dict[str, Any]]) -> list[str]:
    column_sql = []
    for name, field_type, _ in definition.columns:
        if field_type == "string":
            sql_type = "TEXT NOT NULL"
        elif field_type == "float":
            sql_type = "DOUBLE NOT NULL"
        elif field_type == "signed":
            sql_type = "INT NOT NULL"
        else:
            sql_type = "INT UNSIGNED NOT NULL"
        column_sql.append(f"  `{name}` {sql_type}")
    column_sql.append("  PRIMARY KEY (`ID`)")
    statements = [
        f"DROP TABLE IF EXISTS `{definition.table}`;",
        f"CREATE TABLE `{definition.table}` (\n" + ",\n".join(column_sql) + "\n) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;",
    ]
    column_names = ", ".join(f"`{column[0]}`" for column in definition.columns)
    for start in range(0, len(rows), 1000):
        values = []
        for row in rows[start : start + 1000]:
            encoded = [
                _sql_string(str(row[name]))
                if field_type == "string"
                else repr(float(row[name]))
                if field_type == "float"
                else str(int(row[name]))
                for name, field_type, _ in definition.columns
            ]
            values.append("(" + ", ".join(encoded) + ")")
        statements.append(
            f"INSERT INTO `{definition.table}` ({column_names}) VALUES\n" + ",\n".join(values) + ";"
        )
    return statements


def convert_dbc_directory_to_sql(directory: Path, output: Path) -> dict[str, Any]:
    tables, manifest = load_dbc_tables(directory)
    statements = [
        "-- Warcraft Quest Immersion 3.3.5a enUS DBC enrichment tables",
        "-- Generated deterministically from raw build 12340 WDBC files.",
        "SET NAMES utf8mb4;",
    ]
    for definition in DBC_DEFINITIONS:
        statements.extend(_table_sql(definition, tables[definition.table]))
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n\n".join(statements) + "\n", encoding="utf-8", newline="\n")
    report = {
        **manifest,
        "output": str(output),
        "output_sha256": _sha256(output),
        "tables": {name: len(rows) for name, rows in tables.items()},
    }
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    report["manifest"] = str(manifest_path)
    return report
