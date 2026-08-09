from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class AudioProcessingError(RuntimeError):
    """Raised when reference audio cannot be prepared for compact storage."""


def compress_reference_audio(content: bytes, original_name: str) -> bytes:
    """Convert one reference clip to compact, provider-recommended mono MP3."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioProcessingError("Reference audio compression is unavailable on this deployment.")
    suffix = Path(original_name).suffix.lower() or ".audio"
    with tempfile.TemporaryDirectory(prefix="wqi-reference-") as temporary_directory:
        input_path = Path(temporary_directory) / f"input{suffix}"
        output_path = Path(temporary_directory) / "reference.mp3"
        input_path.write_bytes(content)
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(output_path),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AudioProcessingError("Reference audio compression did not complete.") from error
        if result.returncode != 0 or not output_path.is_file():
            raise AudioProcessingError(
                "The selected file could not be decoded as supported reference audio."
            )
        compressed = output_path.read_bytes()
        if not compressed:
            raise AudioProcessingError("Reference audio compression returned an empty file.")
        if suffix == ".mp3" and len(compressed) >= len(content):
            return content
        return compressed
