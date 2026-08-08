"""Verify and extract the legacy Vanilla data module into ignored local storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

DEFAULT_ARCHIVE = Path(
    "imports/source-archives/AI-VoiceOver-1.4.1-plus-VanillaData-0.1-WoW-3.3.5a-Load-Outdated.zip"
)
DEFAULT_MANIFEST = Path("imports/source-archives/manifest.json")
DEFAULT_TARGET = Path("data/imported/mrthinger-vanilla-v1.0.0")
DATA_PREFIX = "AI_VoiceOverData_Vanilla/"


def is_unsafe_archive_path(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return path.is_absolute() or ".." in path.parts or bool(re.match(r"^[A-Za-z]:", normalized))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_artifact(manifest_path: Path, archive: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        if artifact["file"] == archive.name:
            return artifact
    raise ValueError(f"Archive is not listed in {manifest_path}: {archive.name}")


def verify_archive(archive: Path, artifact: dict) -> str:
    if archive.stat().st_size != artifact["size_bytes"]:
        raise ValueError("Archive size does not match the tracked manifest")
    digest = sha256_file(archive)
    if digest.casefold() != artifact["sha256"].casefold():
        raise ValueError("Archive SHA-256 does not match the tracked manifest")
    return digest


def selected_entries(package: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = []
    for entry in package.infolist():
        if is_unsafe_archive_path(entry.filename):
            raise ValueError(f"Unsafe archive path: {entry.filename}")
        normalized = entry.filename.replace("\\", "/")
        if normalized.startswith(DATA_PREFIX):
            entries.append(entry)
    if not entries:
        raise ValueError(f"Archive does not contain {DATA_PREFIX}")
    return entries


def extract_archive(archive: Path, target: Path, digest: str) -> dict:
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Import target already exists: {target}")

    staging = Path(tempfile.mkdtemp(prefix=".legacy-import-", dir=target.parent)).resolve()
    if staging.parent != target.parent or not staging.name.startswith(".legacy-import-"):
        raise RuntimeError("Temporary import directory escaped the intended data directory")

    file_count = 0
    audio_count = 0
    uncompressed_bytes = 0
    try:
        with zipfile.ZipFile(archive) as package:
            entries = selected_entries(package)
            for entry in entries:
                relative = Path(*entry.filename.replace("\\", "/").split("/"))
                destination = staging / relative
                if entry.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                file_count += 1
                uncompressed_bytes += entry.file_size
                if entry.filename.lower().endswith(".mp3"):
                    audio_count += 1

        receipt = {
            "schema_version": 1,
            "source_archive": archive.name,
            "source_sha256": digest,
            "data_prefix": DATA_PREFIX,
            "files": file_count,
            "mp3_files": audio_count,
            "uncompressed_bytes": uncompressed_bytes,
        }
        (staging / "import-receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        for attempt in range(5):
            try:
                staging.replace(target)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.5)
        return receipt
    except Exception:
        if staging.exists() and staging.parent == target.parent:
            shutil.rmtree(staging)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    artifact = expected_artifact(args.manifest, args.archive)
    digest = verify_archive(args.archive, artifact)
    if args.verify_only:
        print(f"Verified {args.archive.name}: {digest}")
        return 0

    receipt = extract_archive(args.archive, args.target, digest)
    print(f"Imported {receipt['mp3_files']} audio files into {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
