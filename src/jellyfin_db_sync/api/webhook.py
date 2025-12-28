"""Webhook receiver for Jellyfin events."""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..config import get_config
from ..models import WebhookPayload
from ..sync import SyncEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


def get_engine(request: Request) -> SyncEngine:
    """Get the sync engine from app state."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("SyncEngine not initialized in app state")
    return engine


@router.post("/{server_name}")
async def receive_webhook(
    server_name: str,
    request: Request,
) -> dict[str, Any]:
    """
    Receive webhook from a Jellyfin server.

    Each Jellyfin server should be configured to send webhooks to:
    POST /webhook/{server_name}

    Where {server_name} matches the name in config.yaml

    The webhook is enqueued for async processing (WAL pattern).
    """
    config = get_config()

    # Validate server name
    server = config.get_server(server_name)
    if not server:
        logger.warning(f"Received webhook for unknown server: {server_name}")
        raise HTTPException(status_code=404, detail=f"Unknown server: {server_name}")

    # Parse the webhook payload
    try:
        body = await request.json()
        logger.debug(f"Received webhook from {server_name}: {body}")
        payload = WebhookPayload.model_validate(body)
    except Exception as e:
        logger.error("Failed to parse webhook payload: %s", e)
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from e

    # Skip if no username (can't sync without knowing the user)
    if not payload.username:
        logger.debug(f"Skipping webhook without username: {payload.event}")
        return {"status": "skipped", "reason": "no username"}

    # Enqueue events for async processing (WAL pattern)
    engine = get_engine(request)
    enqueued_count = await engine.enqueue_events(payload, server_name)

    return {
        "status": "enqueued",
        "events_enqueued": enqueued_count,
    }


@router.get("/test")
async def test_webhook() -> dict[str, str]:
    """Test endpoint to verify webhook receiver is working."""
    return {"status": "ok", "message": "Webhook receiver is running"}


@router.get("/queue")
async def get_queue_status(request: Request) -> dict[str, Any]:
    """Get the current status of the event processing queue."""
    engine = get_engine(request)
    return await engine.get_queue_status()
