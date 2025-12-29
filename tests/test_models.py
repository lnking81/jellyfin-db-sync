"""Tests for data models."""

from datetime import UTC, datetime

from jellyfin_db_sync.models import PendingEvent, PendingEventStatus, SyncEventType, UserMapping


def test_sync_event_type_values():
    """Test SyncEventType enum values."""
    assert SyncEventType.PROGRESS.value == "progress"
    assert SyncEventType.WATCHED.value == "watched"
    assert SyncEventType.FAVORITE.value == "favorite"
    assert SyncEventType.RATING.value == "rating"


def test_pending_event_status_values():
    """Test PendingEventStatus enum values."""
    assert PendingEventStatus.PENDING.value == "pending"
    assert PendingEventStatus.PROCESSING.value == "processing"
    assert PendingEventStatus.FAILED.value == "failed"
    assert PendingEventStatus.WAITING_FOR_ITEM.value == "waiting_for_item"


def test_user_mapping_creation():
    """Test UserMapping dataclass creation."""
    now = datetime.now(UTC)
    mapping = UserMapping(
        username="testuser",
        server_name="test-server",
        jellyfin_user_id="abc-123",
        created_at=now,
        updated_at=now,
    )

    assert mapping.username == "testuser"
    assert mapping.server_name == "test-server"
    assert mapping.jellyfin_user_id == "abc-123"


def test_pending_event_creation():
    """Test PendingEvent dataclass creation."""
    now = datetime.now(UTC)
    event = PendingEvent(
        id=1,
        event_type=SyncEventType.WATCHED,
        source_server="wan",
        target_server="lan",
        username="testuser",
        user_id="user-abc-123",
        item_id="item-123",
        item_name="Test Movie",
        item_path="/movies/test.mkv",
        provider_imdb="tt1234567",
        provider_tmdb=None,
        provider_tvdb=None,
        event_data='{"is_played": true}',
        status=PendingEventStatus.PENDING,
        retry_count=0,
        max_retries=5,
        last_error=None,
        item_not_found_count=0,
        item_not_found_max=0,
        created_at=now,
        updated_at=now,
    )

    assert event.id == 1
    assert event.event_type == SyncEventType.WATCHED
    assert event.status == PendingEventStatus.PENDING
    assert event.event_data == '{"is_played": true}'
    assert event.event_data == '{"is_played": true}'
