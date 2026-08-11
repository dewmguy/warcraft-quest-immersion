from pathlib import Path

import pandas as pd
import pytest

from tts_cli import tts_utils
from tts_cli.config import Settings
from tts_cli.tts_utils import TTSProcessor


def test_addon_resolver_is_additive_and_progress_event_is_enabled():
    root = Path(__file__).resolve().parents[1]
    data_modules = (root / "AI_VoiceOver" / "DataModules.lua").read_text(encoding="utf-8")
    voice_over = (root / "AI_VoiceOver" / "VoiceOver.lua").read_text(encoding="utf-8")
    compatibility = (root / "AI_VoiceOver" / "Compatibility.lua").read_text(encoding="utf-8")

    for lookup in (
        "QuestAudioLookupByNPCID",
        "QuestAudioLookupByObjectID",
        "QuestAudioLookupByItemID",
    ):
        assert lookup in data_modules
    assert 'return format("%d-%s", soundData.questID, stage)' in data_modules
    assert "soundData.unitGUID" in data_modules
    assert "GetQuestLogQuestGiverTypeAndID(soundData.questID)" in data_modules
    assert 'self:RegisterEvent("QUEST_PROGRESS")' in voice_over
    assert "function Addon:QUEST_PROGRESS()" in voice_over
    assert "GetProgressText()" in voice_over
    assert "old_QUEST_PROGRESS(self)" in compatibility
    for toc in (
        "AI_VoiceOver_1.12.toc",
        "AI_VoiceOver_2.4.3.toc",
        "AI_VoiceOver_3.3.5.toc",
    ):
        assert "addon.xml" in (root / "AI_VoiceOver" / toc).read_text(encoding="utf-8")


def test_lookup_writer_emits_per_npc_stage_filenames(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_utils, "OUTPUT_FOLDER", str(tmp_path))
    processor = TTSProcessor(fetch_voices=False, settings=Settings())
    dataframe = pd.DataFrame(
        [
            {
                "source": "accept",
                "type": "creature",
                "quest": "100",
                "id": 1,
                "addon_file_key": "100-accept-c1",
            },
            {
                "source": "accept",
                "type": "creature",
                "quest": "100",
                "id": 2,
                "addon_file_key": "100-accept-c2",
            },
            {
                "source": "progress",
                "type": "creature",
                "quest": "100",
                "id": 1,
                "addon_file_key": "100-progress-c1",
            },
        ]
    )

    processor.write_questlog_npc_lookups_table(
        dataframe,
        "AI_VoiceOverData_Vanilla",
        "creature",
        "NPCIDLookupByQuestID",
        "questlog_npc_lookups",
    )

    output = (tmp_path / "questlog_npc_lookups.lua").read_text(encoding="utf-8")
    assert "QuestAudioLookupByNPCID" in output
    assert "100-accept-c1" in output
    assert "100-accept-c2" in output
    assert "100-progress-c1" in output
    relation_assignment, audio_assignment = output.split("QuestAudioLookupByNPCID", 1)
    assert "[100]" not in relation_assignment
    assert "[100]" in audio_assignment


@pytest.mark.parametrize(
    ("client", "folder"), [("1.12.1", "1.12"), ("2.4.3", "2.4.3"), ("3.3.5", "3.3.5")]
)
def test_supported_legacy_client_compatibility_files_remain_present(client, folder):
    compatibility = Path(__file__).resolve().parents[1] / "AI_VoiceOver" / folder
    assert compatibility.is_dir()
    assert any(compatibility.iterdir())
