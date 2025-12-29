"""SQLite database operations for user mappings and event queue."""

import json
from datetime import UTC, datetime, timedelta

import aiosqlite

from .config import get_config
from .models import PendingEvent, PendingEventStatus, SyncEventType, UserMapping


class Database:
    """Async SQLite database for user mappings."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or get_config().database.path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Connect to the database and create tables."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def _create_tables(self) -> None:
        """Create database tables if they don't exist."""
        assert self._db is not None

        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                server_name TEXT NOT NULL,
                jellyfin_user_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, server_name)
            )
        """
        )

        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_mappings_username
            ON user_mappings(username)
        """
        )

        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                source_server TEXT NOT NULL,
                target_server TEXT NOT NULL,
                username TEXT NOT NULL,
                item_id TEXT,
                success BOOLEAN NOT NULL,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Pending events table (WAL for sync operations)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                source_server TEXT NOT NULL,
                target_server TEXT NOT NULL,
                username TEXT NOT NULL,
                user_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                item_path TEXT,
                provider_imdb TEXT,
                provider_tmdb TEXT,
                provider_tvdb TEXT,
                event_data TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 5,
                last_error TEXT,
                item_not_found_count INTEGER NOT NULL DEFAULT 0,
                item_not_found_max INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                next_retry_at TIMESTAMP
            )
        """
        )

        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pending_events_status
            ON pending_events(status, next_retry_at)
        """
        )

        await self._db.commit()

    async def get_user_mapping(self, username: str, server_name: str) -> UserMapping | None:
        """Get user mapping for a specific server."""
        assert self._db is not None

        async with self._db.execute(
            """
            SELECT id, username, server_name, jellyfin_user_id, created_at, updated_at
            FROM user_mappings
            WHERE username = ? AND server_name = ?
            """,
            (username, server_name),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return UserMapping(
                    id=row["id"],
                    username=row["username"],
                    server_name=row["server_name"],
                    jellyfin_user_id=row["jellyfin_user_id"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            return None

    async def get_user_mappings_by_username(self, username: str) -> list[UserMapping]:
        """Get all mappings for a username across all servers."""
        assert self._db is not None

        mappings = []
        async with self._db.execute(
            """
            SELECT id, username, server_name, jellyfin_user_id, created_at, updated_at
            FROM user_mappings
            WHERE username = ?
            """,
            (username,),
        ) as cursor:
            async for row in cursor:
                mappings.append(
                    UserMapping(
                        id=row["id"],
                        username=row["username"],
                        server_name=row["server_name"],
                        jellyfin_user_id=row["jellyfin_user_id"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
        return mappings

    async def upsert_user_mapping(self, username: str, server_name: str, jellyfin_user_id: str) -> UserMapping:
        """Insert or update a user mapping."""
        assert self._db is not None

        await self._db.execute(
            """
            INSERT INTO user_mappings (username, server_name, jellyfin_user_id, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(username, server_name)
            DO UPDATE SET jellyfin_user_id = excluded.jellyfin_user_id,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (username, server_name, jellyfin_user_id),
        )
        await self._db.commit()

        mapping = await self.get_user_mapping(username, server_name)
        assert mapping is not None
        return mapping

    async def delete_user_mapping(self, username: str, server_name: str) -> bool:
        """Delete a user mapping. Returns True if deleted."""
        assert self._db is not None

        cursor = await self._db.execute(
            "DELETE FROM user_mappings WHERE username = ? AND server_name = ?",
            (username, server_name),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def log_sync(
        self,
        event_type: str,
        source_server: str,
        target_server: str,
        username: str,
        item_id: str | None,
        success: bool,
        message: str,
    ) -> None:
        """Log a sync operation."""
        assert self._db is not None

        await self._db.execute(
            """
            INSERT INTO sync_log
            (event_type, source_server, target_server, username, item_id, success, message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, source_server, target_server, username, item_id, success, message),
        )
        await self._db.commit()

    # ========== Pending Events (WAL) ==========

    async def has_pending_event(
        self,
        event_type: SyncEventType,
        target_server: str,
        username: str,
        item_id: str,
    ) -> bool:
        """Check if a similar pending event already exists (deduplication)."""
        assert self._db is not None

        async with self._db.execute(
            """
            SELECT 1 FROM pending_events
            WHERE event_type = ?
              AND target_server = ?
              AND username = ?
              AND item_id = ?
              AND status IN ('pending', 'processing', 'waiting_for_item')
            LIMIT 1
            """,
            (event_type.value, target_server, username, item_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    async def add_pending_event(
        self,
        event_type: SyncEventType,
        source_server: str,
        target_server: str,
        username: str,
        user_id: str,
        item_id: str,
        item_name: str,
        event_data: dict[str, object],
        item_path: str | None = None,
        provider_imdb: str | None = None,
        provider_tmdb: str | None = None,
        provider_tvdb: str | None = None,
    ) -> int:
        """Add a new pending event to the queue."""
        assert self._db is not None

        cursor = await self._db.execute(
            """
            INSERT INTO pending_events
            (event_type, source_server, target_server, username, user_id,
             item_id, item_name, item_path, provider_imdb, provider_tmdb, provider_tvdb, event_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type.value,
                source_server,
                target_server,
                username,
                user_id,
                item_id,
                item_name,
                item_path,
                provider_imdb,
                provider_tmdb,
                provider_tvdb,
                json.dumps(event_data),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid or 0

    async def get_pending_events(self, limit: int = 100) -> list[PendingEvent]:
        """Get pending events ready for processing."""
        assert self._db is not None

        now = datetime.now(UTC).isoformat()
        events = []

        async with self._db.execute(
            """
            SELECT * FROM pending_events
            WHERE status = 'pending'
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (now, limit),
        ) as cursor:
            async for row in cursor:
                events.append(self._row_to_pending_event(row))

        return events

    async def mark_event_processing(self, event_id: int) -> None:
        """Mark an event as being processed."""
        assert self._db is not None

        await self._db.execute(
            """
            UPDATE pending_events
            SET status = 'processing', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (event_id,),
        )
        await self._db.commit()

    async def mark_event_completed(self, event_id: int) -> None:
        """Remove a successfully processed event."""
        assert self._db is not None

        # Get event data for logging before deletion
        query = "SELECT * FROM pending_events WHERE id = ?"
        async with self._db.execute(query, (event_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                # Log successful sync
                await self.log_sync(
                    event_type=row["event_type"],
                    source_server=row["source_server"],
                    target_server=row["target_server"],
                    username=row["username"],
                    item_id=row["item_id"],
                    success=True,
                    message="Synced successfully",
                )

        # Delete from pending
        await self._db.execute("DELETE FROM pending_events WHERE id = ?", (event_id,))
        await self._db.commit()

    async def mark_event_failed(self, event_id: int, error: str) -> None:
        """Mark an event as failed, schedule retry or give up."""
        assert self._db is not None

        # Get current retry count
        query = """
            SELECT retry_count, max_retries, event_type, source_server,
                   target_server, username, item_id
            FROM pending_events WHERE id = ?
        """
        async with self._db.execute(query, (event_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return

            retry_count = row["retry_count"] + 1
            max_retries = row["max_retries"]

            if retry_count >= max_retries:
                # Move to log as failed and delete
                await self.log_sync(
                    event_type=row["event_type"],
                    source_server=row["source_server"],
                    target_server=row["target_server"],
                    username=row["username"],
                    item_id=row["item_id"],
                    success=False,
                    message=f"Failed after {retry_count} retries: {error}",
                )
                await self._db.execute("DELETE FROM pending_events WHERE id = ?", (event_id,))
            else:
                # Schedule retry with exponential backoff
                backoff_seconds = min(300, 10 * (2**retry_count))  # Max 5 minutes
                next_retry = datetime.now(UTC) + timedelta(seconds=backoff_seconds)

                await self._db.execute(
                    """
                    UPDATE pending_events
                    SET status = 'pending',
                        retry_count = ?,
                        last_error = ?,
                        next_retry_at = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (retry_count, error, next_retry.isoformat(), event_id),
                )

        await self._db.commit()

    async def get_pending_count(self) -> int:
        """Get count of pending events."""
        assert self._db is not None

        async with self._db.execute(
            "SELECT COUNT(*) as count FROM pending_events WHERE status IN ('pending', 'processing')"
        ) as cursor:
            row = await cursor.fetchone()
            return row["count"] if row else 0

    async def reset_stale_processing(self, stale_minutes: int = 5) -> int:
        """Reset events stuck in processing state."""
        assert self._db is not None

        stale_time = (datetime.now(UTC) - timedelta(minutes=stale_minutes)).isoformat()

        cursor = await self._db.execute(
            """
            UPDATE pending_events
            SET status = 'pending', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing' AND updated_at < ?
            """,
            (stale_time,),
        )
        await self._db.commit()
        return cursor.rowcount

    def _row_to_pending_event(self, row: aiosqlite.Row) -> PendingEvent:
        """Convert a database row to PendingEvent model."""
        return PendingEvent(
            id=row["id"],
            event_type=SyncEventType(row["event_type"]),
            source_server=row["source_server"],
            target_server=row["target_server"],
            username=row["username"],
            user_id=row["user_id"],
            item_id=row["item_id"],
            item_name=row["item_name"],
            item_path=row["item_path"],
            provider_imdb=row["provider_imdb"],
            provider_tmdb=row["provider_tmdb"],
            provider_tvdb=row["provider_tvdb"],
            event_data=row["event_data"],
            status=PendingEventStatus(row["status"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            last_error=row["last_error"],
            item_not_found_count=(row["item_not_found_count"] if "item_not_found_count" in row else 0),
            item_not_found_max=(row["item_not_found_max"] if "item_not_found_max" in row else 0),
            created_at=(
                row["created_at"]
                if isinstance(row["created_at"], datetime)
                else datetime.fromisoformat(row["created_at"])
            ),
            updated_at=(
                row["updated_at"]
                if isinstance(row["updated_at"], datetime)
                else datetime.fromisoformat(row["updated_at"])
            ),
        )

    async def mark_event_waiting_for_item(
        self,
        event_id: int,
        max_retries: int,
        retry_delay_seconds: int,
        error_message: str,
    ) -> None:
        """Mark event as waiting for item to appear on target server."""
        assert self._db is not None

        next_retry = datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)

        await self._db.execute(
            """
            UPDATE pending_events
            SET status = 'waiting_for_item',
                item_not_found_count = item_not_found_count + 1,
                item_not_found_max = ?,
                last_error = ?,
                next_retry_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (max_retries, error_message, next_retry.isoformat(), event_id),
        )
        await self._db.commit()

    async def get_waiting_for_item_events(self, limit: int = 100) -> list[PendingEvent]:
        """Get events waiting for items to be imported."""
        assert self._db is not None

        now = datetime.now(UTC).isoformat()
        events: list[PendingEvent] = []

        async with self._db.execute(
            """
            SELECT * FROM pending_events
            WHERE status = 'waiting_for_item'
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (now, limit),
        ) as cursor:
            async for row in cursor:
                events.append(self._row_to_pending_event(row))

        return events

    async def get_waiting_for_item_count(self) -> int:
        """Get count of events waiting for items."""
        assert self._db is not None

        async with self._db.execute(
            "SELECT COUNT(*) as count FROM pending_events WHERE status = 'waiting_for_item'"
        ) as cursor:
            row = await cursor.fetchone()
            return row["count"] if row else 0

    # ========== Statistics Methods ==========

    async def get_processing_count(self) -> int:
        """Get count of events currently being processed."""
        assert self._db is not None

        async with self._db.execute(
            "SELECT COUNT(*) as count FROM pending_events WHERE status = 'processing'"
        ) as cursor:
            row = await cursor.fetchone()
            return row["count"] if row else 0

    async def get_failed_count(self) -> int:
        """Get count of failed events (exceeded max retries)."""
        assert self._db is not None

        async with self._db.execute("SELECT COUNT(*) as count FROM pending_events WHERE status = 'failed'") as cursor:
            row = await cursor.fetchone()
            return row["count"] if row else 0

    async def get_user_mappings_count(self) -> int:
        """Get count of user mappings."""
        assert self._db is not None

        async with self._db.execute("SELECT COUNT(*) as count FROM user_mappings") as cursor:
            row = await cursor.fetchone()
            return row["count"] if row else 0

    async def get_all_user_mappings(self) -> list[UserMapping]:
        """Get all user mappings."""
        assert self._db is not None

        mappings: list[UserMapping] = []
        async with self._db.execute(
            """
            SELECT id, username, server_name, jellyfin_user_id, created_at, updated_at
            FROM user_mappings
            ORDER BY username, server_name
            """
        ) as cursor:
            async for row in cursor:
                mappings.append(
                    UserMapping(
                        id=row["id"],
                        username=row["username"],
                        server_name=row["server_name"],
                        jellyfin_user_id=row["jellyfin_user_id"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
        return mappings

    async def get_sync_log_count(self) -> int:
        """Get count of sync log entries."""
        assert self._db is not None

        async with self._db.execute("SELECT COUNT(*) as count FROM sync_log") as cursor:
            row = await cursor.fetchone()
            return row["count"] if row else 0

    async def get_sync_stats(self) -> dict[str, object]:
        """Get sync statistics."""
        assert self._db is not None

        stats: dict[str, object] = {"total": 0, "successful": 0, "failed": 0, "last_sync_at": None}

        async with self._db.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed,
                MAX(created_at) as last_sync_at
            FROM sync_log
            """
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                stats["total"] = row["total"] or 0
                stats["successful"] = row["successful"] or 0
                stats["failed"] = row["failed"] or 0
                if row["last_sync_at"]:
                    stats["last_sync_at"] = (
                        row["last_sync_at"]
                        if isinstance(row["last_sync_at"], datetime)
                        else datetime.fromisoformat(row["last_sync_at"])
                    )

        return stats

    async def get_failed_events(self, limit: int = 50) -> list[PendingEvent]:
        """Get failed events that exceeded max retries."""
        assert self._db is not None

        events: list[PendingEvent] = []
        async with self._db.execute(
            """
            SELECT * FROM pending_events
            WHERE status = 'failed'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            async for row in cursor:
                events.append(self._row_to_pending_event(row))

        return events

    async def reset_event_for_retry(self, event_id: int) -> bool:
        """Reset a failed event to pending for retry."""
        assert self._db is not None

        cursor = await self._db.execute(
            """
            UPDATE pending_events
            SET status = 'pending',
                retry_count = 0,
                next_retry_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'failed'
            """,
            (event_id,),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def get_recent_sync_log(self, limit: int = 100, since_minutes: int | None = None) -> list[dict[str, object]]:
        """Get recent sync log entries.

        Args:
            limit: Maximum number of entries to return
            since_minutes: Only return entries from the last N minutes (default: all)
        """
        assert self._db is not None

        entries: list[dict[str, object]] = []

        if since_minutes is not None:
            since_time = (datetime.now(UTC) - timedelta(minutes=since_minutes)).isoformat()
            query = """
                SELECT id, event_type, source_server, target_server,
                       username, item_id, success, message, created_at
                FROM sync_log
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            params = (since_time, limit)
        else:
            query = """
                SELECT id, event_type, source_server, target_server,
                       username, item_id, success, message, created_at
                FROM sync_log
                ORDER BY created_at DESC
                LIMIT ?
            """
            params = (limit,)

        async with self._db.execute(query, params) as cursor:
            async for row in cursor:
                entries.append(
                    {
                        "id": row["id"],
                        "event_type": row["event_type"],
                        "source_server": row["source_server"],
                        "target_server": row["target_server"],
                        "username": row["username"],
                        "item_id": row["item_id"],
                        "success": bool(row["success"]),
                        "message": row["message"],
                        "created_at": (
                            row["created_at"].isoformat()
                            if isinstance(row["created_at"], datetime)
                            else row["created_at"]
                        ),
                    }
                )

        return entries


# Global database instance
_db: Database | None = None


async def get_db() -> Database:
    """Get the global database instance."""
    global _db
    if _db is None:
        _db = Database()
        await _db.connect()
    return _db


async def close_db() -> None:
    """Close the global database connection."""
    global _db
    if _db:
        await _db.close()
        _db = None
        _db = None
        _db = None
        _db = None
        _db = None
        _db = None
        _db = None
        _db = None
        _db = None
        _db = None
