from scripts.analyze_legacy_pack import (
    QUEST_AUDIO_PATTERN,
    decode_lua_string,
    extract_terms,
    is_unsafe_archive_path,
)


def test_quest_audio_filename_contract():
    match = QUEST_AUDIO_PATTERN.search("female-123-complete.mp3")
    assert match is not None
    assert match.groupdict() == {
        "player_gender": "female",
        "quest_id": "123",
        "source": "complete",
    }


def test_archive_path_guard():
    assert not is_unsafe_archive_path("AI_VoiceOver/generated/sounds/quests/5-accept.mp3")
    assert is_unsafe_archive_path("../outside.mp3")
    assert is_unsafe_archive_path("C:/outside.mp3")


def test_lua_string_and_candidate_extraction():
    assert decode_lua_string(r"Quel\'Thalas") == "Quel'Thalas"
    rows = extract_terms(
        {
            "npc_name": ["Thrall"],
            "gossip": ["Go to Orgrimmar and speak to Thrall."],
            "quest_lookup": ["The Gates of Orgrimmar"],
        }
    )
    by_term = {row["term"]: row for row in rows}
    assert by_term["Orgrimmar"]["occurrences"] == 2
    assert by_term["Thrall"]["occurrences"] == 2
