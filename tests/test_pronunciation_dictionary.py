import csv

import pytest

from scripts.build_pronunciation_dictionary import build_lexicon, read_entries


def test_project_dictionary_is_valid():
    entries = read_entries("pronunciation/warcraft-en-US.csv")
    assert entries
    assert {entry["review_status"] for entry in entries} == {"approved"}
    assert not any(entry["notes"] for entry in entries)

    tree = build_lexicon(entries, "ipa")
    root = tree.getroot()
    assert root.attrib["alphabet"] == "ipa"
    assert len(root) == len(entries)


def test_duplicate_graphemes_are_rejected(tmp_path):
    source = tmp_path / "dictionary.csv"
    fieldnames = [
        "grapheme",
        "ipa",
        "alias",
        "category",
        "expansions",
        "review_status",
        "notes",
    ]
    with source.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(
            [
                {
                    "grapheme": "Azeroth",
                    "ipa": "/test/",
                    "alias": "test",
                    "category": "world",
                    "expansions": "vanilla",
                    "review_status": "pending-review",
                    "notes": "",
                },
                {
                    "grapheme": "azeroth",
                    "ipa": "/test/",
                    "alias": "test",
                    "category": "world",
                    "expansions": "vanilla",
                    "review_status": "pending-review",
                    "notes": "",
                },
            ]
        )
    with pytest.raises(ValueError, match="Duplicate grapheme"):
        read_entries(source)


def test_missing_trailing_field_is_rejected(tmp_path):
    source = tmp_path / "dictionary.csv"
    source.write_text(
        "grapheme,ipa,alias,category,expansions,review_status,notes\n"
        "Azeroth,/test/,test,world,vanilla,approved\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing CSV fields"):
        read_entries(source)


def test_pending_entries_are_not_emitted():
    entries = [
        {
            "grapheme": "Review me",
            "ipa": "/test/",
            "alias": "test",
            "category": "test",
            "expansions": "vanilla",
            "review_status": "pending-review",
            "notes": "",
        }
    ]

    assert len(build_lexicon(entries, "ipa").getroot()) == 0
