from __future__ import annotations

from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = (
    "source",
    "quest",
    "quest_title",
    "text",
    "DisplayRaceID",
    "DisplaySexID",
    "name",
    "type",
    "id",
    "original_text",
)
VALID_SOURCES = {"accept", "progress", "complete", "gossip"}
VALID_TYPES = {"creature", "gameobject", "item"}


class DataSourceError(ValueError):
    """Raised when dialogue input cannot satisfy the generator schema."""


def validate_dialogue_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise DataSourceError(f"Dialogue data is missing columns: {', '.join(missing)}")

    # Keep joined source fields (broadcast IDs, zone, faction, provenance, etc.)
    # available to the production database while enforcing the generator contract.
    data = df.copy()
    for column in ("source", "quest", "quest_title", "text", "name", "type", "original_text"):
        data[column] = data[column].fillna("").astype(str)

    for column in ("DisplayRaceID", "DisplaySexID", "id"):
        try:
            data[column] = pd.to_numeric(data[column], errors="raise").astype(int)
        except (TypeError, ValueError) as error:
            raise DataSourceError(f"Dialogue column {column} must contain integers.") from error

    invalid_sources = sorted(set(data["source"]) - VALID_SOURCES)
    if invalid_sources:
        raise DataSourceError(f"Unsupported dialogue source values: {', '.join(invalid_sources)}")

    invalid_types = sorted(set(data["type"]) - VALID_TYPES)
    if invalid_types:
        raise DataSourceError(f"Unsupported dialogue type values: {', '.join(invalid_types)}")

    if data.empty:
        raise DataSourceError("Dialogue data contains no rows.")
    if (data["text"].str.strip() == "").any():
        raise DataSourceError("Dialogue text cannot be empty.")

    return data


def load_dialogue_csv(path: str | Path) -> pd.DataFrame:
    csv_path = Path(path).expanduser().resolve()
    if not csv_path.is_file():
        raise DataSourceError(f"Dialogue CSV was not found: {csv_path}")

    try:
        dataframe = pd.read_csv(csv_path, keep_default_na=False)
    except (OSError, pd.errors.ParserError) as error:
        raise DataSourceError(f"Could not read dialogue CSV {csv_path}: {error}") from error
    return validate_dialogue_dataframe(dataframe)


def write_dialogue_csv(df: pd.DataFrame, path: str | Path) -> Path:
    output_path = Path(path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    validate_dialogue_dataframe(df).to_csv(output_path, index=False, encoding="utf-8")
    return output_path
