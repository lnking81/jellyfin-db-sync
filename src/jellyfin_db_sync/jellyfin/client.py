"""Jellyfin API client for sync operations."""

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
        response = await self._request("GET", "/Users")
        return response.json()

    async def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        """Find a user by username."""
        users = await self.get_users()
        for user in users:
            if user.get("Name", "").lower() == username.lower():
                return user
        return None

    async def get_user_id(self, username: str) -> str | None:
        """Get user ID by username."""
        user = await self.get_user_by_name(username)
        return user.get("Id") if user else None

    async def create_user(self, username: str, password: str | None = None) -> dict[str, Any] | None:
        """
        Create a new user on the server.

        Args:
            username: The username for the new user
            password: Password for the user (None for passwordless servers)

        Returns:
            User data dict if successful, None otherwise
        """
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
            logger.info("Created user '%s' on %s", username, self.server.name)
            return user
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                logger.warning("User '%s' may already exist on %s", username, self.server.name)
            else:
                logger.error("Failed to create user '%s' on %s: %s", username, self.server.name, e)
            return None

    async def delete_user(self, user_id: str) -> bool:
        """
        Delete a user from the server.

        Args:
            user_id: The Jellyfin user ID to delete

        Returns:
            True if deleted successfully
        """
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}",
            )
            logger.info("Deleted user %s from %s", user_id, self.server.name)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Failed to delete user %s from %s: %s", user_id, self.server.name, e)
            return False

    # ========== Item Lookup ==========

    async def find_item_by_path(
        self,
        user_id: str,
        path: str,
    ) -> dict[str, Any] | None:
        """
        Find an item by its file path.

        This is the most reliable method when all Jellyfin instances
        share the same storage (NFS/CIFS mount). Works for all content
        including home photos/videos that don't have provider IDs.
        """
        try:
            response = await self._request(
                "GET",
                "/Items",
                params={
                    "userId": user_id,
                    "path": path,
                    "recursive": "true",
                    "limit": 1,
                },
            )
            data = response.json()
            items = data.get("Items", [])
            if items:
                return items[0]
        except httpx.HTTPStatusError as e:
            logger.debug("Item not found by path %s: %s", path, e)
        return None

    async def find_item_by_provider_id(
        self,
        user_id: str,
        imdb_id: str | None = None,
        tmdb_id: str | None = None,
        tvdb_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find an item by external provider ID."""
        # Build search params with available provider IDs
        params: dict[str, Any] = {
            "userId": user_id,
            "recursive": "true",
            "fields": "ProviderIds",
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
                    response = await self._request("GET", "/Items", params=params)
                    data = response.json()
                    items = data.get("Items", [])
                    if items:
                        return items[0]
                except httpx.HTTPStatusError:
                    continue

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
            logger.debug("Updated progress on %s: item=%s, position=%s", self.server.name, item_id, position_ticks)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Failed to update progress: %s", e)
            return False

    # ========== Watched Status ==========

    async def mark_played(self, user_id: str, item_id: str) -> bool:
        """Mark an item as played/watched."""
        try:
            await self._request(
                "POST",
                f"/Users/{user_id}/PlayedItems/{item_id}",
            )
            logger.debug("Marked as played on %s: item=%s", self.server.name, item_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Failed to mark as played: %s", e)
            return False

    async def mark_unplayed(self, user_id: str, item_id: str) -> bool:
        """Mark an item as unplayed/unwatched."""
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}/PlayedItems/{item_id}",
            )
            logger.debug("Marked as unplayed on %s: item=%s", self.server.name, item_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Failed to mark as unplayed: %s", e)
            return False

    # ========== Favorites ==========

    async def add_favorite(self, user_id: str, item_id: str) -> bool:
        """Add item to favorites."""
        try:
            await self._request(
                "POST",
                f"/Users/{user_id}/FavoriteItems/{item_id}",
            )
            logger.debug("Added to favorites on %s: item=%s", self.server.name, item_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Failed to add favorite: %s", e)
            return False

    async def remove_favorite(self, user_id: str, item_id: str) -> bool:
        """Remove item from favorites."""
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}/FavoriteItems/{item_id}",
            )
            logger.debug("Removed from favorites on %s: item=%s", self.server.name, item_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Failed to remove favorite: %s", e)
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
            logger.debug("Updated rating on %s: item=%s, rating=%s", self.server.name, item_id, rating)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Failed to update rating: %s", e)
            return False

    async def delete_rating(self, user_id: str, item_id: str) -> bool:
        """Delete user rating for an item."""
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}/Items/{item_id}/Rating",
            )
            logger.debug("Deleted rating on %s: item=%s", self.server.name, item_id)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Failed to delete rating: %s", e)
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
                return True  # Nothing to update

            await self._request(
                "POST",
                f"/Users/{user_id}/Items/{item_id}/UserData",
                json=update_data,
            )
            logger.debug("Updated user data on %s: item=%s, data=%s", self.server.name, item_id, update_data)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Failed to update user data: %s", e)
            return False

    # ========== User Data (combined) ==========

    async def get_item_info(self, user_id: str, item_id: str) -> dict[str, Any] | None:
        """Get full item information including Path, ProviderIds, etc."""
        try:
            response = await self._request(
                "GET",
                f"/Users/{user_id}/Items/{item_id}",
            )
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error("Failed to get item info: %s", e)
            return None

    async def get_user_data(self, user_id: str, item_id: str) -> dict[str, Any] | None:
        """Get user data for an item (played status, position, favorites, etc.)."""
        try:
            response = await self._request(
                "GET",
                f"/Users/{user_id}/Items/{item_id}",
            )
            data = response.json()
            return data.get("UserData")
        except httpx.HTTPStatusError as e:
            logger.error("Failed to get user data: %s", e)
            return None

    # ========== Health Check ==========

    async def health_check(self) -> bool:
        """Check if the server is reachable."""
        try:
            await self._request("GET", "/System/Info/Public")
            return True
        except Exception as e:
            logger.warning("Health check failed for %s: %s", self.server.name, e)
            return False
