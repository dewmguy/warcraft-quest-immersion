from __future__ import annotations

import base64
import json
import math
import os
import re
import secrets
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
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

from tts_cli.alpha_store import (
    AFFILIATION_OPTIONS,
    DELIVERIES,
    IMPORTANCE_SCORES,
    MAX_REFERENCE_BYTES,
    ROLE_OPTIONS,
    AlphaError,
    AlphaStore,
)
from tts_cli.config import load_settings
from tts_cli.consts import GENDER_DICT, RACE_DICT
from tts_cli.data_sources import REQUIRED_COLUMNS, DataSourceError, load_dialogue_csv
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
MAX_REFERENCE_FILES = 20
MAX_REFERENCE_BATCH_BYTES = 100 * 1024 * 1024
REFERENCE_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
VOICE_ID_AUDITION_TEXT = (
    "The road through the mountains is dangerous after nightfall. Stay near the lanterns, "
    "keep your companions close, and listen carefully for movement beyond the pass. We have "
    "endured harder winters than this, and we will endure this one as well."
)

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
    return RedirectResponse("/alpha/races", status_code=307)


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


def _alpha_context(**values) -> dict:
    return {
        "provider_configured": elevenlabs.configured,
        "progress": alpha_store.progress(),
        "app_settings": alpha_store.get_app_settings(),
        "voice_id_audition_text": VOICE_ID_AUDITION_TEXT,
        **values,
    }


def _dialogue_generation_availability(dialogue: dict) -> tuple[bool, str]:
    if not dialogue.get("revision_id"):
        return False, "Prepare the spoken text before generating an audio sample."
    if dialogue.get("warnings"):
        return False, "Resolve the spoken-text warnings above before generating an audio sample."
    if not dialogue.get("voice_id"):
        return False, "Assign an active voice profile to this NPC before generating audio."
    if not dialogue.get("provider_voice_id"):
        delivery = str(dialogue.get("delivery") or "neutral").replace("_", " ").title()
        return (
            False,
            "The active voice profile needs an ElevenLabs Voice ID for the "
            f"{delivery} delivery before audio can be generated.",
        )
    if not elevenlabs.configured:
        return False, "ElevenLabs is not configured, so audio samples cannot be generated."
    if dialogue.get("production_state") == "approved":
        return False, "Production audio has already been approved for this quest phase."
    if dialogue.get("production_state") not in {
        "ready_to_generate",
        "generation_failed",
        "audio_to_review",
    }:
        return False, "Audio sample generation is unavailable for this quest's current state."
    return True, ""


def _subscription_summary(payload: dict) -> dict:
    used = payload.get("character_count")
    limit = payload.get("character_limit")
    remaining = None
    percent_used = None
    if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
        remaining = max(int(limit - used), 0)
        percent_used = round((used / limit) * 100, 1)
    reset_unix = payload.get("next_character_count_reset_unix")
    reset_at = None
    if isinstance(reset_unix, (int, float)):
        reset_at = datetime.fromtimestamp(reset_unix, tz=UTC).isoformat()
    return {
        "tier": payload.get("tier") or payload.get("billing_period") or "unknown",
        "status": payload.get("status") or "active",
        "credits_used": used,
        "credits_limit": limit,
        "credits_remaining": remaining,
        "percent_used": percent_used,
        "next_reset_at": reset_at,
        "refresh_period": payload.get("character_refresh_period") or payload.get("billing_period"),
        "max_credit_limit_extension": payload.get("max_credit_limit_extension"),
        "can_extend_credits": payload.get("can_extend_character_limit"),
        "voice_limit": payload.get("voice_limit"),
        "can_use_instant_voice_cloning": payload.get("can_use_instant_voice_cloning"),
    }


def _provider_clone_description(value: str, limit: int = 500) -> str:
    """Fit stored voice context into ElevenLabs' clone-description metadata field."""
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    shortened = normalized[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{shortened or normalized[: limit - 1]}…"


def _bounded_provider_float(
    payload: dict,
    key: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
    label: str,
) -> float:
    raw_value = payload.get(key, default)
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as error:
        raise AlphaError(f"{label} must be a number.") from error
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise AlphaError(f"{label} must be between {minimum:g} and {maximum:g}.")
    return value


def _optional_provider_seed(payload: dict) -> int | None:
    raw_value = payload.get("seed")
    if raw_value in (None, ""):
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as error:
        raise AlphaError("Seed must be a whole number.") from error
    if not 0 <= value <= 2_147_483_647:
        raise AlphaError("Seed must be between 0 and 2,147,483,647.")
    return value


def _usage_snapshot(subscription: dict, character_cost: int | None) -> dict:
    usage = dict(subscription)
    if character_cost is not None:
        usage["request_character_cost"] = character_cost
    return usage


def _generate_voice_id_audition(candidate: dict, voice: dict) -> dict:
    model_id = alpha_store.get_app_settings()["tts_model_id"]
    settings = {"stability": 0.5} if model_id == "eleven_v3" else voice["settings"]
    result = elevenlabs.text_to_speech(
        voice_id=candidate["provider_voice_id"],
        text=VOICE_ID_AUDITION_TEXT,
        model_id=model_id,
        settings=settings,
    )
    try:
        subscription = elevenlabs.subscription()
    except ElevenLabsError:
        subscription = {}
    usage = _usage_snapshot(subscription, result.character_cost)
    alpha_store.record_provider_usage(
        action="voice_id_audition",
        subject_id=candidate["candidate_id"],
        input_character_count=len(VOICE_ID_AUDITION_TEXT),
        character_cost=result.character_cost,
        provider_request_id=result.request_id,
        subscription=subscription,
    )
    return alpha_store.attach_voice_id_candidate_sample(
        candidate["candidate_id"],
        sample_text=VOICE_ID_AUDITION_TEXT,
        sample_model_id=model_id,
        content=result.content,
        provider_request_id=result.request_id,
        subscription=usage,
    )


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
        if source == "gossip":
            return RedirectResponse("/alpha/gossip", status_code=307)
        quest_source = source if source in {"accept", "progress", "complete"} else "quest"
        return templates.TemplateResponse(
            request=request,
            name="alpha.html",
            context=_alpha_context(
                dashboard=alpha_store.dashboard(),
                listing=alpha_store.list_dialogue(
                    query=q,
                    state=production_state,
                    source=quest_source,
                    expansion=expansion,
                    race_id=race_id,
                    gender_id=gender_id,
                    page=max(page, 1),
                ),
                filters={
                    "q": q,
                    "production_state": production_state,
                    "source": quest_source if quest_source != "quest" else "",
                    "expansion": expansion,
                    "race_id": race_id,
                    "gender_id": gender_id,
                },
            ),
        )
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.get("/alpha/gossip", response_class=HTMLResponse)
def alpha_gossip(
    request: Request,
    _: Annotated[str, Depends(require_auth)],
    q: str = "",
    production_state: str = "",
    expansion: str = "",
    race_id: str = "",
    gender_id: str = "",
    page: int = 1,
):
    try:
        return templates.TemplateResponse(
            request=request,
            name="alpha-gossip.html",
            context=_alpha_context(
                dashboard=alpha_store.dashboard(),
                listing=alpha_store.list_dialogue(
                    query=q,
                    state=production_state,
                    source="gossip",
                    expansion=expansion,
                    race_id=race_id,
                    gender_id=gender_id,
                    page=max(page, 1),
                ),
                filters={
                    "q": q,
                    "production_state": production_state,
                    "expansion": expansion,
                    "race_id": race_id,
                    "gender_id": gender_id,
                },
            ),
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
        dialogue = alpha_store.get_dialogue(dialogue_id)
        if dialogue["source"] != "gossip" and dialogue["revision_id"] is None:
            dialogue = alpha_store.ensure_spoken_text(dialogue_id)
        can_generate_audio, generation_blocker = _dialogue_generation_availability(dialogue)
        dialogue["can_generate_audio"] = can_generate_audio
        dialogue["generation_blocker"] = generation_blocker
        return templates.TemplateResponse(
            request=request,
            name="alpha-dialogue.html",
            context=_alpha_context(dialogue=dialogue, deliveries=DELIVERIES),
        )
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.get("/alpha/npcs/{entity_type}/{entity_id}", response_class=HTMLResponse)
@app.get(
    "/alpha/speakers/{entity_type}/{entity_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def alpha_npc(
    entity_type: str,
    entity_id: int,
    request: Request,
    _: Annotated[str, Depends(require_auth)],
):
    try:
        return templates.TemplateResponse(
            request=request,
            name="alpha-speaker.html",
            context=_alpha_context(
                record=alpha_store.get_speaker(f"{entity_type}-{entity_id}"),
                role_options=ROLE_OPTIONS,
                affiliation_options=AFFILIATION_OPTIONS,
                importance_scores=IMPORTANCE_SCORES,
            ),
        )
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.get("/alpha/npcs", response_class=HTMLResponse)
def alpha_npcs(
    request: Request,
    _: Annotated[str, Depends(require_auth)],
    q: str = "",
    race_id: str = "",
    gender_id: str = "",
    role: str = "",
    faction: str = "",
    importance: str = "",
    voice_approach: str = "",
    voice_state: str = "",
    page: int = 1,
):
    try:
        return templates.TemplateResponse(
            request=request,
            name="alpha-npcs.html",
            context=_alpha_context(
                listing=alpha_store.list_npcs(
                    query=q,
                    race_id=race_id,
                    gender_id=gender_id,
                    role=role,
                    faction=faction,
                    importance=importance,
                    voice_approach=voice_approach,
                    voice_state=voice_state,
                    page=max(page, 1),
                ),
                filters={
                    "q": q,
                    "race_id": race_id,
                    "gender_id": gender_id,
                    "role": role,
                    "faction": faction,
                    "importance": importance,
                    "voice_approach": voice_approach,
                    "voice_state": voice_state,
                },
                role_options=ROLE_OPTIONS,
                faction_options=AFFILIATION_OPTIONS,
                importance_scores=IMPORTANCE_SCORES,
            ),
        )
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.get("/alpha/races", response_class=HTMLResponse)
def alpha_races(
    request: Request,
    _: Annotated[str, Depends(require_auth)],
    completion: str = "",
):
    try:
        return templates.TemplateResponse(
            request=request,
            name="alpha-voices.html",
            context=_alpha_context(
                voices=alpha_store.list_voices("baseline", completion),
                completion=completion,
            ),
        )
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.get("/alpha/voices", response_class=RedirectResponse, include_in_schema=False)
def legacy_alpha_voices(
    _: Annotated[str, Depends(require_auth)],
    scope: str = "",
    completion: str = "",
):
    if scope == "unique":
        return RedirectResponse("/alpha/npcs?voice_approach=unique", status_code=307)
    suffix = f"?completion={completion}" if completion else ""
    return RedirectResponse(f"/alpha/races{suffix}", status_code=307)


@app.get("/alpha/races/{voice_id}", response_class=HTMLResponse)
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
            context=_alpha_context(voice=alpha_store.get_voice(voice_id)),
        )
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.get("/alpha/import-export", response_class=HTMLResponse)
def alpha_import_export(request: Request, _: Annotated[str, Depends(require_auth)]):
    return templates.TemplateResponse(
        request=request,
        name="alpha-import-export.html",
        context=_alpha_context(
            dashboard=alpha_store.dashboard(),
            manifest=alpha_store.export_manifest(),
            required_columns=REQUIRED_COLUMNS,
        ),
    )


@app.get("/alpha/export", response_class=RedirectResponse)
def retired_alpha_export(_: Annotated[str, Depends(require_auth)]):
    return RedirectResponse("/alpha/import-export", status_code=307)


@app.get("/alpha/settings", response_class=HTMLResponse)
def alpha_settings(request: Request, _: Annotated[str, Depends(require_auth)]):
    return templates.TemplateResponse(
        request=request,
        name="alpha-settings.html",
        context=_alpha_context(provider_events=alpha_store.list_provider_usage()),
    )


@app.get("/api/alpha/provider-status")
def api_alpha_provider_status(_: Annotated[str, Depends(require_auth)]) -> dict:
    if not elevenlabs.configured:
        return {
            "configured": False,
            "provider": "ElevenLabs",
            "message": "No ElevenLabs API key is configured on this deployment.",
        }
    try:
        subscription = elevenlabs.subscription()
    except ElevenLabsError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return {
        "configured": True,
        "provider": "ElevenLabs",
        "message": "ElevenLabs accepted the configured API key.",
        "account": _subscription_summary(subscription),
    }


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


@app.get("/api/alpha/import-template.csv")
def api_alpha_import_template(
    _: Annotated[str, Depends(require_auth)],
) -> FileResponse:
    return FileResponse(
        SAMPLE_DATA_PATH,
        media_type="text/csv",
        filename="warcraft-dialogue-import-template.csv",
    )


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


@app.patch("/api/alpha/npcs/{speaker_id}")
@app.patch("/api/alpha/speakers/{speaker_id}", include_in_schema=False)
def api_update_npc(
    speaker_id: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        record = alpha_store.update_speaker(speaker_id, payload)
        return {
            "message": "NPC context was saved.",
            "npc": record,
            "speaker": record,
        }
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.post("/api/alpha/npcs/{speaker_id}/unique-voice")
@app.post("/api/alpha/speakers/{speaker_id}/unique-voice", include_in_schema=False)
def api_create_unique_voice(
    speaker_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        voice = alpha_store.create_unique_voice(speaker_id)
        return {
            "message": f"{voice['name']} now uses its unique voice profile.",
            "voice_id": voice["voice_id"],
        }
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.post("/api/alpha/npcs/{speaker_id}/baseline-voice")
@app.post("/api/alpha/speakers/{speaker_id}/baseline-voice", include_in_schema=False)
def api_use_baseline_voice(
    speaker_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        record = alpha_store.use_baseline_voice(speaker_id)
        baseline = record["baseline_voice"]
        return {
            "message": (
                f"{record['npc']['name']} now uses {baseline['name']}; "
                "the unique profile remains dormant."
            ),
            "npc": record,
            "speaker": record,
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
        message = (
            f"Saved {voice['name']} as version {voice['version_number']}."
            if voice.get("version_changed")
            else "No settings changed, so no new version was created."
        )
        return {
            "message": message,
            "voice": voice,
        }
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.post("/api/alpha/voices/{voice_id}/prompts/{version_id}/restore")
def api_restore_voice_prompt(
    voice_id: str,
    version_id: int,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        voice = alpha_store.restore_voice_prompt(voice_id, version_id)
        return {
            "message": (
                f"Restored the prompt as version {voice['version_number']}."
                if voice.get("version_changed")
                else "That prompt already matches the current prompt."
            ),
            "voice": voice,
        }
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.patch("/api/alpha/voices/{voice_id}/deliveries/{delivery}")
def api_update_delivery_preset(
    voice_id: str,
    delivery: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        voice = alpha_store.update_delivery_preset(voice_id, delivery, payload)
        return {
            "message": f"Saved the {delivery.replace('_', ' ')} delivery settings locally.",
            "voice": voice,
        }
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.post("/api/alpha/voices/{voice_id}/deliveries/{delivery}/generate")
def api_generate_delivery_preview(
    voice_id: str,
    delivery: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
    ___: Annotated[None, Depends(require_paid_action_header)],
) -> dict:
    _require_elevenlabs()
    try:
        request = alpha_store.delivery_preview_request(
            voice_id, delivery, str(payload.get("sample_text", ""))
        )
        result = elevenlabs.text_to_speech(
            voice_id=request["voice_id"],
            text=request["text"],
            model_id=request["model_id"],
            settings=request["voice_settings"],
        )
        try:
            subscription = elevenlabs.subscription()
        except ElevenLabsError:
            subscription = {}
        usage = _usage_snapshot(subscription, result.character_cost)
        alpha_store.record_provider_usage(
            action="delivery_preview",
            subject_id=f"{voice_id}:{delivery}",
            input_character_count=len(request["text"]),
            character_cost=result.character_cost,
            provider_request_id=result.request_id,
            subscription=subscription,
        )
        preview = alpha_store.record_delivery_preview(
            voice_id,
            delivery,
            request,
            content=result.content,
            provider_request_id=result.request_id,
            subscription=usage,
        )
        return {
            "message": (f"Generated {delivery} sample #{preview['generation_number']} for review."),
            **preview,
        }
    except (AlphaError, ElevenLabsError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/alpha/delivery-previews/{preview_id}/approve")
def api_approve_delivery_preview(
    preview_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        voice = alpha_store.approve_delivery_preview(preview_id)
        return {"message": "Approved this delivery sample.", "voice": voice}
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.patch("/api/alpha/delivery-previews/{preview_id}")
def api_update_delivery_preview_name(
    preview_id: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        display_name = payload.get("display_name", "")
        voice = alpha_store.update_delivery_preview_name(preview_id, display_name)
        return {
            "message": (
                "Saved the sample name."
                if str(display_name or "").strip()
                else "Cleared the sample name."
            ),
            "voice": voice,
        }
    except AlphaError as error:
        raise _alpha_error(error, 404 if "not found" in str(error).lower() else 422) from error


@app.delete("/api/alpha/delivery-previews/{preview_id}")
def api_delete_delivery_preview(
    preview_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        voice = alpha_store.delete_delivery_preview(preview_id)
        return {
            "message": "Delivery sample was deleted from local storage.",
            "voice": voice,
        }
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.get("/api/alpha/delivery-previews/{preview_id}/audio")
def api_delivery_preview_audio(
    preview_id: str,
    _: Annotated[str, Depends(require_auth)],
) -> FileResponse:
    try:
        return FileResponse(alpha_store.delivery_preview_path(preview_id), media_type="audio/mpeg")
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.patch("/api/alpha/settings")
def api_update_alpha_settings(
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        settings = alpha_store.update_app_settings(payload)
        return {"message": "Generation defaults were saved.", "settings": settings}
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.post("/api/alpha/voices/{voice_id}/reference-clips")
async def api_upload_reference_clip(
    voice_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
    file: Annotated[list[UploadFile], File()],
    provenance: Annotated[str, Form()] = "",
) -> dict:
    if not file:
        raise HTTPException(status_code=422, detail="Select at least one reference audio file.")
    if len(file) > MAX_REFERENCE_FILES:
        raise HTTPException(
            status_code=422,
            detail=f"Upload no more than {MAX_REFERENCE_FILES} reference files at once.",
        )
    invalid_names = [
        upload.filename or "unnamed file"
        for upload in file
        if Path(upload.filename or "").suffix.lower() not in REFERENCE_AUDIO_EXTENSIONS
    ]
    if invalid_names:
        supported = ", ".join(
            sorted(extension[1:].upper() for extension in REFERENCE_AUDIO_EXTENSIONS)
        )
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported reference file: {invalid_names[0]}. Use {supported}.",
        )
    pending: list[tuple[str, bytes]] = []
    total_size = 0
    try:
        for upload in file:
            content = await upload.read(MAX_REFERENCE_BYTES + 1)
            if not content or len(content) > MAX_REFERENCE_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail=f"{upload.filename or 'A reference file'} must contain 1 byte–50 MB.",
                )
            total_size += len(content)
            if total_size > MAX_REFERENCE_BATCH_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail="The selected reference batch exceeds 100 MB.",
                )
            pending.append((upload.filename or "reference-audio", content))
    finally:
        for upload in file:
            await upload.close()
    try:
        voice = None
        for original_name, content in pending:
            voice = alpha_store.save_reference_clip(
                voice_id,
                original_name=original_name,
                content=content,
                provenance=provenance,
                provider_eligible=True,
            )
        count = len(pending)
        return {
            "message": f"Stored {count} reference {'clip' if count == 1 else 'clips'} outside Git.",
            "voice": voice,
        }
    except AlphaError as error:
        raise _alpha_error(error) from error


@app.get("/api/alpha/reference-clips/{clip_id}/audio")
def api_reference_clip_audio(
    clip_id: str,
    _: Annotated[str, Depends(require_auth)],
) -> FileResponse:
    try:
        return FileResponse(alpha_store.reference_path(clip_id))
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.delete("/api/alpha/reference-clips/{clip_id}")
def api_delete_reference_clip(
    clip_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        voice = alpha_store.delete_reference_clip(clip_id)
        return {"message": "Reference clip was deleted from local storage.", "voice": voice}
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.delete("/api/alpha/voice-previews/{preview_id}")
def api_delete_voice_preview(
    preview_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        voice = alpha_store.delete_voice_preview(preview_id)
        return {"message": "Voice Design preview was deleted from local storage.", "voice": voice}
    except AlphaError as error:
        status_code = 404 if "not found" in str(error).lower() else 422
        raise _alpha_error(error, status_code) from error


@app.get("/api/alpha/voice-id-candidates/{candidate_id}/audio")
def api_voice_id_candidate_audio(
    candidate_id: str,
    _: Annotated[str, Depends(require_auth)],
) -> FileResponse:
    try:
        return FileResponse(
            alpha_store.voice_id_candidate_path(candidate_id), media_type="audio/mpeg"
        )
    except AlphaError as error:
        raise _alpha_error(error, 404) from error


@app.patch("/api/alpha/voice-id-candidates/{candidate_id}")
def api_update_voice_id_candidate_name(
    candidate_id: str,
    payload: Annotated[dict, Body()],
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    try:
        display_name = payload.get("display_name", "")
        voice = alpha_store.update_voice_id_candidate_name(candidate_id, display_name)
        return {
            "message": (
                "Saved the Voice ID name."
                if str(display_name or "").strip()
                else "Cleared the Voice ID name."
            ),
            "voice": voice,
        }
    except AlphaError as error:
        raise _alpha_error(error, 404 if "not found" in str(error).lower() else 422) from error


@app.post("/api/alpha/voice-id-candidates/{candidate_id}/audition")
def api_generate_voice_id_candidate_audition(
    candidate_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
    ___: Annotated[None, Depends(require_paid_action_header)],
) -> dict:
    _require_elevenlabs()
    try:
        candidate = alpha_store.get_voice_id_candidate(candidate_id)
        voice = alpha_store.get_voice(candidate["voice_id"])
        audition = _generate_voice_id_audition(candidate, voice)
        return {
            "message": (
                f"Generated the sample for voice ID candidate #{audition['generation_number']}."
            ),
            "candidate": audition,
        }
    except (AlphaError, ElevenLabsError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.delete("/api/alpha/voice-id-candidates/{candidate_id}")
def api_delete_voice_id_candidate(
    candidate_id: str,
    _: Annotated[str, Depends(require_auth)],
    __: Annotated[None, Depends(require_action_header)],
) -> dict:
    _require_elevenlabs()
    try:
        candidate = alpha_store.get_voice_id_candidate(candidate_id)
        elevenlabs.delete_voice(candidate["provider_voice_id"])
        voice = alpha_store.delete_voice_id_candidate(candidate_id)
        affected = int(voice.pop("affected_delivery_count", 0))
        voice.pop("cleared_legacy_voice_link", False)
        consequences = []
        if affected:
            consequences.append(
                f"cleared it from {affected} delivery preset{'s' if affected != 1 else ''}"
            )
        suffix = f" This also {' and '.join(consequences)}." if consequences else ""
        return {
            "message": (
                f"Deleted voice ID candidate #{candidate['generation_number']} from ElevenLabs "
                f"and local storage.{suffix}"
            ),
            "voice": voice,
        }
    except (AlphaError, ElevenLabsError) as error:
        status_code = 404 if "not found" in str(error).lower() else 422
        raise HTTPException(status_code=status_code, detail=str(error)) from error


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
        creation_method = str(payload.get("creation_method", voice["creation_method"])).strip()
        if creation_method not in {"designed", "reference_design"}:
            raise AlphaError("Select a Voice Design method before generating design candidates.")
        description = str(payload.get("description", voice["description"])).strip()
        if not 20 <= len(description) <= 1000:
            raise AlphaError("Voice Design prompt context must contain 20–1,000 characters.")
        preview_text = str(payload.get("preview_text", "")).strip()
        if not 100 <= len(preview_text) <= 1000:
            raise AlphaError("Voice preview text must contain 100–1,000 characters.")
        reference_audio = None
        clip_id = str(payload.get("reference_clip_id", "")).strip()
        if creation_method == "reference_design" and not clip_id:
            raise AlphaError("Reference-guided Voice Design requires one reference clip.")
        if creation_method == "reference_design":
            clip = alpha_store.get_reference_clip(clip_id)
            if clip["voice_id"] != voice_id:
                raise AlphaError("The reference clip belongs to another voice.")
            if not clip["provider_eligible"]:
                raise AlphaError("The selected reference clip is not marked provider eligible.")
            reference_audio = clip["path"].read_bytes()
        design_model = alpha_store.get_app_settings()["voice_design_model_id"]
        if reference_audio is not None and design_model != "eleven_ttv_v3":
            raise AlphaError("Reference-guided Voice Design requires the v3 design model.")
        prompt_strength = _bounded_provider_float(
            payload,
            "prompt_strength",
            default=0.5,
            minimum=0,
            maximum=1,
            label="Prompt versus reference balance",
        )
        guidance_scale = _bounded_provider_float(
            payload,
            "guidance_scale",
            default=5,
            minimum=0,
            maximum=100,
            label="Guidance scale",
        )
        loudness = _bounded_provider_float(
            payload,
            "loudness",
            default=0.5,
            minimum=-1,
            maximum=1,
            label="Loudness",
        )
        quality = None
        if payload.get("quality_override") is True:
            quality = _bounded_provider_float(
                payload,
                "quality",
                default=0,
                minimum=-1,
                maximum=1,
                label="Quality",
            )
        seed = _optional_provider_seed(payload)
        result = elevenlabs.design_voice(
            description=description,
            preview_text=preview_text,
            model_id=design_model,
            reference_audio=reference_audio,
            prompt_strength=prompt_strength,
            guidance_scale=guidance_scale,
            loudness=loudness,
            quality=quality,
            seed=seed,
        )
        try:
            subscription = elevenlabs.subscription()
        except ElevenLabsError:
            subscription = {}
        alpha_store.record_provider_usage(
            action="voice_design",
            subject_id=voice_id,
            input_character_count=len(preview_text),
            character_cost=result.character_cost,
            provider_request_id=result.request_id,
            subscription=subscription,
        )
        previews = []
        for preview in result.payload.get("previews", []):
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
            prompt=description,
            preview_text=preview_text,
            model_id=design_model,
            previews=previews,
            creation_method=creation_method,
            replace_existing=True,
        )
        alpha_store.update_voice(
            voice_id,
            {
                "description": description,
                "creation_method": creation_method,
            },
        )
        replaced_count = len(voice["previews"])
        cost_note = (
            f" ElevenLabs reported {result.character_cost:,} credits for the request."
            if result.character_cost is not None
            else ""
        )
        replacement_note = (
            f" Replaced {replaced_count} former temporary Voice Design preview"
            f"{'s' if replaced_count != 1 else ''}."
            if replaced_count
            else ""
        )
        return {
            "message": (
                f"Stored {len(preview_ids)} new temporary Voice Design previews."
                f"{replacement_note}{cost_note}"
            ),
            "preview_ids": preview_ids,
            "provider_request_id": result.request_id,
            "character_cost": result.character_cost,
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
        candidate_id = str(voice.pop("created_voice_id_candidate", ""))
        candidate = alpha_store.get_voice_id_candidate(candidate_id)
        discarded_count = int(voice.pop("discarded_preview_count", 0))
        cleanup_note = (
            f" Deleted {discarded_count} temporary Voice Design preview"
            f"{'s' if discarded_count != 1 else ''}."
            if discarded_count
            else ""
        )
        return {
            "message": (
                f"Generated voice ID candidate #{candidate['generation_number']} for "
                f"{voice['name']}.{cleanup_note}"
            ),
            "voice": voice,
        }
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
    try:
        voice = alpha_store.get_voice(voice_id)
        creation_method = str(payload.get("creation_method", voice["creation_method"])).strip()
        if creation_method != "instant_clone":
            raise AlphaError("Select Instant Voice Clone before creating a clone.")
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
            description=_provider_clone_description(voice["description"]),
            labels={
                "gender": GENDER_DICT.get(voice["gender_id"], "unknown"),
                "accent": RACE_DICT.get(voice["race_id"], "unknown"),
                "language": "en",
            },
            files=[clip["path"] for clip in clips],
            remove_background_noise=payload.get("remove_background_noise") is True,
        )
        provider_voice_id = str(result.get("voice_id", "")).strip()
        if not provider_voice_id:
            raise ElevenLabsError("ElevenLabs did not return a voice ID.")
        candidate = alpha_store.record_voice_id_candidate(
            voice_id,
            provider_voice_id=provider_voice_id,
            creation_method="instant_clone",
            creation_model_id="instant_voice_clone",
        )
        updated = alpha_store.connect_voice_id_candidate(candidate["candidate_id"])
        audition_error = None
        try:
            candidate = _generate_voice_id_audition(candidate, updated)
        except (AlphaError, ElevenLabsError) as error:
            audition_error = str(error)
        sample_message = (
            " Its standardized sample is ready."
            if not audition_error
            else (
                " The voice ID is safely tracked, but its sample could not be "
                f"generated: {audition_error} Use Generate Sample on its card to retry."
            )
        )
        return {
            "message": (
                f"Generated voice ID candidate #{candidate['generation_number']} from the "
                f"selected clips.{sample_message}"
            ),
            "voice": updated,
            "candidate": candidate,
            "audition_error": audition_error,
        }
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
        usage = _usage_snapshot(subscription, result.character_cost)
        alpha_store.record_provider_usage(
            action="dialogue_tts",
            subject_id=dialogue_id,
            input_character_count=len(generation["text"]),
            character_cost=result.character_cost,
            provider_request_id=result.request_id,
            subscription=subscription,
        )
        candidate = alpha_store.complete_generation(
            generation_id,
            content=result.content,
            mime_type=result.content_type,
            provider_request_id=result.request_id,
            subscription=usage,
        )
        return {
            "message": (
                "Generated one audio candidate for review."
                + (
                    f" ElevenLabs reported {result.character_cost:,} credits."
                    if result.character_cost is not None
                    else ""
                )
            ),
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
