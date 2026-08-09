import pandas as pd
import pytest

from tts_cli.data_sources import DataSourceError, load_dialogue_csv, validate_dialogue_dataframe
from tts_cli.paths import SAMPLE_DATA_PATH


def test_bundled_sample_matches_the_dialogue_schema():
    dataframe = load_dialogue_csv(SAMPLE_DATA_PATH)

    assert len(dataframe) == 4
    assert set(dataframe["source"]) == {"accept", "complete", "gossip"}
    assert dataframe["DisplayRaceID"].dtype.kind in {"i", "u"}


def test_missing_columns_are_reported_together():
    with pytest.raises(DataSourceError, match="missing columns"):
        validate_dialogue_dataframe(pd.DataFrame({"source": ["accept"]}))


def test_unsupported_source_is_rejected():
    dataframe = load_dialogue_csv(SAMPLE_DATA_PATH)
    dataframe.loc[0, "source"] = "unknown"

    with pytest.raises(DataSourceError, match="Unsupported dialogue source"):
        validate_dialogue_dataframe(dataframe)


def test_enriched_source_columns_are_preserved():
    dataframe = load_dialogue_csv(SAMPLE_DATA_PATH)
    dataframe["broadcast_text_id"] = [1001, 1002, 1003, 1004]

    validated = validate_dialogue_dataframe(dataframe)

    assert validated["broadcast_text_id"].tolist() == [1001, 1002, 1003, 1004]
