import pytest


@pytest.fixture(autouse=True)
def isolated_config_home(monkeypatch, tmp_path_factory):
    config = tmp_path_factory.mktemp("config")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    return config
