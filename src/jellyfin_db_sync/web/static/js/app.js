/**
 * Jellyfin DB Sync Dashboard
 */

class Dashboard {
    constructor() {
        this.refreshInterval = 10000;
        this.intervalId = null;
    }

    async fetchStatus() {
        try {
            const response = await fetch('/api/status');
            return await response.json();
        } catch (e) {
            console.error('Failed to fetch status:', e);
            return null;
        }
    }

    async fetchPendingEvents() {
        try {
            const response = await fetch('/api/events/pending?limit=20');
            return await response.json();
        } catch (e) {
            console.error('Failed to fetch events:', e);
            return [];
        }
    }

    async fetchWaitingEvents() {
        try {
            const response = await fetch('/api/events/waiting?limit=20');
            return await response.json();
        } catch (e) {
            console.error('Failed to fetch waiting events:', e);
            return [];
        }
    }

    formatUptime(seconds) {
        const days = Math.floor(seconds / 86400);
        const hours = Math.floor((seconds % 86400) / 3600);
        const mins = Math.floor((seconds % 3600) / 60);

        if (days > 0) return `Uptime: ${days}d ${hours}h ${mins}m`;
        if (hours > 0) return `Uptime: ${hours}h ${mins}m`;
        return `Uptime: ${mins}m`;
    }

    formatDate(isoString) {
        if (!isoString) return 'Never';
        const date = new Date(isoString);
        return date.toLocaleString();
    }

    formatDateShort(isoString) {
        if (!isoString) return 'Never';
        const date = new Date(isoString);
        return date.toLocaleDateString();
    }

    truncatePath(path) {
        if (!path) return '-';
        if (path.length <= 40) return path;
        return '...' + path.slice(-37);
    }

    truncateError(error) {
        if (!error) return '-';
        if (error.length <= 50) return error;
        return error.slice(0, 47) + '...';
    }

    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    updateUI(status, events, waitingEvents) {
        if (!status) return;

        // Overall status
        const statusEl = document.getElementById('overall-status');
        statusEl.textContent = status.status.toUpperCase();
        statusEl.className = 'status-badge status-' + status.status;

        // Uptime
        document.getElementById('uptime').textContent = this.formatUptime(status.uptime_seconds);

        // Servers
        this.updateServers(status.servers);

        // Queue stats
        this.updateQueueStats(status.queue);

        // Database stats
        this.updateDatabaseStats(status.database);

        // Sync stats
        this.updateSyncStats(status.sync_stats);

        // Events tables
        this.updatePendingEvents(events);
        this.updateWaitingEvents(waitingEvents);
    }

    updateServers(servers) {
        const serversList = document.getElementById('servers-list');
        if (servers.length === 0) {
            serversList.innerHTML = '<div class="empty-state">No servers configured</div>';
            return;
        }

        serversList.innerHTML = servers.map(s => `
            <div class="server-item">
                <div class="server-info">
                    <span class="server-name">${this.escapeHtml(s.name)}</span>
                    <span class="server-url">${this.escapeHtml(s.url)}</span>
                </div>
                <div class="server-status">
                    ${s.passwordless ? '<span class="tag">NO AUTH</span>' : ''}
                    <span class="status-dot ${s.healthy ? 'online' : 'offline'}"></span>
                </div>
            </div>
        `).join('');
    }

    updateQueueStats(queue) {
        document.getElementById('pending-count').textContent = queue.pending_events;
        document.getElementById('processing-count').textContent = queue.processing_events;
        document.getElementById('waiting-count').textContent = queue.waiting_for_item_events;
        document.getElementById('failed-count').textContent = queue.failed_events;

        const workerEl = document.getElementById('worker-status');
        workerEl.textContent = queue.worker_running ? '✓' : '✗';
        workerEl.className = 'stat-value ' + (queue.worker_running ? 'color-green' : 'color-red');
    }

    updateDatabaseStats(database) {
        const dbStatusEl = document.getElementById('db-status');
        dbStatusEl.textContent = database.connected ? '✓' : '✗';
        dbStatusEl.className = 'stat-value ' + (database.connected ? 'color-green' : 'color-red');

        document.getElementById('user-mappings').textContent = database.user_mappings_count;
        document.getElementById('sync-log-count').textContent = database.sync_log_entries;
    }

    updateSyncStats(syncStats) {
        document.getElementById('sync-successful').textContent = syncStats.successful;
        document.getElementById('sync-failed').textContent = syncStats.failed;
        document.getElementById('total-synced').textContent = syncStats.total_synced;
        document.getElementById('last-sync').textContent = this.formatDateShort(syncStats.last_sync_at);
    }

    updatePendingEvents(events) {
        const container = document.getElementById('pending-events');
        if (events.length === 0) {
            container.innerHTML = '<div class="empty-state">No pending events</div>';
            return;
        }

        container.innerHTML = `
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
                            <td>${this.escapeHtml(e.event_type)}</td>
                            <td>${this.escapeHtml(e.item_name) || '-'}</td>
                            <td>${this.escapeHtml(e.username)}</td>
                            <td>${this.escapeHtml(e.source_server)} → ${this.escapeHtml(e.target_server)}</td>
                            <td>${e.retry_count}</td>
                            <td>${this.formatDate(e.created_at)}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        `;
    }

    updateWaitingEvents(waitingEvents) {
        const container = document.getElementById('waiting-events');
        if (waitingEvents.length === 0) {
            container.innerHTML = '<div class="empty-state">No events waiting for item import</div>';
            return;
        }

        container.innerHTML = `
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
                            <td>${this.escapeHtml(e.event_type)}</td>
                            <td>${this.escapeHtml(e.item_name) || '-'}</td>
                            <td title="${this.escapeHtml(e.item_path) || ''}">${this.escapeHtml(this.truncatePath(e.item_path))}</td>
                            <td>${this.escapeHtml(e.target_server)}</td>
                            <td>${e.item_not_found_count} / ${maxDisplay}</td>
                            <td>${this.formatDate(e.next_retry_at)}</td>
                            <td title="${this.escapeHtml(e.last_error) || ''}">${this.escapeHtml(this.truncateError(e.last_error))}</td>
                        </tr>
                    `}).join('')}
                </tbody>
            </table>
        `;
    }

    async refresh() {
        const btn = document.querySelector('.refresh-btn');
        btn.disabled = true;
        btn.textContent = '↻ Loading...';

        try {
            const [status, events, waitingEvents] = await Promise.all([
                this.fetchStatus(),
                this.fetchPendingEvents(),
                this.fetchWaitingEvents()
            ]);

            this.updateUI(status, events, waitingEvents);
        } finally {
            btn.disabled = false;
            btn.textContent = '↻ Refresh';
        }
    }

    start() {
        this.refresh();
        this.intervalId = setInterval(() => this.refresh(), this.refreshInterval);
    }

    stop() {
        if (this.intervalId) {
            clearInterval(this.intervalId);
            this.intervalId = null;
        }
    }
}

// Initialize dashboard when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    const dashboard = new Dashboard();
    dashboard.start();

    // Expose refresh function globally for the button
    window.refreshData = () => dashboard.refresh();
});
