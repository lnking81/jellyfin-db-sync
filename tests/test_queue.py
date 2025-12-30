"""Tests for event queue operations in database."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jellyfin_db_sync.database import Database
from jellyfin_db_sync.models import PendingEventStatus, SyncEventType


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


class TestEventQueue:
    """Test event queue (WAL) functionality."""

    @pytest.mark.asyncio
    async def test_add_pending_event(self, db: Database):
        """Test adding a pending event to the queue."""
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
            provider_imdb="tt1234567",
        )

        assert event_id > 0

        # Verify event is in queue
        events = await db.get_pending_events(limit=10)
        assert len(events) == 1
        assert events[0].event_type == SyncEventType.WATCHED
        assert events[0].username == "testuser"
        assert events[0].item_path == "/movies/test.mkv"
        assert events[0].provider_imdb == "tt1234567"

    @pytest.mark.asyncio
    async def test_get_pending_events_respects_status(self, db: Database):
        """Test that get_pending_events only returns pending events."""
        # Add multiple events
        event1_id = await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="user1",
            user_id="u1",
            item_id="item1",
            item_name="Movie 1",
            event_data={},
        )

        event2_id = await db.add_pending_event(
            event_type=SyncEventType.FAVORITE,
            source_server="wan",
            target_server="lan",
            username="user2",
            user_id="u2",
            item_id="item2",
            item_name="Movie 2",
            event_data={},
        )

        # Mark first event as processing
        await db.mark_event_processing(event1_id)

        # Only second event should be returned
        events = await db.get_pending_events(limit=10)
        assert len(events) == 1
        assert events[0].id == event2_id

    @pytest.mark.asyncio
    async def test_event_lifecycle_complete(self, db: Database):
        """Test complete event lifecycle: pending -> processing -> completed."""
        event_id = await db.add_pending_event(
            event_type=SyncEventType.PROGRESS,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="Test Episode",
            event_data={"position_ticks": 36000000000},
        )

        # Initial state: pending
        assert await db.get_pending_count() == 1
        assert await db.get_processing_count() == 0

        # Transition to processing
        await db.mark_event_processing(event_id)
        assert await db.get_pending_count() == 1  # Still counted in pending
        assert await db.get_processing_count() == 1

        # Complete the event
        await db.mark_event_completed(event_id)
        assert await db.get_pending_count() == 0
        assert await db.get_processing_count() == 0

        # Check sync log was created
        log_count = await db.get_sync_log_count()
        assert log_count == 1

    @pytest.mark.asyncio
    async def test_event_lifecycle_failed_with_retry(self, db: Database):
        """Test event failure with retry scheduling."""
        event_id = await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="Test Movie",
            event_data={"is_played": True},
        )

        await db.mark_event_processing(event_id)

        # Fail the event
        await db.mark_event_failed(event_id, "Connection timeout")

        # Event should still exist (not yet max retries)
        _events = await db.get_pending_events(limit=10)
        # Events are scheduled for retry, so they might not be immediately returned
        # because next_retry_at is in the future

        # Verify retry count increased
        all_events = await db._db.execute("SELECT * FROM pending_events WHERE id = ?", (event_id,))
        row = await all_events.fetchone()
        assert row["retry_count"] == 1
        assert row["last_error"] == "Connection timeout"
        assert row["status"] == "pending"

    @pytest.mark.asyncio
    async def test_event_max_retries_exceeded(self, db: Database):
        """Test that events are deleted after max retries."""
        event_id = await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="Test Movie",
            event_data={"is_played": True},
        )

        # Set max_retries to 2 for faster test
        await db._db.execute("UPDATE pending_events SET max_retries = 2 WHERE id = ?", (event_id,))
        await db._db.commit()

        # Fail twice
        await db.mark_event_processing(event_id)
        await db.mark_event_failed(event_id, "Error 1")

        # Update next_retry_at to now so we can process it
        await db._db.execute(
            "UPDATE pending_events SET next_retry_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), event_id)
        )
        await db._db.commit()

        events = await db.get_pending_events(limit=10)
        assert len(events) == 1

        await db.mark_event_processing(event_id)
        await db.mark_event_failed(event_id, "Error 2")

        # Event should be deleted after max retries
        events = await db.get_pending_events(limit=10)
        assert len(events) == 0

        # Sync log should have failure entry
        entries, total = await db.get_recent_sync_log(limit=10)
        assert len(entries) == 1
        assert total == 1
        assert entries[0]["success"] is False
        assert "Failed after 2 retries" in entries[0]["message"]

    @pytest.mark.asyncio
    async def test_reset_stale_processing(self, db: Database):
        """Test resetting events stuck in processing state."""
        event_id = await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="Test Movie",
            event_data={},
        )

        await db.mark_event_processing(event_id)

        # Simulate stale event (set updated_at to 10 minutes ago)
        # Use the same format as reset_stale_processing: "%Y-%m-%d %H:%M:%S"
        stale_time = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        await db._db.execute("UPDATE pending_events SET updated_at = ? WHERE id = ?", (stale_time, event_id))
        await db._db.commit()

        # Reset stale events (stale_minutes=5)
        reset_count = await db.reset_stale_processing(stale_minutes=5)
        assert reset_count == 1

        # Event should be back to pending
        events = await db.get_pending_events(limit=10)
        assert len(events) == 1
        assert events[0].status == PendingEventStatus.PENDING

    @pytest.mark.asyncio
    async def test_event_ordering_fifo(self, db: Database):
        """Test that events are processed in FIFO order."""
        # Add events with slight delays
        event1_id = await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="user1",
            user_id="u1",
            item_id="item1",
            item_name="First",
            event_data={},
        )

        event2_id = await db.add_pending_event(
            event_type=SyncEventType.FAVORITE,
            source_server="wan",
            target_server="lan",
            username="user2",
            user_id="u2",
            item_id="item2",
            item_name="Second",
            event_data={},
        )

        event3_id = await db.add_pending_event(
            event_type=SyncEventType.PROGRESS,
            source_server="wan",
            target_server="lan",
            username="user3",
            user_id="u3",
            item_id="item3",
            item_name="Third",
            event_data={},
        )

        events = await db.get_pending_events(limit=10)
        assert len(events) == 3
        assert events[0].id == event1_id
        assert events[1].id == event2_id
        assert events[2].id == event3_id


class TestWaitingForItemEvents:
    """Test events waiting for item to be imported on target server."""

    @pytest.mark.asyncio
    async def test_mark_event_waiting_for_item(self, db: Database):
        """Test marking event as waiting for item."""
        event_id = await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="New Movie",
            item_path="/movies/new.mkv",
            event_data={"is_played": True},
        )

        await db.mark_event_processing(event_id)
        await db.mark_event_waiting_for_item(
            event_id=event_id,
            max_retries=10,
            retry_delay_seconds=300,
            error_message="Item not found on lan",
        )

        # Should not appear in pending events
        pending = await db.get_pending_events(limit=10)
        assert len(pending) == 0

        # Should appear in waiting events (after retry time)
        waiting_count = await db.get_waiting_for_item_count()
        assert waiting_count == 1

    @pytest.mark.asyncio
    async def test_waiting_events_respect_retry_delay(self, db: Database):
        """Test that waiting events respect retry delay."""
        event_id = await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="New Movie",
            event_data={},
        )

        await db.mark_event_processing(event_id)
        await db.mark_event_waiting_for_item(
            event_id=event_id,
            max_retries=10,
            retry_delay_seconds=3600,  # 1 hour delay
            error_message="Item not found",
        )

        # Should not be returned yet (retry time in future)
        waiting = await db.get_waiting_for_item_events(limit=10)
        assert len(waiting) == 0

        # Set next_retry_at to past
        past_time = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        await db._db.execute("UPDATE pending_events SET next_retry_at = ? WHERE id = ?", (past_time, event_id))
        await db._db.commit()

        # Now should be returned
        waiting = await db.get_waiting_for_item_events(limit=10)
        assert len(waiting) == 1


class TestQueueStatistics:
    """Test queue statistics methods."""

    @pytest.mark.asyncio
    async def test_pending_count(self, db: Database):
        """Test pending event count."""
        assert await db.get_pending_count() == 0

        await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="user1",
            user_id="u1",
            item_id="item1",
            item_name="Movie 1",
            event_data={},
        )

        assert await db.get_pending_count() == 1

        await db.add_pending_event(
            event_type=SyncEventType.FAVORITE,
            source_server="wan",
            target_server="lan",
            username="user2",
            user_id="u2",
            item_id="item2",
            item_name="Movie 2",
            event_data={},
        )

        assert await db.get_pending_count() == 2

    @pytest.mark.asyncio
    async def test_sync_stats(self, db: Database):
        """Test sync statistics aggregation."""
        # Log some events
        await db.log_sync("watched", "wan", "lan", "user1", "item1", True, "ok")
        await db.log_sync("watched", "wan", "lan", "user1", "item2", True, "ok")
        await db.log_sync("favorite", "wan", "lan", "user1", "item3", False, "error")
        await db.log_sync("progress", "wan", "lan", "user2", "item4", True, "ok")

        stats = await db.get_sync_stats()

        assert stats["total"] == 4
        assert stats["successful"] == 3
        assert stats["failed"] == 1
        assert stats["last_sync_at"] is not None

    @pytest.mark.asyncio
    async def test_get_recent_sync_log(self, db: Database):
        """Test getting recent sync log entries."""
        await db.log_sync("watched", "wan", "lan", "user1", "item1", True, "msg1")
        await db.log_sync("favorite", "wan", "lan", "user2", "item2", False, "msg2")

        entries, total = await db.get_recent_sync_log(limit=10)

        assert len(entries) == 2
        assert total == 2
        # Should return 2 entries (order may vary if timestamps are identical)
        event_types = {e["event_type"] for e in entries}
        assert "watched" in event_types
        assert "favorite" in event_types


class TestProviderIdMatching:
    """Test events with provider IDs for matching."""

    @pytest.mark.asyncio
    async def test_event_with_provider_ids(self, db: Database):
        """Test creating event with all provider IDs."""
        _event_id = await db.add_pending_event(
            event_type=SyncEventType.WATCHED,
            source_server="wan",
            target_server="lan",
            username="testuser",
            user_id="user-123",
            item_id="item-456",
            item_name="Inception",
            item_path="/movies/Inception (2010)/Inception.mkv",
            provider_imdb="tt1375666",
            provider_tmdb="27205",
            provider_tvdb=None,
            event_data={"is_played": True},
        )

        events = await db.get_pending_events(limit=10)
        assert len(events) == 1

        event = events[0]
        assert event.item_path == "/movies/Inception (2010)/Inception.mkv"
        assert event.provider_imdb == "tt1375666"
        assert event.provider_tmdb == "27205"
        assert event.provider_tvdb is None
