/**
 * Jellyfin DB Sync Dashboard
 * Uses HTML <template> elements for clean separation of markup and logic
 */

const STORAGE_KEY = 'jellyfin-db-sync-log-filters';

class Dashboard {
    constructor() {
        this.refreshInterval = 10000;
        this.intervalId = null;
        this.templates = {};

        // Pagination state
        this.logPage = 0;
        this.logTotal = 0;
        this.logPageSize = 50;
    }

    // === Storage ===

    loadFilters() {
        try {
            const saved = localStorage.getItem(STORAGE_KEY);
            if (saved) {
                const filters = JSON.parse(saved);
                for (const [id, value] of Object.entries(filters)) {
                    const el = document.getElementById(id);
                    if (el) el.value = value;
                }
                // Restore page size
                if (filters['log-page-size']) {
                    this.logPageSize = parseInt(filters['log-page-size'], 10) || 50;
                }
            }
        } catch (e) {
            console.warn('Failed to load filters from storage:', e);
        }
    }

    saveFilters() {
        try {
            const filterIds = [
                'log-item-filter',
                'log-source-filter',
                'log-target-filter',
                'log-type-filter',
                'log-time-filter',
                'log-page-size'
            ];
            const filters = {};
            for (const id of filterIds) {
                const el = document.getElementById(id);
                if (el) filters[id] = el.value;
            }
            localStorage.setItem(STORAGE_KEY, JSON.stringify(filters));
        } catch (e) {
            console.warn('Failed to save filters to storage:', e);
        }
    }

    // === Template helpers ===

    /**
     * Get and cache a template by ID
     */
    getTemplate(id) {
        if (!this.templates[id]) {
            this.templates[id] = document.getElementById(id);
        }
        return this.templates[id];
    }

    /**
     * Clone a template and return the first element child
     */
    cloneTemplate(id) {
        const template = this.getTemplate(id);
        return template.content.cloneNode(true).firstElementChild;
    }

    /**
     * Set text content of element with data-field attribute
     */
    setField(parent, field, value) {
        const el = parent.querySelector(`[data-field="${field}"]`);
        if (el) el.textContent = value ?? '';
        return el;
    }

    /**
     * Clear container and show empty state message
     */
    showEmpty(container, message) {
        container.innerHTML = '';
        const div = document.createElement('div');
        div.className = 'empty-state';
        div.textContent = message;
        container.appendChild(div);
    }

    // === API Fetchers ===

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
            const params = new URLSearchParams();
            params.append('limit', this.logPageSize.toString());
            params.append('offset', (this.logPage * this.logPageSize).toString());

            const filters = {
                'log-time-filter': 'since_minutes',
                'log-source-filter': 'source_server',
                'log-target-filter': 'target_server',
                'log-type-filter': 'event_type',
                'log-item-filter': 'item_name'
            };

            for (const [elementId, paramName] of Object.entries(filters)) {
                const el = document.getElementById(elementId);
                const value = el?.value?.trim();
                if (value) params.append(paramName, value);
            }

            const response = await fetch(`/api/sync-log?${params.toString()}`);
            return await response.json();
        } catch (e) {
            console.error('Failed to fetch sync log:', e);
            return { entries: [], total: 0, limit: this.logPageSize, offset: 0 };
        }
    }

    // === Formatters ===

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
        const normalized = dateString.replace(' ', 'T');
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

    formatTimeAgo(dateString) {
        if (!dateString) return 'Never';
        const date = this.parseUtcDate(dateString);
        if (!date || isNaN(date.getTime())) return 'Invalid';

        const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
        if (seconds < 0) return 'Just now';
        if (seconds < 60) return `${seconds}s ago`;
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
        if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
        return `${Math.floor(seconds / 86400)}d ago`;
    }

    // === UI Update Methods ===

    updateUI(status, events, waitingEvents) {
        if (!status) return;

        // Overall status
        const statusEl = document.getElementById('overall-status');
        statusEl.textContent = status.status.toUpperCase();
        statusEl.className = 'status-badge status-' + status.status;

        // Uptime and version
        document.getElementById('uptime').textContent = this.formatUptime(status.uptime_seconds);
        document.getElementById('version').textContent = status.version;

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

    updateServers(servers) {
        const container = document.getElementById('servers-list');
        container.innerHTML = '';

        if (servers.length === 0) {
            this.showEmpty(container, 'No servers configured');
            return;
        }

        for (const server of servers) {
            const item = this.cloneTemplate('tpl-server-item');

            this.setField(item, 'name', server.name);
            this.setField(item, 'url', server.url);

            const versionEl = this.setField(item, 'version', server.version);
            if (!server.version) versionEl.style.display = 'none';

            const noauthEl = item.querySelector('[data-field="noauth"]');
            if (server.passwordless) noauthEl.style.display = '';

            const statusEl = item.querySelector('[data-field="status"]');
            statusEl.classList.add(server.healthy ? 'online' : 'offline');

            container.appendChild(item);
        }
    }

    updateServerFilters(servers) {
        const sourceFilter = document.getElementById('log-source-filter');
        const targetFilter = document.getElementById('log-target-filter');
        if (!sourceFilter || !targetFilter) return;

        const currentSource = sourceFilter.value;
        const currentTarget = targetFilter.value;

        // Clear and rebuild options
        for (const select of [sourceFilter, targetFilter]) {
            const defaultText = select === sourceFilter ? 'All sources' : 'All targets';
            select.innerHTML = '';

            const defaultOpt = document.createElement('option');
            defaultOpt.value = '';
            defaultOpt.textContent = defaultText;
            select.appendChild(defaultOpt);

            for (const s of servers) {
                const opt = document.createElement('option');
                opt.value = s.name;
                opt.textContent = s.name;
                select.appendChild(opt);
            }
        }

        sourceFilter.value = currentSource;
        targetFilter.value = currentTarget;
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
        document.getElementById('item-cache-total').textContent = database.item_cache_total || 0;
        document.getElementById('db-size').textContent = this.formatBytes(database.database_size_bytes || 0);

        // Per-server cache details
        const cacheDetails = document.getElementById('item-cache-details');
        if (cacheDetails && database.item_cache_by_server) {
            cacheDetails.innerHTML = '';
            const servers = Object.entries(database.item_cache_by_server);

            if (servers.length > 0) {
                for (const [server, count] of servers) {
                    const item = this.cloneTemplate('tpl-cache-server');
                    this.setField(item, 'name', server);
                    this.setField(item, 'count', count);
                    cacheDetails.appendChild(item);
                }
            } else {
                const span = document.createElement('span');
                span.className = 'cache-empty';
                span.textContent = 'No cached items';
                cacheDetails.appendChild(span);
            }
        }
    }

    updateSyncStats(syncStats) {
        document.getElementById('sync-successful').textContent = syncStats.successful;
        document.getElementById('sync-failed').textContent = syncStats.failed;
        document.getElementById('total-synced').textContent = syncStats.total_synced;
        document.getElementById('last-sync').textContent = this.formatTimeAgo(syncStats.last_sync_at);
    }

    updateSyncLog(data) {
        const container = document.getElementById('sync-log');
        container.innerHTML = '';

        const logs = data.entries || [];
        this.logTotal = data.total || 0;

        if (logs.length === 0) {
            this.showEmpty(container, 'No log entries');
            this.updatePagination();
            return;
        }

        const logContainer = document.createElement('div');
        logContainer.className = 'log-container';

        for (const log of logs) {
            const entry = this.cloneTemplate('tpl-log-entry');

            // Set entry classes
            entry.classList.add(log.success ? 'success-entry' : 'error-entry');

            // Determine if this was skipped (already set)
            const isSkipped = log.synced_value && (
                log.synced_value.includes('already set') ||
                log.synced_value.includes('target >=') ||
                log.synced_value.includes('target newer')
            );

            // Status icon (success/error)
            const statusIconEl = entry.querySelector('[data-field="status-icon"]');
            if (log.success) {
                statusIconEl.innerHTML = '<i data-lucide="circle-check"></i>';
                statusIconEl.classList.add('success');
            } else {
                statusIconEl.innerHTML = '<i data-lucide="circle-x"></i>';
                statusIconEl.classList.add('error');
            }

            // Action icon (synced/skipped)
            const actionIconEl = entry.querySelector('[data-field="action-icon"]');
            if (isSkipped) {
                actionIconEl.innerHTML = '<i data-lucide="skip-forward"></i>';
                actionIconEl.classList.add('skipped');
                actionIconEl.title = 'Skipped (already set)';
            } else if (log.success) {
                actionIconEl.innerHTML = '<i data-lucide="arrow-right-left"></i>';
                actionIconEl.classList.add('synced');
                actionIconEl.title = 'Synced';
            } else {
                actionIconEl.innerHTML = '<i data-lucide="alert-triangle"></i>';
                actionIconEl.classList.add('failed');
                actionIconEl.title = 'Failed';
            }

            // Basic fields
            this.setField(entry, 'time', this.formatTime(log.created_at));
            this.setField(entry, 'type', log.event_type);
            this.setField(entry, 'flow', `${log.source_server} → ${log.target_server}`);
            this.setField(entry, 'user', `@${log.username}`);

            // Item name (hide if empty)
            const itemEl = this.setField(entry, 'item', log.item_name || '');
            if (!log.item_name) itemEl.style.display = 'none';

            // Synced value
            const valueEl = this.setField(entry, 'value', log.synced_value || '');
            if (!log.synced_value) {
                valueEl.style.display = 'none';
            } else if (isSkipped) {
                valueEl.classList.add('skipped');
            }

            // Message
            const messageEl = this.setField(entry, 'message', log.message);
            if (!log.success) messageEl.classList.add('error');

            logContainer.appendChild(entry);
        }

        container.appendChild(logContainer);

        // Re-initialize Lucide icons for dynamically added content
        if (window.lucide) {
            window.lucide.createIcons();
        }

        this.updatePagination();
    }

    updatePagination() {
        const totalPages = Math.ceil(this.logTotal / this.logPageSize);
        const currentPage = this.logPage + 1;

        const prevBtn = document.getElementById('log-prev');
        const nextBtn = document.getElementById('log-next');
        const pageInfo = document.getElementById('log-page-info');

        prevBtn.disabled = this.logPage === 0;
        nextBtn.disabled = currentPage >= totalPages;

        if (this.logTotal === 0) {
            pageInfo.textContent = 'No entries';
        } else {
            const start = this.logPage * this.logPageSize + 1;
            const end = Math.min(start + this.logPageSize - 1, this.logTotal);
            pageInfo.textContent = `${start}-${end} of ${this.logTotal}`;
        }
    }

    updatePendingEvents(events) {
        const container = document.getElementById('pending-events');
        container.innerHTML = '';

        if (events.length === 0) {
            this.showEmpty(container, 'No pending events');
            return;
        }

        const table = this.cloneTemplate('tpl-events-table');
        const thead = table.querySelector('thead');
        const tbody = table.querySelector('tbody');

        // Build header
        const headerRow = document.createElement('tr');
        for (const text of ['Type', 'Item', 'User', 'Source → Target', 'Retries', 'Created']) {
            const th = document.createElement('th');
            th.textContent = text;
            headerRow.appendChild(th);
        }
        thead.appendChild(headerRow);

        // Build rows
        for (const e of events) {
            const row = this.cloneTemplate('tpl-pending-row');
            this.setField(row, 'type', e.event_type);
            this.setField(row, 'item', e.item_name || '-');
            this.setField(row, 'user', e.username);
            this.setField(row, 'flow', `${e.source_server} → ${e.target_server}`);
            this.setField(row, 'retries', e.retry_count);
            this.setField(row, 'created', this.formatDate(e.created_at));
            tbody.appendChild(row);
        }

        container.appendChild(table);
    }

    updateWaitingEvents(waitingEvents) {
        const container = document.getElementById('waiting-events');
        container.innerHTML = '';

        if (waitingEvents.length === 0) {
            this.showEmpty(container, 'No events waiting for item import');
            return;
        }

        const table = this.cloneTemplate('tpl-events-table');
        const thead = table.querySelector('thead');
        const tbody = table.querySelector('tbody');

        // Build header
        const headerRow = document.createElement('tr');
        for (const text of ['Type', 'Item', 'Path', 'Target', 'Attempt', 'Next Retry', 'Error']) {
            const th = document.createElement('th');
            th.textContent = text;
            headerRow.appendChild(th);
        }
        thead.appendChild(headerRow);

        // Build rows
        for (const e of waitingEvents) {
            const row = this.cloneTemplate('tpl-waiting-row');
            const maxDisplay = e.item_not_found_max === -1 ? '∞' : e.item_not_found_max;

            this.setField(row, 'type', e.event_type);
            this.setField(row, 'item', e.item_name || '-');

            const pathCell = row.querySelector('[data-field="path"]');
            pathCell.textContent = this.truncatePath(e.item_path);
            pathCell.title = e.item_path || '';

            this.setField(row, 'target', e.target_server);
            this.setField(row, 'attempt', `${e.item_not_found_count} / ${maxDisplay}`);
            this.setField(row, 'next-retry', this.formatDate(e.next_retry_at));

            const errorCell = row.querySelector('[data-field="error"]');
            errorCell.textContent = this.truncateError(e.last_error);
            errorCell.title = e.last_error || '';

            tbody.appendChild(row);
        }

        container.appendChild(table);
    }

    updateUserMappings(data) {
        const container = document.getElementById('user-mappings-table');
        container.innerHTML = '';

        if (!data.users || data.users.length === 0) {
            this.showEmpty(container, 'No users found');
            return;
        }

        const table = this.cloneTemplate('tpl-users-table');
        const thead = table.querySelector('thead tr');
        const tbody = table.querySelector('tbody');

        // Add server headers
        for (const server of data.servers) {
            const th = document.createElement('th');
            th.className = 'user-server-header';
            th.textContent = server;
            thead.appendChild(th);
        }

        // Build user rows
        for (const user of data.users) {
            const row = this.cloneTemplate('tpl-user-row');
            this.setField(row, 'avatar', user.username.charAt(0).toUpperCase());
            this.setField(row, 'name', user.username);

            // Add server cells
            for (const server of data.servers) {
                const cell = document.createElement('td');
                const hasMapping = user.servers[server] !== null;

                cell.textContent = hasMapping ? '✓' : '—';
                cell.className = hasMapping ? 'user-present' : 'user-absent';
                cell.title = hasMapping ? `ID: ${user.servers[server]}` : 'Not present';
                row.appendChild(cell);
            }

            tbody.appendChild(row);
        }

        container.appendChild(table);
    }

    // === Lifecycle ===

    setupEventListeners() {
        // Filter change handlers - reset to page 0 and refresh
        const filterIds = [
            'log-item-filter',
            'log-source-filter',
            'log-target-filter',
            'log-type-filter',
            'log-time-filter'
        ];

        for (const id of filterIds) {
            const el = document.getElementById(id);
            if (el) {
                el.addEventListener('change', () => {
                    this.logPage = 0;
                    this.saveFilters();
                    this.refreshLogOnly();
                });
            }
        }

        // Page size change
        const pageSizeEl = document.getElementById('log-page-size');
        if (pageSizeEl) {
            pageSizeEl.addEventListener('change', () => {
                this.logPageSize = parseInt(pageSizeEl.value, 10) || 50;
                this.logPage = 0;
                this.saveFilters();
                this.refreshLogOnly();
            });
        }

        // Pagination buttons
        document.getElementById('log-prev')?.addEventListener('click', () => {
            if (this.logPage > 0) {
                this.logPage--;
                this.refreshLogOnly();
            }
        });

        document.getElementById('log-next')?.addEventListener('click', () => {
            const totalPages = Math.ceil(this.logTotal / this.logPageSize);
            if (this.logPage + 1 < totalPages) {
                this.logPage++;
                this.refreshLogOnly();
            }
        });
    }

    async refreshLogOnly() {
        const syncLog = await this.fetchSyncLog();
        this.updateSyncLog(syncLog);
    }

    async refresh() {
        const btn = document.querySelector('.refresh-btn');
        btn.disabled = true;
        const icon = btn.querySelector('svg');
        if (icon) icon.classList.add('spinning');

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
            const icon = btn.querySelector('svg');
            if (icon) icon.classList.remove('spinning');
        }
    }

    start() {
        this.loadFilters();
        this.setupEventListeners();
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
