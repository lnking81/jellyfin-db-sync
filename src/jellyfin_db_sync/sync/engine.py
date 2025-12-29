"""Sync engine for coordinating updates across Jellyfin servers."""

import asyncio
import contextlib
import json
import logging
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

    def _is_in_cooldown(self, server: str, username: str, item_id: str, event_type: SyncEventType) -> bool:
        """Check if this item/event is in cooldown (recently synced TO this server).

        This prevents sync loops: after syncing to server B, we ignore webhooks
        from server B about the same item for a short period.
        """
        key = f"{server}:{username}:{item_id}:{event_type.value}"
        expiry = self._sync_cooldowns.get(key)

        if expiry is None:
            return False

        now = datetime.now(UTC)
        if now >= expiry:
            # Cooldown expired, clean up
            del self._sync_cooldowns[key]
            return False

        return True

    def _set_cooldown(self, server: str, username: str, item_id: str, event_type: SyncEventType) -> None:
        """Set cooldown for an item after syncing it TO a server.

        After we sync to server B, we'll ignore webhooks FROM server B
        about this item for SYNC_COOLDOWN_SECONDS.
        """
        key = f"{server}:{username}:{item_id}:{event_type.value}"
        self._sync_cooldowns[key] = datetime.now(UTC) + timedelta(seconds=SYNC_COOLDOWN_SECONDS)

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
        events_data = [
            e
            for e in events_data
            if not self._is_in_cooldown(source_server_name, payload.username, payload.item_id, e["event_type"])
        ]

        if not events_data:
            logger.debug(f"No sync events generated from webhook: {payload.event}")
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
                        f"Skipping duplicate event: {event_data['event_type'].value} "
                        f"for {payload.item_name} → {target_server.name}"
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

        logger.info(
            f"Enqueued {enqueued} events from {source_server_name}: "
            f"event={payload.event}, user={payload.username}, item={payload.item_name}"
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
            # Handle various user data changes
            # Only sync if we have a definitive value (not None)
            if self.config.sync.watched_status and payload.is_played is not None:
                events.append(
                    {
                        "event_type": SyncEventType.WATCHED,
                        "data": {"is_played": payload.is_played},
                    }
                )

            if self.config.sync.favorites and payload.is_favorite is not None:
                events.append(
                    {
                        "event_type": SyncEventType.FAVORITE,
                        "data": {"is_favorite": payload.is_favorite},
                    }
                )

        return events

    # ========== Consumer: WAL → Sync → Log ==========

    async def start_worker(self, interval_seconds: float = 5.0) -> None:
        """Start the background worker that processes pending events."""
        if self._running:
            return

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
                    logger.warning(f"Reset {reset_count} stale processing events")

                # Process pending events
                processed = await self.process_pending_events()

                # Process events waiting for items (with delay check)
                waiting_processed = await self.process_waiting_for_item_events()

                if processed > 0 or waiting_processed > 0:
                    logger.debug(f"Processed {processed} pending, {waiting_processed} waiting events")

            except Exception as e:
                logger.exception(f"Error in worker loop: {e}")

            await asyncio.sleep(interval_seconds)

    async def process_pending_events(self, limit: int = 100) -> int:
        """Process pending events from the queue. Returns number processed."""
        db = await get_db()
        events = await db.get_pending_events(limit=limit)

        if not events:
            return 0

        processed = 0
        for event in events:
            # Mark as processing
            assert event.id is not None
            await db.mark_event_processing(event.id)

            # Try to sync
            result = await self._sync_event(event)

            if result.success:
                await db.mark_event_completed(event.id)
            else:
                await db.mark_event_failed(event.id, result.message)

            processed += 1

        return processed

    async def process_waiting_for_item_events(self, limit: int = 50) -> int:
        """Process events that are waiting for items to appear.

        These are events where the item wasn't found on target server,
        but path_sync_policy allows retrying.
        """
        db = await get_db()
        events = await db.get_waiting_for_item_events(limit=limit)

        if not events:
            return 0

        processed = 0
        for event in events:
            assert event.id is not None
            await db.mark_event_processing(event.id)

            # Try to sync again
            result = await self._sync_event(event)

            if result.success:
                # Check if it was actually synced or just re-queued for waiting
                if "Waiting for item" not in result.message:
                    await db.mark_event_completed(event.id)
                # Otherwise it's already marked as waiting_for_item by _handle_item_not_found
            else:
                await db.mark_event_failed(event.id, result.message)

            processed += 1

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
                target_item = await client.find_item_by_path(
                    user_id=target_user_id,
                    path=event.item_path,
                )

            if not target_item and (event.provider_imdb or event.provider_tmdb or event.provider_tvdb):
                target_item = await client.find_item_by_provider_id(
                    user_id=target_user_id,
                    imdb_id=event.provider_imdb,
                    tmdb_id=event.provider_tmdb,
                    tvdb_id=event.provider_tvdb,
                )

            if not target_item:
                # Item not found — check path policy for retry behavior
                return await self._handle_item_not_found(event, target_server.name)

            target_item_id = target_item["Id"]

            # Execute the sync operation
            event_data = json.loads(event.event_data)
            success = await self._execute_sync(
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
                    event.item_id,
                    event.event_type,
                )

            return SyncResult(
                success=success,
                target_server=target_server.name,
                event_type=event.event_type,
                message="Synced successfully" if success else "Sync operation returned false",
            )

        except Exception as e:
            logger.exception(f"Error syncing to {target_server.name}")
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
    ) -> bool:
        """Execute the actual sync operation on target server."""
        match event_type:
            case SyncEventType.PROGRESS:
                position_ticks = event_data.get("position_ticks")
                if position_ticks is not None:
                    return await client.update_playback_progress(user_id, item_id, int(position_ticks))

            case SyncEventType.WATCHED:
                is_played = event_data.get("is_played")
                if is_played is not None:
                    if is_played:
                        return await client.mark_played(user_id, item_id)
                    else:
                        return await client.mark_unplayed(user_id, item_id)

            case SyncEventType.FAVORITE:
                is_favorite = event_data.get("is_favorite")
                if is_favorite is not None:
                    if is_favorite:
                        return await client.add_favorite(user_id, item_id)
                    else:
                        return await client.remove_favorite(user_id, item_id)

            case SyncEventType.RATING:
                rating = event_data.get("rating")
                if rating is not None:
                    return await client.update_rating(user_id, item_id, float(rating))

            case _:
                pass

        return False

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
            f"Item not found, will retry: {event.item_name} on {target_server_name} "
            f"(attempt {current_count}/{max_display})"
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
                logger.error(f"Failed to get users from {server.name}: {e}")

        # Save mappings
        for username, servers in all_users.items():
            for server_name, user_id in servers.items():
                await db.upsert_user_mapping(username, server_name, user_id)

        logger.info(f"Synced {len(all_users)} users across servers")

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

    async def get_queue_status(self) -> dict[str, Any]:
        """Get current queue status."""
        db = await get_db()
        pending_count = await db.get_pending_count()

        return {
            "pending_events": pending_count,
            "worker_running": self._running,
        }
