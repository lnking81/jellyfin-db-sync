"""Health check endpoints for Kubernetes/Docker."""

from fastapi import APIRouter, Request, Response

from ..database import get_db
from ..sync import SyncEngine

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> Response:
    """
    Liveness probe for Kubernetes.

    Returns 200 if the service is alive.
    This should be a simple check - if the process is running, it's alive.
    """
    return Response(content="ok", media_type="text/plain")


@router.get("/readyz")
async def readyz(request: Request) -> Response:
    """
    Readiness probe for Kubernetes.

    Returns 200 if the service is ready to accept traffic.
    Checks:
    - Database is connected
    - Worker is running
    - At least one Jellyfin server is reachable
    """
    try:
        # Check database
        db = await get_db()
        if db._db is None:
            return Response(
                content="database not connected",
                status_code=503,
                media_type="text/plain",
            )

        # Check engine
        engine: SyncEngine = getattr(request.app.state, "engine", None)
        if engine is None:
            return Response(
                content="engine not initialized",
                status_code=503,
                media_type="text/plain",
            )

        # Check worker
        queue_status = await engine.get_queue_status()
        if not queue_status.get("worker_running"):
            return Response(
                content="worker not running",
                status_code=503,
                media_type="text/plain",
            )

        # Check at least one server is reachable
        server_health = await engine.health_check_all()
        if not any(server_health.values()):
            return Response(
                content="no servers reachable",
                status_code=503,
                media_type="text/plain",
            )

        return Response(content="ok", media_type="text/plain")

    except Exception as e:
        return Response(
            content=f"error: {e}",
            status_code=503,
            media_type="text/plain",
        )
