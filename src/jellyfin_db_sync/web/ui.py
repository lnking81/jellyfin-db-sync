"""Web UI for monitoring dashboard."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

router = APIRouter(tags=["ui"])

# Path to static files directory
STATIC_DIR = Path(__file__).parent / "static"


def get_static_files() -> StaticFiles:
    """Get StaticFiles instance for mounting."""
    return StaticFiles(directory=STATIC_DIR)


@router.get("/")
async def dashboard() -> FileResponse:
    """Serve the monitoring dashboard."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")
