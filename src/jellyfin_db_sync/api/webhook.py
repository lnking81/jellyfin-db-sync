"""Webhook receiver for Jellyfin events."""

import logging
import secrets
import string
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..config import get_config
from ..database import get_db
from ..jellyfin import JellyfinClient
from ..models import WebhookPayload
from ..sync import SyncEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


def generate_random_password(length: int = 16) -> str:
    """Generate a random password for new users on password-required servers."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def get_engine(request: Request) -> SyncEngine:
    """Get the sync engine from app state."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("SyncEngine not initialized in app state")
    return engine


async def sync_user_creation(source_server: str, username: str, user_id: str) -> dict[str, Any]:
    """
    Create user on all other servers when a user is created.

    - Passwordless servers: create without password
    - Password servers: create with random password (user must reset)
    """
    config = get_config()
    db = await get_db()
    results: dict[str, Any] = {"created": [], "skipped": [], "failed": []}

    # Save mapping for source server
    await db.upsert_user_mapping(
        username=username.lower(),
        server_name=source_server,
        jellyfin_user_id=user_id,
    )

    # Create on other servers
    for server in config.servers:
        if server.name == source_server:
            continue

        client = JellyfinClient(server)

        # Check if user already exists
        existing = await client.get_user_by_name(username)
        if existing:
            logger.debug("User '%s' already exists on %s", username, server.name)
            # Update mapping anyway
            await db.upsert_user_mapping(
                username=username.lower(),
                server_name=server.name,
                jellyfin_user_id=existing["Id"],
            )
            results["skipped"].append(server.name)
            continue

        # Determine password
        password = None if server.passwordless else generate_random_password()

        # Create user
        new_user = await client.create_user(username, password)
        if new_user:
            await db.upsert_user_mapping(
                username=username.lower(),
                server_name=server.name,
                jellyfin_user_id=new_user["Id"],
            )
            results["created"].append(
                {
                    "server": server.name,
                    "passwordless": server.passwordless,
                    "password": password,  # Return so admin can share if needed
                }
            )
        else:
            results["failed"].append(server.name)

    return results


async def sync_user_deletion(source_server: str, username: str) -> dict[str, Any]:
    """Delete user from all servers when deleted from one."""
    config = get_config()
    db = await get_db()
    results: dict[str, Any] = {"deleted": [], "not_found": [], "failed": []}

    # Delete mapping for source server
    await db.delete_user_mapping(username=username.lower(), server_name=source_server)
    results["deleted"].append(source_server)

    # Delete from other servers
    for server in config.servers:
        if server.name == source_server:
            continue

        client = JellyfinClient(server)

        # Find user
        user = await client.get_user_by_name(username)
        if not user:
            logger.debug("User '%s' not found on %s", username, server.name)
            results["not_found"].append(server.name)
            # Still remove mapping if exists
            await db.delete_user_mapping(username=username.lower(), server_name=server.name)
            continue

        # Delete user
        success = await client.delete_user(user["Id"])
        if success:
            await db.delete_user_mapping(username=username.lower(), server_name=server.name)
            results["deleted"].append(server.name)
        else:
            results["failed"].append(server.name)

    return results


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
        logger.warning("Received webhook for unknown server: %s", server_name)
        raise HTTPException(status_code=404, detail=f"Unknown server: {server_name}")

    # Parse the webhook payload
    try:
        body = await request.json()
        logger.debug("[RAW WEBHOOK] %s: %s", server_name, body)
        payload = WebhookPayload.model_validate(body)
    except Exception as e:
        logger.error("Failed to parse webhook payload: %s", e)
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from e

    # Handle user lifecycle events (sync to all servers)
    if payload.event == "UserCreated":
        if payload.username and payload.user_id:
            logger.info("User created on %s: %s - syncing to other servers", server_name, payload.username)
            results = await sync_user_creation(server_name, payload.username, payload.user_id)
            return {
                "status": "user_synced",
                "username": payload.username,
                "source_server": server_name,
                **results,
            }
        return {"status": "skipped", "reason": "missing user info"}

    if payload.event == "UserDeleted":
        if payload.username:
            logger.info("User deleted on %s: %s - deleting from all servers", server_name, payload.username)
            results = await sync_user_deletion(server_name, payload.username)
            return {
                "status": "user_deleted_all",
                "username": payload.username,
                "source_server": server_name,
                **results,
            }
        return {"status": "skipped", "reason": "missing username"}

    # Skip if no username (can't sync without knowing the user)
    if not payload.username:
        logger.debug("Skipping webhook without username: %s", payload.event)
        return {"status": "skipped", "reason": "no username"}

    # If Path is missing, fetch it from Jellyfin API
    # The webhook plugin doesn't include Path, but we need it for reliable item matching
    if not payload.item_path and payload.item_id and payload.user_id:
        client = JellyfinClient(server)
        item_info = await client.get_item_info(payload.user_id, payload.item_id)
        if item_info:
            payload.item_path = item_info.get("Path")
            # Also fill in provider IDs if missing
            provider_ids = item_info.get("ProviderIds", {})
            if not payload.provider_imdb:
                payload.provider_imdb = provider_ids.get("Imdb")
            if not payload.provider_tmdb:
                payload.provider_tmdb = provider_ids.get("Tmdb")
            if not payload.provider_tvdb:
                payload.provider_tvdb = provider_ids.get("Tvdb")
            logger.debug("[API ENRICHED] path=%s, imdb=%s", payload.item_path, payload.provider_imdb)

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
