from tts_cli.config import Settings
from tts_cli.data_sources import load_dialogue_csv
from tts_cli.paths import SAMPLE_DATA_PATH
from tts_cli.tts_utils import TTSProcessor, get_hash, prune_quest_id_table


def test_preprocess_does_not_need_elevenlabs():
    processor = TTSProcessor(fetch_voices=False, settings=Settings())

    processed = processor.preprocess_dataframe(load_dialogue_csv(SAMPLE_DATA_PATH))

    assert set(processed["voice_name"]) == {"human-male", "nightelf-female", "narrator-male"}
    assert "Adventurer" in processed.iloc[0]["cleanedText"]
    assert len(processed) == 5  # The $G row expands to male and female player variants.


def test_hash_is_stable():
    assert get_hash("Lok'tar") == get_hash("Lok'tar")
    assert get_hash("Lok'tar") != get_hash("For the Alliance")


def test_prune_quest_id_table_collapses_unambiguous_paths():
    source = {"accept": {"A Quest": {"Quest Giver": {"some text": 42}}}}

    assert prune_quest_id_table(source) == {"accept": {"A Quest": 42}}
