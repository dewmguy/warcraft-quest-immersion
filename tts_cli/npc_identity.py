from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from tts_cli.consts import GENDER_DICT, RACE_DICT


@dataclass(frozen=True)
class CreatureIdentity:
    race_id: int
    gender_id: int
    race_basis: str
    gender_basis: str
    ambiguous: bool = False


# Ordered from specific model families to broader playable-race markers.
MODEL_RACE_RULES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        -105,
        (
            "clockworkgnome",
            "gnomebot",
            "gnomespidertank",
            "demolishervehicle",
            "felreaver",
            "golemharvest",
            "gyrocopter",
        ),
    ),
    (-124, ("thorum", "freia", "hodir")),
    (5, ("ladysylvanaswindrunner",)),
    (
        5,
        (
            "banshee",
            "fleshgolem",
            "northrendgeist",
            "vampyrbloodprince",
            "arthaslichking",
            "lich",
            "ghoul",
            "deathknight",
            "necromancer",
            "valkier",
            "zombiefiedvrykul",
        ),
    ),
    (
        -102,
        (
            "dreadlord",
            "eredar",
            "malganis",
            "moarg",
            "succubus",
            "doomguard",
            "shivan",
            "infernal",
        ),
    ),
    (14, ("akama", "lostone")),
    (-109, ("earthendwarf",)),
    (-107, ("etherial", "ethereal")),
    (-108, ("furbolg",)),
    (-110, ("ogre",)),
    (-111, ("wolvar",)),
    (-112, ("oracle",)),
    (-113, ("dryad", "frostnymph")),
    (-114, ("keeperofthegrove",)),
    (-115, ("arakkoa",)),
    (-116, ("murloc",)),
    (-117, ("sporeling",)),
    (-118, ("naaru",)),
    (-119, ("centaur",)),
    (-120, ("satyr",)),
    (-121, ("gnoll",)),
    (-122, ("quillboar",)),
    (-123, ("troglodyte",)),
    (-125, ("spirithealer", "ghost")),
    (10, ("bloodelf",)),
    (11, ("draenei", "velen")),
    (3, ("dwarf",)),
    (7, ("gnome",)),
    (4, ("nightelf",)),
    (1, ("human", "jaina", "kingvarianwrynn", "landro", "madscientist", "medivh", "huml", "hums")),
    (2, ("orc", "rexxar")),
    (8, ("troll",)),
    (6, ("tauren",)),
    (9, ("goblin",)),
    (13, ("naga", "siren")),
    (16, ("vrykul",)),
    (17, ("tuskarr",)),
    (22, ("worgen",)),
    (15, ("skeleton",)),
)

CREATURE_TYPE_RACES = {
    1: -100,  # Beast
    2: -101,  # Dragonkin
    3: -102,  # Demon
    4: -103,  # Elemental
    5: -104,  # Giant
    6: 5,  # Undead / Scourge
    7: -106,  # Humanoid without a more specific model family
    8: -100,  # Critter
    9: -105,  # Mechanical
    10: -1,  # Not specified; preserve the explicit narrator fallback
    11: -103,  # Totem
    12: -100,  # Non-combat pet
    13: -103,  # Gas cloud
}

FEMALE_MODEL_MARKERS = ("female", "lady", "jaina", "sylvanas", "freia", "alexstrasza")
MALE_MODEL_MARKERS = ("male", "kingvarian", "rexxar", "akama", "hodir", "thorum")
FEMALE_NAME_MARKERS = re.compile(
    r"\b(lady|queen|princess|mistress|matron|mother|sister|dame|maiden|witch)\b",
    re.IGNORECASE,
)
MALE_NAME_MARKERS = re.compile(
    r"\b(king|prince|lord|sir|father|brother|baron|earl|duke)\b",
    re.IGNORECASE,
)


def _normalized_paths(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(re.sub(r"[^a-z0-9]", "", value.lower()) for value in paths if value)


def _path_races(paths: tuple[str, ...]) -> set[int]:
    matches: set[int] = set()
    for path in paths:
        for race_id, markers in MODEL_RACE_RULES:
            if any(marker in path for marker in markers):
                matches.add(race_id)
                break
    return matches


def infer_creature_identity(
    *,
    name: str,
    creature_type: int,
    race_candidates: Iterable[int],
    gender_candidates: Iterable[int],
    model_paths: Iterable[str],
    model_genders: Iterable[int],
) -> CreatureIdentity:
    direct_races = {value for value in race_candidates if value in RACE_DICT and value != -1}
    direct_genders = {value for value in gender_candidates if value in GENDER_DICT}
    paths = _normalized_paths(model_paths)
    path_races = _path_races(paths)

    race_ambiguous = len(direct_races) > 1
    if len(direct_races) == 1:
        race_id = next(iter(direct_races))
        race_basis = "CreatureDisplayInfoExtra.DisplayRaceID"
    elif len(path_races) == 1:
        race_id = next(iter(path_races))
        race_basis = "CreatureModelData.ModelPath"
        race_ambiguous = False
    elif path_races:
        race_id = -1
        race_basis = "conflicting CreatureModelData.ModelPath families"
        race_ambiguous = True
    elif race_ambiguous:
        race_id = -1
        race_basis = "conflicting CreatureDisplayInfoExtra.DisplayRaceID values"
    else:
        race_id = CREATURE_TYPE_RACES.get(creature_type, -1)
        race_basis = "creature_template.type" if race_id != -1 else "explicit narrator fallback"

    gender_ambiguous = len({value for value in direct_genders if value in {0, 1}}) > 1
    if len(direct_genders) == 1:
        gender_id = next(iter(direct_genders))
        gender_basis = "CreatureDisplayInfoExtra.DisplaySexID"
    else:
        known_model_genders = {value for value in model_genders if value in {0, 1}}
        path_gender = None
        if any(marker in path for path in paths for marker in FEMALE_MODEL_MARKERS):
            path_gender = 1
        elif any(marker in path for path in paths for marker in MALE_MODEL_MARKERS):
            path_gender = 0
        if len(known_model_genders) == 1:
            gender_id = next(iter(known_model_genders))
            gender_basis = "creature_model_info.Gender"
            gender_ambiguous = False
        elif path_gender is not None:
            gender_id = path_gender
            gender_basis = "CreatureModelData.ModelPath"
            gender_ambiguous = False
        elif FEMALE_NAME_MARKERS.search(name):
            gender_id = 1
            gender_basis = "NPC title"
            gender_ambiguous = False
        elif MALE_NAME_MARKERS.search(name):
            gender_id = 0
            gender_basis = "NPC title"
            gender_ambiguous = False
        else:
            gender_id = 2
            gender_basis = "model gender unspecified"

    return CreatureIdentity(
        race_id=race_id,
        gender_id=gender_id,
        race_basis=race_basis,
        gender_basis=gender_basis,
        ambiguous=race_ambiguous or gender_ambiguous,
    )
