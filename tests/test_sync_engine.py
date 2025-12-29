"""Tests for SyncEngine."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jellyfin_db_sync.config import Config, DatabaseConfig, PathSyncPolicy, ServerConfig, SyncConfig
from jellyfin_db_sync.database import Database
from jellyfin_db_sync.models import PendingEvent, PendingEventStatus, SyncEventType, WebhookPayload
from jellyfin_db_sync.sync.engine import SyncEngine


@pytest.fixture
async def db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    database = Database(str(db_path))
    await database.connect()
    yield database
    await database.close()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def test_config(db):
    """Create test configuration."""
    return Config(
        servers=[
            ServerConfig(name="wan", url="http://wan:8096", api_key="key1"),
            ServerConfig(name="lan", url="http://lan:8096", api_key="key2"),
            ServerConfig(name="backup", url="http://backup:8096", api_key="key3"),
        ],
        sync=SyncConfig(
            playback_progress=True,
            watched_status=True,
            favorites=True,
            progress_debounce_seconds=30,
        ),
        database=DatabaseConfig(path=db.db_path),
        path_sync_policy=[
            PathSyncPolicy(prefix="/movies", absent_retry_count=5, retry_delay_seconds=60),
            PathSyncPolicy(prefix="/movies/new", absent_retry_count=-1, retry_delay_seconds=300),
        ],
    )


class TestWebhookParsing:
    """Test webhook payload parsing."""

    def test_parse_playback_stop_completed(self, test_config):
        """Test parsing PlaybackStop event with completion."""
        engine = SyncEngine(test_config)

        payload = WebhookPayload(
            event="PlaybackStop",
            user_id="user-123",
            username="testuser",
            item_id="item-456",
            item_name="Test Movie",
            played_to_completion=True,
        )

        events = engine._parse_webhook_to_event_data(payload, "wan")

        assert len(events) == 1
        assert events[0]["event_type"] == SyncEventType.WATCHED
        assert events[0]["data"]["is_played"] is True

    def test_parse_playback_stop_not_completed(self, test_config):
        """Test parsing PlaybackStop event without completion."""
        engine = SyncEngine(test_config)

        payload = WebhookPayload(
            event="PlaybackStop",
            user_id="user-123",
            username="testuser",
            item_id="item-456",
            item_name="Test Movie",
            played_to_completion=False,
        )

        events = engine._parse_webhook_to_event_data(payload, "wan")

        assert len(events) == 0

    def test_parse_playback_progress(self, test_config):
        """Test parsing PlaybackProgress event."""
        engine = SyncEngine(test_config)

        payload = WebhookPayload(
            event="PlaybackProgress",
            user_id="user-123",
            username="testuser",
            item_id="item-456",
            item_name="Test Movie",
            playback_position_ticks=36000000000,
        )

        events = engine._parse_webhook_to_event_data(payload, "wan")

        assert len(events) == 1
        assert events[0]["event_type"] == SyncEventType.PROGRESS
        assert events[0]["data"]["position_ticks"] == 36000000000

    def test_parse_playback_progress_debounce(self, test_config):
        """Test that PlaybackProgress events are debounced."""
        engine = SyncEngine(test_config)

        payload = WebhookPayload(
            event="PlaybackProgress",
            user_id="user-123",
            username="testuser",
            item_id="item-456",
            item_name="Test Movie",
            playback_position_ticks=36000000000,
        )

        # First call should produce event
        events1 = engine._parse_webhook_to_event_data(payload, "wan")
        assert len(events1) == 1

        # Second call immediately should be debounced
        events2 = engine._parse_webhook_to_event_data(payload, "wan")
        assert len(events2) == 0

    def test_parse_user_data_saved(self, test_config):
        """Test parsing UserDataSaved event."""
        engine = SyncEngine(test_config)

        payload = WebhookPayload(
            event="UserDataSaved",
            user_id="user-123",
            username="testuser",
            item_id="item-456",
            item_name="Test Movie",
            is_played=True,
            is_favorite=True,
        )

        events = engine._parse_webhook_to_event_data(payload, "wan")

        # Should produce watched, favorite, and play_count events
        assert len(events) == 3
        event_types = [e["event_type"] for e in events]
        assert SyncEventType.WATCHED in event_types
        assert SyncEventType.FAVORITE in event_types
        assert SyncEventType.PLAY_COUNT in event_types

    def test_parse_unknown_event(self, test_config):
        """Test parsing unknown event type."""
        engine = SyncEngine(test_config)

        payload = WebhookPayload(
            event="UnknownEvent",
            user_id="user-123",
            username="testuser",
            item_id="item-456",
            item_name="Test Movie",
        )

        events = engine._parse_webhook_to_event_data(payload, "wan")

        assert len(events) == 0


class TestEnqueueEvents:
    """Test event enqueueing."""

    @pytest.mark.asyncio
    async def test_enqueue_events_to_all_targets(self, test_config, db):
        """Test that events are enqueued for all target servers."""
        engine = SyncEngine(test_config)

        payload = WebhookPayload(
            event="PlaybackStop",
            user_id="user-123",
            username="testuser",
            item_id="item-456",
            item_name="Test Movie",
            item_path="/movies/test.mkv",
            played_to_completion=True,
            provider_imdb="tt1234567",
        )

        # Patch get_db to return our test db
        with patch("jellyfin_db_sync.sync.engine.get_db", return_value=db):
            enqueued = await engine.enqueue_events(payload, "wan")

        # Should create events for lan and backup (not wan which is source)
        assert enqueued == 2

        # Verify events in database
        events = await db.get_pending_events(limit=10)
        assert len(events) == 2

        target_servers = {e.target_server for e in events}
        assert target_servers == {"lan", "backup"}

        # Verify event data
        for event in events:
            assert event.source_server == "wan"
            assert event.username == "testuser"
            assert event.item_path == "/movies/test.mkv"
            assert event.provider_imdb == "tt1234567"

    @pytest.mark.asyncio
    async def test_enqueue_creates_user_mapping(self, test_config, db):
        """Test that user mapping is created when enqueueing."""
        engine = SyncEngine(test_config)

        payload = WebhookPayload(
            event="PlaybackStop",
            user_id="user-123",
            username="newuser",
            item_id="item-456",
            item_name="Test Movie",
            played_to_completion=True,
        )

        with patch("jellyfin_db_sync.sync.engine.get_db", return_value=db):
            await engine.enqueue_events(payload, "wan")

        # Check user mapping was created
        mapping = await db.get_user_mapping("newuser", "wan")
        assert mapping is not None
        assert mapping.jellyfin_user_id == "user-123"

    @pytest.mark.asyncio
    async def test_enqueue_no_events_generated(self, test_config, db):
        """Test enqueueing when no events are generated."""
        engine = SyncEngine(test_config)

        payload = WebhookPayload(
            event="UnknownEvent",
            user_id="user-123",
            username="testuser",
            item_id="item-456",
            item_name="Test Movie",
        )

        with patch("jellyfin_db_sync.sync.engine.get_db", return_value=db):
            enqueued = await engine.enqueue_events(payload, "wan")

        assert enqueued == 0


class TestSyncExecution:
    """Test sync execution logic."""

    @pytest.mark.asyncio
    async def test_execute_sync_watched(self, test_config, db):
        """Test executing watched status sync."""
        engine = SyncEngine(test_config)

        # Mock the Jellyfin client
        mock_client = MagicMock()
        mock_client.mark_played = AsyncMock(return_value=True)
        mock_client.mark_unplayed = AsyncMock(return_value=True)
        # Return different value so sync happens (Played=False, we want True)
        mock_client.get_user_data = AsyncMock(return_value={"Played": False, "IsFavorite": False})

        # Test mark as played
        result = await engine._execute_sync(
            client=mock_client,
            user_id="target-user-id",
            item_id="target-item-id",
            event_type=SyncEventType.WATCHED,
            event_data={"is_played": True},
        )

        assert result is True
        mock_client.mark_played.assert_called_once_with("target-user-id", "target-item-id")

        # Test mark as unplayed (target has Played=True)
        mock_client.mark_played.reset_mock()
        mock_client.get_user_data = AsyncMock(return_value={"Played": True, "IsFavorite": False})
        result = await engine._execute_sync(
            client=mock_client,
            user_id="target-user-id",
            item_id="target-item-id",
            event_type=SyncEventType.WATCHED,
            event_data={"is_played": False},
        )

        assert result is True
        mock_client.mark_unplayed.assert_called_once_with("target-user-id", "target-item-id")

    @pytest.mark.asyncio
    async def test_execute_sync_favorite(self, test_config, db):
        """Test executing favorite sync."""
        engine = SyncEngine(test_config)

        mock_client = MagicMock()
        mock_client.add_favorite = AsyncMock(return_value=True)
        mock_client.remove_favorite = AsyncMock(return_value=True)
        # Return different value so sync happens (IsFavorite=False, we want True)
        mock_client.get_user_data = AsyncMock(return_value={"Played": False, "IsFavorite": False})

        # Test add favorite
        result = await engine._execute_sync(
            client=mock_client,
            user_id="target-user-id",
            item_id="target-item-id",
            event_type=SyncEventType.FAVORITE,
            event_data={"is_favorite": True},
        )

        assert result is True
        mock_client.add_favorite.assert_called_once()

        # Test remove favorite (target has IsFavorite=True)
        mock_client.add_favorite.reset_mock()
        mock_client.get_user_data = AsyncMock(return_value={"Played": False, "IsFavorite": True})
        result = await engine._execute_sync(
            client=mock_client,
            user_id="target-user-id",
            item_id="target-item-id",
            event_type=SyncEventType.FAVORITE,
            event_data={"is_favorite": False},
        )

        assert result is True
        mock_client.remove_favorite.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_sync_progress(self, test_config, db):
        """Test executing playback progress sync."""
        engine = SyncEngine(test_config)

        mock_client = MagicMock()
        mock_client.update_playback_progress = AsyncMock(return_value=True)
        mock_client.get_user_data = AsyncMock(return_value={"PlaybackPositionTicks": 0})

        result = await engine._execute_sync(
            client=mock_client,
            user_id="target-user-id",
            item_id="target-item-id",
            event_type=SyncEventType.PROGRESS,
            event_data={"position_ticks": 36000000000},
        )

        assert result is True
        mock_client.update_playback_progress.assert_called_once_with("target-user-id", "target-item-id", 36000000000)

    @pytest.mark.asyncio
    async def test_smart_sync_skips_when_watched_already_matches(self, test_config, db):
        """Test that smart sync skips when target already has same watched status."""
        engine = SyncEngine(test_config)

        mock_client = MagicMock()
        mock_client.mark_played = AsyncMock(return_value=True)
        mock_client.mark_unplayed = AsyncMock(return_value=True)
        # Target already has Played=True, and we want to set is_played=True
        mock_client.get_user_data = AsyncMock(return_value={"Played": True, "IsFavorite": False})

        result = await engine._execute_sync(
            client=mock_client,
            user_id="target-user-id",
            item_id="target-item-id",
            event_type=SyncEventType.WATCHED,
            event_data={"is_played": True},
        )

        # Should succeed but NOT call mark_played (already in sync)
        assert result is True
        mock_client.mark_played.assert_not_called()
        mock_client.mark_unplayed.assert_not_called()

    @pytest.mark.asyncio
    async def test_smart_sync_skips_when_favorite_already_matches(self, test_config, db):
        """Test that smart sync skips when target already has same favorite status."""
        engine = SyncEngine(test_config)

        mock_client = MagicMock()
        mock_client.add_favorite = AsyncMock(return_value=True)
        mock_client.remove_favorite = AsyncMock(return_value=True)
        # Target already has IsFavorite=False, and we want to set is_favorite=False
        mock_client.get_user_data = AsyncMock(return_value={"Played": False, "IsFavorite": False})

        result = await engine._execute_sync(
            client=mock_client,
            user_id="target-user-id",
            item_id="target-item-id",
            event_type=SyncEventType.FAVORITE,
            event_data={"is_favorite": False},
        )

        # Should succeed but NOT call remove_favorite (already in sync)
        assert result is True
        mock_client.add_favorite.assert_not_called()
        mock_client.remove_favorite.assert_not_called()

    @pytest.mark.asyncio
    async def test_smart_sync_handles_get_user_data_failure(self, test_config, db):
        """Test that sync proceeds when get_user_data returns None."""
        engine = SyncEngine(test_config)

        mock_client = MagicMock()
        mock_client.mark_played = AsyncMock(return_value=True)
        # get_user_data fails/returns None
        mock_client.get_user_data = AsyncMock(return_value=None)

        result = await engine._execute_sync(
            client=mock_client,
            user_id="target-user-id",
            item_id="target-item-id",
            event_type=SyncEventType.WATCHED,
            event_data={"is_played": True},
        )

        # Should still proceed with sync
        assert result is True
        mock_client.mark_played.assert_called_once()


class TestPathSyncPolicy:
    """Test path sync policy for handling missing items."""

    def test_get_path_policy_exact_match(self, test_config):
        """Test path policy with exact prefix match."""
        policy = test_config.get_path_policy("/movies/test.mkv")
        assert policy is not None
        assert policy.prefix == "/movies"
        assert policy.absent_retry_count == 5

    def test_get_path_policy_longest_match(self, test_config):
        """Test path policy uses longest prefix match."""
        policy = test_config.get_path_policy("/movies/new/latest.mkv")
        assert policy is not None
        assert policy.prefix == "/movies/new"
        assert policy.absent_retry_count == -1  # Infinite retries

    def test_get_path_policy_no_match(self, test_config):
        """Test path policy when no prefix matches."""
        policy = test_config.get_path_policy("/photos/vacation.jpg")
        assert policy is None

    def test_get_path_policy_none_path(self, test_config):
        """Test path policy with None path."""
        policy = test_config.get_path_policy(None)
        assert policy is None

    @pytest.mark.asyncio
    async def test_handle_item_not_found_no_policy(self, test_config, db):
        """Test handling missing item with no policy."""
        from datetime import UTC, datetime

        engine = SyncEngine(test_config)

        # First add the event to the database
        event_id = await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="Test Photo",
            item_path="/photos/test.jpg",  # No policy for /photos
            event_data={"is_played": True},
        )

        event = PendingEvent(
            id=event_id,
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="Test Photo",
            item_path="/photos/test.jpg",
            status=PendingEventStatus.PENDING,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        with patch("jellyfin_db_sync.sync.engine.get_db", return_value=db):
            result = await engine._handle_item_not_found(event, "lan")

        assert result.success is False
        assert "not found" in result.message

    @pytest.mark.asyncio
    async def test_handle_item_not_found_with_policy(self, test_config, db):
        """Test handling missing item with retry policy."""
        from datetime import UTC, datetime

        engine = SyncEngine(test_config)

        # First add the event to the database
        event_id = await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="Test Movie",
            item_path="/movies/test.mkv",
            event_data={"is_played": True},
        )

        event = PendingEvent(
            id=event_id,
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="Test Movie",
            item_path="/movies/test.mkv",  # Policy exists for /movies
            status=PendingEventStatus.PENDING,
            item_not_found_count=0,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        with patch("jellyfin_db_sync.sync.engine.get_db", return_value=db):
            result = await engine._handle_item_not_found(event, "lan")

        # Should be marked as waiting, not failed
        assert result.success is True  # Not a failure, just waiting
        assert "Waiting" in result.message


class TestQueueStatus:
    """Test queue status reporting."""

    @pytest.mark.asyncio
    async def test_get_queue_status(self, test_config, db):
        """Test getting queue status."""
        engine = SyncEngine(test_config)

        with patch("jellyfin_db_sync.sync.engine.get_db", return_value=db):
            status = await engine.get_queue_status()

        assert "pending_events" in status
        assert "worker_running" in status
        assert status["pending_events"] == 0
        assert status["worker_running"] is False


class TestWorkerLifecycle:
    """Test background worker lifecycle."""

    @pytest.mark.asyncio
    async def test_start_stop_worker(self, test_config, db):
        """Test starting and stopping the worker."""
        engine = SyncEngine(test_config)

        assert engine._running is False
        assert engine._worker_task is None

        with patch("jellyfin_db_sync.sync.engine.get_db", return_value=db):
            await engine.start_worker(interval_seconds=1.0)

        assert engine._running is True
        assert engine._worker_task is not None

        await engine.stop_worker()

        assert engine._running is False
        assert engine._worker_task is None

    @pytest.mark.asyncio
    async def test_double_start_worker(self, test_config, db):
        """Test that starting worker twice is safe."""
        engine = SyncEngine(test_config)

        with patch("jellyfin_db_sync.sync.engine.get_db", return_value=db):
            await engine.start_worker(interval_seconds=1.0)
            task1 = engine._worker_task

            # Second start should be no-op
            await engine.start_worker(interval_seconds=1.0)
            task2 = engine._worker_task

        assert task1 is task2

        await engine.stop_worker()

        await engine.stop_worker()

        await engine.stop_worker()


class TestSyncLoopPrevention:
    """Test sync loop prevention using cooldown mechanism."""

    def test_get_item_identity_key_with_path(self, test_config):
        """Test identity key generation with item path."""
        engine = SyncEngine(test_config)

        key = engine._get_item_identity_key("/movies/test.mkv")
        assert key == "path:/movies/test.mkv"

    def test_get_item_identity_key_with_imdb(self, test_config):
        """Test identity key generation with IMDB ID."""
        engine = SyncEngine(test_config)

        key = engine._get_item_identity_key(None, provider_imdb="tt1234567")
        assert key == "imdb:tt1234567"

    def test_get_item_identity_key_with_tmdb(self, test_config):
        """Test identity key generation with TMDB ID."""
        engine = SyncEngine(test_config)

        key = engine._get_item_identity_key(None, provider_tmdb="12345")
        assert key == "tmdb:12345"

    def test_get_item_identity_key_with_tvdb(self, test_config):
        """Test identity key generation with TVDB ID."""
        engine = SyncEngine(test_config)

        key = engine._get_item_identity_key(None, provider_tvdb="67890")
        assert key == "tvdb:67890"

    def test_get_item_identity_key_path_takes_priority(self, test_config):
        """Test that path takes priority over provider IDs."""
        engine = SyncEngine(test_config)

        key = engine._get_item_identity_key(
            "/movies/test.mkv",
            provider_imdb="tt1234567",
            provider_tmdb="12345",
        )
        assert key == "path:/movies/test.mkv"

    def test_get_item_identity_key_no_identifiers(self, test_config):
        """Test identity key generation with no identifiers."""
        engine = SyncEngine(test_config)

        key = engine._get_item_identity_key(None)
        assert key == ""

    def test_cooldown_set_and_check_with_path(self, test_config):
        """Test cooldown is set and checked correctly using item path."""
        engine = SyncEngine(test_config)

        # Initially not in cooldown
        assert not engine._is_in_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

        # Set cooldown
        engine._set_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

        # Now should be in cooldown
        assert engine._is_in_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

    def test_cooldown_different_event_types_independent(self, test_config):
        """Test that cooldowns for different event types are independent."""
        engine = SyncEngine(test_config)

        # Set cooldown for WATCHED
        engine._set_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

        # WATCHED should be in cooldown
        assert engine._is_in_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

        # FAVORITE should NOT be in cooldown
        assert not engine._is_in_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.FAVORITE,
        )

    def test_cooldown_different_servers_independent(self, test_config):
        """Test that cooldowns for different servers are independent."""
        engine = SyncEngine(test_config)

        # Set cooldown for wan
        engine._set_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

        # wan should be in cooldown
        assert engine._is_in_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

        # lan should NOT be in cooldown
        assert not engine._is_in_cooldown(
            server="lan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

    def test_cooldown_with_provider_ids(self, test_config):
        """Test cooldown works with provider IDs instead of path."""
        engine = SyncEngine(test_config)

        # Set cooldown using IMDB ID
        engine._set_cooldown(
            server="wan",
            username="testuser",
            item_path=None,
            event_type=SyncEventType.WATCHED,
            provider_imdb="tt1234567",
        )

        # Should be in cooldown with same IMDB ID
        assert engine._is_in_cooldown(
            server="wan",
            username="testuser",
            item_path=None,
            event_type=SyncEventType.WATCHED,
            provider_imdb="tt1234567",
        )

        # Should NOT be in cooldown with different IMDB ID
        assert not engine._is_in_cooldown(
            server="wan",
            username="testuser",
            item_path=None,
            event_type=SyncEventType.WATCHED,
            provider_imdb="tt9999999",
        )

    def test_cooldown_without_identifiers_returns_false(self, test_config):
        """Test that cooldown check returns False when no identifiers."""
        engine = SyncEngine(test_config)

        # Without identifiers, should not be in cooldown (allows sync)
        assert not engine._is_in_cooldown(
            server="wan",
            username="testuser",
            item_path=None,
            event_type=SyncEventType.WATCHED,
        )

    @pytest.mark.asyncio
    async def test_enqueue_filters_cooldown_events(self, test_config, db):
        """Test that enqueue_events filters out events in cooldown."""
        engine = SyncEngine(test_config)

        # Set cooldown for wan server for WATCHED event (simulating we just synced TO wan)
        engine._set_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

        # Now a webhook comes FROM wan for the same item with only is_played
        # (so only WATCHED event is generated, not FAVORITE)
        payload = WebhookPayload(
            event="PlaybackStop",  # This generates only WATCHED event when played_to_completion
            user_id="user-123",
            username="testuser",
            item_id="item-456",
            item_name="Test Movie",
            item_path="/movies/test.mkv",
            played_to_completion=True,
        )

        with patch("jellyfin_db_sync.sync.engine.get_db", return_value=db):
            enqueued = await engine.enqueue_events(payload, "wan")

        # Should NOT enqueue anything since the WATCHED event from wan is in cooldown
        assert enqueued == 0

    @pytest.mark.asyncio
    async def test_enqueue_allows_events_from_different_server(self, test_config, db):
        """Test that events from different servers are not filtered."""
        engine = SyncEngine(test_config)

        # Set cooldown for wan server for WATCHED event
        engine._set_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

        # Webhook comes FROM lan (different server) for same item
        # Uses PlaybackStop to generate only WATCHED event
        payload = WebhookPayload(
            event="PlaybackStop",
            user_id="user-123",
            username="testuser",
            item_id="item-456",
            item_name="Test Movie",
            item_path="/movies/test.mkv",
            played_to_completion=True,
        )

        with patch("jellyfin_db_sync.sync.engine.get_db", return_value=db):
            enqueued = await engine.enqueue_events(payload, "lan")

        # Should enqueue events for wan and backup (not lan which is source)
        # Event from lan is NOT in cooldown (cooldown is for wan)
        assert enqueued == 2

    def test_cleanup_expired_cooldowns(self, test_config):
        """Test that expired cooldowns are cleaned up."""
        from datetime import UTC, datetime, timedelta

        engine = SyncEngine(test_config)

        # Manually set an expired cooldown
        key = "wan:testuser:path:/movies/test.mkv:watched"
        engine._sync_cooldowns[key] = datetime.now(UTC) - timedelta(seconds=1)

        # Check should return False (not in cooldown) and clean up
        assert not engine._is_in_cooldown(
            server="wan",
            username="testuser",
            item_path="/movies/test.mkv",
            event_type=SyncEventType.WATCHED,
        )

        # Key should be removed
        assert key not in engine._sync_cooldowns
        # Key should be removed
        assert key not in engine._sync_cooldowns
        # Key should be removed
        assert key not in engine._sync_cooldowns
        # Key should be removed
        assert key not in engine._sync_cooldowns
        # Key should be removed
        assert key not in engine._sync_cooldowns
        # Key should be removed
        assert key not in engine._sync_cooldowns
        # Key should be removed
        assert key not in engine._sync_cooldowns
        # Key should be removed
        assert key not in engine._sync_cooldowns
        # Key should be removed
        assert key not in engine._sync_cooldowns
