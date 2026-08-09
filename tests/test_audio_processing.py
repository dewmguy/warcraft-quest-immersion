from types import SimpleNamespace

from tts_cli import audio_processing


def test_reference_audio_is_converted_to_compact_mono_mp3(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_path = command[-1]
        with open(output_path, "wb") as output:
            output.write(b"compressed-mp3")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(audio_processing.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(audio_processing.subprocess, "run", fake_run)

    result = audio_processing.compress_reference_audio(b"large-wave-source", "speaker.wav")

    assert result == b"compressed-mp3"
    assert "-ac" in captured["command"]
    assert captured["command"][captured["command"].index("-ac") + 1] == "1"
    assert captured["command"][captured["command"].index("-b:a") + 1] == "192k"
    assert captured["kwargs"]["timeout"] == 180
