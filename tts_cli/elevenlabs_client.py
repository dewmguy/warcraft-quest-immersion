from __future__ import annotations

import base64
import json
import mimetypes
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from tts_cli.config import ConfigurationError, Settings, load_settings


class ElevenLabsError(RuntimeError):
    """Raised when an explicit ElevenLabs operation cannot be completed."""


@dataclass(frozen=True)
class AudioResponse:
    content: bytes
    content_type: str
    request_id: str | None
    trace_id: str | None
    character_cost: int | None


@dataclass(frozen=True)
class VoiceDesignResponse:
    payload: dict[str, Any]
    request_id: str | None
    trace_id: str | None
    character_cost: int | None


class ElevenLabsClient:
    base_url = "https://api.elevenlabs.io"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self._provided_session = session
        self._thread_sessions = threading.local()

    @property
    def session(self) -> requests.Session:
        if self._provided_session is not None:
            return self._provided_session
        session = getattr(self._thread_sessions, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_sessions.session = session
        return session

    @property
    def configured(self) -> bool:
        key = self.settings.elevenlabs_api_key
        return bool(key and key != "API_KEY_HERE")

    def _headers(self) -> dict[str, str]:
        try:
            key = self.settings.require_elevenlabs()
        except ConfigurationError as error:
            raise ElevenLabsError(str(error)) from error
        return {"xi-api-key": key}

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"ElevenLabs returned HTTP {response.status_code}."
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail") or str(detail)
        return f"ElevenLabs returned HTTP {response.status_code}: {detail}"

    @staticmethod
    def _character_cost(response: requests.Response) -> int | None:
        value = response.headers.get("character-cost")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def subscription(self) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{self.base_url}/v1/user/subscription",
                headers=self._headers(),
                timeout=30,
            )
        except requests.RequestException as error:
            raise ElevenLabsError(f"Could not read ElevenLabs usage: {error}") from error
        if not response.ok:
            raise ElevenLabsError(self._error_message(response))
        return response.json()

    def design_voice(
        self,
        *,
        description: str,
        preview_text: str,
        model_id: str = "eleven_ttv_v3",
        reference_audio: bytes | None = None,
        prompt_strength: float = 0.5,
    ) -> VoiceDesignResponse:
        payload: dict[str, Any] = {
            "voice_description": description,
            "text": preview_text,
            "model_id": model_id,
            "auto_generate_text": False,
        }
        if reference_audio is not None:
            payload["reference_audio_base64"] = base64.b64encode(reference_audio).decode("ascii")
            payload["prompt_strength"] = prompt_strength
        try:
            response = self.session.post(
                f"{self.base_url}/v1/text-to-voice/design",
                params={"output_format": "mp3_44100_128"},
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
        except requests.RequestException as error:
            raise ElevenLabsError(f"Could not design the voice: {error}") from error
        if not response.ok:
            raise ElevenLabsError(self._error_message(response))
        return VoiceDesignResponse(
            payload=response.json(),
            request_id=response.headers.get("request-id") or response.headers.get("x-request-id"),
            trace_id=response.headers.get("x-trace-id"),
            character_cost=self._character_cost(response),
        )

    def create_designed_voice(
        self, *, name: str, description: str, generated_voice_id: str
    ) -> dict[str, Any]:
        try:
            response = self.session.post(
                f"{self.base_url}/v1/text-to-voice",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={
                    "voice_name": name,
                    "voice_description": description,
                    "generated_voice_id": generated_voice_id,
                },
                timeout=60,
            )
        except requests.RequestException as error:
            raise ElevenLabsError(f"Could not save the designed voice: {error}") from error
        if not response.ok:
            raise ElevenLabsError(self._error_message(response))
        return response.json()

    def clone_voice(
        self,
        *,
        name: str,
        description: str,
        labels: dict[str, str],
        files: list[Path],
    ) -> dict[str, Any]:
        opened_files = []
        try:
            multipart_files = []
            for path in files:
                handle = path.open("rb")
                opened_files.append(handle)
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                multipart_files.append(("files", (path.name, handle, content_type)))
            response = self.session.post(
                f"{self.base_url}/v1/voices/add",
                headers=self._headers(),
                data={"name": name, "description": description, "labels": json.dumps(labels)},
                files=multipart_files,
                timeout=180,
            )
        except (OSError, requests.RequestException) as error:
            raise ElevenLabsError(f"Could not create the instant voice clone: {error}") from error
        finally:
            for handle in opened_files:
                handle.close()
        if not response.ok:
            raise ElevenLabsError(self._error_message(response))
        return response.json()

    def delete_voice(self, voice_id: str) -> dict[str, Any]:
        try:
            response = self.session.delete(
                f"{self.base_url}/v1/voices/{voice_id}",
                headers=self._headers(),
                timeout=60,
            )
        except requests.RequestException as error:
            raise ElevenLabsError(f"Could not delete the reusable voice: {error}") from error
        if not response.ok:
            raise ElevenLabsError(self._error_message(response))
        try:
            return response.json()
        except ValueError:
            return {"status": "ok"}

    def text_to_speech(
        self,
        *,
        voice_id: str,
        text: str,
        model_id: str,
        settings: dict[str, Any],
        pronunciation_locators: list[dict[str, str]] | None = None,
    ) -> AudioResponse:
        payload: dict[str, Any] = {
            "text": text,
            "model_id": model_id,
            "voice_settings": settings,
        }
        if pronunciation_locators:
            payload["pronunciation_dictionary_locators"] = pronunciation_locators
        try:
            response = self.session.post(
                f"{self.base_url}/v1/text-to-speech/{voice_id}",
                params={"output_format": "mp3_44100_128"},
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
        except requests.RequestException as error:
            raise ElevenLabsError(f"Could not generate speech: {error}") from error
        content_type = response.headers.get("Content-Type", "")
        if not response.ok or not content_type.startswith("audio/"):
            raise ElevenLabsError(self._error_message(response))
        return AudioResponse(
            content=response.content,
            content_type=content_type,
            request_id=response.headers.get("request-id") or response.headers.get("x-request-id"),
            trace_id=response.headers.get("x-trace-id"),
            character_cost=self._character_cost(response),
        )
