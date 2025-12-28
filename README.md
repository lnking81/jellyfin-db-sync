# jellyfin-db-sync

[![CI](https://github.com/lnking81/jellyfin-db-sync/actions/workflows/ci.yaml/badge.svg)](https://github.com/lnking81/jellyfin-db-sync/actions/workflows/ci.yaml)
[![Build](https://github.com/lnking81/jellyfin-db-sync/actions/workflows/build.yaml/badge.svg)](https://github.com/lnking81/jellyfin-db-sync/actions/workflows/build.yaml)
[![Experimental](https://img.shields.io/badge/status-experimental-orange.svg)](https://github.com)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

> ⚠️ **Highly Experimental** — This project is under active development. APIs and configuration may change without notice. Use at your own risk.

Bidirectional sync service for multiple Jellyfin instances with WAL-based event processing.

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Jellyfin Webhook Plugin Setup](#jellyfin-webhook-plugin-setup)
- [API Endpoints](#api-endpoints)
- [Development](#development)
- [How it works](#how-it-works)
- [License](#license)

## Features

- **Full bidirectional sync**: Any change on any server is synced to all others
- **User matching by username**: Automatically matches users across servers
- **Sync support**:
  - Playback progress (with debouncing)
  - Watched/unwatched status
  - Favorites
  - Ratings
  - Playlists
- **Item matching**:
  - Primary: by file path (works for all content including home media)
  - Fallback: by provider IDs (IMDB/TMDB/TVDB)
- **WAL-based architecture**: Reliable event processing with retries
- **Path sync policies**: Configurable retry behavior for items not yet imported
- **Web dashboard**: Real-time monitoring UI
- **Kubernetes-ready**: Health/readiness probes, Helm chart support

## Architecture

```
┌─────────────────┐                                     ┌─────────────────┐
│  Jellyfin WAN   │                                     │  Jellyfin LAN   │
│  (passwords)    │                                     │  (passwordless) │
└────────┬────────┘                                     └────────┬────────┘
     ▲   │                                                       │   ▲
     │   │ webhook                                       webhook │   │
     │   │                                                       │   │
     │   │         ┌───────────────────────────────────┐         │   │
     │   │         │        jellyfin-db-sync           │         │   │
     │   │         │                                   │         │   │
     │   │         │  ┌─────────────────────────────┐  │         │   │
     │   └────────▶│  │      Webhook Receiver       │  │◀────────┘   │
     │             │  └─────────────┬───────────────┘  │             │
     │             │                │                  │             │
     │             │                ▼                  │             │
     │             │  ┌─────────────────────────────┐  │             │
     │             │  │  pending_events  (SQLite)   │  │             │
     │             │  └─────────────┬───────────────┘  │             │
     │             │                │                  │             │
     │             │                ▼                  │             │
     │             │  ┌─────────────────────────────┐  │             │
     │  API        │  │        Worker Loop          │  │        API  │
     └─────────────┼──┤                             ├──┼─────────────┘
                   │  └─────────────┬───────────────┘  │
                   │                │                  │
                   └────────────────┼──────────────────┘
                                    │
                                    ▼
                      ┌─────────────────────────────┐
                      │    sync_log  (SQLite)       │
                      └─────────────────────────────┘
```

## Installation

### Prerequisites

1. [**Jellyfin Webhook Plugin**](https://github.com/jellyfin/jellyfin-plugin-webhook) installed on all Jellyfin servers
2. **API Keys** for each Jellyfin server (Dashboard → API Keys)

### Docker (GHCR)

```bash
docker pull ghcr.io/lnking81/jellyfin-db-sync:latest

docker run -d \
  --name jellyfin-db-sync \
  -p 8080:8080 \
  -v $(pwd)/config.yaml:/config/config.yaml:ro \
  -v jellyfin-db-sync-data:/data \
  ghcr.io/lnking81/jellyfin-db-sync:latest
```

Available tags:

- `latest` — latest build from main branch
- `vX.Y.Z` — specific version (e.g., `v1.0.0`)
- `sha-XXXXXX` — specific commit

### Build locally

```bash
docker build -t jellyfin-db-sync .
```

### Kubernetes (Helm)

```bash
helm install jellyfin-db-sync ./charts/jellyfin-db-sync \
  -n home-media \
  -f my-values.yaml
```

## Configuration

See [config.example.yaml](config.example.yaml) for a complete example.

```yaml
servers:
  - name: wan
    url: https://jellyfin-wan.example.com
    api_key: your-api-key-here
    passwordless: false

  - name: lan
    url: https://jellyfin-lan.example.com
    api_key: your-api-key-here
    passwordless: true

sync:
  playback_progress: true
  watched_status: true
  favorites: true
  ratings: true
  playlists: true
  progress_debounce_seconds: 30
  worker_interval_seconds: 5.0
  max_retries: 5

# Retry behavior for items not yet imported on target server
path_sync_policy:
  - prefix: /mnt/nfs/movies
    absent_retry_count: 10 # Retry 10 times
    retry_delay_seconds: 600 # Every 10 minutes
  - prefix: /mnt/nfs/tv
    absent_retry_count: -1 # Infinite retries
    retry_delay_seconds: 300 # Every 5 minutes
  - prefix: /mnt/nfs/music
    absent_retry_count: 0 # No retry (fail immediately)

database:
  path: /data/jellyfin-db-sync.db

server:
  host: 0.0.0.0
  port: 8080

logging:
  level: INFO
```

### Path Sync Policy

Controls retry behavior when an item is not found on a target server (e.g., library still being imported):

| `absent_retry_count` | Behavior                             |
| -------------------- | ------------------------------------ |
| `-1`                 | Infinite retries                     |
| `0`                  | No retry, fail immediately (default) |
| `>0`                 | Retry specified number of times      |

Policies are matched by **longest prefix** of the item's file path.

## Jellyfin Webhook Plugin Setup

For each Jellyfin server, configure the Webhook Plugin:

1. Go to **Dashboard → Plugins → Webhook**
2. Add a new **Generic Destination**
3. Configure:

   - **Webhook URL**: `http://jellyfin-db-sync:8080/webhook/{server_name}`
   - **Notification Type**: Select:
     - Playback Start
     - Playback Stop
     - Playback Progress
     - User Data Saved
   - **User Filter**: (optional)
   - **Item Type**: Movies, Episodes, etc.

4. **Template** (Generic Destination):

```json
{
  "NotificationType": "{{NotificationType}}",
  "ServerId": "{{ServerId}}",
  "ServerName": "{{ServerName}}",
  "UserId": "{{UserId}}",
  "NotificationUsername": "{{NotificationUsername}}",
  "ItemId": "{{ItemId}}",
  "Name": "{{Name}}",
  "ItemType": "{{ItemType}}",
  "Path": "{{Path}}",
  "PlaybackPositionTicks": {{PlaybackPositionTicks}},
  "PlaybackPosition": "{{PlaybackPosition}}",
  "PlayedToCompletion": {{PlayedToCompletion}},
  "IsFavorite": {{IsFavorite}},
  "Played": {{Played}},
  "Provider_imdb": "{{Provider_imdb}}",
  "Provider_tmdb": "{{Provider_tmdb}}",
  "Provider_tvdb": "{{Provider_tvdb}}"
}
```

> **Important**: `{{Path}}` field is required for path-based item matching.

## API Endpoints

### Health & Readiness

| Endpoint   | Method | Description                                  |
| ---------- | ------ | -------------------------------------------- |
| `/healthz` | GET    | Liveness probe (always 200 if alive)         |
| `/readyz`  | GET    | Readiness probe (checks DB, worker, servers) |

### Webhooks

| Endpoint                 | Method | Description                   |
| ------------------------ | ------ | ----------------------------- |
| `/webhook/{server_name}` | POST   | Receive webhook from Jellyfin |
| `/webhook/test`          | GET    | Test webhook receiver         |

### Status API

| Endpoint              | Method | Description                                |
| --------------------- | ------ | ------------------------------------------ |
| `/api/status`         | GET    | Comprehensive system status                |
| `/api/queue`          | GET    | Queue status (pending, processing, failed) |
| `/api/events/pending` | GET    | List pending events                        |
| `/api/events/waiting` | GET    | List events waiting for item import        |

### Web Dashboard

| Endpoint | Method | Description          |
| -------- | ------ | -------------------- |
| `/`      | GET    | Monitoring dashboard |

## Development

### Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Copy example config
cp config.example.yaml config.yaml
# Edit config.yaml with your settings
```

### Run locally

```bash
# Set config path
export CONFIG_PATH=./config.yaml

# Run the service
jellyfin-db-sync

# Or with uvicorn for development (auto-reload)
uvicorn jellyfin_db_sync.main:create_app --factory --reload --port 8080
```

### Testing

```bash
pytest
```

### Linting

```bash
ruff check .
ruff format .
mypy src/
```

## How it works

1. **Webhook Reception**: Jellyfin sends events via Webhook Plugin to `/webhook/{server_name}`
2. **Event Queuing**: Events are stored in `pending_events` table (WAL pattern)
3. **User Mapping**: Users are matched by username across servers
4. **Item Matching**:
   - First, try to match by file path (primary method)
   - Fall back to provider IDs (IMDB, TMDB, TVDB) if path doesn't match
5. **Worker Processing**: Background worker processes pending events with retries
6. **Path Policy**: If item not found and path matches a policy, schedule for retry
7. **Sync Execution**: Apply changes to target server via Jellyfin API
8. **Logging**: Record results in `sync_log` table

## License

[GPL-3.0](LICENSE)
