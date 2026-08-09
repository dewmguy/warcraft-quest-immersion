from __future__ import annotations

import base64
import json
import os
import re
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
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tts_cli.alpha_store import DELIVERIES, AlphaError, AlphaStore
from tts_cli.config import load_settings
from tts_cli.consts import GENDER_DICT, RACE_DICT
from tts_cli.data_sources import DataSourceError, load_dialogue_csv
from tts_cli.elevenlabs_client import ElevenLabsClient, ElevenLabsError
from tts_cli.paths import OUTPUT_DIR, PROJECT_ROOT, SAMPLE_DATA_PATH
from tts_cli.sql_queries import make_connection
from tts_cli.tts_utils import TTSProcessor
from tts_cli.voice_profiles import VoiceProfileError, load_phase2_review

WEB_DIR = Path(__file__).resolve().parent / "web"
DATA_DIR = Path(os.getenv("WQI_DATA_DIR", PROJECT_ROOT / "data")).resolve()
DIALOGUE_PATH = DATA_DIR / "dialogue.csv"
SOURCE_DIR = DATA_DIR / "sources"
ALPHA_DIR = DATA_DIR / "alpha"
ALPHA_DB_PATH = ALPHA_DIR / "production.sqlite3"
MAX_UPLOAD_BYTES = int(os.getenv("WQI_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wqi-generator")
job_lock = Lock()


@dataclass
class JobState:
    state: str = "idle"
    message: str = "Ready"


job = JobState()
alpha_store = AlphaStore(ALPHA_DB_PATH, ALPHA_DIR)
elevenlabs = ElevenLabsClient()

SUPPORTED_EXPANSIONS = {"1.12.1", "2.4.3", "3.3.5", "classic"}


def _import_alpha_sources() -> list[dict]:
    results = [alpha_store.import_csv(DIALOGUE_PATH, expansion="3.3.5", locale="enUS")]
    if SOURCE_DIR.exists():
        for source_path in sorted(SOURCE_DIR.glob("*.csv")):
            match = re.fullmatch(r"(.+)-([A-Za-z]{4})\.csv", source_path.name)
            if not match or match.group(1) not in SUPPORTED_EXPANSIONS:
                continue
            results.append(
                alpha_store.import_csv(
                    source_path,
                    source_name=source_path.name,
                    expansion=match.group(1),
                    locale=match.group(2),
                )
            )
    return results


@asynccontextmanager
async def lifespan(_: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not DIALOGUE_PATH.exists():
        shutil.copyfile(SAMPLE_DATA_PATH, DIALOGUE_PATH)
    alpha_store.initialize()
    _import_alpha_sources()
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


def require_paid_action_header(
    x_wqi_paid_action: Annotated[str | None, Header()] = None,
) -> None:
    if x_wqi_paid_action != "confirmed":
        raise HTTPException(status_code=400, detail="Missing paid-action confirmation header.")


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


@app.get("/", response_class=RedirectResponse)
def dashboard(_: Annotated[str, Depends(require_auth)]):
    return RedirectResponse("/alpha", status_code=307)


@app.get("/voices", response_class=RedirectResponse)
def voice_profiles(_: Annotated[str, Depends(require_auth)]):
    return RedirectResponse("/alpha/voices", status_code=307)


@app.get("/api/status")
def api_status(_: Annotated[str, Depends(require_auth)]) -> dict:
    return _status_payload()


@app.get("/api/phase2")
def api_phase2(_: Annotated[str, Depends(require_auth)]) -> dict:
    try:
        return load_phase2_review()
    except VoiceProfileError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/poc/dwarves", response_class=RedirectResponse)
def retired_dwarf_poc(_: Annotated[str, Depends(require_auth)]):
    return RedirectResponse("/alpha", status_code=307)


def _alpha_error(error: AlphaError, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(error))


@app.get("/alpha", response_class=HTMLResponse)
def alpha_dashboard(
    request: Request,
    _: Annotated[str, Depends(require_auth)],
    q: str = "",
    production_state: str = "",
    source: str = "",
    expansion: str = "",
    race_id: str = "",
    gender_id: str = "",
    page: int = 1,
):
    try:
        return templates.TemplateResponse(
            request=request,
            name="alpha.html",
            context={
                "dashboard": alpha_store.dashboard(),
                "listing": alpha_store.list_dialogue(
                    query=q,
                    state=production_state,
                    source=source,
                    expansion=expansion,
                    race_id=race_id,
                    gender_id=gender_id,
                    page=max(page, 1),
                ),
                "filters": {
                    "q": q,
                    "production_state": production_state,
                    "source": source,
                    "expansion": expansion,
                    "race_id": race_id,
                    "gender_id": gender_id,
                },
                "provider_configured": elevenlabs.configured,
            },
        )
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.get("/alpha/dialogue/{dialogue_id}", response_class=HTMLResponse)
def alpha_dialogue(
    dialogue_id: str,
    request: Request,
    _: Annotated[str, Depends(require_auth)],
):
    try:
        return templates.TemplateResponse(
            request=request,
            name="alpha-dialogue.html",
            context={
                "dialogue": alpha_store.get_dialogue(dialogue_id),
                "deliveries": DELIVERIES,
                "provider_configured": elevenlabs.configured,
            },
        )
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.get("/alpha/speakers/{entity_type}/{entity_id}", response_class=HTMLResponse)
def alpha_speaker(
    entity_type: str,
    entity_id: int,
    request: Request,
    _: Annotated[str, Depends(require_auth)],
):
    try:
        return templates.TemplateResponse(
            request=request,
            name="alpha-speaker.html",
            context={
                "record": alpha_store.get_speaker(f"{entity_type}-{entity_id}"),
                "provider_configured": elevenlabs.configured,
            },
        )
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.get("/alpha/voices", response_class=HTMLResponse)
def alpha_voices(
    request: Request,
    _: Annotated[str, Depends(require_auth)],
    scope: str = "",
):
    try:
        return templates.TemplateResponse(
            request=request,
            name="alpha-voices.html",
            context={
                "voices": alpha_store.list_voices(scope),
                "scope": scope,
                "provider_configured": elevenlabs.configured,
            },
        )
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.get("/alpha/voices/{voice_id}", response_class=HTMLResponse)
def alpha_voice(
    voice_id: str,
    request: Request,
    _: Annotated[str, Depends(require_auth)],
):
    try:
        return templates.TemplateResponse(
            request=request,
            name="alpha-voice.html",
            context={
                "voice": alpha_store.get_voice(voice_id),
                "provider_configured": elevenlabs.configured,
            },
        )
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.get("/alpha/export", response_class=HTMLResponse)
def alpha_export(request: Request, _: Annotated[str, Depends(require_auth)]):
    return templates.TemplateResponse(
        request=request,
        name="alpha-export.html",
        context={"manifest": alpha_store.export_manifest()},
    )


@app.post("/api/data")
async def upload_data(
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
    file: Annotated[UploadFile, File()],
    expansion: Annotated[str, Form()] = "3.3.5",
    locale: Annotated[str, Form()] = "enUS",
) -> dict:
    if expansion not in SUPPORTED_EXPANSIONS:
        raise HTTPException(status_code=422, detail="Select a supported expansion.")
    if not re.fullmatch(r"[A-Za-z]{4}", locale):
        raise HTTPException(status_code=422, detail="Locale must look like enUS.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
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
        source_path = SOURCE_DIR / f"{expansion}-{locale}.csv"
        temporary_path.replace(source_path)
        imported = alpha_store.import_csv(
            source_path,
            source_name=file.filename or source_path.name,
            expansion=expansion,
            locale=locale,
        )
    except DataSourceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
        await file.close()
    return {
        "message": f"Loaded {len(dataframe):,} dialogue rows into the Alpha database.",
        "rows": len(dataframe),
        "snapshot_id": imported["snapshot_id"],
    }


@app.get("/api/alpha")
def api_alpha(_: Annotated[str, Depends(require_auth)]) -> dict:
    return alpha_store.dashboard()


@app.post("/api/alpha/reimport")
def api_alpha_reimport(
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        results = _import_alpha_sources()
        imported = sum(result["dialogue_records"] for result in results)
        return {
            "message": f"Imported {imported:,} dialogue records across {len(results)} sources.",
            "dialogue_records": imported,
            "sources": results,
        }
    except (AlphaError, DataSourceError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/alpha/dialogue/{dialogue_id}/prepare-spoken-text")
def api_prepare_spoken_text(
    dialogue_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        dialogue = alpha_store.prepare_spoken_text(dialogue_id)
        return {"message": "Spoken text was prepared for review.", "dialogue": dialogue}
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.patch("/api/alpha/dialogue/{dialogue_id}/spoken-text")
def api_save_spoken_text(
    dialogue_id: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        dialogue = alpha_store.save_spoken_text(dialogue_id, str(payload.get("spoken_text", "")))
        return {"message": "A new spoken-text revision was saved.", "dialogue": dialogue}
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.patch("/api/alpha/dialogue/{dialogue_id}/delivery")
def api_set_delivery(
    dialogue_id: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        alpha_store.set_delivery(dialogue_id, str(payload.get("delivery", "")))
        return {"message": "Delivery was saved.", "dialogue": alpha_store.get_dialogue(dialogue_id)}
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.patch("/api/alpha/speakers/{speaker_id}")
def api_update_speaker(
    speaker_id: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        return {
            "message": "Speaker context and voice assignment were saved.",
            "speaker": alpha_store.update_speaker(speaker_id, payload),
        }
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.post("/api/alpha/speakers/{speaker_id}/unique-voice")
def api_create_unique_voice(
    speaker_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        voice = alpha_store.create_unique_voice(speaker_id)
        return {
            "message": f"Created the unique voice record {voice['name']}.",
            "voice_id": voice["voice_id"],
        }
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.patch("/api/alpha/voices/{voice_id}")
def api_update_voice(
    voice_id: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        voice = alpha_store.update_voice(voice_id, payload)
        return {
            "message": f"Saved {voice['name']} as version {voice['version_number']}.",
            "voice": voice,
        }
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.post("/api/alpha/voices/{voice_id}/reference-clips")
async def api_upload_reference_clip(
    voice_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
    file: Annotated[UploadFile, File()],
    provenance: Annotated[str, Form()] = "",
    provider_eligible: Annotated[bool, Form()] = False,
) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if not (file.content_type or "").startswith("audio/") and suffix not in {
        ".mp3",
        ".wav",
        ".m4a",
        ".ogg",
        ".flac",
    }:
        raise HTTPException(status_code=422, detail="Upload a recognized audio file.")
    content = await file.read()
    await file.close()
    try:
        voice = alpha_store.save_reference_clip(
            voice_id,
            original_name=file.filename or "reference-audio",
            content=content,
            provenance=provenance,
            provider_eligible=provider_eligible,
        )
        return {"message": "Reference audio was stored outside Git.", "voice": voice}
    except AlphaError as error:
        raise _alpha_error(error) from error


def _require_elevenlabs() -> None:
    if not elevenlabs.configured:
        raise HTTPException(
            status_code=503,
            detail="ElevenLabs is not configured on this deployment. No paid request was made.",
        )


@app.post("/api/alpha/voices/{voice_id}/design")
def api_design_voice(
    voice_id: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
    ___: Annotated[None, Depends(require_paid_action_header)],
) -> dict:
    _require_elevenlabs()
    try:
        voice = alpha_store.get_voice(voice_id)
        preview_text = str(payload.get("preview_text", "")).strip()
        if not 100 <= len(preview_text) <= 1000:
            raise AlphaError("Voice preview text must contain 100–1,000 characters.")
        reference_audio = None
        clip_id = str(payload.get("reference_clip_id", "")).strip()
        if clip_id:
            clip = alpha_store.get_reference_clip(clip_id)
            if clip["voice_id"] != voice_id:
                raise AlphaError("The reference clip belongs to another voice.")
            if not clip["provider_eligible"]:
                raise AlphaError("The selected reference clip is not marked provider eligible.")
            reference_audio = clip["path"].read_bytes()
        result = elevenlabs.design_voice(
            description=voice["description"],
            preview_text=preview_text,
            reference_audio=reference_audio,
            prompt_strength=float(payload.get("prompt_strength", 0.5)),
        )
        previews = []
        for preview in result.get("previews", []):
            try:
                content = base64.b64decode(preview["audio_base_64"], validate=True)
                generated_voice_id = str(preview["generated_voice_id"])
            except (KeyError, ValueError) as error:
                raise ElevenLabsError("ElevenLabs returned an invalid voice preview.") from error
            previews.append({"content": content, "generated_voice_id": generated_voice_id})
        if not previews:
            raise ElevenLabsError("ElevenLabs returned no voice previews.")
        preview_ids = alpha_store.record_voice_previews(
            voice_id,
            prompt=voice["description"],
            preview_text=preview_text,
            model_id="eleven_ttv_v3",
            previews=previews,
        )
        return {
            "message": f"Stored {len(preview_ids)} new voice candidates.",
            "preview_ids": preview_ids,
        }
    except (AlphaError, ElevenLabsError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/alpha/voice-previews/{preview_id}/activate")
def api_activate_voice_preview(
    preview_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
    ___: Annotated[None, Depends(require_paid_action_header)],
) -> dict:
    _require_elevenlabs()
    try:
        preview = alpha_store.get_voice_preview(preview_id)
        result = elevenlabs.create_designed_voice(
            name=preview["voice_name"],
            description=preview["description"],
            generated_voice_id=preview["generated_voice_id"],
        )
        provider_voice_id = str(result.get("voice_id", "")).strip()
        if not provider_voice_id:
            raise ElevenLabsError("ElevenLabs did not return a voice ID.")
        voice = alpha_store.activate_voice_preview(preview_id, provider_voice_id)
        return {"message": f"Activated {voice['name']} in ElevenLabs.", "voice": voice}
    except (AlphaError, ElevenLabsError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/alpha/voices/{voice_id}/clone")
def api_clone_voice(
    voice_id: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
    ___: Annotated[None, Depends(require_paid_action_header)],
) -> dict:
    _require_elevenlabs()
    if payload.get("rights_confirmed") is not True:
        raise HTTPException(
            status_code=422,
            detail="Confirm provider eligibility and required voice rights before cloning.",
        )
    try:
        voice = alpha_store.get_voice(voice_id)
        clip_ids = payload.get("clip_ids", [])
        if not isinstance(clip_ids, list) or not clip_ids:
            raise AlphaError("Select at least one eligible reference clip.")
        clips = [alpha_store.get_reference_clip(str(clip_id)) for clip_id in clip_ids]
        if any(clip["voice_id"] != voice_id for clip in clips):
            raise AlphaError("A selected reference clip belongs to another voice.")
        if any(not clip["provider_eligible"] for clip in clips):
            raise AlphaError("Every selected clip must be marked provider eligible.")
        result = elevenlabs.clone_voice(
            name=voice["name"],
            description=voice["description"],
            labels={
                "gender": GENDER_DICT.get(voice["gender_id"], "unknown"),
                "accent": RACE_DICT.get(voice["race_id"], "unknown"),
                "language": "en",
            },
            files=[clip["path"] for clip in clips],
        )
        provider_voice_id = str(result.get("voice_id", "")).strip()
        if not provider_voice_id:
            raise ElevenLabsError("ElevenLabs did not return a voice ID.")
        updated = alpha_store.update_voice(
            voice_id,
            {
                "description": voice["description"],
                "creation_method": "instant_clone",
                "provider_voice_id": provider_voice_id,
                "status": "active",
            },
        )
        return {"message": f"Activated the instant clone for {voice['name']}.", "voice": updated}
    except (AlphaError, ElevenLabsError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/alpha/dialogue/{dialogue_id}/generate")
def api_generate_dialogue(
    dialogue_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
    ___: Annotated[None, Depends(require_paid_action_header)],
) -> dict:
    _require_elevenlabs()
    generation_id = None
    try:
        generation = alpha_store.begin_generation(dialogue_id)
        generation_id = generation["generation_id"]
        result = elevenlabs.text_to_speech(
            voice_id=generation["voice_id"],
            text=generation["text"],
            model_id=generation["model_id"],
            settings=generation["voice_settings"],
        )
        try:
            subscription = elevenlabs.subscription()
        except ElevenLabsError:
            subscription = {}
        candidate = alpha_store.complete_generation(
            generation_id,
            content=result.content,
            mime_type=result.content_type,
            provider_request_id=result.request_id,
            subscription=subscription,
        )
        return {
            "message": "Generated one audio candidate for review.",
            "generation_id": generation_id,
            **candidate,
        }
    except (AlphaError, ElevenLabsError) as error:
        if generation_id:
            alpha_store.fail_generation(generation_id, str(error))
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/alpha/candidates/{candidate_id}/approve")
def api_approve_candidate(
    candidate_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        dialogue = alpha_store.approve_candidate(candidate_id)
        return {
            "message": f"Approved {dialogue['addon_filename']} for production.",
            "dialogue": dialogue,
        }
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.get("/api/alpha/candidates/{candidate_id}/audio")
def api_candidate_audio(
    candidate_id: str,
    _: Annotated[str, Depends(require_auth)],
) -> FileResponse:
    try:
        return FileResponse(alpha_store.candidate_path(candidate_id), media_type="audio/mpeg")
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.get("/api/alpha/voice-previews/{preview_id}/audio")
def api_voice_preview_audio(
    preview_id: str,
    _: Annotated[str, Depends(require_auth)],
) -> FileResponse:
    try:
        return FileResponse(alpha_store.preview_path(preview_id), media_type="audio/mpeg")
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


def _public_manifest() -> dict:
    manifest = alpha_store.export_manifest()
    manifest["assets"] = [
        {key: value for key, value in asset.items() if key != "storage_path"}
        for asset in manifest["assets"]
    ]
    return manifest


@app.get("/api/alpha/export.json")
def api_alpha_export_json(_: Annotated[str, Depends(require_auth)]) -> Response:
    return Response(
        content=json.dumps(_public_manifest(), indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="wqi-production-manifest.json"'},
    )


@app.get("/api/alpha/export.zip")
def api_alpha_export_zip(_: Annotated[str, Depends(require_auth)]) -> Response:
    internal = alpha_store.export_manifest()
    public = _public_manifest()
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(
            "production-manifest.json", json.dumps(public, indent=2, ensure_ascii=False)
        )
        for asset in internal["assets"]:
            path = alpha_store.candidate_path(asset["candidate_id"])
            output.write(path, arcname=asset["package_path"])
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="wqi-production-assets.zip"'},
    )


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
