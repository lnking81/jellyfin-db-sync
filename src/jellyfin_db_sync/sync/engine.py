"""Sync engine for coordinating updates across Jellyfin servers."""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import Config, ServerConfig, get_config
from ..database import get_db
from ..jellyfin import JellyfinClient
from ..models import PendingEvent, SyncEventType, SyncResult, WebhookPayload

logger = logging.getLogger(__name__)


# Cooldown period to prevent sync loops (seconds)
# When we sync item X to server B, ignore webhooks from B about item X for this duration
SYNC_COOLDOWN_SECONDS = 30


class SyncEngine:
    """Engine for syncing user data across Jellyfin servers.

    Architecture:
    1. Webhook → enqueue_events() → pending_events table (WAL)
    2. Worker loop → process_pending_events() → Jellyfin API → sync_log
    """

    def __init__(self, config: Config | None = None):
        self.config = config or get_config()
        self._clients: dict[str, JellyfinClient] = {}
        self._last_progress_sync: dict[str, datetime] = {}
        self._running = False
        self._worker_task: asyncio.Task[None] | None = None
        # Cooldown tracking: key = "server:username:item_id:event_type" -> expiry time
        # Prevents sync loops by ignoring return webhooks after we just synced
        self._sync_cooldowns: dict[str, datetime] = {}

    def _get_client(self, server: ServerConfig) -> JellyfinClient:
        """Get or create a client for a server."""
        if server.name not in self._clients:
            self._clients[server.name] = JellyfinClient(server)
        return self._clients[server.name]

    def _should_sync_progress(self, key: str) -> bool:
        """Check if enough time has passed for progress sync (debounce)."""
        now = datetime.now(UTC)
        last_sync = self._last_progress_sync.get(key)

        if last_sync is None:
            return True

        elapsed = (now - last_sync).total_seconds()
        return elapsed >= self.config.sync.progress_debounce_seconds

    def _update_progress_timestamp(self, key: str) -> None:
        """Update the last progress sync timestamp."""
        self._last_progress_sync[key] = datetime.now(UTC)

    def _get_item_identity_key(
        self,
        item_path: str | None,
        provider_imdb: str | None = None,
        provider_tmdb: str | None = None,
        provider_tvdb: str | None = None,
    ) -> str:
        """Generate a consistent identity key for an item across servers.

        Uses item_path as primary (works for all content including home media).
        Falls back to provider IDs for movies/series from public DBs.
        """
        if item_path:
            return f"path:{item_path}"

        # Fallback to provider IDs (consistent across servers)
        if provider_imdb:
            return f"imdb:{provider_imdb}"
        if provider_tmdb:
            return f"tmdb:{provider_tmdb}"
        if provider_tvdb:
            return f"tvdb:{provider_tvdb}"

        # No consistent identifier available - cannot track cooldown
        return ""

    def _is_in_cooldown(
        self,
        server: str,
        username: str,
        item_path: str | None,
        event_type: SyncEventType,
        provider_imdb: str | None = None,
        provider_tmdb: str | None = None,
        provider_tvdb: str | None = None,
    ) -> bool:
        """Check if this item/event is in cooldown (recently synced TO this server).

        This prevents sync loops: after syncing to server B, we ignore webhooks
        from server B about the same item for a short period.

        Uses item_path or provider IDs as the identity key (not item_id, which
        differs across servers).
        """
        item_key = self._get_item_identity_key(item_path, provider_imdb, provider_tmdb, provider_tvdb)
        if not item_key:
            # No consistent identifier - can't track, allow sync
            return False

        key = f"{server}:{username}:{item_key}:{event_type.value}"
        expiry = self._sync_cooldowns.get(key)

        if expiry is None:
            return False

        now = datetime.now(UTC)
        if now >= expiry:
            # Cooldown expired, clean up
            del self._sync_cooldowns[key]
            return False

        logger.debug("Event in cooldown (ignoring sync loop): %s", key)
        return True

    def _set_cooldown(
        self,
        server: str,
        username: str,
        item_path: str | None,
        event_type: SyncEventType,
        provider_imdb: str | None = None,
        provider_tmdb: str | None = None,
        provider_tvdb: str | None = None,
    ) -> None:
        """Set cooldown for an item after syncing it TO a server.

        After we sync to server B, we'll ignore webhooks FROM server B
        about this item for SYNC_COOLDOWN_SECONDS.

        Uses item_path or provider IDs as the identity key (not item_id, which
        differs across servers).
        """
        item_key = self._get_item_identity_key(item_path, provider_imdb, provider_tmdb, provider_tvdb)
        if not item_key:
            # No consistent identifier - can't track cooldown
            logger.warning("Cannot set cooldown: no item_path or provider IDs for %s", server)
            return

        key = f"{server}:{username}:{item_key}:{event_type.value}"
        self._sync_cooldowns[key] = datetime.now(UTC) + timedelta(seconds=SYNC_COOLDOWN_SECONDS)
        logger.debug("Set cooldown for: %s", key)

    def _cleanup_expired_cooldowns(self) -> None:
        """Remove expired cooldown entries."""
        now = datetime.now(UTC)
        expired_keys = [k for k, v in self._sync_cooldowns.items() if now >= v]
        for key in expired_keys:
            del self._sync_cooldowns[key]

    # ========== Producer: Webhook → WAL ==========

    async def enqueue_events(
        self,
        payload: WebhookPayload,
        source_server_name: str,
    ) -> int:
        """
        Enqueue sync events from webhook to pending_events table.

        Returns number of events enqueued.
        """
        db = await get_db()

        # Log all incoming webhooks in detail (DEBUG level)
        logger.debug(
            "[WEBHOOK] server=%s, event=%s, user=%s, item=%s, path=%s, played=%s, favorite=%s, position=%s, imdb=%s",
            source_server_name,
            payload.event,
            payload.username,
            payload.item_name,
            payload.item_path,
            payload.is_played,
            payload.is_favorite,
            payload.playback_position_ticks,
            payload.provider_imdb,
        )

        # Periodic cleanup of expired cooldowns
        self._cleanup_expired_cooldowns()

        # Ensure user mapping exists for source server
        await db.upsert_user_mapping(
            username=payload.username,
            server_name=source_server_name,
            jellyfin_user_id=payload.user_id,
        )

        # Parse webhook into event data
        events_data = self._parse_webhook_to_event_data(payload, source_server_name)

        # Filter out events that are in cooldown (prevent sync loops)
        # If we recently synced this item TO source_server, ignore webhooks FROM source_server
        events_before_filter = len(events_data)
        events_data = [
            e
            for e in events_data
            if not self._is_in_cooldown(
                source_server_name,
                payload.username,
                payload.item_path,
                e["event_type"],
                payload.provider_imdb,
                payload.provider_tmdb,
                payload.provider_tvdb,
            )
        ]

        # Log filtered events
        if events_before_filter > len(events_data):
            filtered = events_before_filter - len(events_data)
            logger.debug(
                "[COOLDOWN] Filtered %d events from %s for %s (prevented sync loop)",
                filtered,
                source_server_name,
                payload.item_name,
            )

        if not events_data:
            logger.debug("No sync events generated from webhook: %s", payload.event)
            return 0

        # Get target servers
        target_servers = self.config.get_other_servers(source_server_name)

        # Enqueue event for each target server
        enqueued = 0
        for event_data in events_data:
            for target_server in target_servers:
                # Deduplication: skip if similar event already pending
                if await db.has_pending_event(
                    event_type=event_data["event_type"],
                    target_server=target_server.name,
                    username=payload.username,
                    item_id=payload.item_id,
                ):
                    logger.debug(
                        "Skipping duplicate event: %s for %s -> %s",
                        event_data["event_type"].value,
                        payload.item_name,
                        target_server.name,
                    )
                    continue

                await db.add_pending_event(
                    event_type=event_data["event_type"],
                    source_server=source_server_name,
                    target_server=target_server.name,
                    username=payload.username,
                    user_id=payload.user_id,
                    item_id=payload.item_id,
                    item_name=payload.item_name,
                    event_data=event_data["data"],
                    item_path=payload.item_path,  # Primary: path-based matching
                    provider_imdb=payload.provider_imdb,  # Fallback: provider IDs
                    provider_tmdb=payload.provider_tmdb,
                    provider_tvdb=payload.provider_tvdb,
                )
                enqueued += 1

        logger.debug(
            "Enqueued %d events from %s: event=%s, user=%s, item=%s",
            enqueued,
            source_server_name,
            payload.event,
            payload.username,
            payload.item_name,
        )

        return enqueued

    def _parse_webhook_to_event_data(
        self,
        payload: WebhookPayload,
        source_server: str,
    ) -> list[dict[str, Any]]:
        """Parse webhook payload into event data for queueing."""
        events: list[dict[str, Any]] = []

        # Handle different event types
        if payload.event == "PlaybackStop" and payload.played_to_completion:
            # Mark as watched when playback completes
            if self.config.sync.watched_status:
                events.append(
                    {
                        "event_type": SyncEventType.WATCHED,
                        "data": {"is_played": True},
                    }
                )

        elif payload.event == "PlaybackProgress":
            # Sync playback progress
            if self.config.sync.playback_progress and payload.playback_position_ticks:
                # Debounce check
                debounce_key = f"{source_server}:{payload.username}:{payload.item_id}"
                if self._should_sync_progress(debounce_key):
                    events.append(
                        {
                            "event_type": SyncEventType.PROGRESS,
                            "data": {"position_ticks": payload.playback_position_ticks},
                        }
                    )
                    self._update_progress_timestamp(debounce_key)

        elif payload.event == "UserDataSaved":
            # Skip Import events - these are bulk operations (migration, restore, etc.)
            # that should not trigger sync to avoid flooding the queue
            if payload.save_reason == "Import":
                logger.debug(
                    "[PARSE] Skipping Import event for %s (bulk operation)",
                    payload.item_name,
                )
                return events

            # Handle user data changes (watched status, favorites, etc.)
            # Smart sync: actual value check happens in _execute_sync()
            # to only sync when target state differs from source
            if self.config.sync.watched_status and payload.is_played is not None:
                events.append(
                    {
                        "event_type": SyncEventType.WATCHED,
                        "data": {"is_played": payload.is_played},
                    }
                )
                logger.debug("[PARSE] Generated WATCHED event: is_played=%s", payload.is_played)

            if self.config.sync.favorites and payload.is_favorite is not None:
                events.append(
                    {
                        "event_type": SyncEventType.FAVORITE,
                        "data": {"is_favorite": payload.is_favorite},
                    }
                )
                logger.debug("[PARSE] Generated FAVORITE event: is_favorite=%s", payload.is_favorite)

            if self.config.sync.likes and payload.likes is not None:
                events.append(
                    {
                        "event_type": SyncEventType.LIKES,
                        "data": {"likes": payload.likes},
                    }
                )
                logger.debug("[PARSE] Generated LIKES event: likes=%s", payload.likes)

            if self.config.sync.play_count and payload.play_count is not None:
                events.append(
                    {
                        "event_type": SyncEventType.PLAY_COUNT,
                        "data": {"play_count": payload.play_count},
                    }
                )
                logger.debug("[PARSE] Generated PLAY_COUNT event: play_count=%s", payload.play_count)

            if self.config.sync.last_played_date and payload.last_played_date:
                events.append(
                    {
                        "event_type": SyncEventType.LAST_PLAYED,
                        "data": {"last_played_date": payload.last_played_date},
                    }
                )
                logger.debug("[PARSE] Generated LAST_PLAYED event: date=%s", payload.last_played_date)

            if self.config.sync.audio_stream and payload.audio_stream_index is not None:
                events.append(
                    {
                        "event_type": SyncEventType.AUDIO_STREAM,
                        "data": {"audio_stream_index": payload.audio_stream_index},
                    }
                )
                logger.debug("[PARSE] Generated AUDIO_STREAM event: index=%s", payload.audio_stream_index)

            if self.config.sync.subtitle_stream and payload.subtitle_stream_index is not None:
                events.append(
                    {
                        "event_type": SyncEventType.SUBTITLE_STREAM,
                        "data": {"subtitle_stream_index": payload.subtitle_stream_index},
                    }
                )
                logger.debug("[PARSE] Generated SUBTITLE_STREAM event: index=%s", payload.subtitle_stream_index)

        return events

    # ========== Consumer: WAL → Sync → Log ==========

    async def start_worker(self, interval_seconds: float = 5.0) -> None:
        """Start the background worker that processes pending events."""
        if self._running:
            return

        # Reset any events stuck in processing from previous run
        db = await get_db()
        reset_count = await db.reset_all_processing()
        if reset_count > 0:
            logger.info("Reset %d events stuck in processing from previous run", reset_count)

        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop(interval_seconds))
        logger.info("Sync worker started")

    async def stop_worker(self) -> None:
        """Stop the background worker."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        logger.info("Sync worker stopped")

    async def _worker_loop(self, interval_seconds: float) -> None:
        """Main worker loop that processes pending events."""
        db = await get_db()

        while self._running:
            try:
                # Reset any stale processing events (crashed during processing)
                reset_count = await db.reset_stale_processing()
                if reset_count > 0:
                    logger.warning("Reset %d stale processing events", reset_count)

                # Process pending events
                processed = await self.process_pending_events()

                # Process events waiting for items (with delay check)
                waiting_processed = await self.process_waiting_for_item_events()

                if processed > 0 or waiting_processed > 0:
                    logger.debug("Processed %d pending, %d waiting events", processed, waiting_processed)

            except Exception as e:
                logger.exception("Error in worker loop: %s", e)

            await asyncio.sleep(interval_seconds)

    async def process_pending_events(self, limit: int = 100, max_concurrent: int = 5) -> int:
        """Process pending events from the queue. Returns number processed.

        Args:
            limit: Maximum number of events to fetch
            max_concurrent: Maximum number of events to process in parallel
        """
        db = await get_db()
        events = await db.get_pending_events(limit=limit)

        if not events:
            return 0

        # Mark all as processing first
        for event in events:
            assert event.id is not None
            await db.mark_event_processing(event.id)

        # Process in parallel with semaphore to limit concurrency
        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_one(event: PendingEvent) -> bool:
            async with semaphore:
                try:
                    result = await self._sync_event(event)
                    assert event.id is not None
                    if result.success:
                        await db.mark_event_completed(event.id, synced_value=result.synced_value)
                    else:
                        await db.mark_event_failed(event.id, result.message)
                    return result.success
                except Exception as e:
                    assert event.id is not None
                    logger.warning("Error processing event %d: %s", event.id, e)
                    await db.mark_event_failed(event.id, f"Connection error: {e}")
                    return False

        results = await asyncio.gather(*[process_one(e) for e in events], return_exceptions=True)

        # Count successful (non-exception) results
        processed = sum(1 for r in results if r is True)
        return processed

    async def process_waiting_for_item_events(self, limit: int = 50, max_concurrent: int = 5) -> int:
        """Process events that are waiting for items to appear.

        These are events where the item wasn't found on target server,
        but path_sync_policy allows retrying.

        Args:
            limit: Maximum number of events to fetch
            max_concurrent: Maximum number of events to process in parallel
        """
        db = await get_db()
        events = await db.get_waiting_for_item_events(limit=limit)

        if not events:
            return 0

        # Mark all as processing first
        for event in events:
            assert event.id is not None
            await db.mark_event_processing(event.id)

        # Process in parallel with semaphore to limit concurrency
        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_one(event: PendingEvent) -> bool:
            async with semaphore:
                try:
                    result = await self._sync_event(event)
                    assert event.id is not None
                    if result.success:
                        # Check if it was actually synced or just re-queued for waiting
                        if "Waiting for item" not in result.message:
                            await db.mark_event_completed(event.id, synced_value=result.synced_value)
                        # Otherwise it's already marked as waiting_for_item by _handle_item_not_found
                        return True
                    else:
                        await db.mark_event_failed(event.id, result.message)
                        return False
                except Exception as e:
                    assert event.id is not None
                    logger.warning("Error processing waiting event %d: %s", event.id, e)
                    await db.mark_event_failed(event.id, f"Connection error: {e}")
                    return False

        results = await asyncio.gather(*[process_one(e) for e in events], return_exceptions=True)

        # Count successful (non-exception) results
        processed = sum(1 for r in results if r is True)
        return processed

    async def _sync_event(self, event: PendingEvent) -> SyncResult:
        """Sync a single pending event to target server."""
        target_server = self.config.get_server(event.target_server)
        if not target_server:
            return SyncResult(
                success=False,
                target_server=event.target_server,
                event_type=event.event_type,
                message=f"Target server '{event.target_server}' not found in config",
            )

        client = self._get_client(target_server)
        db = await get_db()

        try:
            # Get or discover user ID on target server
            mapping = await db.get_user_mapping(event.username, event.target_server)

            target_user_id: str
            if mapping:
                target_user_id = mapping.jellyfin_user_id
            else:
                # Try to find user by username
                found_user_id = await client.get_user_id(event.username)
                if not found_user_id:
                    return SyncResult(
                        success=False,
                        target_server=target_server.name,
                        event_type=event.event_type,
                        message=f"User '{event.username}' not found on {target_server.name}",
                    )
                target_user_id = found_user_id
                # Cache the mapping
                await db.upsert_user_mapping(
                    username=event.username,
                    server_name=target_server.name,
                    jellyfin_user_id=target_user_id,
                )

            # Find the item on target server
            # Primary: by file path (works for all content, including home media)
            # Fallback: by provider IDs (only for movies/series from public DBs)
            target_item: dict[str, Any] | None = None

            if event.item_path:
                target_item = await client.find_item_by_path(path=event.item_path)

            if not target_item and (event.provider_imdb or event.provider_tmdb or event.provider_tvdb):
                target_item = await client.find_item_by_provider_id(
                    imdb_id=event.provider_imdb,
                    tmdb_id=event.provider_tmdb,
                    tvdb_id=event.provider_tvdb,
                )

            if not target_item:
                # Item not found — check path policy for retry behavior
                return await self._handle_item_not_found(event, target_server.name)

            target_item_id = target_item["Id"]
            target_item_path = target_item.get("Path", "")

            # Log source and target paths for debugging
            logger.debug(
                "[SYNC] %s -> %s: %s, source_path=%s, target_path=%s",
                event.source_server,
                event.target_server,
                event.event_type.value,
                event.item_path,
                target_item_path,
            )

            # Execute the sync operation
            event_data = json.loads(event.event_data)
            success, synced_value = await self._execute_sync(
                client=client,
                user_id=target_user_id,
                item_id=target_item_id,
                event_type=event.event_type,
                event_data=event_data,
            )

            if success:
                # Set cooldown to prevent sync loop
                # After syncing to target_server, ignore webhooks FROM target_server
                # about this item for a period
                self._set_cooldown(
                    target_server.name,
                    event.username,
                    event.item_path,
                    event.event_type,
                    event.provider_imdb,
                    event.provider_tmdb,
                    event.provider_tvdb,
                )

            return SyncResult(
                success=success,
                target_server=target_server.name,
                event_type=event.event_type,
                message="Synced successfully" if success else "Sync operation returned false",
                synced_value=synced_value,
            )

        except Exception as e:
            logger.exception("Error syncing to %s", target_server.name)
            return SyncResult(
                success=False,
                target_server=target_server.name,
                event_type=event.event_type,
                message=str(e),
            )

    async def _execute_sync(
        self,
        client: JellyfinClient,
        user_id: str,
        item_id: str,
        event_type: SyncEventType,
        event_data: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Execute the actual sync operation on target server.

        Smart sync: checks current state on target before syncing.
        If target already has the same value, skip the sync.
        This prevents unnecessary API calls and sync loops.

        Returns:
            Tuple of (success, synced_value) where synced_value is a human-readable
            representation of what was synced (e.g., "played=True", "position=1:23:45").
        """
        synced_value: str | None = None

        # Get current user data for smart sync comparison
        current_user_data: dict[str, Any] | None = None
        needs_smart_check = event_type in (
            SyncEventType.WATCHED,
            SyncEventType.FAVORITE,
            SyncEventType.LIKES,
            SyncEventType.PLAY_COUNT,
            SyncEventType.LAST_PLAYED,
            SyncEventType.AUDIO_STREAM,
            SyncEventType.SUBTITLE_STREAM,
            SyncEventType.PROGRESS,
            SyncEventType.RATING,
        )

        if needs_smart_check:
            current_user_data = await client.get_user_data(user_id, item_id)

        # Smart sync checks - skip if target already has same value
        if current_user_data:
            match event_type:
                case SyncEventType.WATCHED:
                    current = current_user_data.get("Played", False)
                    desired = event_data.get("is_played")
                    if current == desired:
                        logger.debug("[SMART SYNC] Skipping WATCHED: target has Played=%s", current)
                        return True, f"played={desired} (already set)"

                case SyncEventType.FAVORITE:
                    current = current_user_data.get("IsFavorite", False)
                    desired = event_data.get("is_favorite")
                    if current == desired:
                        logger.debug("[SMART SYNC] Skipping FAVORITE: target has IsFavorite=%s", current)
                        return True, f"favorite={desired} (already set)"

                case SyncEventType.LIKES:
                    current = current_user_data.get("Likes")
                    desired = event_data.get("likes")
                    if current == desired:
                        logger.debug("[SMART SYNC] Skipping LIKES: target has Likes=%s", current)
                        return True, f"likes={desired} (already set)"

                case SyncEventType.PLAY_COUNT:
                    current = current_user_data.get("PlayCount", 0)
                    desired = event_data.get("play_count", 0)
                    # Only sync if source has higher play count
                    if current >= desired:
                        logger.debug("[SMART SYNC] Skipping PLAY_COUNT: target=%d >= source=%d", current, desired)
                        return True, f"play_count={current} (target >= source)"

                case SyncEventType.LAST_PLAYED:
                    current = current_user_data.get("LastPlayedDate")
                    desired = event_data.get("last_played_date")
                    # Only sync if source date is newer (simple string comparison works for ISO format)
                    if current and desired and current >= desired:
                        logger.debug("[SMART SYNC] Skipping LAST_PLAYED: target is newer")
                        return True, f"last_played={current[:10] if current else None} (target newer)"

                case SyncEventType.AUDIO_STREAM:
                    current = current_user_data.get("AudioStreamIndex")
                    desired = event_data.get("audio_stream_index")
                    if current == desired:
                        logger.debug("[SMART SYNC] Skipping AUDIO_STREAM: target has index=%s", current)
                        return True, f"audio_stream={desired} (already set)"

                case SyncEventType.SUBTITLE_STREAM:
                    current = current_user_data.get("SubtitleStreamIndex")
                    desired = event_data.get("subtitle_stream_index")
                    if current == desired:
                        logger.debug("[SMART SYNC] Skipping SUBTITLE_STREAM: target has index=%s", current)
                        return True, f"subtitle_stream={desired} (already set)"

                case SyncEventType.PROGRESS:
                    # No smart sync for PROGRESS - always sync the latest user action
                    # If user rewinds, that's intentional and should be synced
                    # Cooldown mechanism already prevents sync loops
                    pass

                case SyncEventType.RATING:
                    current = current_user_data.get("Rating")
                    desired = event_data.get("rating")
                    if current == desired:
                        logger.debug("[SMART SYNC] Skipping RATING: target has Rating=%s", current)
                        return True, f"rating={desired} (already set)"

                case _:
                    pass

        # Determine which API call to make
        func: Callable[..., Awaitable[bool]] | None = None
        func_name: str = ""
        args: tuple[Any, ...] = ()
        kwargs: dict[str, Any] = {}

        match event_type:
            case SyncEventType.PROGRESS:
                position_ticks = event_data.get("position_ticks")
                if position_ticks is not None:
                    func = client.update_playback_progress
                    func_name = "update_playback_progress"
                    args = (user_id, item_id, int(position_ticks))
                    synced_value = f"position={self._format_ticks(int(position_ticks))}"

            case SyncEventType.WATCHED:
                is_played = event_data.get("is_played")
                if is_played is not None:
                    if is_played:
                        func = client.mark_played
                        func_name = "mark_played"
                    else:
                        func = client.mark_unplayed
                        func_name = "mark_unplayed"
                    args = (user_id, item_id)
                    synced_value = f"played={is_played}"

            case SyncEventType.FAVORITE:
                is_favorite = event_data.get("is_favorite")
                if is_favorite is not None:
                    if is_favorite:
                        func = client.add_favorite
                        func_name = "add_favorite"
                    else:
                        func = client.remove_favorite
                        func_name = "remove_favorite"
                    args = (user_id, item_id)
                    synced_value = f"favorite={is_favorite}"

            case SyncEventType.RATING:
                rating = event_data.get("rating")
                if rating is not None:
                    func = client.update_rating
                    func_name = "update_rating"
                    args = (user_id, item_id, float(rating))
                    synced_value = f"rating={rating}"

            case SyncEventType.LIKES:
                func = client.update_user_data
                func_name = "update_user_data"
                args = (user_id, item_id)
                likes_val = event_data.get("likes")
                kwargs = {"likes": likes_val}
                synced_value = f"likes={likes_val}"

            case SyncEventType.PLAY_COUNT:
                func = client.update_user_data
                func_name = "update_user_data"
                args = (user_id, item_id)
                play_count_val = event_data.get("play_count")
                kwargs = {"play_count": play_count_val}
                synced_value = f"play_count={play_count_val}"

            case SyncEventType.LAST_PLAYED:
                func = client.update_user_data
                func_name = "update_user_data"
                args = (user_id, item_id)
                last_played_val = event_data.get("last_played_date")
                kwargs = {"last_played_date": last_played_val}
                synced_value = f"last_played={last_played_val[:10] if last_played_val else None}"

            case SyncEventType.AUDIO_STREAM:
                func = client.update_user_data
                func_name = "update_user_data"
                args = (user_id, item_id)
                audio_idx = event_data.get("audio_stream_index")
                kwargs = {"audio_stream_index": audio_idx}
                synced_value = f"audio_stream={audio_idx}"

            case SyncEventType.SUBTITLE_STREAM:
                func = client.update_user_data
                func_name = "update_user_data"
                args = (user_id, item_id)
                sub_idx = event_data.get("subtitle_stream_index")
                kwargs = {"subtitle_stream_index": sub_idx}
                synced_value = f"subtitle_stream={sub_idx}"

            case _:
                pass

        if func is None:
            return False, None

        # Format args for logging
        args_str = ", ".join(repr(a) for a in args)
        if kwargs:
            kwargs_str = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
            args_str = f"{args_str}, {kwargs_str}" if args_str else kwargs_str

        if self.config.sync.dry_run:
            logger.info("[DRY RUN] %s(%s)", func_name, args_str)
            return True, synced_value

        result = await func(*args, **kwargs)
        return result, synced_value if result else None

    def _format_ticks(self, ticks: int) -> str:
        """Format ticks (100-nanosecond units) to human-readable time."""
        seconds = ticks // 10_000_000
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    async def _handle_item_not_found(
        self,
        event: PendingEvent,
        target_server_name: str,
    ) -> SyncResult:
        """Handle case when item is not found on target server.

        Checks path_sync_policy to determine retry behavior.
        """
        db = await get_db()
        assert event.id is not None

        # Check if there's a policy for this path
        policy = self.config.get_path_policy(event.item_path)
        error_msg = f"Item '{event.item_name}' not found on {target_server_name}"

        if policy is None or policy.absent_retry_count == 0:
            # No policy or no retries — fail immediately
            return SyncResult(
                success=False,
                target_server=target_server_name,
                event_type=event.event_type,
                message=error_msg,
            )

        # Check if we've exceeded max retries (unless infinite)
        current_count = event.item_not_found_count + 1
        max_retries = policy.absent_retry_count

        if max_retries != -1 and current_count >= max_retries:
            # Exceeded max retries — fail permanently
            return SyncResult(
                success=False,
                target_server=target_server_name,
                event_type=event.event_type,
                message=f"{error_msg} (gave up after {current_count} attempts)",
            )

        # Schedule for retry
        max_display = max_retries if max_retries != -1 else "∞"
        await db.mark_event_waiting_for_item(
            event_id=event.id,
            max_retries=max_retries,
            retry_delay_seconds=policy.retry_delay_seconds,
            error_message=f"{error_msg} (attempt {current_count}/{max_display})",
        )

        logger.info(
            "Item not found, will retry: %s on %s (attempt %s/%s)",
            event.item_name,
            target_server_name,
            current_count,
            max_display,
        )

        # Return success=True to prevent marking as failed (it's waiting, not failed)
        return SyncResult(
            success=True,  # Not a failure, just waiting
            target_server=target_server_name,
            event_type=event.event_type,
            message=f"Waiting for item import (attempt {current_count})",
        )

    # ========== Utilities ==========

    async def sync_all_users(self) -> None:
        """Discover and sync all user mappings across servers."""
        db = await get_db()

        # Collect users from all servers
        all_users: dict[str, dict[str, str]] = {}  # username -> {server: user_id}

        for server in self.config.servers:
            client = self._get_client(server)
            try:
                users = await client.get_users()
                for user in users:
                    username = user.get("Name", "").lower()
                    user_id = user.get("Id", "")
                    if username and user_id:
                        if username not in all_users:
                            all_users[username] = {}
                        all_users[username][server.name] = user_id
            except Exception as e:
                logger.error("Failed to get users from %s: %s", server.name, e)

        # Save mappings
        for username, servers in all_users.items():
            for server_name, user_id in servers.items():
                await db.upsert_user_mapping(username, server_name, user_id)

        logger.info("Synced %d users across servers", len(all_users))

    async def health_check_all(self) -> dict[str, bool]:
        """Check health of all configured servers."""
        results: dict[str, bool] = {}

        async def check_server(server: ServerConfig) -> tuple[str, bool]:
            client = self._get_client(server)
            healthy = await client.health_check()
            return server.name, healthy

        tasks = [check_server(s) for s in self.config.servers]
        for name, healthy in await asyncio.gather(*tasks):
            results[name] = healthy

        return results

    async def get_server_versions(self) -> dict[str, str | None]:
        """Get Jellyfin version for all configured servers."""
        results: dict[str, str | None] = {}

        async def get_version(server: ServerConfig) -> tuple[str, str | None]:
            client = self._get_client(server)
            info = await client.get_server_info()
            version = info.get("Version") if info else None
            return server.name, version

        tasks = [get_version(s) for s in self.config.servers]
        for name, version in await asyncio.gather(*tasks):
            results[name] = version

        return results

    async def get_queue_status(self) -> dict[str, Any]:
        """Get current queue status."""
        db = await get_db()
        pending_count = await db.get_pending_count()

        return {
            "pending_events": pending_count,
            "worker_running": self._running,
        }
