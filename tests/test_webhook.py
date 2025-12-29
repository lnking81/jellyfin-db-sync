"""Tests for webhook endpoint."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jellyfin_db_sync.api.webhook import router
from jellyfin_db_sync.config import Config, DatabaseConfig, ServerConfig, SyncConfig
from jellyfin_db_sync.database import Database
from jellyfin_db_sync.sync import SyncEngine


@pytest.fixture
def test_config():
    """Create test configuration."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    return (
        Config(
            servers=[
                ServerConfig(name="wan", url="http://wan:8096", api_key="key1"),
                ServerConfig(name="lan", url="http://lan:8096", api_key="key2"),
            ],
            sync=SyncConfig(
                playback_progress=True,
                watched_status=True,
                favorites=True,
            ),
            database=DatabaseConfig(path=str(db_path)),
        ),
        db_path,
    )


@pytest.fixture
async def db(test_config):
    """Create test database."""
    config, db_path = test_config
    database = Database(str(db_path))
    await database.connect()
    yield database
    await database.close()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def app_with_engine(test_config, db):
    """Create FastAPI app with mocked engine."""
    config, _ = test_config

    app = FastAPI()
    app.include_router(router)

    # Create engine with mocked enqueue_events
    engine = MagicMock(spec=SyncEngine)
    engine.enqueue_events = AsyncMock(return_value=2)
    engine.get_queue_status = AsyncMock(return_value={"pending_events": 5, "worker_running": True})

    app.state.engine = engine

    # Patch get_config to return test config
    import jellyfin_db_sync.api.webhook as webhook_module

    original_get_config = webhook_module.get_config
    webhook_module.get_config = lambda: config

    yield app, engine

    # Restore original
    webhook_module.get_config = original_get_config


def test_webhook_test_endpoint(app_with_engine):
    """Test the /webhook/test endpoint."""
    app, _ = app_with_engine
    client = TestClient(app)

    response = client.get("/webhook/test")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert "message" in data


def test_webhook_unknown_server(app_with_engine):
    """Test webhook with unknown server name."""
    app, _ = app_with_engine
    client = TestClient(app)

    payload = {
        "NotificationType": "PlaybackStop",
        "UserId": "user-123",
        "NotificationUsername": "testuser",
        "ItemId": "item-456",
        "Name": "Test Movie",
        "PlayedToCompletion": True,
    }

    response = client.post("/webhook/unknown-server", json=payload)
    assert response.status_code == 404
    assert "Unknown server" in response.json()["detail"]


def test_webhook_valid_playback_stop(app_with_engine):
    """Test valid webhook for playback stop event."""
    app, engine = app_with_engine
    client = TestClient(app)

    payload = {
        "NotificationType": "PlaybackStop",
        "ServerId": "server-id-1",
        "ServerName": "wan",
        "UserId": "user-123",
        "NotificationUsername": "testuser",
        "ItemId": "item-456",
        "Name": "Test Movie",
        "ItemType": "Movie",
        "Path": "/movies/test.mkv",
        "PlayedToCompletion": True,
        "Provider_imdb": "tt1234567",
    }

    response = client.post("/webhook/wan", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "enqueued"
    assert data["events_enqueued"] == 2

    # Verify engine.enqueue_events was called
    engine.enqueue_events.assert_called_once()
    call_args = engine.enqueue_events.call_args
    # Check positional args: (payload, source_server_name)
    assert call_args[0][1] == "wan"  # Second positional arg is source_server_name


def test_webhook_skip_without_username(app_with_engine):
    """Test webhook is skipped when no username provided."""
    app, engine = app_with_engine
    client = TestClient(app)

    payload = {
        "NotificationType": "PlaybackStop",
        "UserId": "user-123",
        "NotificationUsername": "",  # Empty username
        "ItemId": "item-456",
        "Name": "Test Movie",
    }

    response = client.post("/webhook/wan", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "skipped"
    assert data["reason"] == "no username"

    # Engine should not be called
    engine.enqueue_events.assert_not_called()


def test_webhook_invalid_payload(app_with_engine):
    """Test webhook with invalid JSON payload."""
    app, _ = app_with_engine
    client = TestClient(app)

    # Send invalid JSON
    response = client.post(
        "/webhook/wan",
        content="not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "Invalid" in response.json()["detail"] or response.status_code == 422


def test_webhook_queue_status(app_with_engine):
    """Test /webhook/queue endpoint."""
    app, engine = app_with_engine
    client = TestClient(app)

    response = client.get("/webhook/queue")
    assert response.status_code == 200

    data = response.json()
    assert "pending_events" in data
    assert data["pending_events"] == 5
    assert data["worker_running"] is True


def test_webhook_user_data_saved(app_with_engine):
    """Test webhook for UserDataSaved event."""
    app, engine = app_with_engine
    client = TestClient(app)

    payload = {
        "NotificationType": "UserDataSaved",
        "ServerId": "server-id-1",
        "ServerName": "wan",
        "UserId": "user-123",
        "NotificationUsername": "testuser",
        "ItemId": "item-456",
        "Name": "Test Movie",
        "ItemType": "Movie",
        "Path": "/movies/test.mkv",  # Include Path to skip API call
        "Favorite": True,  # Jellyfin webhook sends 'Favorite', not 'IsFavorite'
        "Played": True,
    }

    response = client.post("/webhook/wan", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "enqueued"


def test_webhook_playback_progress(app_with_engine):
    """Test webhook for PlaybackProgress event."""
    app, engine = app_with_engine
    client = TestClient(app)

    payload = {
        "NotificationType": "PlaybackProgress",
        "ServerId": "server-id-1",
        "ServerName": "wan",
        "UserId": "user-123",
        "NotificationUsername": "testuser",
        "ItemId": "item-456",
        "Name": "Test Movie",
        "Path": "/movies/test.mkv",  # Include Path to skip API call
        "PlaybackPositionTicks": 36000000000,  # 1 hour
        "PlaybackPosition": "01:00:00",
    }

    response = client.post("/webhook/wan", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "enqueued"
    assert response.json()["status"] == "enqueued"
    assert response.json()["status"] == "enqueued"
