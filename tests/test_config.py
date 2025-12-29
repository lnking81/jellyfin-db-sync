"""Tests for configuration loading."""

import tempfile
from pathlib import Path

import yaml

from jellyfin_db_sync.config import Config, ServerConfig, SyncConfig


def test_server_config_creation():
    """Test ServerConfig model creation."""
    server = ServerConfig(
        name="test",
        url="http://localhost:8096",
        api_key="test-key",
        passwordless=False,
    )
    assert server.name == "test"
    assert server.url == "http://localhost:8096"
    assert server.api_key == "test-key"
    assert server.passwordless is False


def test_sync_config_defaults():
    """Test SyncConfig default values."""
    sync = SyncConfig()
    assert sync.playback_progress is True
    assert sync.watched_status is True
    assert sync.favorites is True
    assert sync.ratings is True
    assert sync.playlists is True
    assert sync.progress_debounce_seconds == 30


def test_config_get_server():
    """Test Config.get_server method."""
    config = Config(
        servers=[
            ServerConfig(name="wan", url="http://wan:8096", api_key="key1"),
            ServerConfig(name="lan", url="http://lan:8096", api_key="key2"),
        ],
        sync=SyncConfig(),
    )

    assert config.get_server("wan") is not None
    assert config.get_server("wan").url == "http://wan:8096"
    assert config.get_server("lan") is not None
    assert config.get_server("unknown") is None


def test_config_from_yaml():
    """Test loading config from YAML file."""
    config_data = {
        "servers": [
            {
                "name": "test-server",
                "url": "http://test:8096",
                "api_key": "test-key",
                "passwordless": True,
            }
        ],
        "sync": {
            "playback_progress": True,
            "watched_status": True,
        },
        "database": {
            "path": "/tmp/test.db",
        },
        "server": {
            "host": "0.0.0.0",
            "port": 8080,
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config_data, f)
        config_path = Path(f.name)

    try:
        config = Config.from_yaml(config_path)
        assert len(config.servers) == 1
        assert config.servers[0].name == "test-server"
        assert config.servers[0].passwordless is True
    finally:
        config_path.unlink()
