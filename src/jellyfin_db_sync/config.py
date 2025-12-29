"""Configuration models for jellyfin-db-sync."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    """Configuration for a single Jellyfin server."""

    name: str
    url: str
    api_key: str
    passwordless: bool = False


class SyncConfig(BaseModel):
    """Sync behavior configuration."""

    playback_progress: bool = True
    watched_status: bool = True
    favorites: bool = True
    ratings: bool = True
    playlists: bool = True
    likes: bool = True  # Thumbs up/down
    play_count: bool = True  # Number of times played
    last_played_date: bool = True  # Last played timestamp
    audio_stream: bool = True  # Selected audio track
    subtitle_stream: bool = True  # Selected subtitle track
    progress_debounce_seconds: int = 30
    worker_interval_seconds: float = 5.0
    max_retries: int = 5
    dry_run: bool = False  # Prevent API calls to target servers (webhooks still enqueued)


class DatabaseConfig(BaseModel):
    """Database configuration."""

    path: str = "/data/jellyfin-db-sync.db"
    journal_mode: str = "WAL"  # WAL, DELETE, TRUNCATE, MEMORY, OFF


class ServerSettings(BaseModel):
    """HTTP server configuration."""

    host: str = "0.0.0.0"
    port: int = 8080


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = "INFO"


class PathSyncPolicy(BaseModel):
    """Policy for syncing items by path prefix.

    Controls retry behavior when an item is not found on target server.
    Useful when libraries are still being imported.
    """

    prefix: str  # Path prefix to match (e.g., /mnt/nfs/movies)
    absent_retry_count: int = 0  # -1 = infinite, 0 = no retry, >0 = specific count
    retry_delay_seconds: int = 300  # Delay between retries (5 min default)


class Config(BaseModel):
    """Root configuration model."""

    servers: list[ServerConfig] = Field(default_factory=list)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    path_sync_policy: list[PathSyncPolicy] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Config":
        """Load configuration from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data or {})

    def get_server(self, name: str) -> ServerConfig | None:
        """Get server config by name."""
        for server in self.servers:
            if server.name == name:
                return server
        return None

    def get_other_servers(self, exclude_name: str) -> list[ServerConfig]:
        """Get all servers except the specified one."""
        return [s for s in self.servers if s.name != exclude_name]

    def get_path_policy(self, path: str | None) -> PathSyncPolicy | None:
        """Get the path sync policy for a given path (longest prefix match)."""
        if not path:
            return None

        matching_policy: PathSyncPolicy | None = None
        max_prefix_len = 0

        for policy in self.path_sync_policy:
            if path.startswith(policy.prefix) and len(policy.prefix) > max_prefix_len:
                matching_policy = policy
                max_prefix_len = len(policy.prefix)

        return matching_policy


# Global config instance
_config: Config | None = None


def get_config() -> Config:
    """Get the global configuration instance."""
    if _config is None:
        raise RuntimeError("Configuration not loaded. Call load_config() first.")
    return _config


def load_config(path: str | Path) -> Config:
    """Load configuration from file and set as global."""
    global _config
    _config = Config.from_yaml(path)
    return _config
