"""Jellyfin API client for sync operations."""

import asyncio
import logging
import uuid
from importlib.metadata import metadata
from typing import Any

import httpx

from ..config import ServerConfig

logger = logging.getLogger(__name__)

# Connection pool limits
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

# Lock for cache refresh per server (prevents parallel refreshes)
_cache_refresh_locks: dict[str, asyncio.Lock] = {}


def _get_cache_lock(server_name: str) -> asyncio.Lock:
    """Get or create a lock for cache refresh on a specific server."""
    if server_name not in _cache_refresh_locks:
        _cache_refresh_locks[server_name] = asyncio.Lock()
    return _cache_refresh_locks[server_name]


# Get package metadata for client identification
_PKG_NAME = "jellyfin-db-sync"
_pkg_meta = metadata(_PKG_NAME)
CLIENT_NAME = _pkg_meta["Name"]
CLIENT_VERSION = _pkg_meta["Version"]
# Generate a stable device ID based on machine (or use random for each instance)
DEVICE_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{_PKG_NAME}.local"))


class JellyfinClient:
    """Async client for Jellyfin API."""

    def __init__(self, server: ServerConfig):
        self.server = server
        self.base_url = server.url.rstrip("/")
        # Use proper Jellyfin authorization header format
        # This prevents phantom playback sessions from appearing on dashboard
        auth_header = (
            f'MediaBrowser Client="{CLIENT_NAME}", '
            f'Device="{CLIENT_NAME}", '
            f'DeviceId="{DEVICE_ID}", '
            f'Version="{CLIENT_VERSION}", '
            f'Token="{server.api_key}"'
        )
        self.headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json",
        }
        self._client: httpx.AsyncClient | None = None
        self._admin_user_id: str | None = None  # Cached admin user ID for item lookups

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT,
                limits=DEFAULT_LIMITS,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated request to the Jellyfin API."""
        url = f"{self.base_url}{endpoint}"
        client = await self._get_client()
        response = await client.request(
            method,
            url,
            headers=self.headers,
            **kwargs,
        )
        response.raise_for_status()
        return response

    # ========== User Operations ==========

    async def get_users(self) -> list[dict[str, Any]]:
        """Get all users from the server."""
        logger.debug("[%s] Getting users list", self.server.name)
        response = await self._request("GET", "/Users")
        users = response.json()
        logger.debug("[%s] Found %d users", self.server.name, len(users))
        return users

    async def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        """Find a user by username."""
        users = await self.get_users()
        for user in users:
            if user.get("Name", "").lower() == username.lower():
                logger.debug("[%s] Found user '%s' -> %s", self.server.name, username, user.get("Id"))
                return user
        logger.debug("[%s] User '%s' not found", self.server.name, username)
        return None

    async def get_user_id(self, username: str) -> str | None:
        """Get user ID by username."""
        user = await self.get_user_by_name(username)
        user_id = user.get("Id") if user else None
        if user_id:
            logger.debug("[%s] Resolved user '%s' -> %s", self.server.name, username, user_id)
        return user_id

    async def get_admin_user_id(self) -> str | None:
        """Get admin user ID for item lookups (cached).

        Jellyfin /Items/{id} endpoint requires a user context.
        We use an admin user who has access to all libraries.
        """
        if self._admin_user_id:
            return self._admin_user_id

        users = await self.get_users()
        for user in users:
            policy = user.get("Policy", {})
            if policy.get("IsAdministrator"):
                self._admin_user_id = user.get("Id")
                logger.info(
                    "[%s] Using admin user '%s' (%s) for item lookups",
                    self.server.name,
                    user.get("Name"),
                    self._admin_user_id,
                )
                return self._admin_user_id

        logger.error("[%s] No admin user found! Item lookups will fail.", self.server.name)
        return None

    async def create_user(self, username: str, password: str | None = None) -> dict[str, Any] | None:
        """
        Create a new user on the server.

        Args:
            username: The username for the new user
            password: Password for the user (None for passwordless servers)

        Returns:
            User data dict if successful, None otherwise
        """
        logger.info("[%s] Creating user '%s'", self.server.name, username)
        try:
            response = await self._request(
                "POST",
                "/Users/New",
                json={
                    "Name": username,
                    "Password": password or "",
                },
            )
            user = response.json()
            logger.info("[%s] Created user '%s' -> %s", self.server.name, username, user.get("Id"))
            return user
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                logger.warning("[%s] User '%s' already exists", self.server.name, username)
            else:
                logger.error("[%s] Failed to create user '%s': %s", self.server.name, username, e)
            return None

    async def delete_user(self, user_id: str) -> bool:
        """
        Delete a user from the server.

        Args:
            user_id: The Jellyfin user ID to delete

        Returns:
            True if deleted successfully
        """
        logger.info("[%s] Deleting user %s", self.server.name, user_id)
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}",
            )
            logger.info("[%s] Deleted user %s", self.server.name, user_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to delete user %s: %s", self.server.name, user_id, e)
            return False

    # ========== Item Lookup ==========

    async def find_item_by_path(self, path: str, db: Any = None) -> dict[str, Any] | None:
        """
        Find an item by its file path.

        This is the most reliable method when all Jellyfin instances
        share the same storage (NFS/CIFS mount). Works for all content
        including home photos/videos that don't have provider IDs.

        Strategy:
        1. Check local DB cache first (path -> item_id)
        2. If not in cache, refresh cache from server and try again
        3. Cache all items for future lookups

        Args:
            path: File path to search for
            db: Database instance for caching (optional, imported if not provided)
        """
        # Get database for caching
        if db is None:
            from ..database import get_db

            db = await get_db()

        # Get admin user for item lookups
        admin_id = await self.get_admin_user_id()
        if not admin_id:
            logger.error("[%s] Cannot lookup item - no admin user", self.server.name)
            return None

        # Check cache first
        cached_id = await db.get_cached_item_id(self.server.name, path)
        if cached_id:
            logger.debug("[%s] Cache HIT: %s -> %s", self.server.name, path, cached_id)
            # Return item info from cache - trust the cache to avoid race conditions
            # If item was deleted/moved, the sync operation will fail with 404 and we handle there
            try:
                response = await self._request(
                    "GET",
                    f"/Users/{admin_id}/Items/{cached_id}",
                    params={"fields": "Path,ProviderIds"},
                )
                return response.json()
            except httpx.HTTPStatusError as e:
                # Item no longer exists (404), invalidate cache entry
                logger.warning("[%s] Cached item %s no longer exists: %s", self.server.name, cached_id, e)
                await db.invalidate_item_cache(self.server.name, path)

        # Cache miss - refresh cache from server (new items may have been added)
        logger.info("[%s] Cache MISS for path, will refresh: %s", self.server.name, path)

        return await self._refresh_cache_and_find(path, db)

    async def _refresh_cache_and_find(self, path: str, db: Any) -> dict[str, Any] | None:
        """Refresh item cache from server and find item by path.

        Uses a lock to prevent parallel refreshes for the same server.
        If another refresh is in progress, waits for it and checks cache again.
        """
        lock = _get_cache_lock(self.server.name)

        # Check if another refresh is already in progress
        if lock.locked():
            logger.debug("[%s] Cache refresh in progress, waiting...", self.server.name)
            async with lock:
                pass  # Just wait for the other refresh to complete

            # After waiting, check cache again - the other refresh may have populated it
            cached_id = await db.get_cached_item_id(self.server.name, path)
            if cached_id:
                logger.debug("[%s] Cache HIT after parallel refresh: %s", self.server.name, path)
                admin_id = await self.get_admin_user_id()
                if admin_id:
                    try:
                        response = await self._request(
                            "GET",
                            f"/Users/{admin_id}/Items/{cached_id}",
                            params={"fields": "Path,ProviderIds"},
                        )
                        return response.json()
                    except httpx.HTTPStatusError:
                        pass
            # Still not found after waiting - item doesn't exist on server
            logger.debug("[%s] Item not in cache after parallel refresh: %s", self.server.name, path)
            return None

        # We're the first one - do the actual refresh
        async with lock:
            return await self._do_refresh_cache(path, db)

    async def _do_refresh_cache(self, path: str, db: Any) -> dict[str, Any] | None:
        """Actually refresh the cache from server."""
        try:
            # Get admin user to access all libraries
            admin_id = await self.get_admin_user_id()
            if not admin_id:
                logger.error("[%s] Cannot refresh cache - no admin user", self.server.name)
                return None

            # Jellyfin API has NO path search parameter
            # We must get all items and filter locally
            # Use includeItemTypes to limit to actual media files
            all_items: list[dict[str, Any]] = []
            start_index = 0
            page_size = 500

            while True:
                response = await self._request(
                    "GET",
                    f"/Users/{admin_id}/Items",
                    params={
                        "recursive": "true",
                        "fields": "Path,ProviderIds",
                        "includeItemTypes": "Movie,Episode,Video,Audio,MusicVideo",
                        "startIndex": start_index,
                        "limit": page_size,
                    },
                )
                data = response.json()
                items = data.get("Items", [])
                total = data.get("TotalRecordCount", 0)

                all_items.extend(items)

                # Check if we got all items
                if start_index + len(items) >= total:
                    break
                start_index += page_size

            logger.info("[%s] Fetched %d items from API", self.server.name, len(all_items))

            # Cache ALL items with paths in a single batch (one transaction)
            found_item: dict[str, Any] | None = None
            cache_batch: list[tuple[str, str, str | None]] = []
            for item in all_items:
                item_path = item.get("Path", "")
                if item_path:
                    item_id = item.get("Id", "")
                    item_name = item.get("Name", "")
                    cache_batch.append((item_path, item_id, item_name))
                    if item_path == path:
                        found_item = item

            # Single batch insert - one commit for all items
            if cache_batch:
                await db.cache_items_batch(self.server.name, cache_batch)

            if found_item:
                logger.info(
                    "[%s] Cache refreshed, item found: %s -> %s",
                    self.server.name,
                    found_item.get("Name"),
                    found_item.get("Id"),
                )
                return found_item

            logger.warning("[%s] Cache refreshed but item not found: %s", self.server.name, path)

        except httpx.HTTPStatusError as e:
            logger.error("[%s] Cache refresh failed: %s", self.server.name, e)

        return None

    async def find_item_by_provider_id(
        self,
        imdb_id: str | None = None,
        tmdb_id: str | None = None,
        tvdb_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find an item by external provider ID."""
        logger.debug(
            "[%s] Finding item by provider ID: imdb=%s, tmdb=%s, tvdb=%s",
            self.server.name,
            imdb_id,
            tmdb_id,
            tvdb_id,
        )

        # Get admin user for full library access
        admin_id = await self.get_admin_user_id()
        if not admin_id:
            logger.error("[%s] Cannot find item by provider - no admin user", self.server.name)
            return None

        # Build search params with available provider IDs
        # Use admin user to access all libraries
        # Exclude BoxSet/collections which share provider IDs with actual media
        params: dict[str, Any] = {
            "recursive": "true",
            "fields": "ProviderIds,Path",
            "excludeItemTypes": "BoxSet,Folder,CollectionFolder",
            "limit": 1,
        }

        # Try each provider ID
        for provider, value in [
            ("Imdb", imdb_id),
            ("Tmdb", tmdb_id),
            ("Tvdb", tvdb_id),
        ]:
            if value:
                params["AnyProviderIdEquals"] = f"{provider}.{value}"
                try:
                    response = await self._request("GET", f"/Users/{admin_id}/Items", params=params)
                    data = response.json()
                    items = data.get("Items", [])
                    if items:
                        logger.debug(
                            "[%s] Found item by %s=%s: %s",
                            self.server.name,
                            provider,
                            value,
                            items[0].get("Id"),
                        )
                        return items[0]
                except httpx.HTTPStatusError as e:
                    logger.debug("[%s] Provider search failed for %s=%s: %s", self.server.name, provider, value, e)
                    continue

        logger.debug("[%s] Item not found by any provider ID", self.server.name)
        return None

    # ========== Playback Progress ==========

    async def update_playback_progress(
        self,
        user_id: str,
        item_id: str,
        position_ticks: int,
    ) -> bool:
        """
        Update playback position for an item.

        Uses UserData endpoint instead of PlayingItems/Progress because:
        - PlayingItems/Progress requires an active playback session
        - Without a session, plugins like PlaybackReporting may crash
        - UserData endpoint updates position without triggering session events
        """
        try:
            await self._request(
                "POST",
                f"/Users/{user_id}/Items/{item_id}/UserData",
                json={"PlaybackPositionTicks": position_ticks},
            )
            logger.debug("[%s] Updated progress: item=%s, ticks=%d", self.server.name, item_id, position_ticks)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to update progress for item=%s: %s", self.server.name, item_id, e)
            return False

    # ========== Watched Status ==========

    async def mark_played(self, user_id: str, item_id: str) -> bool:
        """Mark an item as played/watched."""
        try:
            await self._request(
                "POST",
                f"/Users/{user_id}/PlayedItems/{item_id}",
            )
            logger.debug("[%s] Marked played: item=%s", self.server.name, item_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to mark played: item=%s, error=%s", self.server.name, item_id, e)
            return False

    async def mark_unplayed(self, user_id: str, item_id: str) -> bool:
        """Mark an item as unplayed/unwatched."""
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}/PlayedItems/{item_id}",
            )
            logger.debug("[%s] Marked unplayed: item=%s", self.server.name, item_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to mark unplayed: item=%s, error=%s", self.server.name, item_id, e)
            return False

    # ========== Favorites ==========

    async def add_favorite(self, user_id: str, item_id: str) -> bool:
        """Add item to favorites."""
        try:
            await self._request(
                "POST",
                f"/Users/{user_id}/FavoriteItems/{item_id}",
            )
            logger.debug("[%s] Added favorite: item=%s", self.server.name, item_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to add favorite: item=%s, error=%s", self.server.name, item_id, e)
            return False

    async def remove_favorite(self, user_id: str, item_id: str) -> bool:
        """Remove item from favorites."""
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}/FavoriteItems/{item_id}",
            )
            logger.debug("[%s] Removed favorite: item=%s", self.server.name, item_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to remove favorite: item=%s, error=%s", self.server.name, item_id, e)
            return False

    # ========== Ratings ==========

    async def update_rating(
        self,
        user_id: str,
        item_id: str,
        rating: float,
    ) -> bool:
        """Update user rating for an item (0-10 scale)."""
        try:
            await self._request(
                "POST",
                f"/Users/{user_id}/Items/{item_id}/Rating",
                params={"likes": rating >= 5},  # Jellyfin uses likes/dislikes
            )
            logger.debug("[%s] Updated rating: item=%s, rating=%.1f", self.server.name, item_id, rating)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to update rating: item=%s, error=%s", self.server.name, item_id, e)
            return False

    async def delete_rating(self, user_id: str, item_id: str) -> bool:
        """Delete user rating for an item."""
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}/Items/{item_id}/Rating",
            )
            logger.debug("[%s] Deleted rating: item=%s", self.server.name, item_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to delete rating: item=%s, error=%s", self.server.name, item_id, e)
            return False

    # ========== User Data Updates ==========

    async def update_user_data(
        self,
        user_id: str,
        item_id: str,
        play_count: int | None = None,
        played: bool | None = None,
        last_played_date: str | None = None,
        likes: bool | None = None,
        audio_stream_index: int | None = None,
        subtitle_stream_index: int | None = None,
    ) -> bool:
        """Update user data for an item (play count, likes, stream indices, etc.)."""
        try:
            # Build the update payload - only include non-None values
            update_data: dict[str, Any] = {}
            if play_count is not None:
                update_data["PlayCount"] = play_count
            if played is not None:
                update_data["Played"] = played
            if last_played_date is not None:
                update_data["LastPlayedDate"] = last_played_date
            if likes is not None:
                update_data["Likes"] = likes
            if audio_stream_index is not None:
                update_data["AudioStreamIndex"] = audio_stream_index
            if subtitle_stream_index is not None:
                update_data["SubtitleStreamIndex"] = subtitle_stream_index

            if not update_data:
                logger.debug("[%s] No user data to update for item=%s", self.server.name, item_id)
                return True  # Nothing to update

            await self._request(
                "POST",
                f"/Users/{user_id}/Items/{item_id}/UserData",
                json=update_data,
            )
            logger.debug("[%s] Updated user data: item=%s, data=%s", self.server.name, item_id, update_data)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to update user data: item=%s, error=%s", self.server.name, item_id, e)
            return False

    # ========== User Data (combined) ==========

    async def get_item_info(self, user_id: str, item_id: str) -> dict[str, Any] | None:
        """Get full item information including Path, ProviderIds, etc."""
        try:
            response = await self._request(
                "GET",
                f"/Users/{user_id}/Items/{item_id}",
            )
            item = response.json()
            logger.debug("[%s] Got item info: %s (%s)", self.server.name, item.get("Name"), item_id)
            return item
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to get item info: item=%s, error=%s", self.server.name, item_id, e)
            return None

    async def get_user_data(self, user_id: str, item_id: str) -> dict[str, Any] | None:
        """Get user data for an item (played status, position, favorites, etc.)."""
        try:
            response = await self._request(
                "GET",
                f"/Users/{user_id}/Items/{item_id}",
            )
            data = response.json()
            user_data = data.get("UserData")
            logger.debug("[%s] Got user data for item=%s", self.server.name, item_id)
            return user_data
        except httpx.HTTPStatusError as e:
            logger.error("[%s] Failed to get user data: item=%s, error=%s", self.server.name, item_id, e)
            return None

    # ========== Health Check ==========

    async def health_check(self) -> bool:
        """Check if the server is reachable."""
        try:
            await self._request("GET", "/System/Info/Public")
            logger.debug("[%s] Health check OK", self.server.name)
            return True
        except Exception as e:
            logger.warning("[%s] Health check FAILED: %s", self.server.name, e)
            return False
