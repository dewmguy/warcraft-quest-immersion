from scripts.validate_imported_audio import parse_length_table


def test_parse_length_table(tmp_path):
    path = tmp_path / "sound_length_table.lua"
    path.write_text(
        "Module.SoundLengthLookupByFileName = {\n"
        '    ["5-accept"] = 12.5,\n'
        '    ["abc123"] = 3.25,\n'
        "}\n",
        encoding="utf-8",
    )
    assert parse_length_table(path) == {"5-accept": 12.5, "abc123": 3.25}
