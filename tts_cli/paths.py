import os
from collections.abc import Mapping
from pathlib import Path


def resolve_project_root(
    *,
    module_file: Path = Path(__file__),
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    configured = environment.get("WQI_PROJECT_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    package_root = module_file.resolve().parents[1]
    working_root = (cwd or Path.cwd()).resolve()
    for candidate in (working_root, package_root):
        if (candidate / "voice_profiles" / "phase2-manifest.json").is_file():
            return candidate
    return package_root


PROJECT_ROOT = resolve_project_root()
ASSETS_DIR = PROJECT_ROOT / "assets"
SQL_DIR = ASSETS_DIR / "sql"
DB_DUMP_DIR = SQL_DIR / "db_dump"
MODULE_NAME = "AI_VoiceOverData_Vanilla"
MODULE_DIR = PROJECT_ROOT / MODULE_NAME
OUTPUT_DIR = MODULE_DIR / "generated"
SOUND_OUTPUT_DIR = OUTPUT_DIR / "sounds"
SAMPLE_DATA_PATH = ASSETS_DIR / "samples" / "dialogue.csv"
