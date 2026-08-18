from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from tts_cli.cli import main
from tts_cli.dbc import DBC_DEFINITIONS, DBCError, WDBCFile, convert_dbc_directory_to_sql


def _write_dbc(path: Path, field_count: int, *, name_field: int | None = None) -> None:
    values = [0] * field_count
    values[0] = 7
    strings = b"\0"
    if name_field is not None:
        values[name_field] = len(strings)
        strings += (
            b"Creature\\Human\\HumanMale.m2\0"
            if path.name == "CreatureModelData.dbc"
            else b"Explorer's League\0"
        )
    if path.name == "CreatureDisplayInfo.dbc":
        values[1] = 60
        values[3] = 70
    elif path.name == "CreatureDisplayInfoExtra.dbc":
        values[1:3] = [3, 1]
    elif path.name == "FactionTemplate.dbc":
        values[1] = 72
    body = struct.pack(f"<{field_count}I", *values)
    header = struct.pack("<4s4I", b"WDBC", 1, field_count, len(body), len(strings))
    path.write_bytes(header + body + strings)


@pytest.fixture
def dbc_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "dbc" / "DBFilesClient"
    directory.mkdir(parents=True)
    for definition in DBC_DEFINITIONS:
        string_field = next(
            (
                field_index
                for _, field_type, field_index in definition.columns
                if field_type == "string"
            ),
            None,
        )
        _write_dbc(directory / definition.filename, definition.field_count, name_field=string_field)
    return directory.parent


def test_convert_raw_335a_dbc_files_to_minimal_enrichment_sql(dbc_directory: Path, tmp_path: Path):
    output = tmp_path / "dbc.sql"

    report = convert_dbc_directory_to_sql(dbc_directory, output)

    sql = output.read_text(encoding="utf-8")
    assert "CREATE TABLE `db_CreatureDisplayInfo`" in sql
    assert "(7, 60, 70)" in sql
    assert "(7, 3, 1)" in sql
    assert "Creature\\\\Human\\\\HumanMale.m2" in sql
    assert "Explorer\\'s League" in sql
    assert report["build"] == 12340
    assert report["locale"] == "enUS"
    assert report["tables"]["db_AreaTable"] == 1
    assert len(report["artifacts"]) == 6
    saved_manifest = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))
    assert saved_manifest["output_sha256"] == report["output_sha256"]


def test_dbc_conversion_is_deterministic(dbc_directory: Path, tmp_path: Path):
    first = tmp_path / "first.sql"
    second = tmp_path / "second.sql"

    first_report = convert_dbc_directory_to_sql(dbc_directory, first)
    second_report = convert_dbc_directory_to_sql(dbc_directory, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_report["output_sha256"] == second_report["output_sha256"]


def test_dbc_conversion_rejects_missing_required_artifact(dbc_directory: Path, tmp_path: Path):
    (dbc_directory / "DBFilesClient" / "Faction.dbc").unlink()

    with pytest.raises(DBCError, match="Required DBC is missing: Faction.dbc"):
        convert_dbc_directory_to_sql(dbc_directory, tmp_path / "dbc.sql")


def test_wdbc_reader_rejects_unknown_format(tmp_path: Path):
    path = tmp_path / "AreaTable.dbc"
    path.write_bytes(struct.pack("<4s4I", b"WDB2", 0, 36, 144, 0))

    with pytest.raises(DBCError, match="unsupported magic"):
        WDBCFile(path)


def test_corpus_dbc_to_sql_cli(dbc_directory: Path, tmp_path: Path, capsys):
    output = tmp_path / "dbc.sql"

    assert main(["corpus", "dbc-to-sql", str(dbc_directory), "--output", str(output)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["output"] == str(output.resolve())
    assert output.is_file()
