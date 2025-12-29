"""Status API endpoints for monitoring dashboard."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..config import get_config
from ..database import get_db
from ..sync import SyncEngine

router = APIRouter(prefix="/api", tags=["status"])


class ServerStatus(BaseModel):
    """Status of a single Jellyfin server."""

    name: str
    url: str
    passwordless: bool
    healthy: bool
    user_count: int | None = None


class QueueStatus(BaseModel):
    """Status of the sync queue."""

    pending_events: int
    processing_events: int
    waiting_for_item_events: int
    failed_events: int
    worker_running: bool


class SyncStats(BaseModel):
    """Sync statistics."""

    total_synced: int
    successful: int
    failed: int
    last_sync_at: datetime | None


class DatabaseStatus(BaseModel):
    """Database status."""

    connected: bool
    user_mappings_count: int
    pending_events_count: int
    sync_log_entries: int


class OverallStatus(BaseModel):
    """Overall system status."""

    status: str  # healthy, degraded, unhealthy
    uptime_seconds: float
    version: str
    servers: list[ServerStatus]
    queue: QueueStatus
    database: DatabaseStatus
    sync_stats: SyncStats


# Track service start time
_start_time: datetime | None = None


def get_start_time() -> datetime:
    """Get or initialize the service start time."""
    global _start_time
    if _start_time is None:
        _start_time = datetime.now(UTC)
    return _start_time


@router.get("/status", response_model=OverallStatus)
async def get_status(request: Request) -> OverallStatus:
    """Get comprehensive system status for the dashboard."""
    config = get_config()
    engine: SyncEngine = request.app.state.engine
    db = await get_db()

    # Server health
    server_health = await engine.health_check_all()
    servers = [
        ServerStatus(
            name=s.name,
            url=s.url,
            passwordless=s.passwordless,
            healthy=server_health.get(s.name, False),
        )
        for s in config.servers
    ]

    # Queue status
    queue_info = await engine.get_queue_status()
    queue = QueueStatus(
        pending_events=queue_info.get("pending_events", 0),
        processing_events=await db.get_processing_count(),
        waiting_for_item_events=await db.get_waiting_for_item_count(),
        failed_events=await db.get_failed_count(),
        worker_running=queue_info.get("worker_running", False),
    )

    # Database stats
    db_status = DatabaseStatus(
        connected=db._db is not None,
        user_mappings_count=await db.get_user_mappings_count(),
        pending_events_count=queue.pending_events,
        sync_log_entries=await db.get_sync_log_count(),
    )

    # Sync stats
    sync_stats_data = await db.get_sync_stats()
    total = sync_stats_data.get("total", 0)
    successful = sync_stats_data.get("successful", 0)
    failed = sync_stats_data.get("failed", 0)
    sync_stats = SyncStats(
        total_synced=total if isinstance(total, int) else 0,
        successful=successful if isinstance(successful, int) else 0,
        failed=failed if isinstance(failed, int) else 0,
        last_sync_at=sync_stats_data.get("last_sync_at"),  # type: ignore[arg-type]
    )

    # Overall status
    all_servers_healthy = all(s.healthy for s in servers)
    any_server_healthy = any(s.healthy for s in servers)

    if all_servers_healthy and queue.worker_running and db_status.connected:
        status = "healthy"
    elif any_server_healthy and queue.worker_running and db_status.connected:
        status = "degraded"
    else:
        status = "unhealthy"

    start_time = get_start_time()
    uptime = (datetime.now(UTC) - start_time).total_seconds()

    return OverallStatus(
        status=status,
        uptime_seconds=uptime,
        version="0.1.0",
        servers=servers,
        queue=queue,
        database=db_status,
        sync_stats=sync_stats,
    )


@router.get("/servers")
async def get_servers(request: Request) -> list[ServerStatus]:
    """Get status of all configured servers."""
    config = get_config()
    engine: SyncEngine = request.app.state.engine

    server_health = await engine.health_check_all()

    return [
        ServerStatus(
            name=s.name,
            url=s.url,
            passwordless=s.passwordless,
            healthy=server_health.get(s.name, False),
        )
        for s in config.servers
    ]


@router.get("/queue")
async def get_queue(request: Request) -> QueueStatus:
    """Get queue status."""
    engine: SyncEngine = request.app.state.engine
    db = await get_db()

    queue_info = await engine.get_queue_status()

    return QueueStatus(
        pending_events=queue_info.get("pending_events", 0),
        processing_events=await db.get_processing_count(),
        waiting_for_item_events=await db.get_waiting_for_item_count(),
        failed_events=await db.get_failed_count(),
        worker_running=queue_info.get("worker_running", False),
    )


@router.get("/events/pending")
async def get_pending_events(limit: int = 50) -> list[dict[str, Any]]:
    """Get list of pending events."""
    db = await get_db()
    events = await db.get_pending_events(limit=limit)

    return [
        {
            "id": e.id,
            "event_type": e.event_type.value,
            "source_server": e.source_server,
            "target_server": e.target_server,
            "username": e.username,
            "item_name": e.item_name,
            "item_path": e.item_path,
            "status": e.status.value,
            "retry_count": e.retry_count,
            "last_error": e.last_error,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


@router.get("/events/waiting")
async def get_waiting_events(limit: int = 50) -> list[dict[str, Any]]:
    """Get list of events waiting for items to be imported."""
    db = await get_db()
    events = await db.get_waiting_for_item_events(limit=limit)

    return [
        {
            "id": e.id,
            "event_type": e.event_type.value,
            "source_server": e.source_server,
            "target_server": e.target_server,
            "username": e.username,
            "item_name": e.item_name,
            "item_path": e.item_path,
            "status": e.status.value,
            "item_not_found_count": e.item_not_found_count,
            "item_not_found_max": e.item_not_found_max,
            "last_error": e.last_error,
            "next_retry_at": e.next_retry_at.isoformat() if e.next_retry_at else None,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


@router.get("/events/failed")
async def get_failed_events(limit: int = 50) -> list[dict[str, Any]]:
    """Get list of failed events that exceeded max retries."""
    db = await get_db()
    events = await db.get_failed_events(limit=limit)

    return [
        {
            "id": e.id,
            "event_type": e.event_type.value,
            "source_server": e.source_server,
            "target_server": e.target_server,
            "username": e.username,
            "item_name": e.item_name,
            "status": e.status.value,
            "retry_count": e.retry_count,
            "last_error": e.last_error,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


@router.post("/events/{event_id}/retry")
async def retry_event(event_id: int) -> dict[str, Any]:
    """Reset a failed event to pending for retry."""
    db = await get_db()
    success = await db.reset_event_for_retry(event_id)

    return {
        "success": success,
        "message": "Event queued for retry" if success else "Event not found",
    }


@router.get("/sync-log")
async def get_sync_log(limit: int = 100, since_minutes: int | None = None) -> list[dict[str, Any]]:
    """Get recent sync log entries.

    Args:
        limit: Maximum number of entries to return
        since_minutes: Only return entries from the last N minutes (default: all)
    """
    db = await get_db()
    entries = await db.get_recent_sync_log(limit=limit, since_minutes=since_minutes)

    return entries


@router.get("/users")
async def get_user_mappings(request: Request) -> dict[str, Any]:
    """Get user mappings grouped by username with server presence."""
    config = get_config()
    db = await get_db()

    # Get all servers
    server_names = [s.name for s in config.servers]

    # Get all user mappings
    mappings = await db.get_all_user_mappings()

    # Group by username
    users: dict[str, dict[str, str | None]] = {}
    for m in mappings:
        if m.username not in users:
            users[m.username] = {s: None for s in server_names}
        users[m.username][m.server_name] = m.jellyfin_user_id

    return {
        "servers": server_names,
        "users": [
            {
                "username": username,
                "servers": servers,
            }
            for username, servers in sorted(users.items())
        ],
    }
