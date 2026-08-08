import json
import zipfile
from pathlib import Path

import pytest

from scripts.import_legacy_pack import expected_artifact, extract_archive


def test_expected_artifact_selects_by_filename(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"artifacts": [{"file": "pack.zip", "sha256": "abc"}]}),
        encoding="utf-8",
    )
    artifact = expected_artifact(manifest, Path("pack.zip"))
    assert artifact["sha256"] == "abc"


def test_extract_archive_preserves_data_module_and_writes_receipt(tmp_path):
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("AI_VoiceOverData_Vanilla/generated/sounds/quests/5-accept.mp3", b"audio")
        package.writestr("AI_VoiceOver/VoiceOver.lua", b"interface")

    target = tmp_path / "imported"
    receipt = extract_archive(archive, target, "digest")
    assert receipt["mp3_files"] == 1
    assert (target / "AI_VoiceOverData_Vanilla/generated/sounds/quests/5-accept.mp3").exists()
    assert not (target / "AI_VoiceOver/VoiceOver.lua").exists()


def test_extract_archive_refuses_existing_target(tmp_path):
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("AI_VoiceOverData_Vanilla/Module.lua", b"module")
    target = tmp_path / "imported"
    target.mkdir()
    with pytest.raises(FileExistsError):
        extract_archive(archive, target, "digest")
