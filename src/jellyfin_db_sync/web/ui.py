"""Web UI for monitoring dashboard."""

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

router = APIRouter(tags=["ui"])

# Path to static files directory
STATIC_DIR = Path(__file__).parent / "static"

# No-cache headers for development
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles with caching disabled."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add no-cache headers to all responses."""

        async def send_with_no_cache(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for key, value in NO_CACHE_HEADERS.items():
                    headers.append((key.encode(), value.encode()))
                message["headers"] = headers
            await send(message)

        await super().__call__(scope, receive, send_with_no_cache)


def get_static_files() -> NoCacheStaticFiles:
    """Get StaticFiles instance for mounting."""
    return NoCacheStaticFiles(directory=STATIC_DIR)


@router.get("/")
async def dashboard() -> FileResponse:
    """Serve the monitoring dashboard."""
    return FileResponse(
        STATIC_DIR / "index.html",
        media_type="text/html",
        headers=NO_CACHE_HEADERS,
    )
