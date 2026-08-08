"""Build ElevenLabs PLS dictionaries from the reviewable Warcraft CSV."""

from __future__ import annotations

import argparse
import csv
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_SOURCE = Path("pronunciation/warcraft-en-US.csv")
DEFAULT_IPA_OUTPUT = Path("pronunciation/warcraft-en-US-ipa.approved.pls")
DEFAULT_ALIAS_OUTPUT = Path("pronunciation/warcraft-en-US-alias.approved.pls")
REQUIRED_COLUMNS = {
    "grapheme",
    "ipa",
    "alias",
    "category",
    "expansions",
    "review_status",
    "notes",
}
VALID_STATUSES = {"pending-review", "approved", "rejected"}
PLS_NAMESPACE = "http://www.w3.org/2005/01/pronunciation-lexicon"


def read_entries(path: str | Path) -> list[dict[str, str]]:
    path = Path(path)
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing dictionary columns: {sorted(missing)}")
        entries = list(reader)

    seen: set[str] = set()
    for row_number, entry in enumerate(entries, start=2):
        if None in entry:
            raise ValueError(f"Row {row_number} has extra CSV fields: {entry[None]}")
        missing_values = [column for column, value in entry.items() if value is None]
        if missing_values:
            raise ValueError(f"Row {row_number} is missing CSV fields: {missing_values}")
        grapheme = entry["grapheme"].strip()
        if not grapheme:
            raise ValueError(f"Row {row_number} has no grapheme")
        key = grapheme.casefold()
        if key in seen:
            raise ValueError(f"Duplicate grapheme on row {row_number}: {grapheme}")
        seen.add(key)
        if entry["review_status"] not in VALID_STATUSES:
            raise ValueError(f"Invalid review status on row {row_number}: {entry['review_status']}")
        if not entry["ipa"].startswith("/") or not entry["ipa"].endswith("/"):
            raise ValueError(f"IPA must be wrapped in slashes on row {row_number}")
        if not entry["alias"].strip():
            raise ValueError(f"Row {row_number} has no alias")
    return entries


def build_lexicon(entries: list[dict[str, str]], field: str) -> ET.ElementTree:
    ET.register_namespace("", PLS_NAMESPACE)
    root = ET.Element(
        f"{{{PLS_NAMESPACE}}}lexicon",
        {"version": "1.0", "alphabet": "ipa", "xml:lang": "en-US"},
    )
    for entry in entries:
        if entry["review_status"] != "approved":
            continue
        lexeme = ET.SubElement(root, f"{{{PLS_NAMESPACE}}}lexeme")
        ET.SubElement(lexeme, f"{{{PLS_NAMESPACE}}}grapheme").text = entry["grapheme"]
        value = entry[field]
        if field == "ipa":
            ET.SubElement(lexeme, f"{{{PLS_NAMESPACE}}}phoneme").text = value
        else:
            ET.SubElement(lexeme, f"{{{PLS_NAMESPACE}}}alias").text = value
    ET.indent(root, space="  ")
    return ET.ElementTree(root)


def write_lexicon(tree: ET.ElementTree, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    with path.open("ab") as output:
        output.write(b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--ipa-output", type=Path, default=DEFAULT_IPA_OUTPUT)
    parser.add_argument("--alias-output", type=Path, default=DEFAULT_ALIAS_OUTPUT)
    args = parser.parse_args()

    entries = read_entries(args.source)
    write_lexicon(build_lexicon(entries, "ipa"), args.ipa_output)
    write_lexicon(build_lexicon(entries, "alias"), args.alias_output)
    print(f"Built {len(entries)} reviewable pronunciation entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
