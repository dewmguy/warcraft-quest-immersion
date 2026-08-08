import pytest

from tts_cli.config import ConfigurationError, load_settings


def test_load_settings_has_safe_local_defaults():
    settings = load_settings(environ={})

    assert settings.mysql_host == "localhost"
    assert settings.mysql_port == 3306
    assert settings.mysql_database == "mangos"
    assert settings.elevenlabs_api_key is None


def test_invalid_mysql_port_has_a_clear_error():
    with pytest.raises(ConfigurationError, match="MYSQL_PORT must be an integer"):
        load_settings(environ={"MYSQL_PORT": "not-a-port"})


def test_elevenlabs_is_required_only_on_demand():
    settings = load_settings(environ={})

    with pytest.raises(ConfigurationError, match="ELEVENLABS_API_KEY"):
        settings.require_elevenlabs()
