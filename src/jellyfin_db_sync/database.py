"""SQLite database operations for user mappings and event queue."""

import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from .config import get_config
from .models import PendingEvent, PendingEventStatus, SyncEventType, UserMapping

logger = logging.getLogger(__name__)


class Database:
    """Async SQLite database for user mappings."""

    def __init__(self, db_path: str | None = None, journal_mode: str | None = None):
        self._config_db_path = db_path
        self._config_journal_mode = journal_mode
        self._db: aiosqlite.Connection | None = None

    @property
    def db_path(self) -> str:
        """Get database path from config or override."""
        if self._config_db_path:
            return str(self._config_db_path)
        return get_config().database.path

    @property
    def journal_mode(self) -> str:
        """Get journal mode from config or override."""
        if self._config_journal_mode:
            return self._config_journal_mode.upper()
        try:
            return get_config().database.journal_mode.upper()
        except RuntimeError:
            return "WAL"  # Default if config not loaded (tests)

    async def connect(self) -> None:
        """Connect to the database and create tables."""
        # Ensure parent directory exists
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Connecting to database: %s (journal_mode=%s)", db_path, self.journal_mode)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row

        # Set journal mode (WAL is default, use DELETE for NFS compatibility)
        if self.journal_mode in ("WAL", "DELETE", "TRUNCATE", "MEMORY", "OFF"):
            await self._db.execute(f"PRAGMA journal_mode={self.journal_mode}")

        await self._create_tables()
        logger.info("Database connected successfully")

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            logger.info("Closing database connection")
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
                item_name TEXT,
                synced_value TEXT,
                success BOOLEAN NOT NULL,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Migration: add item_name and synced_value columns if they don't exist
        with contextlib.suppress(Exception):
            await self._db.execute("ALTER TABLE sync_log ADD COLUMN item_name TEXT")
        with contextlib.suppress(Exception):
            await self._db.execute("ALTER TABLE sync_log ADD COLUMN synced_value TEXT")

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

        # Item path cache table - maps file path to Jellyfin item ID per server
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS item_path_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_name TEXT NOT NULL,
                item_path TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(server_name, item_path)
            )
        """
        )

        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_item_path_cache_path
            ON item_path_cache(server_name, item_path)
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
                logger.debug("[%s] Found user mapping: %s -> %s", server_name, username, row["jellyfin_user_id"])
                return UserMapping(
                    id=row["id"],
                    username=row["username"],
                    server_name=row["server_name"],
                    jellyfin_user_id=row["jellyfin_user_id"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            logger.debug("[%s] User mapping not found: %s", server_name, username)
            return None

    async def get_user_mappings_by_username(self, username: str) -> list[UserMapping]:
        """Get all mappings for a username across all servers."""
        assert self._db is not None

        logger.debug("Looking up all mappings for user: %s", username)
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
        logger.debug("Found %d mappings for user %s", len(mappings), username)
        return mappings

    async def upsert_user_mapping(self, username: str, server_name: str, jellyfin_user_id: str) -> UserMapping:
        """Insert or update a user mapping."""
        assert self._db is not None

        logger.debug("[%s] Upserting user mapping: %s -> %s", server_name, username, jellyfin_user_id)
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
        logger.info("[%s] User mapping saved: %s -> %s", server_name, username, jellyfin_user_id)
        return mapping

    async def delete_user_mapping(self, username: str, server_name: str) -> bool:
        """Delete a user mapping. Returns True if deleted."""
        assert self._db is not None

        logger.debug("[%s] Deleting user mapping: %s", server_name, username)
        cursor = await self._db.execute(
            "DELETE FROM user_mappings WHERE username = ? AND server_name = ?",
            (username, server_name),
        )
        await self._db.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("[%s] Deleted user mapping: %s", server_name, username)
        return deleted

    async def log_sync(
        self,
        event_type: str,
        source_server: str,
        target_server: str,
        username: str,
        item_id: str | None,
        success: bool,
        message: str,
        item_name: str | None = None,
        synced_value: str | None = None,
    ) -> None:
        """Log a sync operation."""
        assert self._db is not None

        if success:
            logger.info(
                "[%s->%s] Sync OK: %s %s for %s",
                source_server,
                target_server,
                event_type,
                item_name or item_id,
                username,
            )
        else:
            logger.warning(
                "[%s->%s] Sync FAILED: %s %s for %s - %s",
                source_server,
                target_server,
                event_type,
                item_name or item_id,
                username,
                message,
            )

        await self._db.execute(
            """
            INSERT INTO sync_log
            (event_type, source_server, target_server, username, item_id, item_name, synced_value, success, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, source_server, target_server, username, item_id, item_name, synced_value, success, message),
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

        logger.debug("[%s] Checking for duplicate event: %s item=%s", target_server, event_type.value, item_id[:8])
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
            exists = row is not None
            if exists:
                logger.debug("[%s] Duplicate event found, skipping", target_server)
            return exists

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

        logger.info(
            "[%s->%s] Queued event: %s %s for %s",
            source_server,
            target_server,
            event_type.value,
            item_name,
            username,
        )

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
        event_id = cursor.lastrowid or 0
        logger.debug("[%s->%s] Event queued with id=%d", source_server, target_server, event_id)
        return event_id

    async def get_pending_events(self, limit: int = 100) -> list[PendingEvent]:
        """Get pending events ready for processing."""
        assert self._db is not None

        logger.debug("Fetching pending events (limit=%d)", limit)
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

        if events:
            logger.debug("Fetched %d pending events for processing", len(events))
        return events

    async def mark_event_processing(self, event_id: int) -> None:
        """Mark an event as being processed."""
        assert self._db is not None

        logger.debug("Event %d: status -> processing", event_id)
        await self._db.execute(
            """
            UPDATE pending_events
            SET status = 'processing', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (event_id,),
        )
        await self._db.commit()

    async def mark_event_completed(self, event_id: int, synced_value: str | None = None) -> None:
        """Remove a successfully processed event."""
        assert self._db is not None

        # Get event data for logging before deletion
        query = "SELECT * FROM pending_events WHERE id = ?"
        async with self._db.execute(query, (event_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                logger.debug(
                    "Event %d: completed (%s %s)",
                    event_id,
                    row["event_type"],
                    row["item_name"],
                )
                # Log successful sync
                await self.log_sync(
                    event_type=row["event_type"],
                    source_server=row["source_server"],
                    target_server=row["target_server"],
                    username=row["username"],
                    item_id=row["item_id"],
                    success=True,
                    message="Synced successfully",
                    item_name=row["item_name"],
                    synced_value=synced_value,
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
                   target_server, username, item_id, item_name
            FROM pending_events WHERE id = ?
        """
        async with self._db.execute(query, (event_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                logger.warning("Event %d: not found for marking as failed", event_id)
                return

            retry_count = row["retry_count"] + 1
            max_retries = row["max_retries"]

            if retry_count >= max_retries:
                # Move to log as failed and delete
                logger.error(
                    "Event %d: FAILED after %d retries (%s %s) - %s",
                    event_id,
                    retry_count,
                    row["event_type"],
                    row["item_name"],
                    error,
                )
                await self.log_sync(
                    event_type=row["event_type"],
                    source_server=row["source_server"],
                    target_server=row["target_server"],
                    username=row["username"],
                    item_id=row["item_id"],
                    success=False,
                    message=f"Failed after {retry_count} retries: {error}",
                    item_name=row["item_name"],
                )
                await self._db.execute("DELETE FROM pending_events WHERE id = ?", (event_id,))
            else:
                # Schedule retry with exponential backoff
                backoff_seconds = min(300, 10 * (2**retry_count))  # Max 5 minutes
                next_retry = datetime.now(UTC) + timedelta(seconds=backoff_seconds)

                logger.warning(
                    "Event %d: retry %d/%d in %ds (%s) - %s",
                    event_id,
                    retry_count,
                    max_retries,
                    backoff_seconds,
                    row["item_name"],
                    error,
                )

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
        """Reset events stuck in processing state for too long."""
        assert self._db is not None

        logger.debug("Checking for stale events (>%d minutes)", stale_minutes)
        stale_time = (datetime.now(UTC) - timedelta(minutes=stale_minutes)).strftime("%Y-%m-%d %H:%M:%S")

        cursor = await self._db.execute(
            """
            UPDATE pending_events
            SET status = 'pending', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing' AND updated_at < ?
            """,
            (stale_time,),
        )
        await self._db.commit()
        if cursor.rowcount > 0:
            logger.info("Reset %d stale events to pending", cursor.rowcount)
        return cursor.rowcount

    async def reset_all_processing(self) -> int:
        """Reset ALL events in processing state.

        Called on startup to recover from crashes/restarts.
        """
        assert self._db is not None

        logger.debug("Resetting all processing events (startup recovery)")
        cursor = await self._db.execute(
            """
            UPDATE pending_events
            SET status = 'pending', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing'
            """
        )
        await self._db.commit()
        if cursor.rowcount > 0:
            logger.info("Startup recovery: reset %d events from processing to pending", cursor.rowcount)
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

        logger.warning(
            "Event %d: waiting for item import, retry in %ds",
            event_id,
            retry_delay_seconds,
        )

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

        logger.debug("Fetching events waiting for item import (limit=%d)", limit)
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

        if events:
            logger.debug("Found %d events ready for item retry", len(events))
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

        logger.debug("Fetching failed events (limit=%d)", limit)
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

        logger.debug("Found %d failed events", len(events))
        return events

    async def reset_event_for_retry(self, event_id: int) -> bool:
        """Reset a failed event to pending for retry."""
        assert self._db is not None

        logger.debug("Resetting failed event %d for retry", event_id)
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
        if cursor.rowcount > 0:
            logger.info("Event %d reset for retry", event_id)
        return cursor.rowcount > 0

    async def get_recent_sync_log(
        self,
        limit: int = 100,
        offset: int = 0,
        since_minutes: int | None = None,
        source_server: str | None = None,
        target_server: str | None = None,
        event_type: str | None = None,
        item_name: str | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        """Get recent sync log entries with optional filtering.

        Args:
            limit: Maximum number of entries to return
            offset: Number of entries to skip (for pagination)
            since_minutes: Only return entries from the last N minutes (default: all)
            source_server: Filter by source server name (exact match)
            target_server: Filter by target server name (exact match)
            event_type: Filter by event type (exact match)
            item_name: Filter by item name (case-insensitive substring search)

        Returns:
            Tuple of (entries list, total count matching filters)
        """
        assert self._db is not None

        entries: list[dict[str, object]] = []
        conditions: list[str] = []
        params: list[object] = []

        # Time filter
        if since_minutes is not None:
            since_time = (datetime.now(UTC) - timedelta(minutes=since_minutes)).strftime("%Y-%m-%d %H:%M:%S")
            conditions.append("created_at >= ?")
            params.append(since_time)

        # Server filters
        if source_server:
            conditions.append("source_server = ?")
            params.append(source_server)
        if target_server:
            conditions.append("target_server = ?")
            params.append(target_server)

        # Event type filter
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)

        # Item name filter (LIKE for substring search)
        if item_name:
            conditions.append("item_name LIKE ?")
            params.append(f"%{item_name}%")

        # Build WHERE clause
        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Get total count first
        count_query = f"SELECT COUNT(*) FROM sync_log WHERE {where_clause}"
        async with self._db.execute(count_query, params) as cursor:
            row = await cursor.fetchone()
            total_count = row[0] if row else 0

        # Build data query with pagination
        query = f"""
            SELECT id, event_type, source_server, target_server,
                   username, item_id, item_name, synced_value, success, message, created_at
            FROM sync_log
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        data_params = [*params, limit, offset]

        async with self._db.execute(query, data_params) as cursor:
            async for row in cursor:
                entries.append(
                    {
                        "id": row["id"],
                        "event_type": row["event_type"],
                        "source_server": row["source_server"],
                        "target_server": row["target_server"],
                        "username": row["username"],
                        "item_id": row["item_id"],
                        "item_name": row["item_name"],
                        "synced_value": row["synced_value"],
                        "success": bool(row["success"]),
                        "message": row["message"],
                        "created_at": (
                            row["created_at"].isoformat()
                            if isinstance(row["created_at"], datetime)
                            else row["created_at"]
                        ),
                    }
                )

        return entries, total_count

    # ========== Item Path Cache Methods ==========

    async def get_cached_item_id(self, server_name: str, item_path: str) -> str | None:
        """Get cached item ID for a path on a server."""
        assert self._db is not None

        async with self._db.execute(
            """
            SELECT item_id FROM item_path_cache
            WHERE server_name = ? AND item_path = ?
            """,
            (server_name, item_path),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                logger.debug("[%s] Cache hit: %s", server_name, item_path[-50:])
                return row["item_id"]
            return None

    async def cache_item_path(
        self,
        server_name: str,
        item_path: str,
        item_id: str,
        item_name: str | None = None,
        *,
        commit: bool = True,
    ) -> None:
        """Cache a path to item ID mapping.

        Args:
            server_name: Server name
            item_path: File path on server
            item_id: Jellyfin item ID
            item_name: Optional item name for debugging
            commit: Whether to commit after insert (default True, set False for batch ops)
        """
        assert self._db is not None

        await self._db.execute(
            """
            INSERT INTO item_path_cache (server_name, item_path, item_id, item_name, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(server_name, item_path)
            DO UPDATE SET item_id = excluded.item_id,
                          item_name = excluded.item_name,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (server_name, item_path, item_id, item_name),
        )
        if commit:
            await self._db.commit()

    async def cache_items_batch(
        self,
        server_name: str,
        items: list[tuple[str, str, str | None]],
    ) -> int:
        """Cache multiple items in a single transaction.

        Args:
            server_name: Server name
            items: List of (item_path, item_id, item_name) tuples

        Returns:
            Number of items cached
        """
        assert self._db is not None

        if not items:
            return 0

        await self._db.executemany(
            """
            INSERT INTO item_path_cache (server_name, item_path, item_id, item_name, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(server_name, item_path)
            DO UPDATE SET item_id = excluded.item_id,
                          item_name = excluded.item_name,
                          updated_at = CURRENT_TIMESTAMP
            """,
            [(server_name, path, item_id, name) for path, item_id, name in items],
        )
        await self._db.commit()
        logger.info("[%s] Cached %d items", server_name, len(items))
        return len(items)

    async def invalidate_item_cache(self, server_name: str, item_path: str | None = None) -> int:
        """Invalidate cached items. If item_path is None, invalidate all for server."""
        assert self._db is not None

        if item_path:
            cursor = await self._db.execute(
                "DELETE FROM item_path_cache WHERE server_name = ? AND item_path = ?",
                (server_name, item_path),
            )
            logger.debug("[%s] Invalidated cache entry: %s", server_name, item_path[-50:])
        else:
            cursor = await self._db.execute(
                "DELETE FROM item_path_cache WHERE server_name = ?",
                (server_name,),
            )
            logger.info("[%s] Full cache invalidation: %d items removed", server_name, cursor.rowcount)
        await self._db.commit()
        return cursor.rowcount

    async def get_item_cache_count(self, server_name: str | None = None) -> int:
        """Get count of cached items."""
        assert self._db is not None

        if server_name:
            query = "SELECT COUNT(*) as count FROM item_path_cache WHERE server_name = ?"
            params: tuple[str, ...] = (server_name,)
        else:
            query = "SELECT COUNT(*) as count FROM item_path_cache"
            params = ()

        async with self._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return row["count"] if row else 0

    async def get_item_cache_stats(self) -> dict[str, int]:
        """Get item cache count per server.

        Returns:
            Dict mapping server_name to cached item count
        """
        assert self._db is not None

        stats: dict[str, int] = {}
        async with self._db.execute(
            """
            SELECT server_name, COUNT(*) as count
            FROM item_path_cache
            GROUP BY server_name
            ORDER BY server_name
            """
        ) as cursor:
            async for row in cursor:
                stats[row["server_name"]] = row["count"]

        return stats

    def get_database_size(self) -> int:
        """Get database file size in bytes."""
        try:
            db_path = Path(self.db_path)
            if db_path.exists():
                # Main DB + WAL + SHM files
                total_size = db_path.stat().st_size
                wal_path = db_path.with_suffix(".db-wal")
                shm_path = db_path.with_suffix(".db-shm")
                if wal_path.exists():
                    total_size += wal_path.stat().st_size
                if shm_path.exists():
                    total_size += shm_path.stat().st_size
                return total_size
        except OSError:
            pass
        return 0


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
