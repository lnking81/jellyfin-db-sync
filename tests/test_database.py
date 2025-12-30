"""Tests for database operations."""

import tempfile
from pathlib import Path

import pytest

from jellyfin_db_sync.database import Database
from jellyfin_db_sync.models import PendingEventStatus, SyncEventType


@pytest.fixture
async def db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()
    db_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_database_connection(db: Database):
    """Test database connects and creates tables."""
    assert db._db is not None


@pytest.mark.asyncio
async def test_user_mapping_upsert(db: Database):
    """Test upserting user mappings."""
    mapping = await db.upsert_user_mapping(
        username="testuser",
        server_name="test-server",
        jellyfin_user_id="user-123",
    )

    assert mapping.username == "testuser"
    assert mapping.server_name == "test-server"
    assert mapping.jellyfin_user_id == "user-123"


@pytest.mark.asyncio
async def test_user_mapping_get(db: Database):
    """Test getting user mappings."""
    await db.upsert_user_mapping("user1", "server1", "id1")
    await db.upsert_user_mapping("user1", "server2", "id2")

    mappings = await db.get_user_mappings_by_username("user1")
    assert len(mappings) == 2


@pytest.mark.asyncio
async def test_pending_event_lifecycle(db: Database):
    """Test creating, getting, and completing pending events."""
    # Create event
    event_id = await db.add_pending_event(
        event_type=SyncEventType.WATCHED,
        source_server="wan",
        target_server="lan",
        username="testuser",
        user_id="user-123",
        item_id="item-123",
        item_name="Test Movie",
        item_path="/movies/test.mkv",
        event_data={"is_played": True},
    )

    assert event_id > 0

    # Get pending events
    events = await db.get_pending_events(limit=10)
    assert len(events) == 1
    assert events[0].id == event_id
    assert events[0].event_type == SyncEventType.WATCHED
    assert events[0].status == PendingEventStatus.PENDING

    # Mark as processing
    await db.mark_event_processing(event_id)
    events = await db.get_pending_events(limit=10)
    assert len(events) == 0  # Should not return processing events

    # Get processing count
    processing_count = await db.get_processing_count()
    assert processing_count == 1

    # Mark as completed
    await db.mark_event_completed(event_id)
    processing_count = await db.get_processing_count()
    assert processing_count == 0


@pytest.mark.asyncio
async def test_sync_log(db: Database):
    """Test sync logging."""
    await db.log_sync(
        event_type=SyncEventType.WATCHED.value,
        source_server="wan",
        target_server="lan",
        username="testuser",
        item_id="item-123",
        success=True,
        message="Synced successfully",
    )

    log_count = await db.get_sync_log_count()
    assert log_count == 1

    entries, total = await db.get_recent_sync_log(limit=10)
    assert len(entries) == 1
    assert total == 1
    assert entries[0]["success"] is True


@pytest.mark.asyncio
async def test_sync_stats(db: Database):
    """Test sync statistics."""
    # Log some events
    await db.log_sync(SyncEventType.WATCHED.value, "wan", "lan", "user", "item1", True, "ok")
    await db.log_sync(SyncEventType.FAVORITE.value, "wan", "lan", "user", "item2", True, "ok")
    await db.log_sync(SyncEventType.PROGRESS.value, "wan", "lan", "user", "item3", False, "error")

    stats = await db.get_sync_stats()
    assert stats["total"] == 3
    assert stats["successful"] == 2
    assert stats["failed"] == 1
