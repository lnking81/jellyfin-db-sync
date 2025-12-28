"""Web UI for monitoring dashboard."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["ui"])

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Jellyfin DB Sync</title>
    <style>
        :root {
            --bg-primary: #1a1a2e;
            --bg-secondary: #16213e;
            --bg-card: #0f3460;
            --accent: #e94560;
            --accent-green: #4ade80;
            --accent-yellow: #fbbf24;
            --accent-red: #ef4444;
            --text-primary: #ffffff;
            --text-secondary: #94a3b8;
            --border: #334155;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--border);
        }

        h1 {
            font-size: 1.75rem;
            font-weight: 600;
        }

        .status-badge {
            padding: 0.5rem 1rem;
            border-radius: 9999px;
            font-weight: 500;
            font-size: 0.875rem;
            text-transform: uppercase;
        }

        .status-healthy { background: var(--accent-green); color: #000; }
        .status-degraded { background: var(--accent-yellow); color: #000; }
        .status-unhealthy { background: var(--accent-red); color: #fff; }

        .grid {
            display: grid;
            gap: 1.5rem;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
        }

        .card {
            background: var(--bg-secondary);
            border-radius: 0.75rem;
            padding: 1.5rem;
            border: 1px solid var(--border);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
        }

        .card h2 {
            font-size: 1rem;
            font-weight: 500;
            color: var(--text-secondary);
        }

        .card-icon {
            width: 24px;
            height: 24px;
            opacity: 0.7;
        }

        .metric {
            font-size: 2.5rem;
            font-weight: 700;
            line-height: 1;
        }

        .metric-label {
            font-size: 0.875rem;
            color: var(--text-secondary);
            margin-top: 0.25rem;
        }

        .server-list {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .server-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.75rem;
            background: var(--bg-card);
            border-radius: 0.5rem;
        }

        .server-info {
            display: flex;
            flex-direction: column;
        }

        .server-name {
            font-weight: 500;
        }

        .server-url {
            font-size: 0.75rem;
            color: var(--text-secondary);
        }

        .server-status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
        }

        .status-dot.online { background: var(--accent-green); }
        .status-dot.offline { background: var(--accent-red); }

        .tag {
            font-size: 0.625rem;
            padding: 0.25rem 0.5rem;
            border-radius: 0.25rem;
            background: var(--accent);
            color: #fff;
        }

        .events-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.875rem;
        }

        .events-table th,
        .events-table td {
            padding: 0.75rem;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }

        .events-table th {
            color: var(--text-secondary);
            font-weight: 500;
        }

        .events-table tr:hover {
            background: var(--bg-card);
        }

        .full-width {
            grid-column: 1 / -1;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 1rem;
        }

        .stat-item {
            text-align: center;
            padding: 1rem;
            background: var(--bg-card);
            border-radius: 0.5rem;
        }

        .stat-value {
            font-size: 1.5rem;
            font-weight: 600;
        }

        .stat-label {
            font-size: 0.75rem;
            color: var(--text-secondary);
            margin-top: 0.25rem;
        }

        .refresh-btn {
            background: var(--accent);
            color: #fff;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 0.5rem;
            cursor: pointer;
            font-size: 0.875rem;
            transition: opacity 0.2s;
        }

        .refresh-btn:hover {
            opacity: 0.8;
        }

        .refresh-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        .uptime {
            font-size: 0.875rem;
            color: var(--text-secondary);
        }

        .empty-state {
            text-align: center;
            padding: 2rem;
            color: var(--text-secondary);
        }

        .loading {
            opacity: 0.5;
        }

        @media (max-width: 768px) {
            .container {
                padding: 1rem;
            }

            .stats-grid {
                grid-template-columns: repeat(2, 1fr);
            }

            header {
                flex-direction: column;
                gap: 1rem;
                align-items: flex-start;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>🔄 Jellyfin DB Sync</h1>
                <span class="uptime" id="uptime">Loading...</span>
            </div>
            <div style="display: flex; gap: 1rem; align-items: center;">
                <span class="status-badge" id="overall-status">Loading</span>
                <button class="refresh-btn" onclick="refreshData()">↻ Refresh</button>
            </div>
        </header>

        <div class="grid">
            <!-- Servers Card -->
            <div class="card">
                <div class="card-header">
                    <h2>📺 Jellyfin Servers</h2>
                </div>
                <div class="server-list" id="servers-list">
                    <div class="empty-state">Loading...</div>
                </div>
            </div>

            <!-- Queue Status Card -->
            <div class="card">
                <div class="card-header">
                    <h2>📋 Sync Queue</h2>
                </div>
                <div class="stats-grid" id="queue-stats" style="grid-template-columns: repeat(5, 1fr);">
                    <div class="stat-item">
                        <div class="stat-value" id="pending-count">-</div>
                        <div class="stat-label">Pending</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="processing-count">-</div>
                        <div class="stat-label">Processing</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" style="color: var(--accent-yellow);" id="waiting-count">-</div>
                        <div class="stat-label">Waiting</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="failed-count">-</div>
                        <div class="stat-label">Failed</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="worker-status">-</div>
                        <div class="stat-label">Worker</div>
                    </div>
                </div>
            </div>

            <!-- Database Status Card -->
            <div class="card">
                <div class="card-header">
                    <h2>💾 Database</h2>
                </div>
                <div class="stats-grid" id="db-stats">
                    <div class="stat-item">
                        <div class="stat-value" id="db-status">-</div>
                        <div class="stat-label">Status</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="user-mappings">-</div>
                        <div class="stat-label">User Mappings</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="sync-log-count">-</div>
                        <div class="stat-label">Log Entries</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="total-synced">-</div>
                        <div class="stat-label">Total Synced</div>
                    </div>
                </div>
            </div>

            <!-- Sync Stats Card -->
            <div class="card">
                <div class="card-header">
                    <h2>📊 Sync Statistics</h2>
                </div>
                <div class="stats-grid">
                    <div class="stat-item">
                        <div class="stat-value" style="color: var(--accent-green);" id="sync-successful">-</div>
                        <div class="stat-label">Successful</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" style="color: var(--accent-red);" id="sync-failed">-</div>
                        <div class="stat-label">Failed</div>
                    </div>
                    <div class="stat-item" style="grid-column: span 2;">
                        <div class="stat-value" style="font-size: 1rem;" id="last-sync">-</div>
                        <div class="stat-label">Last Sync</div>
                    </div>
                </div>
            </div>

            <!-- Pending Events Table -->
            <div class="card full-width">
                <div class="card-header">
                    <h2>⏳ Pending Events</h2>
                </div>
                <div id="pending-events">
                    <div class="empty-state">Loading...</div>
                </div>
            </div>

            <!-- Waiting for Item Events Table -->
            <div class="card full-width">
                <div class="card-header">
                    <h2>🔍 Waiting for Item Import</h2>
                </div>
                <div id="waiting-events">
                    <div class="empty-state">Loading...</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        async function fetchStatus() {
            try {
                const response = await fetch('/api/status');
                return await response.json();
            } catch (e) {
                console.error('Failed to fetch status:', e);
                return null;
            }
        }

        async function fetchPendingEvents() {
            try {
                const response = await fetch('/api/events/pending?limit=20');
                return await response.json();
            } catch (e) {
                console.error('Failed to fetch events:', e);
                return [];
            }
        }

        async function fetchWaitingEvents() {
            try {
                const response = await fetch('/api/events/waiting?limit=20');
                return await response.json();
            } catch (e) {
                console.error('Failed to fetch waiting events:', e);
                return [];
            }
        }

        function formatUptime(seconds) {
            const days = Math.floor(seconds / 86400);
            const hours = Math.floor((seconds % 86400) / 3600);
            const mins = Math.floor((seconds % 3600) / 60);

            if (days > 0) return `Uptime: ${days}d ${hours}h ${mins}m`;
            if (hours > 0) return `Uptime: ${hours}h ${mins}m`;
            return `Uptime: ${mins}m`;
        }

        function formatDate(isoString) {
            if (!isoString) return 'Never';
            const date = new Date(isoString);
            return date.toLocaleString();
        }

        function truncatePath(path) {
            if (!path) return '-';
            if (path.length <= 40) return path;
            return '...' + path.slice(-37);
        }

        function truncateError(error) {
            if (!error) return '-';
            if (error.length <= 50) return error;
            return error.slice(0, 47) + '...';
        }

        function updateUI(status, events, waitingEvents) {
            if (!status) return;

            // Overall status
            const statusEl = document.getElementById('overall-status');
            statusEl.textContent = status.status.toUpperCase();
            statusEl.className = 'status-badge status-' + status.status;

            // Uptime
            document.getElementById('uptime').textContent = formatUptime(status.uptime_seconds);

            // Servers
            const serversList = document.getElementById('servers-list');
            if (status.servers.length === 0) {
                serversList.innerHTML = '<div class="empty-state">No servers configured</div>';
            } else {
                serversList.innerHTML = status.servers.map(s => `
                    <div class="server-item">
                        <div class="server-info">
                            <span class="server-name">${s.name}</span>
                            <span class="server-url">${s.url}</span>
                        </div>
                        <div class="server-status">
                            ${s.passwordless ? '<span class="tag">NO AUTH</span>' : ''}
                            <span class="status-dot ${s.healthy ? 'online' : 'offline'}"></span>
                        </div>
                    </div>
                `).join('');
            }

            // Queue stats
            document.getElementById('pending-count').textContent = status.queue.pending_events;
            document.getElementById('processing-count').textContent = status.queue.processing_events;
            document.getElementById('waiting-count').textContent = status.queue.waiting_for_item_events;
            document.getElementById('failed-count').textContent = status.queue.failed_events;
            document.getElementById('worker-status').textContent = status.queue.worker_running ? '✓' : '✗';
            document.getElementById('worker-status').style.color = status.queue.worker_running ? 'var(--accent-green)' : 'var(--accent-red)';

            // Database stats
            document.getElementById('db-status').textContent = status.database.connected ? '✓' : '✗';
            document.getElementById('db-status').style.color = status.database.connected ? 'var(--accent-green)' : 'var(--accent-red)';
            document.getElementById('user-mappings').textContent = status.database.user_mappings_count;
            document.getElementById('sync-log-count').textContent = status.database.sync_log_entries;
            document.getElementById('total-synced').textContent = status.sync_stats.total_synced;

            // Sync stats
            document.getElementById('sync-successful').textContent = status.sync_stats.successful;
            document.getElementById('sync-failed').textContent = status.sync_stats.failed;
            document.getElementById('last-sync').textContent = formatDate(status.sync_stats.last_sync_at);

            // Pending events table
            const eventsContainer = document.getElementById('pending-events');
            if (events.length === 0) {
                eventsContainer.innerHTML = '<div class="empty-state">No pending events</div>';
            } else {
                eventsContainer.innerHTML = `
                    <table class="events-table">
                        <thead>
                            <tr>
                                <th>Type</th>
                                <th>Item</th>
                                <th>User</th>
                                <th>Source → Target</th>
                                <th>Retries</th>
                                <th>Created</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${events.map(e => `
                                <tr>
                                    <td>${e.event_type}</td>
                                    <td>${e.item_name || '-'}</td>
                                    <td>${e.username}</td>
                                    <td>${e.source_server} → ${e.target_server}</td>
                                    <td>${e.retry_count}</td>
                                    <td>${formatDate(e.created_at)}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
            }

            // Waiting for item events table
            const waitingContainer = document.getElementById('waiting-events');
            if (waitingEvents.length === 0) {
                waitingContainer.innerHTML = '<div class="empty-state">No events waiting for item import</div>';
            } else {
                waitingContainer.innerHTML = `
                    <table class="events-table">
                        <thead>
                            <tr>
                                <th>Type</th>
                                <th>Item</th>
                                <th>Path</th>
                                <th>Target</th>
                                <th>Attempt</th>
                                <th>Next Retry</th>
                                <th>Error</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${waitingEvents.map(e => {
                                const maxDisplay = e.item_not_found_max === -1 ? '∞' : e.item_not_found_max;
                                return `
                                <tr>
                                    <td>${e.event_type}</td>
                                    <td>${e.item_name || '-'}</td>
                                    <td title="${e.item_path || ''}">${truncatePath(e.item_path)}</td>
                                    <td>${e.target_server}</td>
                                    <td>${e.item_not_found_count} / ${maxDisplay}</td>
                                    <td>${formatDate(e.next_retry_at)}</td>
                                    <td title="${e.last_error || ''}">${truncateError(e.last_error)}</td>
                                </tr>
                            `}).join('')}
                        </tbody>
                    </table>
                `;
            }
        }

        async function refreshData() {
            const btn = document.querySelector('.refresh-btn');
            btn.disabled = true;
            btn.textContent = '↻ Loading...';

            const [status, events, waitingEvents] = await Promise.all([
                fetchStatus(),
                fetchPendingEvents(),
                fetchWaitingEvents()
            ]);

            updateUI(status, events, waitingEvents);

            btn.disabled = false;
            btn.textContent = '↻ Refresh';
        }

        // Initial load
        refreshData();

        // Auto-refresh every 10 seconds
        setInterval(refreshData, 10000);
    </script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the monitoring dashboard."""
    return HTMLResponse(content=DASHBOARD_HTML)
