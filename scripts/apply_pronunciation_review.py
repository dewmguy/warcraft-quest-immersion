"""Apply the owner's first pronunciation review and normalize the CSV."""

from __future__ import annotations

import csv
from pathlib import Path

DICTIONARY_PATH = Path("pronunciation/warcraft-en-US.csv")
FIELDNAMES = [
    "grapheme",
    "ipa",
    "alias",
    "category",
    "expansions",
    "review_status",
    "notes",
]

CORRECTIONS = {
    "Lordaeron": ("/ˈlɔːr.dɔːr.ɒn/", "LORE-door-on"),
    "Feralas": ("/fɛr.əlˈæs/", "fer-ral-AS"),
    "Dun Morogh": ("/dʌn.mɔːr.roʊ/", "done-more-row"),
    "Alterac": ("/ˈɔːl.tər.æk/", "altar-ack"),
    "Gnomeregan": ("/ˈnoʊm.rə.ɡæn/", "gnome-re-gan"),
    "Uldaman": ("/ˈʌl.də.mɒn/", "UL-duh-mon"),
    "Stratholme": ("/ˈstræθ.hoʊlm/", "STRATH-holm"),
    "Jaina": ("/ˈdʒeɪ.nɑː/", "JAY-nah"),
    "Cairne": ("/ˈkɛər.ən/", "CARE-n"),
    "Tyrande": ("/tɪrˈræn.dɛ/", "tir-RAN-deh"),
    "Magni": ("/ˈmæɡ.niː/", "MAG-knee"),
    "Varian": ("/ˈvɛər.i.æn/", "VAIR-ee-an"),
    "Shattrath": ("/ˈʃɑː.træθ/", "SHAH-trath"),
    "Ulduar": ("/ˈʌl.duː.ɑːr/", "UL-doo-are"),
    "Yogg-Saron": ("/ˈjɒɡ.sɔː.rɒn/", "YOG-saw-ron"),
}


def apply_review(path: Path = DICTIONARY_PATH) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != FIELDNAMES:
            raise ValueError(f"Unexpected CSV columns: {reader.fieldnames}")
        rows = list(reader)

    corrected: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        extra = row.pop(None, None)
        if extra:
            raise ValueError(f"Row {row_number} has extra CSV fields: {extra}")
        row["notes"] = row.get("notes") or ""
        grapheme = row["grapheme"]
        if row["notes"]:
            if grapheme not in CORRECTIONS:
                raise ValueError(f"Unrecognized pronunciation note for {grapheme}")
            row["ipa"], row["alias"] = CORRECTIONS[grapheme]
            corrected.add(grapheme)
        elif grapheme in CORRECTIONS and (row["ipa"], row["alias"]) == CORRECTIONS[grapheme]:
            corrected.add(grapheme)
        row["notes"] = ""
        row["review_status"] = "approved"

    missing = CORRECTIONS.keys() - corrected
    if missing:
        raise ValueError(f"Expected review notes were not found: {sorted(missing)}")

    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main() -> int:
    rows = apply_review()
    print(f"Applied owner review to {len(rows)} pronunciation entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
