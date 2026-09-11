from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class NPCReferenceCatalogError(ValueError):
    pass


REFERENCE_TYPES = {
    "film_and_tv",
    "game",
    "holiday",
    "literature",
    "music",
    "mythology",
    "real_person",
    "real_world",
}
REFERENCE_CONFIDENCE = {"high", "strong"}


@dataclass(frozen=True)
class NPCReferenceEntry:
    key: str
    npc_names: tuple[str, ...]
    entity_ids: tuple[int, ...]
    reference_title: str
    reference_type: str
    summary: str
    voice_direction: str
    confidence: str
    source_title: str
    source_url: str


@dataclass(frozen=True)
class NPCReferenceCatalog:
    catalog_id: str
    catalog_version: int
    expansion: str
    locale: str
    researched_at: str
    entries: tuple[NPCReferenceEntry, ...]
    matched_voice_scope: str = "unchanged"


def _required_text(payload: dict[str, Any], field: str, *, maximum: int) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise NPCReferenceCatalogError(f"Reference catalog field '{field}' is required.")
    if len(value) > maximum:
        raise NPCReferenceCatalogError(
            f"Reference catalog field '{field}' exceeds {maximum} characters."
        )
    return value


def load_npc_reference_catalog(path: Path) -> NPCReferenceCatalog:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NPCReferenceCatalogError(f"Could not read NPC reference catalog: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise NPCReferenceCatalogError("NPC reference catalog schema_version must be 1.")

    sources = payload.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise NPCReferenceCatalogError("NPC reference catalog must define its sources.")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise NPCReferenceCatalogError("NPC reference catalog must contain entries.")
    matched_voice_scope = str(payload.get("matched_voice_scope") or "unchanged").strip()
    if matched_voice_scope not in {"unchanged", "unique"}:
        raise NPCReferenceCatalogError(
            "NPC reference catalog matched_voice_scope must be 'unchanged' or 'unique'."
        )

    entries: list[NPCReferenceEntry] = []
    seen_keys: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise NPCReferenceCatalogError("Every NPC reference entry must be an object.")
        key = _required_text(raw_entry, "key", maximum=120)
        if key in seen_keys:
            raise NPCReferenceCatalogError(f"Duplicate NPC reference key: {key}")
        seen_keys.add(key)

        npc_names = raw_entry.get("npc_names")
        if not isinstance(npc_names, list) or not npc_names:
            raise NPCReferenceCatalogError(f"Reference '{key}' must name at least one NPC.")
        normalized_names = tuple(str(name).strip() for name in npc_names if str(name).strip())
        if len(normalized_names) != len(npc_names) or len(set(normalized_names)) != len(npc_names):
            raise NPCReferenceCatalogError(f"Reference '{key}' has invalid or duplicate NPC names.")

        raw_entity_ids = raw_entry.get("entity_ids", [])
        if not isinstance(raw_entity_ids, list):
            raise NPCReferenceCatalogError(f"Reference '{key}' entity_ids must be a list.")
        try:
            entity_ids = tuple(int(value) for value in raw_entity_ids)
        except (TypeError, ValueError) as error:
            raise NPCReferenceCatalogError(
                f"Reference '{key}' contains an invalid entity ID."
            ) from error

        reference_type = _required_text(raw_entry, "reference_type", maximum=40)
        if reference_type not in REFERENCE_TYPES:
            raise NPCReferenceCatalogError(
                f"Reference '{key}' has unsupported type '{reference_type}'."
            )
        confidence = _required_text(raw_entry, "confidence", maximum=20)
        if confidence not in REFERENCE_CONFIDENCE:
            raise NPCReferenceCatalogError(
                f"Reference '{key}' has unsupported confidence '{confidence}'."
            )

        source_key = _required_text(raw_entry, "source", maximum=80)
        source = sources.get(source_key)
        if not isinstance(source, dict):
            raise NPCReferenceCatalogError(f"Reference '{key}' uses unknown source '{source_key}'.")
        entries.append(
            NPCReferenceEntry(
                key=key,
                npc_names=normalized_names,
                entity_ids=entity_ids,
                reference_title=_required_text(raw_entry, "reference_title", maximum=200),
                reference_type=reference_type,
                summary=_required_text(raw_entry, "summary", maximum=1200),
                voice_direction=str(raw_entry.get("voice_direction") or "").strip()[:600],
                confidence=confidence,
                source_title=_required_text(source, "title", maximum=240),
                source_url=_required_text(source, "url", maximum=1000),
            )
        )

    return NPCReferenceCatalog(
        catalog_id=_required_text(payload, "catalog_id", maximum=120),
        catalog_version=int(payload.get("catalog_version") or 0),
        expansion=_required_text(payload, "expansion", maximum=20),
        locale=_required_text(payload, "locale", maximum=10),
        researched_at=_required_text(payload, "researched_at", maximum=40),
        entries=tuple(entries),
        matched_voice_scope=matched_voice_scope,
    )
