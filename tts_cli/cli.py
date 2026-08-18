from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pymysql
from prompt_toolkit.shortcuts import checkboxlist_dialog, radiolist_dialog, yes_no_dialog

from tts_cli import __version__, utils
from tts_cli.alpha_store import AlphaError, AlphaStore
from tts_cli.config import ConfigurationError, load_settings
from tts_cli.consts import GENDER_DICT_INV, RACE_DICT_INV, race_gender_tuple_to_strings
from tts_cli.corpus import (
    AzerothCoreCorpusExtractor,
    MySQLCorpusSource,
    write_corpus_bundle,
)
from tts_cli.data_sources import DataSourceError, load_dialogue_csv, write_dialogue_csv
from tts_cli.dbc import convert_dbc_directory_to_sql
from tts_cli.init_db import download_and_extract_latest_db_dump, import_sql_files_to_database
from tts_cli.paths import PROJECT_ROOT, SAMPLE_DATA_PATH
from tts_cli.sql_queries import (
    make_connection,
    query_dataframe_for_all_quests_and_gossip,
    query_dataframe_for_area,
)
from tts_cli.tts_utils import TTSProcessor
from tts_cli.wrath_model_extraction import write_model_data
from tts_cli.zone_selector import EasternKingdomsZoneSelector, KalimdorZoneSelector

MAP_CHOICES = {
    -1: "All maps (includes dungeons)",
    0: "Eastern Kingdoms",
    1: "Kalimdor",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqi",
        description="Generate World of Warcraft VoiceOver audio and lookup data.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("doctor", help="Check the local setup without changing it").add_argument(
        "--services",
        action="store_true",
        help="also require a database connection and ElevenLabs key",
    )

    init_parser = subparsers.add_parser("init-db", help="Download and import the vMaNGOS database")
    init_parser.add_argument(
        "--skip-download", action="store_true", help="import an already-downloaded database dump"
    )

    interactive_parser = subparsers.add_parser(
        "interactive", help="Interactively select dialogue and generate audio"
    )
    interactive_parser.add_argument(
        "--input-csv",
        type=Path,
        help="use a dialogue CSV instead of querying MySQL",
    )

    lookup_parser = subparsers.add_parser(
        "generate-lookups",
        aliases=["gen_lookup_tables"],
        help="Generate addon lookup tables from CSV or MySQL",
    )
    lookup_parser.add_argument("--lang", default="enUS", help="WoW locale code")
    lookup_parser.add_argument("--input-csv", type=Path, help="validated dialogue CSV input")

    export_parser = subparsers.add_parser(
        "export-data", help="Export the complete MySQL dialogue query to a reusable CSV"
    )
    export_parser.add_argument("--lang", default="enUS", help="WoW locale code")
    export_parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "dialogue.csv",
        help="destination CSV path",
    )

    subparsers.add_parser(
        "extract-model-data",
        aliases=["extract_model_data"],
        help="Generate NPC model metadata from MySQL",
    )

    corpus_parser = subparsers.add_parser(
        "corpus", help="Extract, validate, and atomically import corpus bundles"
    )
    corpus_commands = corpus_parser.add_subparsers(dest="corpus_command", required=True)
    extract_parser = corpus_commands.add_parser(
        "extract", help="Extract a certified bundle from the restored AzerothCore snapshot"
    )
    extract_parser.add_argument("--source-dump", type=Path, required=True)
    extract_parser.add_argument(
        "--source-artifact",
        type=Path,
        action="append",
        default=[],
        help="additional provenance-matched DBC SQL artifact; may be repeated",
    )
    extract_parser.add_argument("--source-version", required=True)
    extract_parser.add_argument("--source-name", default="azerothcore-world")
    extract_parser.add_argument("--expansion", default="3.3.5")
    extract_parser.add_argument("--locale", default="enUS")
    extract_parser.add_argument("--output", type=Path, required=True)
    dbc_parser = corpus_commands.add_parser(
        "dbc-to-sql", help="Convert the eight raw build-12340 DBC files into enrichment SQL"
    )
    dbc_parser.add_argument("dbc_directory", type=Path)
    dbc_parser.add_argument("--output", type=Path, required=True)
    validate_parser = corpus_commands.add_parser(
        "validate", help="Validate a complete corpus bundle without changing Alpha"
    )
    validate_parser.add_argument("bundle", type=Path)
    import_parser = corpus_commands.add_parser(
        "import", help="Dry-run or atomically apply a validated corpus bundle"
    )
    import_parser.add_argument("bundle", type=Path)
    import_parser.add_argument("--dry-run", action="store_true")
    import_parser.add_argument(
        "--yes", action="store_true", help="confirm the production snapshot replacement"
    )
    return parser


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _alpha_store() -> AlphaStore:
    data_dir = Path(os.getenv("WQI_DATA_DIR", PROJECT_ROOT / "data")).resolve()
    store = AlphaStore(data_dir / "alpha" / "production.sqlite3", data_dir / "alpha")
    store.initialize()
    return store


def corpus_command(args: argparse.Namespace) -> int:
    if args.corpus_command == "dbc-to-sql":
        report = convert_dbc_directory_to_sql(args.dbc_directory, args.output)
        print(json.dumps(report, indent=2))
        return 0

    if args.corpus_command == "extract":
        source_dump = args.source_dump.expanduser().resolve()
        if not source_dump.is_file():
            raise FileNotFoundError(f"AzerothCore snapshot was not found: {source_dump}")
        source_artifacts = []
        for artifact in args.source_artifact:
            artifact = artifact.expanduser().resolve()
            if not artifact.is_file():
                raise FileNotFoundError(f"AzerothCore source artifact was not found: {artifact}")
            source_artifacts.append({"name": artifact.name, "sha256": _file_sha256(artifact)})
        settings = load_settings()
        connection = pymysql.connect(
            host=settings.azerothcore_mysql_host,
            port=settings.azerothcore_mysql_port,
            user=settings.azerothcore_mysql_user,
            password=settings.azerothcore_mysql_password,
            database=settings.azerothcore_mysql_database,
            charset="utf8mb4",
            autocommit=True,
        )
        try:
            source = MySQLCorpusSource(connection, settings.azerothcore_mysql_database)
            bundle = AzerothCoreCorpusExtractor(
                source,
                expansion=args.expansion,
                locale=args.locale,
                source_name=args.source_name,
                source_sha256=_file_sha256(source_dump),
                source_version=args.source_version,
                source_artifacts=source_artifacts,
            ).extract()
            output = write_corpus_bundle(bundle, args.output)
        finally:
            connection.close()
        print(json.dumps({"bundle": str(output), "manifest": bundle.manifest}, indent=2))
        return 0

    store = _alpha_store()
    if args.corpus_command == "validate":
        report = store.validate_corpus_bundle(args.bundle)
        print(json.dumps(report, indent=2))
        return 0 if report["valid"] else 2
    if args.corpus_command == "import":
        report = store.import_corpus_bundle(args.bundle, dry_run=True)
        print(json.dumps(report, indent=2))
        if args.dry_run:
            return 0 if report["valid"] else 2
        if not report["valid"]:
            return 2
        if not args.yes:
            confirmation = input("Type IMPORT to atomically replace the active corpus snapshot: ")
            if confirmation.strip() != "IMPORT":
                print("Import cancelled.")
                return 2
        applied = store.import_corpus_bundle(args.bundle)
        print(json.dumps(applied, indent=2))
        return 0
    raise AlphaError("Unknown corpus command.")


def _select_database_area():
    map_id = radiolist_dialog(
        title="Select a map",
        text="Choose a map:",
        values=list(MAP_CHOICES.items()),
    ).run()
    if map_id is None:
        return None, None

    if map_id < 0:
        return query_dataframe_for_all_quests_and_gossip(), MAP_CHOICES[map_id]

    zone_selector = EasternKingdomsZoneSelector() if map_id == 0 else KalimdorZoneSelector()
    coordinate_ranges = zone_selector.select_zone()
    if coordinate_ranges is None:
        return None, None
    x_range, y_range = coordinate_ranges
    return query_dataframe_for_area(x_range, y_range, map_id), (
        f"{MAP_CHOICES[map_id]} (x={x_range}, y={y_range})"
    )


def prompt_user(tts_processor: TTSProcessor, input_csv: Path | None = None):
    if input_csv:
        dataframe = load_dialogue_csv(input_csv)
        source_summary = str(input_csv.resolve())
    else:
        dataframe, source_summary = _select_database_area()
        if dataframe is None:
            return None

    if dataframe.empty:
        raise DataSourceError("The selected data source returned no dialogue rows.")

    unique_combinations = dataframe[["DisplayRaceID", "DisplaySexID"]].drop_duplicates().values
    required_voices = set(race_gender_tuple_to_strings(tuple(map(tuple, unique_combinations))))
    available_voices = set(tts_processor.get_voice_map())
    missing_voices = required_voices - available_voices
    selectable_voices = required_voices & available_voices

    voice_choices = sorted((voice, f"{voice} (found)") for voice in selectable_voices)
    voice_choices.extend(sorted((voice, f"{voice} (missing)") for voice in missing_voices))
    all_found_option = "all-found"
    voice_choices.insert(0, (all_found_option, "All available voices"))

    selected_voices = checkboxlist_dialog(
        title="Choose voices",
        text="Missing voices are shown for reference but will not be generated.",
        values=voice_choices,
    ).run()
    if selected_voices is None:
        return None

    if all_found_option in selected_voices:
        selected_voice_names = sorted(selectable_voices)
    else:
        selected_voice_names = sorted(set(selected_voices) - missing_voices)

    if not selected_voice_names:
        raise DataSourceError("No available voices were selected.")

    selected_race_gender = []
    for voice in selected_voice_names:
        race, gender = voice.rsplit("-", 1)
        selected_race_gender.append((RACE_DICT_INV[race], GENDER_DICT_INV[gender]))
    selected_voice_names = race_gender_tuple_to_strings(selected_race_gender)

    estimate = tts_processor.preprocess_dataframe(dataframe)
    estimate = estimate.loc[estimate["voice_name"].isin(selected_voice_names)]
    estimate = estimate.loc[~estimate["source"].str.contains("progress")]
    estimate = estimate[["text", "DisplayRaceID", "DisplaySexID"]].drop_duplicates()
    total_characters = estimate["text"].str.len().sum()

    confirmed = yes_no_dialog(
        title="Summary",
        text=(
            f"Data source: {source_summary}\n"
            f"Selected voices: {', '.join(selected_voice_names)}\n"
            f"Approximate text characters: {total_characters}"
        ),
        yes_text="Generate",
        no_text="Cancel",
    ).run()
    if not confirmed:
        return None
    return dataframe, selected_voice_names


def interactive_mode(input_csv: Path | None = None) -> None:
    processor = TTSProcessor(fetch_voices=True)
    selection = prompt_user(processor, input_csv)
    if selection is None:
        print("Cancelled.")
        return
    dataframe, selected_voice_names = selection
    processor.tts_dataframe(processor.preprocess_dataframe(dataframe), selected_voice_names)


def doctor(check_services: bool = False) -> int:
    problems = []
    settings = load_settings()

    print(f"[ok] Python {sys.version_info.major}.{sys.version_info.minor}")
    load_dialogue_csv(SAMPLE_DATA_PATH)
    print(f"[ok] Sample data: {SAMPLE_DATA_PATH}")
    print(f"[ok] Project root: {PROJECT_ROOT}")
    docker = shutil.which("docker")
    print(
        f"[info] Docker CLI: {docker or 'not installed (only needed for the full MySQL workflow)'}"
    )

    if settings.elevenlabs_api_key and settings.elevenlabs_api_key != "API_KEY_HERE":
        print("[ok] ElevenLabs API key is configured")
    else:
        message = "ElevenLabs API key is not configured (only needed for audio generation)"
        print(f"[info] {message}")
        if check_services:
            problems.append(message)

    if check_services:
        try:
            connection = make_connection(settings, connect_timeout=3)
            connection.close()
            print(f"[ok] MySQL: {settings.mysql_host}:{settings.mysql_port}")
        except pymysql.MySQLError as error:
            message = (
                f"MySQL is unavailable at {settings.mysql_host}:{settings.mysql_port}: {error}"
            )
            print(f"[error] {message}")
            problems.append(message)
    else:
        print("[info] MySQL connection was not required; use --services to check it")

    if problems:
        print(f"Setup needs attention ({len(problems)} issue(s)).")
        return 1
    print("Local tooling is ready.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command is None:
            interactive_mode()
        elif args.command == "doctor":
            return doctor(args.services)
        if args.command == "init-db":
            if not args.skip_download:
                download_and_extract_latest_db_dump()
            import_sql_files_to_database()
            print("Database initialized successfully.")
        elif args.command == "interactive":
            interactive_mode(args.input_csv)
        elif args.command in {"generate-lookups", "gen_lookup_tables"}:
            language_number = utils.language_code_to_language_number(args.lang)
            dataframe = (
                load_dialogue_csv(args.input_csv)
                if args.input_csv
                else query_dataframe_for_all_quests_and_gossip(language_number)
            )
            processor = TTSProcessor(fetch_voices=False)
            processor.generate_lookup_tables(processor.preprocess_dataframe(dataframe))
        elif args.command == "export-data":
            language_number = utils.language_code_to_language_number(args.lang)
            output = write_dialogue_csv(
                query_dataframe_for_all_quests_and_gossip(language_number), args.output
            )
            print(f"Exported dialogue data to {output}")
        elif args.command in {"extract-model-data", "extract_model_data"}:
            write_model_data()
        elif args.command == "corpus":
            return corpus_command(args)
    except (
        AlphaError,
        ConfigurationError,
        DataSourceError,
        FileNotFoundError,
        ValueError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except (pymysql.MySQLError, RuntimeError) as error:
        print(f"Service error: {error}", file=sys.stderr)
        return 1
    return 0
