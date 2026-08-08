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


def test_doctor_succeeds_without_optional_services(capsys):
    assert main(["doctor"]) == 0
    assert "Local tooling is ready" in capsys.readouterr().out


def test_unknown_locale_is_a_value_error():
    with pytest.raises(ValueError, match="Unsupported locale code"):
        utils.language_code_to_language_number("xxXX")
