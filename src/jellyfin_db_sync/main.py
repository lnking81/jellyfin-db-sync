"""Main entry point for jellyfin-db-sync."""

import logging
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .api import health_router, status_router, webhook_router
from .config import get_config, load_config
from .database import close_db, get_db
from .sync import SyncEngine
from .web import get_static_files, ui_router


def setup_logging(level: str = "INFO") -> None:
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Silence noisy third-party loggers
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def init_config() -> None:
    """Initialize configuration from file.

    Loads config from CONFIG_PATH env var, /config/config.yaml, or ./config.yaml.
    Sets up logging based on config.
    """
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")

    # Allow local development with config.yaml in current directory
    if not Path(config_path).exists():
        local_config = Path("config.yaml")
        if local_config.exists():
            config_path = str(local_config)
        else:
            print(f"Error: Configuration file not found: {config_path}")
            print("Create a config.yaml file or set CONFIG_PATH environment variable")
            sys.exit(1)

    # Load configuration
    config = load_config(config_path)

    # Setup logging
    setup_logging(config.logging.level)

    logger = logging.getLogger(__name__)
    logger.info("Loaded configuration from %s", config_path)
    logger.info("Configured servers: %s", [s.name for s in config.servers])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    logger = logging.getLogger(__name__)

    # Startup
    logger.info("Starting jellyfin-db-sync...")

    # Initialize database
    await get_db()
    logger.info("Database initialized")

    # Sync user mappings on startup
    config = get_config()
    engine = SyncEngine(config)

    try:
        await engine.sync_all_users()
        logger.info("User mappings synchronized")
    except Exception as e:
        logger.warning("Failed to sync users on startup: %s", e)

    # Health check all servers
    health = await engine.health_check_all()
    for server_name, is_healthy in health.items():
        status = "healthy" if is_healthy else "unhealthy"
        logger.info("Server %s: %s", server_name, status)

    # Start the background worker for processing pending events
    worker_interval = config.sync.worker_interval_seconds
    await engine.start_worker(interval_seconds=worker_interval)
    logger.info("Sync worker started (interval: %ss)", worker_interval)

    # Store engine in app state for access by routers
    app.state.engine = engine

    yield

    # Shutdown
    logger.info("Shutting down jellyfin-db-sync...")
    await engine.stop_worker()
    await close_db()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Initialize config before creating app
    init_config()

    app = FastAPI(
        title="jellyfin-db-sync",
        description="Bidirectional sync service for multiple Jellyfin instances",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Mount static files
    app.mount("/static", get_static_files(), name="static")

    # Include routers
    app.include_router(ui_router)  # Dashboard at /
    app.include_router(health_router)  # /healthz, /readyz
    app.include_router(status_router)  # /api/status, /api/servers, etc.
    app.include_router(webhook_router)  # /webhook/{server_name}

    return app


def main() -> None:
    """Main entry point."""
    app = create_app()
    config = get_config()

    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=config.logging.level.lower(),
    )


if __name__ == "__main__":
    main()
