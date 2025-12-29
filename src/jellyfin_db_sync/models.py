"""Data models for jellyfin-db-sync."""

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """Jellyfin webhook event types we handle."""

    PLAYBACK_START = "PlaybackStart"
    PLAYBACK_STOP = "PlaybackStop"
    PLAYBACK_PROGRESS = "PlaybackProgress"
    ITEM_ADDED = "ItemAdded"
    USER_DATA_SAVED = "UserDataSaved"
    # More events can be added as needed


class SyncEventType(str, Enum):
    """Types of sync operations."""

    PROGRESS = "progress"
    WATCHED = "watched"
    FAVORITE = "favorite"
    RATING = "rating"
    PLAYLIST = "playlist"


class UserMapping(BaseModel):
    """Mapping of a user across Jellyfin servers."""

    id: int | None = None
    username: str
    server_name: str
    jellyfin_user_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WebhookPayload(BaseModel):
    """Incoming webhook payload from Jellyfin."""

    # Common fields
    event: str = Field(alias="NotificationType", default="")
    server_id: str = Field(alias="ServerId", default="")
    server_name: str = Field(alias="ServerName", default="")

    # User info
    user_id: str = Field(alias="UserId", default="")
    username: str = Field(alias="NotificationUsername", default="")

    # Item info
    item_id: str = Field(alias="ItemId", default="")
    item_name: str = Field(alias="Name", default="")
    item_type: str = Field(alias="ItemType", default="")
    item_path: str | None = Field(alias="Path", default=None)  # File path on storage

    # Playback info
    playback_position_ticks: int | None = Field(alias="PlaybackPositionTicks", default=None)
    playback_position: str | None = Field(alias="PlaybackPosition", default=None)
    played_to_completion: bool = Field(alias="PlayedToCompletion", default=False)

    # User data
    is_favorite: bool = Field(alias="IsFavorite", default=False)
    is_played: bool = Field(alias="Played", default=False)

    # Provider IDs (for matching items across servers)
    provider_imdb: str | None = Field(alias="Provider_imdb", default=None)
    provider_tmdb: str | None = Field(alias="Provider_tmdb", default=None)
    provider_tvdb: str | None = Field(alias="Provider_tvdb", default=None)

    model_config = {"populate_by_name": True}


class SyncEvent(BaseModel):
    """Internal event for sync operations."""

    event_type: SyncEventType
    source_server: str
    username: str
    item_id: str
    item_name: str

    # Primary matching: file path (works for all content including home media)
    item_path: str | None = None

    # Fallback matching: Provider IDs (only for movies/series from public DBs)
    provider_imdb: str | None = None
    provider_tmdb: str | None = None
    provider_tvdb: str | None = None

    # Event-specific data
    position_ticks: int | None = None
    is_played: bool | None = None
    is_favorite: bool | None = None
    rating: float | None = None

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SyncResult(BaseModel):
    """Result of a sync operation."""

    success: bool
    target_server: str
    event_type: SyncEventType
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PendingEventStatus(str, Enum):
    """Status of a pending sync event."""

    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"  # Will retry
    WAITING_FOR_ITEM = "waiting_for_item"  # Item not found, waiting for import


class PendingEvent(BaseModel):
    """Event waiting to be synced (WAL entry)."""

    id: int | None = None
    event_type: SyncEventType
    source_server: str
    target_server: str
    username: str
    user_id: str  # Source server user ID

    # Item info
    item_id: str
    item_name: str

    # Primary matching: file path (works for all content including home media)
    item_path: str | None = None

    # Fallback matching: Provider IDs (only for movies/series from public DBs)
    provider_imdb: str | None = None
    provider_tmdb: str | None = None
    provider_tvdb: str | None = None

    # Event-specific data (JSON serialized)
    event_data: str = "{}"

    # Status tracking
    status: PendingEventStatus = PendingEventStatus.PENDING
    retry_count: int = 0
    max_retries: int = 5
    last_error: str | None = None

    # Item not found tracking (separate from general retries)
    item_not_found_count: int = 0  # How many times item was not found
    item_not_found_max: int = 0  # Max retries from policy (-1 = infinite)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    next_retry_at: datetime | None = None
