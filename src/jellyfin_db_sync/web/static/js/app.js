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

    async fetchUserMappings() {
        try {
            const response = await fetch('/api/users');
            return await response.json();
        } catch (e) {
            console.error('Failed to fetch user mappings:', e);
            return { servers: [], users: [] };
        }
    }

    async fetchSyncLog() {
        try {
            const timeFilter = document.getElementById('log-time-filter');
            const sourceFilter = document.getElementById('log-source-filter');
            const targetFilter = document.getElementById('log-target-filter');
            const typeFilter = document.getElementById('log-type-filter');
            const itemFilter = document.getElementById('log-item-filter');

            const params = new URLSearchParams();
            params.append('limit', '100');

            if (timeFilter && timeFilter.value) {
                params.append('since_minutes', timeFilter.value);
            }
            if (sourceFilter && sourceFilter.value) {
                params.append('source_server', sourceFilter.value);
            }
            if (targetFilter && targetFilter.value) {
                params.append('target_server', targetFilter.value);
            }
            if (typeFilter && typeFilter.value) {
                params.append('event_type', typeFilter.value);
            }
            if (itemFilter && itemFilter.value.trim()) {
                params.append('item_name', itemFilter.value.trim());
            }

            const response = await fetch(`/api/sync-log?${params.toString()}`);
            return await response.json();
        } catch (e) {
            console.error('Failed to fetch sync log:', e);
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

    formatBytes(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }

    /**
     * Parse UTC datetime string from API and convert to local Date object.
     * Handles formats: "2025-12-29T05:31:47" or "2025-12-29 05:31:47"
     */
    parseUtcDate(dateString) {
        if (!dateString) return null;
        // Normalize format and add Z suffix for UTC
        const normalized = dateString.replace(' ', 'T');
        // Add Z only if not already present
        const utcString = normalized.endsWith('Z') ? normalized : normalized + 'Z';
        return new Date(utcString);
    }

    formatDate(dateString) {
        if (!dateString) return 'Never';
        const date = this.parseUtcDate(dateString);
        if (!date || isNaN(date.getTime())) return 'Invalid';
        return date.toLocaleString();
    }

    formatDateShort(dateString) {
        if (!dateString) return 'Never';
        const date = this.parseUtcDate(dateString);
        if (!date || isNaN(date.getTime())) return 'Invalid';
        return date.toLocaleDateString();
    }

    formatTime(dateString) {
        if (!dateString) return '';
        const date = this.parseUtcDate(dateString);
        if (!date || isNaN(date.getTime())) return 'Invalid';
        return date.toLocaleTimeString();
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

        // Uptime and version
        document.getElementById('uptime').textContent = this.formatUptime(status.uptime_seconds);
        document.getElementById('version').textContent = `${status.version}`;

        // Servers
        this.updateServers(status.servers);
        this.updateServerFilters(status.servers);

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

    updateSyncLog(logs) {
        const container = document.getElementById('sync-log');
        if (logs.length === 0) {
            container.innerHTML = '<div class="empty-state">No log entries</div>';
            return;
        }

        const entries = logs.map(log => {
            const time = this.formatTime(log.created_at);
            const icon = log.success ? '✓' : '✗';
            const iconClass = log.success ? 'success' : 'error';
            const entryClass = log.success ? 'success-entry' : 'error-entry';
            const messageClass = log.success ? '' : 'error';

            // Build item info display
            const itemName = log.item_name ? this.escapeHtml(log.item_name) : '';
            const syncedValue = log.synced_value ? this.escapeHtml(log.synced_value) : '';
            const itemInfo = itemName ? `<span class="log-item">${itemName}</span>` : '';

            // Detect if this was a skip (already set) vs actual sync
            const isSkipped = syncedValue.includes('already set') ||
                syncedValue.includes('target >=') ||
                syncedValue.includes('target newer');
            const valueClass = isSkipped ? 'log-value skipped' : 'log-value';
            const valueInfo = syncedValue ? `<span class="${valueClass}">${syncedValue}</span>` : '';

            return `
                <div class="log-entry ${entryClass}">
                    <div class="log-icon ${iconClass}">${icon}</div>
                    <div class="log-time">${time}</div>
                    <div class="log-content">
                        <div class="log-header">
                            <span class="log-type">${this.escapeHtml(log.event_type)}</span>
                            <span class="log-flow">${this.escapeHtml(log.source_server)} → ${this.escapeHtml(log.target_server)}</span>
                            <span class="log-user">@${this.escapeHtml(log.username)}</span>
                            ${itemInfo}
                        </div>
                        <div class="log-details">
                            ${valueInfo}
                            <span class="log-message ${messageClass}">${this.escapeHtml(log.message)}</span>
                        </div>
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = `<div class="log-container">${entries}</div>`;
    }

    updateServerFilters(servers) {
        const sourceFilter = document.getElementById('log-source-filter');
        const targetFilter = document.getElementById('log-target-filter');

        if (!sourceFilter || !targetFilter) return;

        // Preserve current selections
        const currentSource = sourceFilter.value;
        const currentTarget = targetFilter.value;

        // Update options
        const serverOptions = servers.map(s =>
            `<option value="${this.escapeHtml(s.name)}">${this.escapeHtml(s.name)}</option>`
        ).join('');

        sourceFilter.innerHTML = '<option value="">All sources</option>' + serverOptions;
        targetFilter.innerHTML = '<option value="">All targets</option>' + serverOptions;

        // Restore selections
        sourceFilter.value = currentSource;
        targetFilter.value = currentTarget;
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
                    ${s.version ? `<span class="tag tag-version">${this.escapeHtml(s.version)}</span>` : ''}
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

        // Item cache stats
        document.getElementById('item-cache-total').textContent = database.item_cache_total || 0;

        // Database size
        document.getElementById('db-size').textContent = this.formatBytes(database.database_size_bytes || 0);

        // Per-server cache details
        const cacheDetails = document.getElementById('item-cache-details');
        if (cacheDetails && database.item_cache_by_server) {
            const servers = Object.entries(database.item_cache_by_server);
            if (servers.length > 0) {
                cacheDetails.innerHTML = servers.map(([server, count]) =>
                    `<span class="cache-server">${this.escapeHtml(server)}: <strong>${count}</strong></span>`
                ).join('');
            } else {
                cacheDetails.innerHTML = '<span class="cache-empty">No cached items</span>';
            }
        }
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

    updateUserMappings(data) {
        const container = document.getElementById('user-mappings-table');
        if (!data.users || data.users.length === 0) {
            container.innerHTML = '<div class="empty-state">No users found</div>';
            return;
        }

        const headers = data.servers.map(s => `<th class="user-server-header">${this.escapeHtml(s)}</th>`).join('');
        const rows = data.users.map(user => {
            const cells = data.servers.map(server => {
                const hasMapping = user.servers[server] !== null;
                const icon = hasMapping ? '✓' : '—';
                const className = hasMapping ? 'user-present' : 'user-absent';
                const title = hasMapping ? `ID: ${user.servers[server]}` : 'Not present';
                return `<td class="${className}" title="${title}">${icon}</td>`;
            }).join('');
            return `
                <tr>
                    <td class="user-name-cell">
                        <span class="user-avatar">${user.username.charAt(0).toUpperCase()}</span>
                        <span class="user-name">${this.escapeHtml(user.username)}</span>
                    </td>
                    ${cells}
                </tr>
            `;
        }).join('');

        container.innerHTML = `
            <table class="users-table">
                <thead>
                    <tr>
                        <th>User</th>
                        ${headers}
                    </tr>
                </thead>
                <tbody>
                    ${rows}
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
            const [status, events, waitingEvents, syncLog, userMappings] = await Promise.all([
                this.fetchStatus(),
                this.fetchPendingEvents(),
                this.fetchWaitingEvents(),
                this.fetchSyncLog(),
                this.fetchUserMappings()
            ]);

            this.updateUI(status, events, waitingEvents);
            this.updateSyncLog(syncLog);
            this.updateUserMappings(userMappings);
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
