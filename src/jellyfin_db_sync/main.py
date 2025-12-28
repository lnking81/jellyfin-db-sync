"""Main entry point for jellyfin-db-sync."""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, Request

from .api import health_router, status_router, webhook_router
from .config import get_config, load_config
from .database import close_db, get_db
from .sync import SyncEngine
from .web import ui_router


def setup_logging(level: str = "INFO") -> None:
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    logger = logging.getLogger(__name__)

    # Startup
    logger.info("Starting jellyfin-db-sync...")

    # Initialize database
    db = await get_db()
    logger.info("Database initialized")

    # Sync user mappings on startup
    config = get_config()
    engine = SyncEngine(config)

    try:
        await engine.sync_all_users()
        logger.info("User mappings synchronized")
    except Exception as e:
        logger.warning(f"Failed to sync users on startup: {e}")

    # Health check all servers
    health = await engine.health_check_all()
    for server_name, is_healthy in health.items():
        status = "✓ healthy" if is_healthy else "✗ unhealthy"
        logger.info(f"Server {server_name}: {status}")

    # Start the background worker for processing pending events
    worker_interval = config.sync.worker_interval_seconds
    await engine.start_worker(interval_seconds=worker_interval)
    logger.info(f"Sync worker started (interval: {worker_interval}s)")

    # Store engine in app state for access by routers
    app.state.engine = engine

    yield

    # Shutdown
    logger.info("Shutting down jellyfin-db-sync...")
    await engine.stop_worker()
    await close_db()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="jellyfin-db-sync",
        description="Bidirectional sync service for multiple Jellyfin instances",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Include routers
    app.include_router(ui_router)  # Dashboard at /
    app.include_router(health_router)  # /healthz, /readyz
    app.include_router(status_router)  # /api/status, /api/servers, etc.
    app.include_router(webhook_router)  # /webhook/{server_name}

    return app


def main() -> None:
    """Main entry point."""
    # Get config path from environment or default
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
    logger.info(f"Loaded configuration from {config_path}")
    logger.info(f"Configured servers: {[s.name for s in config.servers]}")

    # Create and run app
    app = create_app()

    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=config.logging.level.lower(),
    )


if __name__ == "__main__":
    main()
