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
DEFAULT_SOUND_DIRECTORY = Path(
    "S:/Personal/Audio/Sound Rips/Wow Sounds/Character/Gnome/GnomeFemaleErrorMessages"
)

TEST_SOUNDS = {
    "quests/783-accept-c823.mp3": "GnomeFemale_err_genericnotarget01.mp3",
    "quests/783-complete-c197.mp3": "GnomeFemale_err_notenoughmoney01.mp3",
    "quests/7-accept-c197.mp3": "GnomeFemale_err_inventoryfull01.mp3",
    "quests/7-progress-c197.mp3": "GnomeFemale_err_outofrange02.mp3",
    "quests/7-complete-c197.mp3": "GnomeFemale_err_abilitycooldown01.mp3",
    "quests/33-accept.mp3": "GnomeFemale_err_cantloot01.mp3",
}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


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
        f"""## Interface: 30300
## Title: VoiceOver Data - WQI Phase 2 Acceptance
## Notes: Disposable Northshire fixture for the certified 3.3.5 per-deliverer playback gate.
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
        ["A Threat Within"] = 783,
        ["Kobold Camp Cleanup"] = 7,
        ["Wolves Across the Border"] = 33
    }},
    ["progress"] = {{
        ["Kobold Camp Cleanup"] = 7
    }},
    ["complete"] = {{
        ["A Threat Within"] = 783,
        ["Kobold Camp Cleanup"] = 7
    }}
}}
""",
    )
    _write(
        generated / "questlog_npc_lookups.lua",
        f"""if not VoiceOver or not VoiceOver.DataModules then return end
{DATA_MODULE}.NPCIDLookupByQuestID = {{
    [7] = 197,
    [33] = 196
}}
{DATA_MODULE}.QuestAudioLookupByNPCID = {{
    [7] = {{
        ["accept"] = {{ [197] = "7-accept-c197" }},
        ["progress"] = {{ [197] = "7-progress-c197" }},
        ["complete"] = {{ [197] = "7-complete-c197" }}
    }},
    [783] = {{
        ["accept"] = {{ [823] = "783-accept-c823" }},
        ["complete"] = {{ [197] = "783-complete-c197" }}
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

This package contains recognizable Gnome female error-message clips as disposable
test audio, not production quest voices. Back up any existing AI_VoiceOver folder,
then copy both addon folders into the 3.3.5a client's Interface/AddOns directory.
The included data module targets client build 3.3.5a (12340) and should not require
"Load out of date AddOns."

Checks

Use a new or low-level Human character in Northshire.

1. A Threat Within (quest 783, level 1):
   - Accept from Deputy Willem (creature 823): generic-no-target Gnome clip.
     Source: GnomeFemale_err_genericnotarget01.mp3
   - Complete at Marshal McBride (creature 197): not-enough-money Gnome clip.
     Source: GnomeFemale_err_notenoughmoney01.mp3
   These different sounds prove that accept and complete resolve through their actual
   delivery NPCs rather than one quest-wide speaker.

2. Kobold Camp Cleanup (quest 7, level 2), from Marshal McBride:
   - Open the offer: inventory-full Gnome clip.
     Source: GnomeFemale_err_inventoryfull01.mp3
   - Accept it, leave it incomplete, then reopen Marshal McBride's quest pane:
     out-of-range Gnome clip.
     Source: GnomeFemale_err_outofrange02.mp3
   - Finish and reopen the quest: ability-cooldown Gnome clip.
     Source: GnomeFemale_err_abilitycooldown01.mp3
   The middle check proves the 3.3.5 QUEST_PROGRESS path.

3. Legacy filename fallback, Wolves Across the Border (quest 33, level 2):
   - Accept from Eagan Peltskinner (creature 196): cannot-loot Gnome clip.
     Source: GnomeFemale_err_cantloot01.mp3
   This fixture deliberately supplies only quests/33-accept.mp3, with no per-NPC
   audio lookup, so successful playback proves compatibility with legacy data packs.

Record the client build, addon load status, NPC/quest tested, heard result, and any
Lua error. Remove AI_VoiceOverData_WQI_Acceptance after the gate; these clips are not
production quest assets.
""",
    )


def build(output: Path, ffprobe: str, sound_directory: Path) -> Path:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    missing = [name for name in TEST_SOUNDS.values() if not (sound_directory / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing {len(missing)} source clip(s) beneath {sound_directory}: "
            + ", ".join(missing)
        )
    with tempfile.TemporaryDirectory(prefix="wqi-acceptance-") as temporary:
        root = Path(temporary)
        _copy_addon(root)
        module = root / DATA_MODULE
        lengths: dict[str, float] = {}
        for relative, source_name in TEST_SOUNDS.items():
            target = module / "generated" / "sounds" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sound_directory / source_name, target)
            lengths[relative] = _duration(ffprobe, target)
        _write_data_module(root, lengths)
        _write_readme(root)

        audio_manifest = {}
        for path in sorted((module / "generated" / "sounds").rglob("*.mp3")):
            relative = path.relative_to(module).as_posix()
            audio_manifest[relative] = {
                "bytes": path.stat().st_size,
                "duration_seconds": lengths[relative.removeprefix("generated/sounds/")],
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source_clip": TEST_SOUNDS[relative.removeprefix("generated/sounds/")],
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
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--sound-directory",
        type=Path,
        default=DEFAULT_SOUND_DIRECTORY,
    )
    args = parser.parse_args()
    package = build(args.output, args.ffprobe, args.sound_directory.resolve())
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
