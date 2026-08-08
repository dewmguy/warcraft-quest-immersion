from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = PROJECT_ROOT / "assets"
SQL_DIR = ASSETS_DIR / "sql"
DB_DUMP_DIR = SQL_DIR / "db_dump"
MODULE_NAME = "AI_VoiceOverData_Vanilla"
MODULE_DIR = PROJECT_ROOT / MODULE_NAME
OUTPUT_DIR = MODULE_DIR / "generated"
SOUND_OUTPUT_DIR = OUTPUT_DIR / "sounds"
SAMPLE_DATA_PATH = ASSETS_DIR / "samples" / "dialogue.csv"
