from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class DatapackError(ValueError):
    pass


QUEST_AUDIO_PATTERN = re.compile(
    r"^(?:(?P<player_gender>[fm])-)?(?P<quest_id>\d+)-"
    r"(?P<stage>accept|progress|complete)\.mp3$",
    re.IGNORECASE,
)
GOSSIP_AUDIO_PATTERN = re.compile(
    r"^(?:(?P<player_gender>[fm])-)?(?P<hash>[0-9a-f]{32})\.mp3$", re.IGNORECASE
)
LENGTH_PATTERN = re.compile(r'\["([^"\\]+)"\]\s*=\s*([0-9.]+)')
GOSSIP_HASH_PATTERN = re.compile(r'=\s*"([0-9a-f]{32})"', re.IGNORECASE)
ARCHIVE_VERSION_PATTERN = re.compile(r"[_-]v(?P<version>\d+(?:\.\d+)+)", re.IGNORECASE)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _unsafe_path(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return path.is_absolute() or ".." in path.parts or bool(re.match(r"^[A-Za-z]:", normalized))


def _decode_entry(package: zipfile.ZipFile, entry: zipfile.ZipInfo) -> str:
    content = package.read(entry)
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DatapackError(f"Could not decode lookup file: {entry.filename}")


def _toc_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^##\s*([^:]+):\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    return fields


@dataclass(frozen=True)
class DatapackAsset:
    asset_id: str
    entry_path: str
    source_kind: str
    logical_key: str
    quest_id: int | None
    stage: str
    legacy_file_key: str
    player_gender_variant: str
    duration_seconds: float
    lookup_referenced: bool


@dataclass(frozen=True)
class DatapackArchive:
    path: Path
    pack_id: str
    archive_name: str
    archive_sha256: str
    archive_bytes: int
    module_name: str
    title: str
    module_version: str
    archive_version: str
    priority: int
    assets: tuple[DatapackAsset, ...]

    def summary(self) -> dict[str, object]:
        return {
            "pack_id": self.pack_id,
            "archive": self.archive_name,
            "sha256": self.archive_sha256,
            "bytes": self.archive_bytes,
            "module": self.module_name,
            "title": self.title,
            "module_version": self.module_version,
            "archive_version": self.archive_version,
            "priority": self.priority,
            "audio_assets": len(self.assets),
            "quest_assets": sum(asset.source_kind == "quest" for asset in self.assets),
            "gossip_assets": sum(asset.source_kind == "gossip" for asset in self.assets),
            "gender_variant_assets": sum(
                bool(asset.player_gender_variant) for asset in self.assets
            ),
            "lookup_unreferenced_assets": sum(not asset.lookup_referenced for asset in self.assets),
        }


def inspect_datapack_archive(path: Path) -> DatapackArchive | None:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Datapack archive was not found: {path}")

    archive_sha256 = _sha256_file(path)
    pack_id = _stable_id("pack", archive_sha256)
    with zipfile.ZipFile(path) as package:
        entries = package.infolist()
        unsafe = [entry.filename for entry in entries if _unsafe_path(entry.filename)]
        if unsafe:
            raise DatapackError(f"Unsafe archive path in {path.name}: {unsafe[0]}")

        audio_entries = [
            entry
            for entry in entries
            if not entry.is_dir() and entry.filename.lower().endswith(".mp3")
        ]
        if not audio_entries:
            return None

        toc_entries = [
            entry
            for entry in entries
            if not entry.is_dir()
            and entry.filename.lower().endswith(".toc")
            and len(PurePosixPath(entry.filename.replace("\\", "/")).parts) == 2
        ]
        if len(toc_entries) != 1:
            raise DatapackError(
                f"Expected one top-level TOC in {path.name}; found {len(toc_entries)}."
            )
        toc_entry = toc_entries[0]
        toc = _toc_fields(_decode_entry(package, toc_entry))
        module_name = PurePosixPath(toc_entry.filename.replace("\\", "/")).parts[0]
        title = toc.get("Title", module_name).replace("VoiceOver Data - ", "").strip()
        module_version = toc.get("Version", "unknown")
        priority_text = toc.get("X-VoiceOver-DataModule-Priority", "0")
        try:
            priority = int(priority_text)
        except ValueError as error:
            raise DatapackError(
                f"Invalid data-module priority in {path.name}: {priority_text}"
            ) from error
        version_match = ARCHIVE_VERSION_PATTERN.search(path.stem)
        archive_version = version_match.group("version") if version_match else module_version

        length_entries = [
            entry
            for entry in entries
            if entry.filename.replace("\\", "/").endswith("/generated/sound_length_table.lua")
        ]
        if len(length_entries) != 1:
            raise DatapackError(
                f"Expected one sound-length table in {path.name}; found {len(length_entries)}."
            )
        durations = {
            stem: float(duration)
            for stem, duration in LENGTH_PATTERN.findall(_decode_entry(package, length_entries[0]))
        }
        gossip_hashes: set[str] = set()
        for entry in entries:
            normalized = entry.filename.replace("\\", "/").lower()
            if normalized.endswith("gossip_file_lookups.lua"):
                gossip_hashes.update(
                    value.lower()
                    for value in GOSSIP_HASH_PATTERN.findall(_decode_entry(package, entry))
                )

        assets: list[DatapackAsset] = []
        seen_variants: set[tuple[str, str]] = set()
        for entry in audio_entries:
            normalized = entry.filename.replace("\\", "/")
            parts = PurePosixPath(normalized).parts
            if (
                len(parts) < 5
                or parts[-3:-1] != ("sounds", "quests")
                and parts[-3:-1]
                != (
                    "sounds",
                    "gossip",
                )
            ):
                raise DatapackError(f"Unsupported audio location in {path.name}: {normalized}")
            filename = parts[-1]
            stem = PurePosixPath(filename).stem
            duration = durations.get(stem)
            if duration is None or duration <= 0:
                raise DatapackError(f"Missing or invalid duration for {normalized}")

            if parts[-2] == "quests":
                match = QUEST_AUDIO_PATTERN.fullmatch(filename)
                if not match:
                    raise DatapackError(f"Unsupported quest audio filename: {normalized}")
                quest_id = int(match.group("quest_id"))
                stage = match.group("stage").lower()
                player_gender = (match.group("player_gender") or "").lower()
                logical_key = f"quest:{quest_id}:{stage}"
                lookup_referenced = True
                source_kind = "quest"
                legacy_file_key = f"{quest_id}-{stage}"
            else:
                match = GOSSIP_AUDIO_PATTERN.fullmatch(filename)
                if not match:
                    raise DatapackError(f"Unsupported gossip audio filename: {normalized}")
                legacy_file_key = match.group("hash").lower()
                logical_key = f"gossip:{legacy_file_key}"
                quest_id = None
                stage = "gossip"
                player_gender = (match.group("player_gender") or "").lower()
                lookup_referenced = legacy_file_key in gossip_hashes
                source_kind = "gossip"

            variant_key = (logical_key, player_gender)
            if variant_key in seen_variants:
                raise DatapackError(
                    f"Duplicate {player_gender or 'generic'} asset for {logical_key} in {path.name}."
                )
            seen_variants.add(variant_key)
            assets.append(
                DatapackAsset(
                    asset_id=_stable_id("asset", pack_id, normalized),
                    entry_path=normalized,
                    source_kind=source_kind,
                    logical_key=logical_key,
                    quest_id=quest_id,
                    stage=stage,
                    legacy_file_key=legacy_file_key,
                    player_gender_variant=player_gender,
                    duration_seconds=duration,
                    lookup_referenced=lookup_referenced,
                )
            )

    return DatapackArchive(
        path=path,
        pack_id=pack_id,
        archive_name=path.name,
        archive_sha256=archive_sha256,
        archive_bytes=path.stat().st_size,
        module_name=module_name,
        title=title,
        module_version=module_version,
        archive_version=archive_version,
        priority=priority,
        assets=tuple(sorted(assets, key=lambda asset: asset.entry_path)),
    )


def inspect_datapack_directory(path: Path) -> tuple[list[DatapackArchive], list[str]]:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"Datapack directory was not found: {path}")
    packs: list[DatapackArchive] = []
    ignored: list[str] = []
    archives = sorted(path.glob("*.zip"), key=lambda item: item.name.casefold())
    if not archives:
        raise DatapackError(f"No ZIP archives were found in {path}.")
    for archive in archives:
        inspected = inspect_datapack_archive(archive)
        if inspected is None:
            ignored.append(archive.name)
        else:
            packs.append(inspected)
    if not packs:
        raise DatapackError(f"No VoiceOver data archives with MP3 assets were found in {path}.")
    return packs, ignored


__all__ = [
    "DatapackArchive",
    "DatapackAsset",
    "DatapackError",
    "inspect_datapack_archive",
    "inspect_datapack_directory",
]
