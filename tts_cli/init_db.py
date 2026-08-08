from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path

import pymysql
import requests
from tqdm import tqdm

from tts_cli.config import Settings, load_settings
from tts_cli.paths import DB_DUMP_DIR, SQL_DIR

VMANGOS_DB_DUMP_URL = "https://api.github.com/repos/vmangos/core/releases/tags/db_latest"
EXPORTED_FILES = (
    SQL_DIR / "exported" / "CreatureDisplayInfo.sql",
    SQL_DIR / "exported" / "CreatureDisplayInfoExtra.sql",
)
SQL_DELIMITER = b";\n"


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if target != destination and destination not in target.parents:
            raise RuntimeError(f"Unsafe path in database archive: {member.filename}")
    archive.extractall(destination)


def download_and_extract_latest_db_dump(
    destination: str | Path = SQL_DIR,
    session: requests.Session | None = None,
) -> Path:
    """Download the current vMaNGOS database dump without loading it all into memory."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    session = session or requests.Session()

    print("Retrieving the latest vMaNGOS database release...")
    release_response = session.get(VMANGOS_DB_DUMP_URL, timeout=30)
    release_response.raise_for_status()
    assets = release_response.json().get("assets", [])
    if not assets:
        raise RuntimeError("The vMaNGOS database release does not contain a downloadable asset.")

    download_url = assets[0].get("browser_download_url")
    if not download_url:
        raise RuntimeError("The vMaNGOS database asset has no download URL.")

    temporary_path: Path | None = None
    try:
        with session.get(download_url, stream=True, timeout=(30, 300)) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length", 0))
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".zip", dir=destination, delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                with tqdm(
                    total=total, unit="B", unit_scale=True, desc="Downloading database"
                ) as progress:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            temporary_file.write(chunk)
                            progress.update(len(chunk))

        print("Extracting database files...")
        with zipfile.ZipFile(temporary_path) as archive:
            _safe_extract(archive, destination)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()

    if not DB_DUMP_DIR.is_dir():
        raise RuntimeError(f"Database archive did not create the expected folder: {DB_DUMP_DIR}")
    return DB_DUMP_DIR


def count_total_chunks(files: list[Path], delimiter: bytes) -> int:
    total_chunks = 0
    for file in files:
        with file.open("rb") as handle:
            total_chunks += handle.read().count(delimiter)
    return total_chunks


def count_commands_from_file(filename: Path) -> int:
    with filename.open("r", encoding="utf-8") as handle:
        return len(handle.read().split(";"))


def execute_scripts_from_file(cursor, filename: Path, progress_update_fn) -> None:
    with filename.open("r", encoding="utf-8") as handle:
        sql_commands = handle.read().split(";")

    for command in sql_commands:
        if command.strip():
            cursor.execute(command)
        progress_update_fn()


def _validated_database_name(database_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", database_name):
        raise ValueError("MYSQL_DATABASE may contain only letters, numbers, and underscores.")
    return database_name


def import_sql_files_to_database(settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    database_name = _validated_database_name(settings.mysql_database)
    if not DB_DUMP_DIR.is_dir():
        raise FileNotFoundError(
            f"Database dump not found at {DB_DUMP_DIR}. Download it before importing."
        )

    db = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        charset="utf8mb4",
        connect_timeout=10,
    )
    try:
        with db.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database_name}`;")
            cursor.execute(f"USE `{database_name}`;")

            sql_files = sorted(DB_DUMP_DIR.rglob("*.sql"))
            if not sql_files:
                raise FileNotFoundError(f"No SQL files were found in {DB_DUMP_DIR}.")

            total_chunks = count_total_chunks(sql_files, SQL_DELIMITER) + sum(
                count_commands_from_file(file) for file in EXPORTED_FILES
            )

            with tqdm(
                total=total_chunks,
                unit="commands",
                desc="Importing SQL files",
                ncols=100,
            ) as progress:
                for file in sql_files:
                    _execute_chunked_file(db, cursor, file, progress)

                for file in EXPORTED_FILES:
                    execute_scripts_from_file(cursor, file, progress.update)
                    db.commit()
    finally:
        db.close()


def _execute_chunked_file(db, cursor, filename: Path, progress) -> None:
    chunk_size = 1024 * 1024
    with filename.open("rb") as handle:
        buffer = bytearray()
        while chunk := handle.read(chunk_size):
            buffer.extend(chunk)
            while SQL_DELIMITER in buffer:
                position = buffer.index(SQL_DELIMITER)
                sql_command = buffer[:position].decode("utf-8")
                if sql_command.strip():
                    cursor.execute(sql_command)
                buffer = buffer[position + len(SQL_DELIMITER) :]
                progress.update(1)
        if buffer.strip():
            cursor.execute(buffer.decode("utf-8"))
    db.commit()


if __name__ == "__main__":
    download_and_extract_latest_db_dump()
    import_sql_files_to_database()
    print("Database initialized successfully.")
