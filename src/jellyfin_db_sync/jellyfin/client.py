"""Jellyfin API client for sync operations."""

import logging
from typing import Any

import httpx

from ..config import ServerConfig

logger = logging.getLogger(__name__)


class JellyfinClient:
    """Async client for Jellyfin API."""

    def __init__(self, server: ServerConfig):
        self.server = server
        self.base_url = server.url.rstrip("/")
        self.headers = {
            "X-Emby-Token": server.api_key,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated request to the Jellyfin API."""
        url = f"{self.base_url}{endpoint}"
        async with httpx.AsyncClient() as client:
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
            logger.debug(f"Item not found by path {path}: {e}")
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
        """Update playback position for an item."""
        try:
            await self._request(
                "POST",
                f"/Users/{user_id}/PlayingItems/{item_id}/Progress",
                params={"positionTicks": position_ticks},
            )
            logger.info(f"Updated progress on {self.server.name}: item={item_id}, position={position_ticks}")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to update progress: {e}")
            return False

    # ========== Watched Status ==========

    async def mark_played(self, user_id: str, item_id: str) -> bool:
        """Mark an item as played/watched."""
        try:
            await self._request(
                "POST",
                f"/Users/{user_id}/PlayedItems/{item_id}",
            )
            logger.info(f"Marked as played on {self.server.name}: item={item_id}")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to mark as played: {e}")
            return False

    async def mark_unplayed(self, user_id: str, item_id: str) -> bool:
        """Mark an item as unplayed/unwatched."""
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}/PlayedItems/{item_id}",
            )
            logger.info(f"Marked as unplayed on {self.server.name}: item={item_id}")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to mark as unplayed: {e}")
            return False

    # ========== Favorites ==========

    async def add_favorite(self, user_id: str, item_id: str) -> bool:
        """Add item to favorites."""
        try:
            await self._request(
                "POST",
                f"/Users/{user_id}/FavoriteItems/{item_id}",
            )
            logger.info(f"Added to favorites on {self.server.name}: item={item_id}")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to add favorite: {e}")
            return False

    async def remove_favorite(self, user_id: str, item_id: str) -> bool:
        """Remove item from favorites."""
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}/FavoriteItems/{item_id}",
            )
            logger.info(f"Removed from favorites on {self.server.name}: item={item_id}")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to remove favorite: {e}")
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
            logger.info(f"Updated rating on {self.server.name}: item={item_id}, rating={rating}")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to update rating: {e}")
            return False

    async def delete_rating(self, user_id: str, item_id: str) -> bool:
        """Delete user rating for an item."""
        try:
            await self._request(
                "DELETE",
                f"/Users/{user_id}/Items/{item_id}/Rating",
            )
            logger.info(f"Deleted rating on {self.server.name}: item={item_id}")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to delete rating: {e}")
            return False

    # ========== User Data (combined) ==========

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
            logger.error(f"Failed to get user data: {e}")
            return None

    # ========== Health Check ==========

    async def health_check(self) -> bool:
        """Check if the server is reachable."""
        try:
            await self._request("GET", "/System/Info/Public")
            return True
        except Exception as e:
            logger.warning(f"Health check failed for {self.server.name}: {e}")
            return False
