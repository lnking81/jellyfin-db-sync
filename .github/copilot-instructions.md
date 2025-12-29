# Copilot Instructions for jellyfin-db-sync

## Language Guidelines

- **Always respond in the user's language** — match the language of the user's query
- **Code, comments, documentation, commit messages — ALWAYS in English only**
- Variable names, function names, file names — English only

## Task Planning

- **For complex, multi-step tasks** — always create a TODO list using `manage_todo_list` tool
- Break down the task into specific, actionable items
- Mark items as in-progress before starting, completed immediately after finishing
- This ensures nothing is forgotten and provides visibility into progress

## Critical Research & Accuracy Guidelines

### MANDATORY: Verify Current Information

1. **Always check current versions** — before any installation or upgrade recommendations:

   - Use `fetch_webpage` to get fresh documentation from official sources
   - Check latest image versions on Docker Hub, GitHub Releases, Artifact Hub
   - Do not rely on cached knowledge about versions — they become outdated

2. **Use search** — when in doubt:

   - Search the repository (`grep_search`, `semantic_search`) before proposing solutions
   - Search the web for recent discussions, issues, or solutions
   - Check existing configuration before making changes
   - Read README, CHANGELOG and release notes before updates

3. **Never make things up**:

   - If you don't know the answer or solution doesn't exist — honestly say "I don't know" or "no solution exists"
   - Do not invent parameter names, API endpoints, commands
   - Do not make up versions — always verify current ones

4. **Documentation sources to verify**:

   - Helm charts: https://artifacthub.io/
   - Docker images: https://hub.docker.com/
   - Kubernetes: https://kubernetes.io/docs/
   - Jellyfin: https://jellyfin.org/docs/

5. **Before executing commands**:
   - Verify the command is current for the tool version
   - Ensure flags and options exist
   - When in doubt — check `--help` or documentation first

## Architecture Overview

This is a **bidirectional sync service** for multiple Jellyfin instances using a **WAL (Write-Ahead Log) pattern**:

1. **Webhook Receiver** (`api/webhook.py`) → receives Jellyfin events at `/webhook/{server_name}`
2. **Event Queue** (`database.py`) → SQLite `pending_events` table stores events durably
3. **Sync Worker** (`sync/engine.py`) → background task processes queue, applies changes via Jellyfin API
4. **Sync Log** → records results for monitoring

Key flow: Webhook → `enqueue_events()` → `pending_events` table → Worker → `_sync_event()` → Jellyfin API → `sync_log`

## API Endpoints Reference

**IMPORTANT: Always verify these endpoints before configuring health checks!**

| Endpoint                 | Method | Description                                  |
| ------------------------ | ------ | -------------------------------------------- |
| `/healthz`               | GET    | Liveness probe (always 200 if alive)         |
| `/readyz`                | GET    | Readiness probe (checks DB, worker, servers) |
| `/webhook/{server_name}` | POST   | Receive webhook from Jellyfin                |
| `/api/status`            | GET    | Comprehensive system status                  |
| `/api/queue`             | GET    | Queue status (pending, processing, failed)   |
| `/api/events/pending`    | GET    | List pending events                          |
| `/api/events/waiting`    | GET    | List events waiting for item import          |
| `/`                      | GET    | Web dashboard                                |

**Note:** There is NO `/health` endpoint! Use `/healthz` for liveness and `/readyz` for readiness probes.

## Project Structure

```
src/jellyfin_db_sync/
├── main.py          # FastAPI app factory, lifespan, entry point
├── config.py        # Pydantic config models (Config.from_yaml)
├── database.py      # Async SQLite (aiosqlite) with Database class
├── models.py        # Pydantic models (WebhookPayload, PendingEvent, etc.)
├── api/             # FastAPI routers (health, status, webhook)
├── jellyfin/        # JellyfinClient for API calls
├── sync/            # SyncEngine orchestration
└── web/             # Dashboard UI
    ├── ui.py        # Router serving index.html, get_static_files()
    └── static/      # Static assets served at /static
        ├── index.html
        ├── css/styles.css
        └── js/app.js
```

## Key Patterns

### PEP guidelines

- Always try to follow PEP guidelines for code style and structure
- Don't use f-strings in logging calls; use lazy formatting instead:
  ```python
  logger.info("Processing item %s for user %s", item_id, user_id)
  ```

### Global Singletons with Lazy Init

- Config: `load_config()` sets global, `get_config()` retrieves
- Database: `get_db()` returns singleton, `close_db()` cleans up
- Engine stored in `app.state.engine` for router access

### Async-First Design

- All I/O operations are async (httpx, aiosqlite)
- Background worker uses `asyncio.Task` with graceful shutdown
- Tests use `pytest-asyncio` with `asyncio_mode = "auto"`

### Item Matching Strategy

Items are matched across servers using (in order):

1. **File path** (`item_path`) - primary, works for all content
2. **Provider IDs** (IMDB/TMDB/TVDB) - fallback for movies/series

### Path Sync Policies

Handle items not yet imported on target server via `path_sync_policy` in config:

- `absent_retry_count: -1` = infinite retries
- `absent_retry_count: 0` = fail immediately (default)
- `absent_retry_count: >0` = retry N times

## Development Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml

# Run locally
export CONFIG_PATH=./config.yaml
jellyfin-db-sync
# Or with auto-reload:
uvicorn jellyfin_db_sync.main:create_app --factory --reload --port 8080

# Test & Lint (using tox)
tox                       # Run all checks (lint + type + test)
tox -e test               # Run tests with coverage
tox -e lint               # Run linters (ruff check and format)
tox -e type               # Run type checking (mypy)
tox -e helm               # Lint and test Helm chart
tox -e format             # Auto-format code

# Or run tools directly
pytest                    # Uses tests/ with pytest-asyncio
ruff check . && ruff format .
mypy src/
```

## Code Conventions

- **Line length**: 119 chars (configured in ruff/black)
- **Python version**: 3.11+ (uses `match` statements, modern type hints)
- **Imports**: Use relative imports within package (`from ..config import get_config`)
- **Logging**: Use `logging.getLogger(__name__)` per module
- **Models**: All data classes use Pydantic with `Field()` for defaults and aliases
- **WebhookPayload**: Uses `alias="JsonFieldName"` for Jellyfin JSON mapping

## Testing Patterns

Tests use temporary SQLite databases:

```python
@pytest.fixture
async def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()
```

Database class accepts optional `db_path` parameter for testing (bypasses global config).

## Adding New Sync Event Types

1. Add to `SyncEventType` enum in `models.py`
2. Update `_parse_webhook_to_event_data()` in `sync/engine.py` to detect event
3. Add handler in `_execute_sync()` match statement
4. Implement Jellyfin API method in `jellyfin/client.py`

## Configuration

- Runtime: `CONFIG_PATH` env var or `config.yaml` in cwd
- Docker: Mount to `/config/config.yaml`, data at `/data/`
- See `config.example.yaml` for all options
