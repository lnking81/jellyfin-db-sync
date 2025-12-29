"""Tests for Jellyfin API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from jellyfin_db_sync.config import ServerConfig
from jellyfin_db_sync.jellyfin.client import JellyfinClient


@pytest.fixture
def server_config():
    """Create test server configuration."""
    return ServerConfig(
        name="test-server",
        url="http://jellyfin:8096",
        api_key="test-api-key",
    )


@pytest.fixture
def client(server_config):
    """Create Jellyfin client for testing."""
    return JellyfinClient(server_config)


class TestClientInitialization:
    """Test client initialization."""

    def test_client_base_url(self, client, server_config):
        """Test that base URL is set correctly."""
        assert client.base_url == "http://jellyfin:8096"
        assert client.server == server_config

    def test_client_headers(self, client):
        """Test that headers include API key."""
        assert "X-Emby-Token" in client.headers
        assert client.headers["X-Emby-Token"] == "test-api-key"
        assert client.headers["Content-Type"] == "application/json"

    def test_client_strips_trailing_slash(self):
        """Test that trailing slash is stripped from URL."""
        config = ServerConfig(
            name="test",
            url="http://jellyfin:8096/",
            api_key="key",
        )
        client = JellyfinClient(config)
        assert client.base_url == "http://jellyfin:8096"


class TestUserOperations:
    """Test user-related API operations."""

    @pytest.mark.asyncio
    async def test_get_users(self, client):
        """Test getting all users."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"Id": "user-1", "Name": "alice"},
            {"Id": "user-2", "Name": "bob"},
        ]
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response
            users = await client.get_users()

        mock_request.assert_called_once_with("GET", "/Users")
        assert len(users) == 2
        assert users[0]["Name"] == "alice"

    @pytest.mark.asyncio
    async def test_get_user_by_name(self, client):
        """Test finding user by username."""
        with patch.object(client, "get_users", new_callable=AsyncMock) as mock_get_users:
            mock_get_users.return_value = [
                {"Id": "user-1", "Name": "Alice"},
                {"Id": "user-2", "Name": "Bob"},
            ]

            user = await client.get_user_by_name("bob")

        assert user is not None
        assert user["Id"] == "user-2"
        assert user["Name"] == "Bob"

    @pytest.mark.asyncio
    async def test_get_user_by_name_case_insensitive(self, client):
        """Test that username lookup is case-insensitive."""
        with patch.object(client, "get_users", new_callable=AsyncMock) as mock_get_users:
            mock_get_users.return_value = [
                {"Id": "user-1", "Name": "Alice"},
            ]

            user = await client.get_user_by_name("ALICE")

        assert user is not None
        assert user["Name"] == "Alice"

    @pytest.mark.asyncio
    async def test_get_user_by_name_not_found(self, client):
        """Test user not found returns None."""
        with patch.object(client, "get_users", new_callable=AsyncMock) as mock_get_users:
            mock_get_users.return_value = [
                {"Id": "user-1", "Name": "Alice"},
            ]

            user = await client.get_user_by_name("nonexistent")

        assert user is None

    @pytest.mark.asyncio
    async def test_get_user_id(self, client):
        """Test getting user ID by username."""
        with patch.object(client, "get_user_by_name", new_callable=AsyncMock) as mock:
            mock.return_value = {"Id": "user-123", "Name": "testuser"}

            user_id = await client.get_user_id("testuser")

        assert user_id == "user-123"

    @pytest.mark.asyncio
    async def test_get_user_id_not_found(self, client):
        """Test getting user ID when user doesn't exist."""
        with patch.object(client, "get_user_by_name", new_callable=AsyncMock) as mock:
            mock.return_value = None

            user_id = await client.get_user_id("nonexistent")

        assert user_id is None


class TestItemLookup:
    """Test item lookup operations."""

    @pytest.mark.asyncio
    async def test_find_item_by_path(self, client):
        """Test finding item by file path."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "Items": [{"Id": "item-123", "Name": "Test Movie", "Path": "/movies/test.mkv"}],
            "TotalRecordCount": 1,
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            item = await client.find_item_by_path("user-1", "/movies/test.mkv")

        assert item is not None
        assert item["Id"] == "item-123"
        mock_request.assert_called_once()
        call_kwargs = mock_request.call_args
        assert call_kwargs[1]["params"]["path"] == "/movies/test.mkv"

    @pytest.mark.asyncio
    async def test_find_item_by_path_not_found(self, client):
        """Test item not found by path."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"Items": [], "TotalRecordCount": 0}
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            item = await client.find_item_by_path("user-1", "/movies/nonexistent.mkv")

        assert item is None

    @pytest.mark.asyncio
    async def test_find_item_by_path_http_error(self, client):
        """Test handling HTTP error when searching by path."""
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = httpx.HTTPStatusError(
                "Not Found",
                request=MagicMock(),
                response=MagicMock(status_code=404),
            )

            item = await client.find_item_by_path("user-1", "/movies/error.mkv")

        assert item is None

    @pytest.mark.asyncio
    async def test_find_item_by_provider_id_imdb(self, client):
        """Test finding item by IMDB ID."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "Items": [{"Id": "item-123", "Name": "Inception"}],
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            item = await client.find_item_by_provider_id(
                user_id="user-1",
                imdb_id="tt1375666",
            )

        assert item is not None
        assert item["Id"] == "item-123"

    @pytest.mark.asyncio
    async def test_find_item_by_provider_id_fallback(self, client):
        """Test fallback through provider IDs."""
        mock_response_empty = MagicMock()
        mock_response_empty.json.return_value = {"Items": []}
        mock_response_empty.raise_for_status = MagicMock()

        mock_response_found = MagicMock()
        mock_response_found.json.return_value = {
            "Items": [{"Id": "item-123", "Name": "Movie"}],
        }
        mock_response_found.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            # First call (IMDB) returns empty, second call (TMDB) returns item
            mock_request.side_effect = [mock_response_empty, mock_response_found]

            item = await client.find_item_by_provider_id(
                user_id="user-1",
                imdb_id="tt0000000",
                tmdb_id="12345",
            )

        assert item is not None
        assert mock_request.call_count == 2


class TestPlaybackProgress:
    """Test playback progress operations."""

    @pytest.mark.asyncio
    async def test_update_playback_progress(self, client):
        """Test updating playback progress via UserData endpoint."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.update_playback_progress(
                user_id="user-1",
                item_id="item-123",
                position_ticks=36000000000,
            )

        assert result is True
        mock_request.assert_called_once_with(
            "POST",
            "/Users/user-1/Items/item-123/UserData",
            json={"PlaybackPositionTicks": 36000000000},
        )

    @pytest.mark.asyncio
    async def test_update_playback_progress_error(self, client):
        """Test handling error when updating progress."""
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = httpx.HTTPStatusError(
                "Server Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            )

            result = await client.update_playback_progress(
                user_id="user-1",
                item_id="item-123",
                position_ticks=36000000000,
            )

        assert result is False


class TestWatchedStatus:
    """Test watched status operations."""

    @pytest.mark.asyncio
    async def test_mark_played(self, client):
        """Test marking item as played."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.mark_played("user-1", "item-123")

        assert result is True
        mock_request.assert_called_once_with(
            "POST",
            "/Users/user-1/PlayedItems/item-123",
        )

    @pytest.mark.asyncio
    async def test_mark_unplayed(self, client):
        """Test marking item as unplayed."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.mark_unplayed("user-1", "item-123")

        assert result is True
        mock_request.assert_called_once_with(
            "DELETE",
            "/Users/user-1/PlayedItems/item-123",
        )

    @pytest.mark.asyncio
    async def test_mark_played_error(self, client):
        """Test handling error when marking as played."""
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = httpx.HTTPStatusError(
                "Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            )

            result = await client.mark_played("user-1", "item-123")

        assert result is False


class TestFavorites:
    """Test favorite operations."""

    @pytest.mark.asyncio
    async def test_add_favorite(self, client):
        """Test adding item to favorites."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.add_favorite("user-1", "item-123")

        assert result is True
        mock_request.assert_called_once_with(
            "POST",
            "/Users/user-1/FavoriteItems/item-123",
        )

    @pytest.mark.asyncio
    async def test_remove_favorite(self, client):
        """Test removing item from favorites."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.remove_favorite("user-1", "item-123")

        assert result is True
        mock_request.assert_called_once_with(
            "DELETE",
            "/Users/user-1/FavoriteItems/item-123",
        )


class TestRatings:
    """Test rating operations."""

    @pytest.mark.asyncio
    async def test_update_rating(self, client):
        """Test updating item rating."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.update_rating("user-1", "item-123", 8.5)

        assert result is True
        call_args = mock_request.call_args
        assert call_args[1]["params"]["likes"] is True  # 8.5 >= 5

    @pytest.mark.asyncio
    async def test_update_rating_dislike(self, client):
        """Test updating item rating with dislike."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.update_rating("user-1", "item-123", 3.0)

        assert result is True
        call_args = mock_request.call_args
        assert call_args[1]["params"]["likes"] is False  # 3.0 < 5

    @pytest.mark.asyncio
    async def test_delete_rating(self, client):
        """Test deleting item rating."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.delete_rating("user-1", "item-123")

        assert result is True
        mock_request.assert_called_once_with(
            "DELETE",
            "/Users/user-1/Items/item-123/Rating",
        )


class TestUserData:
    """Test user data operations."""

    @pytest.mark.asyncio
    async def test_get_user_data(self, client):
        """Test getting user data for an item."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "Id": "item-123",
            "Name": "Test Movie",
            "UserData": {
                "PlaybackPositionTicks": 36000000000,
                "Played": True,
                "IsFavorite": False,
            },
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            user_data = await client.get_user_data("user-1", "item-123")

        assert user_data is not None
        assert user_data["Played"] is True
        assert user_data["IsFavorite"] is False

    @pytest.mark.asyncio
    async def test_get_user_data_error(self, client):
        """Test handling error when getting user data."""
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = httpx.HTTPStatusError(
                "Not Found",
                request=MagicMock(),
                response=MagicMock(status_code=404),
            )

            user_data = await client.get_user_data("user-1", "item-123")

        assert user_data is None


class TestHealthCheck:
    """Test health check operation."""

    @pytest.mark.asyncio
    async def test_health_check_success(self, client):
        """Test successful health check."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.health_check()

        assert result is True
        mock_request.assert_called_once_with("GET", "/System/Info/Public")

    @pytest.mark.asyncio
    async def test_health_check_failure(self, client):
        """Test failed health check."""
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Connection refused")

            result = await client.health_check()

        assert result is False
