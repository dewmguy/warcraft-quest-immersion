import subprocess
import sys

import pytest

from tts_cli import utils
from tts_cli.cli import main


def test_help_works_without_an_env_file(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "tts_cli", "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "generate-lookups" in result.stdout
    assert "corpus" in result.stdout


def test_doctor_succeeds_without_optional_services(capsys):
    assert main(["doctor"]) == 0
    assert "Local tooling is ready" in capsys.readouterr().out


def test_unknown_locale_is_a_value_error():
    with pytest.raises(ValueError, match="Unsupported locale code"):
        utils.language_code_to_language_number("xxXX")


def test_corpus_validate_command_is_read_only(corpus_bundle_path, tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("WQI_DATA_DIR", str(data_dir))

    assert main(["corpus", "validate", str(corpus_bundle_path)]) == 0

    output = capsys.readouterr().out
    assert '"valid": true' in output
    assert '"active_bindings"' in output
