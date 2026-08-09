import threading

from tts_cli.config import Settings
from tts_cli.elevenlabs_client import ElevenLabsClient


class FakeResponse:
    def __init__(
        self,
        *,
        payload=None,
        content=b"",
        content_type="application/json",
        character_cost=None,
    ):
        self.status_code = 200
        self._payload = payload or {}
        self.content = content
        self.headers = {"Content-Type": content_type, "request-id": "request-1"}
        if character_cost is not None:
            self.headers["character-cost"] = str(character_cost)

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(content=b"audio", content_type="audio/mpeg", character_cost=19)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(payload={"character_count": 50, "character_limit": 1000})


def test_default_http_sessions_are_isolated_per_worker_thread(monkeypatch):
    created_sessions = []

    def create_session():
        session = FakeSession()
        created_sessions.append(session)
        return session

    monkeypatch.setattr("tts_cli.elevenlabs_client.requests.Session", create_session)
    client = ElevenLabsClient(settings=Settings(elevenlabs_api_key="test-key"))
    barrier = threading.Barrier(3)
    sessions = []

    def read_session():
        barrier.wait()
        sessions.append(client.session)

    workers = [threading.Thread(target=read_session) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    assert len(created_sessions) == 2
    assert len({id(session) for session in sessions}) == 2


def test_tts_is_called_only_through_explicit_client_method():
    session = FakeSession()
    client = ElevenLabsClient(
        settings=Settings(elevenlabs_api_key="test-key"),
        session=session,
    )

    assert client.configured is True
    assert session.calls == []

    result = client.text_to_speech(
        voice_id="voice-1",
        text="Prepared dialogue.",
        model_id="eleven_v3",
        settings={"stability": 0.5},
    )

    assert result.content == b"audio"
    assert result.request_id == "request-1"
    assert result.character_cost == 19
    assert len(session.calls) == 1
    assert session.calls[0][1].endswith("/v1/text-to-speech/voice-1")


def test_voice_design_exposes_provider_cost_metadata():
    session = FakeSession()
    client = ElevenLabsClient(
        settings=Settings(elevenlabs_api_key="test-key"),
        session=session,
    )

    result = client.design_voice(
        description="A deliberate Warcraft test voice with a grounded tone.",
        preview_text="This comparison passage is intentionally long enough for a useful preview.",
    )

    assert result.payload == {}
    assert result.request_id == "request-1"
    assert result.character_cost == 19
    assert session.calls[0][1].endswith("/v1/text-to-voice/design")


def test_voice_clone_preserves_each_reference_files_mime_type(tmp_path):
    mp3 = tmp_path / "speaker-one.mp3"
    wav = tmp_path / "speaker-two.wav"
    mp3.write_bytes(b"mp3")
    wav.write_bytes(b"wav")
    session = FakeSession()
    client = ElevenLabsClient(
        settings=Settings(elevenlabs_api_key="test-key"),
        session=session,
    )

    client.clone_voice(
        name="Test voice",
        description="A controlled test voice.",
        labels={"language": "en"},
        files=[mp3, wav],
    )

    multipart = session.calls[0][2]["files"]
    assert multipart[0][1][2] == "audio/mpeg"
    assert multipart[1][1][2] in {"audio/wav", "audio/x-wav"}
