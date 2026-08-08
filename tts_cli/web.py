from __future__ import annotations

import os
import secrets
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Annotated

import pymysql
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tts_cli.config import load_settings
from tts_cli.data_sources import DataSourceError, load_dialogue_csv
from tts_cli.paths import OUTPUT_DIR, PROJECT_ROOT, SAMPLE_DATA_PATH
from tts_cli.sql_queries import make_connection
from tts_cli.tts_utils import TTSProcessor

WEB_DIR = Path(__file__).resolve().parent / "web"
DATA_DIR = Path(os.getenv("WQI_DATA_DIR", PROJECT_ROOT / "data")).resolve()
DIALOGUE_PATH = DATA_DIR / "dialogue.csv"
MAX_UPLOAD_BYTES = int(os.getenv("WQI_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wqi-generator")
job_lock = Lock()


@dataclass
class JobState:
    state: str = "idle"
    message: str = "Ready"


job = JobState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not DIALOGUE_PATH.exists():
        shutil.copyfile(SAMPLE_DATA_PATH, DIALOGUE_PATH)
    yield
    executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Warcraft Quest Immersion", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEB_DIR / "templates")
security = HTTPBasic(auto_error=False)


def require_auth(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
) -> str:
    expected_user = os.getenv("WQI_ADMIN_USER", "admin")
    expected_password = os.getenv("WQI_ADMIN_PASSWORD", "")
    if not expected_password or expected_password == "CHANGE_ME":
        return "reverse-proxy"
    valid = (
        credentials is not None
        and secrets.compare_digest(
            credentials.username.encode("utf-8"), expected_user.encode("utf-8")
        )
        and secrets.compare_digest(
            credentials.password.encode("utf-8"), expected_password.encode("utf-8")
        )
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Warcraft Quest Immersion"'},
        )
    return credentials.username


def require_action_header(x_wqi_action: Annotated[str | None, Header()] = None) -> None:
    if x_wqi_action != "confirmed":
        raise HTTPException(status_code=400, detail="Missing action confirmation header.")


def _data_summary() -> dict:
    if not DIALOGUE_PATH.is_file():
        return {"available": False, "rows": 0, "source": str(DIALOGUE_PATH), "error": None}
    try:
        dataframe = load_dialogue_csv(DIALOGUE_PATH)
        return {
            "available": True,
            "rows": len(dataframe),
            "source": str(DIALOGUE_PATH),
            "error": None,
        }
    except DataSourceError as error:
        return {
            "available": False,
            "rows": 0,
            "source": str(DIALOGUE_PATH),
            "error": str(error),
        }


def _database_summary() -> dict:
    settings = load_settings()
    try:
        connection = make_connection(settings, connect_timeout=2)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s",
                (settings.mysql_database,),
            )
            table_count = cursor.fetchone()[0]
        connection.close()
        return {"available": True, "tables": table_count, "error": None}
    except pymysql.MySQLError as error:
        return {"available": False, "tables": 0, "error": str(error)}


def _output_summary() -> dict:
    lookup_files = sorted(OUTPUT_DIR.glob("*.lua")) if OUTPUT_DIR.is_dir() else []
    sound_files = list((OUTPUT_DIR / "sounds").rglob("*.mp3")) if OUTPUT_DIR.is_dir() else []
    return {"lookups": len(lookup_files), "sounds": len(sound_files)}


def _status_payload() -> dict:
    with job_lock:
        job_payload = asdict(job)
    settings = load_settings()
    return {
        "data": _data_summary(),
        "database": _database_summary(),
        "output": _output_summary(),
        "job": job_payload,
        "elevenlabs_configured": bool(
            settings.elevenlabs_api_key and settings.elevenlabs_api_key != "API_KEY_HERE"
        ),
        "authentication_enabled": bool(
            os.getenv("WQI_ADMIN_PASSWORD") and os.getenv("WQI_ADMIN_PASSWORD") != "CHANGE_ME"
        ),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: Annotated[str, Depends(require_auth)]):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"status": _status_payload()},
    )


@app.get("/api/status")
def api_status(_: Annotated[str, Depends(require_auth)]) -> dict:
    return _status_payload()


@app.post("/api/data")
async def upload_data(
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
    file: Annotated[UploadFile, File()],
) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = DATA_DIR / f".{secrets.token_hex(8)}.csv"
    size = 0
    try:
        with temporary_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Uploaded CSV is too large.")
                output.write(chunk)
        dataframe = load_dialogue_csv(temporary_path)
        temporary_path.replace(DIALOGUE_PATH)
    except DataSourceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
        await file.close()
    return {"message": f"Loaded {len(dataframe):,} dialogue rows.", "rows": len(dataframe)}


def _run_lookup_generation() -> None:
    with job_lock:
        job.state = "running"
        job.message = "Generating addon lookup tables..."
    try:
        dataframe = load_dialogue_csv(DIALOGUE_PATH)
        processor = TTSProcessor(fetch_voices=False)
        processor.generate_lookup_tables(processor.preprocess_dataframe(dataframe))
        with job_lock:
            job.state = "complete"
            job.message = f"Generated lookup tables from {len(dataframe):,} rows."
    except Exception as error:
        with job_lock:
            job.state = "failed"
            job.message = str(error)


@app.post("/api/generate-lookups", status_code=202)
def generate_lookups(
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    if not DIALOGUE_PATH.is_file():
        raise HTTPException(status_code=409, detail="Upload dialogue data first.")
    with job_lock:
        if job.state == "running":
            raise HTTPException(status_code=409, detail="A generation job is already running.")
        job.state = "queued"
        job.message = "Lookup generation queued."
    executor.submit(_run_lookup_generation)
    return {"message": "Lookup generation started."}


@app.get("/api/download-lookups")
def download_lookups(_: Annotated[str, Depends(require_auth)]) -> Response:
    lookup_files = sorted(OUTPUT_DIR.glob("*.lua")) if OUTPUT_DIR.is_dir() else []
    if not lookup_files:
        raise HTTPException(status_code=404, detail="No lookup tables have been generated.")

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for file in lookup_files:
            output.write(file, arcname=f"generated/{file.name}")
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="warcraft-lookups.zip"'},
    )
