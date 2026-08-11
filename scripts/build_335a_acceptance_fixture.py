#!/usr/bin/env python3
"""Build a disposable 3.3.5a addon package for the Phase 2 playback gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_MODULE = "AI_VoiceOverData_WQI_Acceptance"
CORPUS_SNAPSHOT_ID = "04fc042b76e8d195012fac39"
LEGACY_AUDIO_MEMBER = "AI_VoiceOverData_Vanilla/generated/sounds/quests/10-accept.mp3"

TEST_SOUNDS = {
    "quests/6075-accept-c895.mp3": (440, 1.2),
    "quests/6075-accept-c11807.mp3": (880, 1.2),
    "quests/6075-complete-c1231.mp3": (660, 1.2),
    "quests/362-progress-c1500.mp3": (550, 1.2),
    "gossip/a7ff88fc7f275c6071153623e049fbf1.mp3": (990, 1.2),
}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _tone(ffmpeg: str, path: Path, frequency: int, duration: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={duration}",
            "-af",
            "volume=0.18,afade=t=in:st=0:d=0.04,afade=t=out:st=1.1:d=0.1",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-b:a",
            "128k",
            str(path),
        ],
        check=True,
    )


def _duration(ffprobe: str, path: Path) -> float:
    output = subprocess.check_output(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    )
    return round(float(output.strip()), 3)


def _copy_addon(destination: Path) -> None:
    addon = destination / "AI_VoiceOver"
    shutil.copytree(PROJECT_ROOT / "AI_VoiceOver", addon)
    shutil.copy2(addon / "AI_VoiceOver_3.3.5.toc", addon / "AI_VoiceOver.toc")


def _write_data_module(destination: Path, lengths: dict[str, float]) -> None:
    module = destination / DATA_MODULE
    generated = module / "generated"
    _write(
        module / f"{DATA_MODULE}.toc",
        f"""## Interface: 100000
## Title: VoiceOver Data - WQI Phase 2 Acceptance
## Notes: Disposable tone fixture for the certified 3.3.5 per-deliverer playback gate.
## Version: {CORPUS_SNAPSHOT_ID}
## LoadOnDemand: 1
## RequiredDeps: AI_VoiceOver
## X-Part-Of: VoiceOver
## X-Child-Of: VoiceOver
## X-VoiceOver-DataModule-Version: 1
## X-VoiceOver-DataModule-Priority: 1000
## X-VoiceOver-DataModule-Maps: 0, 1

Module.lua
generated\\quest_id_lookups.lua
generated\\questlog_npc_lookups.lua
generated\\npc_gossip_file_lookups.lua
generated\\sound_length_table.lua
""",
    )
    _write(
        module / "Module.lua",
        f"""if not VoiceOver or not VoiceOver.DataModules then return end

{DATA_MODULE} = {{}}

function {DATA_MODULE}:GetSoundPath(fileName, event)
    setfenv(1, VoiceOver)
    if Enums.SoundEvent:IsQuestEvent(event) then
        return format([[generated\\sounds\\quests\\%s.mp3]], fileName)
    elseif Enums.SoundEvent:IsGossipEvent(event) then
        return format([[generated\\sounds\\gossip\\%s.mp3]], fileName)
    end
end

VoiceOver.DataModules:Register("{DATA_MODULE}", {DATA_MODULE})
""",
    )
    _write(
        generated / "quest_id_lookups.lua",
        f"""if not VoiceOver or not VoiceOver.DataModules then return end
{DATA_MODULE}.QuestIDLookup = {{
    ["accept"] = {{
        ["The Hunter's Path"] = 6075,
        ["The Scrimshank Redemption"] = 10
    }},
    ["progress"] = {{
        ["The Haunted Mills"] = 362
    }},
    ["complete"] = {{
        ["The Hunter's Path"] = 6075
    }}
}}
""",
    )
    _write(
        generated / "questlog_npc_lookups.lua",
        f"""if not VoiceOver or not VoiceOver.DataModules then return end
{DATA_MODULE}.NPCIDLookupByQuestID = {{
    [362] = 1500
}}
{DATA_MODULE}.QuestAudioLookupByNPCID = {{
    [362] = {{
        ["progress"] = {{ [1500] = "362-progress-c1500" }}
    }},
    [6075] = {{
        ["accept"] = {{
            [895] = "6075-accept-c895",
            [11807] = "6075-accept-c11807"
        }},
        ["complete"] = {{ [1231] = "6075-complete-c1231" }}
    }}
}}
""",
    )
    _write(
        generated / "npc_gossip_file_lookups.lua",
        f"""if not VoiceOver or not VoiceOver.DataModules then return end
{DATA_MODULE}.GossipLookupByNPCID = {{
    [16131] = {{
        ["Members only, scrub!"] = "a7ff88fc7f275c6071153623e049fbf1"
    }}
}}
""",
    )
    entries = "\n".join(
        f'    ["{name.removesuffix(".mp3").split("/", 1)[1]}"] = {duration:.3f},'
        for name, duration in sorted(lengths.items())
    )
    _write(
        generated / "sound_length_table.lua",
        f"""if not VoiceOver or not VoiceOver.DataModules then return end
{DATA_MODULE}.SoundLengthLookupByFileName = {{
{entries}
}}
""",
    )


def _write_readme(destination: Path) -> None:
    _write(
        destination / "WQI-PHASE-2-ACCEPTANCE.txt",
        """WQI Phase 2 3.3.5a playback acceptance

This package contains disposable tones, not production voice audio. Copy both addon
folders into the 3.3.5a client's Interface/AddOns directory and enable out-of-date
addons. Back up any existing AI_VoiceOver folder first.

Checks

1. Shared quest 6075, The Hunter's Path, Dun Morogh:
   - Thorgas Grimson (creature 895) must play the low 440 Hz tone on accept.
   - Tristane Shadowstone (creature 11807) must play the high 880 Hz tone on accept.
   Abandon/reset the quest between NPCs. The two distinct tones prove that the same
   quest text resolves through the interacting creature GUID.

2. Completion for quest 6075:
   - Grif Wildheart (creature 1231) must play the 660 Hz tone.

3. Progress event:
   - Add but do not complete quest 362, The Haunted Mills, then open it at Coleman
     Farthing (creature 1500). The incomplete/progress pane must play the 550 Hz tone.

4. Nested gossip:
   - Rohan the Assassin (creature 16131), Eastern Plaguelands.
   - Follow: What is it that you do exactly? > So what brings you to Light's Hope? >
     What? Bonescythe? > Wow, you're insane, aren't you? > Hey wait, Gadgetzan has a
     disco? The final Members only, scrub! pane must play the 990 Hz tone.

5. Legacy fallback:
   - Quest 10, The Scrimshank Redemption, at Senior Surveyor Fizzledowser
     (creature 7724) must play the inherited spoken 10-accept.mp3. There is no
     per-NPC lookup for quest 10 in this fixture, so playback proves legacy fallback.

Record the client build, addon load status, NPC/quest tested, heard result, and any
Lua error. Remove AI_VoiceOverData_WQI_Acceptance after the gate; its tones are not
production assets.
""",
    )


def build(output: Path, ffmpeg: str, ffprobe: str, legacy_pack: Path) -> Path:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wqi-acceptance-") as temporary:
        root = Path(temporary)
        _copy_addon(root)
        module = root / DATA_MODULE
        lengths: dict[str, float] = {}
        for relative, (frequency, duration) in TEST_SOUNDS.items():
            target = module / "generated" / "sounds" / relative
            _tone(ffmpeg, target, frequency, duration)
            lengths[relative] = _duration(ffprobe, target)
        legacy_target = module / "generated" / "sounds" / "quests" / "10-accept.mp3"
        legacy_target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(legacy_pack) as source:
            legacy_target.write_bytes(source.read(LEGACY_AUDIO_MEMBER))
        lengths["quests/10-accept.mp3"] = _duration(ffprobe, legacy_target)
        _write_data_module(root, lengths)
        _write_readme(root)

        audio_manifest = {}
        for path in sorted((module / "generated" / "sounds").rglob("*.mp3")):
            relative = path.relative_to(module).as_posix()
            audio_manifest[relative] = {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        _write(
            root / "WQI-PHASE-2-ACCEPTANCE.json",
            json.dumps(
                {
                    "corpus_snapshot_id": CORPUS_SNAPSHOT_ID,
                    "data_module": DATA_MODULE,
                    "audio": audio_manifest,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        output.unlink(missing_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "acceptance" / "wqi-3.3.5-phase2-acceptance.zip",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--legacy-pack",
        type=Path,
        default=PROJECT_ROOT
        / "imports"
        / "source-archives"
        / "AI-VoiceOver-1.4.1-plus-VanillaData-0.1-WoW-3.3.5a-Load-Outdated.zip",
    )
    args = parser.parse_args()
    package = build(args.output, args.ffmpeg, args.ffprobe, args.legacy_pack.resolve())
    print(
        json.dumps(
            {
                "package": str(package),
                "bytes": package.stat().st_size,
                "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
