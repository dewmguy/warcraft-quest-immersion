from __future__ import annotations

from pathlib import Path

import pytest

from tts_cli.corpus import AzerothCoreCorpusExtractor, MappingCorpusSource, write_corpus_bundle


@pytest.fixture
def azerothcore_tables() -> dict[str, list[dict]]:
    return {
        "version_db_world": [{"sql_rev": "2026_08_11_00", "required_rev": "2026_08_10_00"}],
        "quest_template": [
            {
                "ID": 100,
                "LogTitle": "A Shared Duty",
                "QuestDescription": "Bring this warning to the pass, $N.",
                "LogDescription": "Speak with the wardens.",
                "QuestCompletionLog": "Return to the watch.",
                "QuestType": 2,
                "Flags": 0,
            },
            {
                "ID": 200,
                "LogTitle": "Disabled Fixture",
                "QuestDescription": "This disabled line remains retained.",
                "QuestType": 2,
                "Flags": 0,
            },
        ],
        "quest_request_items": [{"ID": 100, "CompletionText": "Have you warned them?"}],
        "quest_offer_reward": [{"ID": 100, "RewardText": "You have done well."}],
        "creature_template": [
            {
                "entry": 1,
                "name": "Marshal Rowan",
                "subname": "Watch Commander",
                "faction": 11,
                "npcflag": 2,
                "gossip_menu_id": 10,
            },
            {
                "entry": 2,
                "name": "Sentinel Amara",
                "subname": "Sentinel",
                "faction": 11,
                "npcflag": 2,
                "gossip_menu_id": 0,
            },
        ],
        "creature_template_model": [
            {"CreatureID": 1, "CreatureDisplayID": 1001},
            {"CreatureID": 2, "CreatureDisplayID": 1002},
        ],
        "creature_model_info": [
            {"DisplayID": 1001, "Gender": 0},
            {"DisplayID": 1002, "Gender": 1},
        ],
        "creature": [
            {"guid": 1, "id1": 1, "map": 0, "zoneId": 12},
            {"guid": 2, "id1": 1, "map": 0, "zoneId": 12},
            {"guid": 3, "id1": 2, "map": 1, "zoneId": 14},
        ],
        "creature_queststarter": [
            {"id": 1, "quest": 100},
            {"id": 2, "quest": 100},
            {"id": 1, "quest": 200},
        ],
        "creature_questender": [{"id": 1, "quest": 100}, {"id": 2, "quest": 100}],
        "gameobject_template": [{"entry": 20, "name": "Warden's Notice", "type": 10, "data19": 10}],
        "gameobject_queststarter": [{"id": 20, "quest": 100}],
        "gameobject_questender": [{"id": 20, "quest": 100}],
        "item_template": [{"entry": 30, "name": "Sealed Warning", "StartQuest": 100}],
        "disables": [{"sourceType": 1, "entry": 200, "flags": 0}],
        "quest_greeting": [{"ID": 1, "Type": 0, "Greeting": "The pass needs you."}],
        "gossip_menu": [
            {"MenuID": 10, "TextID": 500},
            {"MenuID": 11, "TextID": 501},
            {"MenuID": 12, "TextID": 502},
        ],
        "gossip_menu_option": [
            {"MenuID": 10, "OptionID": 0, "OptionText": "Tell me more.", "ActionMenuID": 11},
            {"MenuID": 11, "OptionID": 0, "OptionText": "Go back.", "ActionMenuID": 10},
            {"MenuID": 11, "OptionID": 1, "OptionText": "Continue.", "ActionMenuID": 12},
        ],
        "conditions": [
            {
                "SourceTypeOrReferenceId": 15,
                "SourceGroup": 10,
                "SourceEntry": 0,
                "ConditionTypeOrReference": 8,
                "ConditionValue1": 100,
            }
        ],
        "npc_text": [
            {"ID": 500, "BroadcastTextID0": 600, "Probability0": 1.0},
            {"ID": 501, "text0_0": "The lower path is safer.", "text0_1": ""},
            {"ID": 502, "text0_0": "The lower path is safer.", "text0_1": ""},
            {"ID": 999, "text0_0": "Unrooted but retained.", "text0_1": ""},
        ],
        "broadcast_text": [
            {
                "ID": 600,
                "MaleText": "Stand ready, traveler.",
                "FemaleText": "Remain alert, traveler.",
                "LanguageID": 7,
                "SoundEntriesID": 0,
            }
        ],
        "db_CreatureDisplayInfo": [
            {"ID": 1001, "ModelID": 3001, "ExtendedDisplayInfoID": 2001},
            {"ID": 1002, "ModelID": 3002, "ExtendedDisplayInfoID": 2002},
        ],
        "db_CreatureDisplayInfoExtra": [
            {"ID": 2001, "DisplayRaceID": 1, "DisplaySexID": 0},
            {"ID": 2002, "DisplayRaceID": 1, "DisplaySexID": 1},
        ],
        "db_CreatureModelData": [
            {"ID": 3001, "ModelPath": "Creature\\Human\\HumanMale.mdx"},
            {"ID": 3002, "ModelPath": "Creature\\Human\\HumanFemale.mdx"},
        ],
        "db_FactionTemplate": [{"ID": 11, "Faction": 72}],
        "db_Faction": [{"ID": 72, "Name_Lang_enUS": "Stormwind"}],
        "db_AreaTable": [
            {
                "ID": 12,
                "ContinentID": 0,
                "ParentAreaID": 0,
                "Flags": 0,
                "AreaName_Lang_enUS": "Elwynn Forest",
            },
            {
                "ID": 14,
                "ContinentID": 1,
                "ParentAreaID": 0,
                "Flags": 0,
                "AreaName_Lang_enUS": "Darnassus",
            },
            {
                "ID": 40,
                "ContinentID": 0,
                "ParentAreaID": 0,
                "Flags": 0,
                "AreaName_Lang_enUS": "Westfall",
            },
            {
                "ID": 1581,
                "ContinentID": 36,
                "ParentAreaID": 0,
                "Flags": 0,
                "AreaName_Lang_enUS": "The Deadmines",
            },
        ],
        "db_Map": [
            {"ID": 0, "MapType": 0, "MapName_Lang_enUS": "Eastern Kingdoms"},
            {"ID": 1, "MapType": 0, "MapName_Lang_enUS": "Kalimdor"},
            {"ID": 36, "MapType": 1, "MapName_Lang_enUS": "Deadmines"},
        ],
        "db_WorldMapArea": [
            {"ID": 1, "MapID": 0, "AreaID": 12, "Y1": 0, "Y2": 1, "X1": 0, "X2": 1},
            {"ID": 2, "MapID": 1, "AreaID": 14, "Y1": 0, "Y2": 1, "X1": 0, "X2": 1},
            {"ID": 3, "MapID": 0, "AreaID": 40, "Y1": 0, "Y2": 1, "X1": 0, "X2": 1},
        ],
        "instance_template": [{"map": 36, "parent": 0, "script": "instance_deadmines"}],
    }


@pytest.fixture
def corpus_bundle_path(tmp_path: Path, azerothcore_tables: dict[str, list[dict]]) -> Path:
    bundle = AzerothCoreCorpusExtractor(
        MappingCorpusSource(azerothcore_tables),
        source_sha256="a" * 64,
        source_version="AzerothCore rev fixture",
    ).extract()
    return write_corpus_bundle(bundle, tmp_path / "azerothcore-3.3.5-enUS.zip")
