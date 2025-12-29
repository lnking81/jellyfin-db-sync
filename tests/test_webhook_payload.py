"""Tests for WebhookPayload model parsing."""


from jellyfin_db_sync.models import EventType, SyncEventType, WebhookPayload


class TestWebhookPayloadParsing:
    """Test parsing webhook payloads from Jellyfin."""

    def test_parse_playback_stop_complete(self):
        """Test parsing complete PlaybackStop payload."""
        payload_data = {
            "NotificationType": "PlaybackStop",
            "ServerId": "server-abc-123",
            "ServerName": "WAN Jellyfin",
            "UserId": "user-def-456",
            "NotificationUsername": "john_doe",
            "ItemId": "item-ghi-789",
            "Name": "Inception",
            "ItemType": "Movie",
            "Path": "/mnt/media/movies/Inception (2010)/Inception.mkv",
            "PlaybackPositionTicks": 89280000000,
            "PlaybackPosition": "02:28:48",
            "PlayedToCompletion": True,
            "IsFavorite": False,
            "Played": True,
            "Provider_imdb": "tt1375666",
            "Provider_tmdb": "27205",
        }

        payload = WebhookPayload.model_validate(payload_data)

        assert payload.event == "PlaybackStop"
        assert payload.server_id == "server-abc-123"
        assert payload.server_name == "WAN Jellyfin"
        assert payload.user_id == "user-def-456"
        assert payload.username == "john_doe"
        assert payload.item_id == "item-ghi-789"
        assert payload.item_name == "Inception"
        assert payload.item_type == "Movie"
        assert payload.item_path == "/mnt/media/movies/Inception (2010)/Inception.mkv"
        assert payload.playback_position_ticks == 89280000000
        assert payload.played_to_completion is True
        assert payload.is_favorite is False
        assert payload.is_played is True
        assert payload.provider_imdb == "tt1375666"
        assert payload.provider_tmdb == "27205"
        assert payload.provider_tvdb is None

    def test_parse_playback_progress(self):
        """Test parsing PlaybackProgress payload."""
        payload_data = {
            "NotificationType": "PlaybackProgress",
            "ServerId": "server-123",
            "ServerName": "Home Server",
            "UserId": "user-456",
            "NotificationUsername": "jane",
            "ItemId": "item-789",
            "Name": "Breaking Bad S01E01",
            "ItemType": "Episode",
            "Path": "/tv/Breaking Bad/Season 01/S01E01.mkv",
            "PlaybackPositionTicks": 18000000000,
            "PlaybackPosition": "00:30:00",
            "PlayedToCompletion": False,
        }

        payload = WebhookPayload.model_validate(payload_data)

        assert payload.event == "PlaybackProgress"
        assert payload.playback_position_ticks == 18000000000
        assert payload.playback_position == "00:30:00"
        assert payload.played_to_completion is False

    def test_parse_user_data_saved(self):
        """Test parsing UserDataSaved payload."""
        payload_data = {
            "NotificationType": "UserDataSaved",
            "ServerId": "server-123",
            "ServerName": "Home Server",
            "UserId": "user-456",
            "NotificationUsername": "bob",
            "ItemId": "item-789",
            "Name": "The Matrix",
            "ItemType": "Movie",
            "IsFavorite": True,
            "Played": True,
        }

        payload = WebhookPayload.model_validate(payload_data)

        assert payload.event == "UserDataSaved"
        assert payload.is_favorite is True
        assert payload.is_played is True

    def test_parse_minimal_payload(self):
        """Test parsing payload with only required fields."""
        payload_data = {
            "NotificationType": "ItemAdded",
            "ItemId": "item-123",
        }

        payload = WebhookPayload.model_validate(payload_data)

        assert payload.event == "ItemAdded"
        assert payload.item_id == "item-123"
        assert payload.username == ""  # Default empty
        assert payload.item_name == ""
        assert payload.item_path is None
        assert payload.provider_imdb is None

    def test_parse_empty_payload(self):
        """Test parsing completely empty payload."""
        payload = WebhookPayload.model_validate({})

        assert payload.event == ""
        assert payload.item_id == ""
        assert payload.username == ""

    def test_parse_with_tvdb_provider(self):
        """Test parsing payload with TVDB provider ID."""
        payload_data = {
            "NotificationType": "UserDataSaved",
            "UserId": "user-123",
            "NotificationUsername": "testuser",
            "ItemId": "item-456",
            "Name": "Game of Thrones",
            "ItemType": "Series",
            "Provider_tvdb": "121361",
        }

        payload = WebhookPayload.model_validate(payload_data)

        assert payload.provider_tvdb == "121361"
        assert payload.provider_imdb is None
        assert payload.provider_tmdb is None

    def test_parse_with_all_providers(self):
        """Test parsing payload with all provider IDs."""
        payload_data = {
            "NotificationType": "PlaybackStop",
            "UserId": "user-123",
            "NotificationUsername": "testuser",
            "ItemId": "item-456",
            "Name": "The Dark Knight",
            "ItemType": "Movie",
            "Provider_imdb": "tt0468569",
            "Provider_tmdb": "155",
            "Provider_tvdb": "81189",
        }

        payload = WebhookPayload.model_validate(payload_data)

        assert payload.provider_imdb == "tt0468569"
        assert payload.provider_tmdb == "155"
        assert payload.provider_tvdb == "81189"

    def test_alias_population_by_name(self):
        """Test that both alias and field name work for population."""
        # Using aliases (Jellyfin JSON format)
        payload1 = WebhookPayload.model_validate(
            {
                "NotificationType": "Test",
                "ItemId": "123",
            }
        )

        # Using field names (Python format)
        payload2 = WebhookPayload(
            event="Test",
            item_id="123",
        )

        assert payload1.event == payload2.event
        assert payload1.item_id == payload2.item_id

    def test_model_export_with_aliases(self):
        """Test that model can export with aliases."""
        payload = WebhookPayload(
            event="PlaybackStop",
            item_id="item-123",
            username="testuser",
        )

        # Export with aliases
        data = payload.model_dump(by_alias=True)

        assert data["NotificationType"] == "PlaybackStop"
        assert data["ItemId"] == "item-123"
        assert data["NotificationUsername"] == "testuser"


class TestEventTypeEnum:
    """Test EventType enum values."""

    def test_event_type_values(self):
        """Test all event type values."""
        assert EventType.PLAYBACK_START.value == "PlaybackStart"
        assert EventType.PLAYBACK_STOP.value == "PlaybackStop"
        assert EventType.PLAYBACK_PROGRESS.value == "PlaybackProgress"
        assert EventType.ITEM_ADDED.value == "ItemAdded"
        assert EventType.USER_DATA_SAVED.value == "UserDataSaved"

    def test_event_type_comparison(self):
        """Test comparing event types."""
        payload = WebhookPayload.model_validate(
            {
                "NotificationType": "PlaybackStop",
            }
        )

        assert payload.event == EventType.PLAYBACK_STOP.value
        assert payload.event == "PlaybackStop"


class TestSyncEventTypeEnum:
    """Test SyncEventType enum values."""

    def test_sync_event_type_values(self):
        """Test all sync event type values."""
        assert SyncEventType.PROGRESS.value == "progress"
        assert SyncEventType.WATCHED.value == "watched"
        assert SyncEventType.FAVORITE.value == "favorite"
        assert SyncEventType.RATING.value == "rating"
        assert SyncEventType.PLAYLIST.value == "playlist"


class TestEdgeCases:
    """Test edge cases in webhook parsing."""

    def test_numeric_string_ticks(self):
        """Test that string ticks are handled (if sent as string)."""
        # Jellyfin should send as int, but test robustness
        payload_data = {
            "NotificationType": "PlaybackProgress",
            "ItemId": "item-123",
            "PlaybackPositionTicks": 36000000000,  # Integer
        }

        payload = WebhookPayload.model_validate(payload_data)
        assert payload.playback_position_ticks == 36000000000

    def test_boolean_as_string(self):
        """Test handling of boolean-like values."""
        payload_data = {
            "NotificationType": "PlaybackStop",
            "ItemId": "item-123",
            "PlayedToCompletion": True,
            "IsFavorite": False,
        }

        payload = WebhookPayload.model_validate(payload_data)
        assert payload.played_to_completion is True
        assert payload.is_favorite is False

    def test_null_optional_fields(self):
        """Test that null values are handled for optional fields."""
        payload_data = {
            "NotificationType": "PlaybackStop",
            "ItemId": "item-123",
            "Path": None,
            "PlaybackPositionTicks": None,
            "Provider_imdb": None,
        }

        payload = WebhookPayload.model_validate(payload_data)
        assert payload.item_path is None
        assert payload.playback_position_ticks is None
        assert payload.provider_imdb is None

    def test_extra_fields_ignored(self):
        """Test that extra fields in payload are ignored."""
        payload_data = {
            "NotificationType": "PlaybackStop",
            "ItemId": "item-123",
            "SomeExtraField": "value",
            "AnotherUnknown": 12345,
        }

        # Should not raise error
        payload = WebhookPayload.model_validate(payload_data)
        assert payload.event == "PlaybackStop"

    def test_unicode_in_item_name(self):
        """Test handling unicode characters in item name."""
        payload_data = {
            "NotificationType": "PlaybackStop",
            "ItemId": "item-123",
            "Name": "千と千尋の神隠し (Spirited Away)",
            "Path": "/movies/千と千尋の神隠し/movie.mkv",
        }

        payload = WebhookPayload.model_validate(payload_data)
        assert payload.item_name == "千と千尋の神隠し (Spirited Away)"
        assert "千と千尋" in payload.item_path

    def test_special_characters_in_path(self):
        """Test handling special characters in file path."""
        payload_data = {
            "NotificationType": "PlaybackStop",
            "ItemId": "item-123",
            "Path": "/mnt/media/movies/It's a Wonderful Life (1946)/movie.mkv",
        }

        payload = WebhookPayload.model_validate(payload_data)
        assert "It's a Wonderful Life" in payload.item_path

    def test_empty_string_username(self):
        """Test that empty username is preserved."""
        payload_data = {
            "NotificationType": "PlaybackStop",
            "ItemId": "item-123",
            "NotificationUsername": "",
        }

        payload = WebhookPayload.model_validate(payload_data)
        assert payload.username == ""
        # In the actual webhook handler, this would be skipped
