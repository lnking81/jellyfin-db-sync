# jellyfin-db-sync Helm Chart

A Helm chart for deploying jellyfin-db-sync - a bidirectional sync service for multiple Jellyfin instances.

## Features

- 🔄 Bidirectional sync of watched status, playback progress, favorites, ratings, and more
- 📊 Web dashboard for monitoring sync status
- 🔐 Secure handling of API keys via Kubernetes Secrets
- 📈 Prometheus ServiceMonitor support
- 🛡️ Security-hardened with non-root user, read-only filesystem, dropped capabilities

## Prerequisites

- Kubernetes 1.23+
- Helm 3.8+
- PV provisioner (if persistence is enabled)

## Installation

### Quick Start

```bash
helm repo add jellyfin-db-sync https://lnking81.github.io/jellyfin-db-sync
helm repo update

helm install jellyfin-db-sync jellyfin-db-sync/jellyfin-db-sync \
  --set "config.servers[0].name=server1" \
  --set "config.servers[0].url=https://jellyfin-1.example.com" \
  --set "config.servers[0].apiKey=your-api-key-1" \
  --set "config.servers[1].name=server2" \
  --set "config.servers[1].url=https://jellyfin-2.example.com" \
  --set "config.servers[1].apiKey=your-api-key-2"
```

### Using values file (recommended)

Create a `values.yaml`:

```yaml
config:
  servers:
    - name: wan
      url: https://jellyfin-wan.example.com
      apiKey: your-wan-api-key
      passwordless: false
    - name: lan
      url: https://jellyfin-lan.example.com
      apiKey: your-lan-api-key
      passwordless: true

  sync:
    playbackProgress: true
    watchedStatus: true
    favorites: true
    ratings: true
    dryRun: false

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: jellyfin-sync.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: jellyfin-sync-tls
      hosts:
        - jellyfin-sync.example.com
```

Install:

```bash
helm install jellyfin-db-sync jellyfin-db-sync/jellyfin-db-sync -f values.yaml
```

### Using External Secrets (recommended for production)

Each server can reference an existing Kubernetes Secret:

```yaml
config:
  servers:
    - name: wan
      url: https://jellyfin-wan.example.com
      existingSecret:
        name: my-jellyfin-secrets
        key: wan-api-key
    - name: lan
      url: https://jellyfin-lan.example.com
      existingSecret:
        name: my-jellyfin-secrets
        key: lan-api-key
```

You can also mix approaches — some servers with `apiKey`, others with `existingSecret`:

```yaml
config:
  servers:
    - name: dev
      url: https://jellyfin-dev.example.com
      apiKey: dev-key-for-testing # Stored in chart-managed Secret
    - name: prod
      url: https://jellyfin-prod.example.com
      existingSecret: # Referenced from external Secret
        name: prod-secrets
        key: jellyfin-api-key
```

## Configuration

### Jellyfin Webhook Setup

After deploying, configure webhooks in each Jellyfin server:

1. Go to **Dashboard → Plugins → Webhook**
2. Add a new webhook with URL: `http://<service-url>/webhook/<server-name>`
3. Enable events:
   - Playback Progress
   - Playback Start
   - Playback Stop
   - User Data Saved
   - Item Added

### Key Values

| Key                      | Description                                   | Default                             |
| ------------------------ | --------------------------------------------- | ----------------------------------- |
| `replicaCount`           | Number of replicas (1 recommended for SQLite) | `1`                                 |
| `image.repository`       | Container image repository                    | `ghcr.io/lnking81/jellyfin-db-sync` |
| `image.tag`              | Image tag (defaults to chart appVersion)      | `""`                                |
| `config.servers`         | List of Jellyfin servers to sync              | `[]`                                |
| `config.sync.dryRun`     | Dry run mode (no changes applied)             | `false`                             |
| `config.logging.level`   | Log level (DEBUG, INFO, WARNING, ERROR)       | `INFO`                              |
| `persistence.enabled`    | Enable persistent storage for SQLite          | `true`                              |
| `persistence.size`       | PVC size                                      | `1Gi`                               |
| `ingress.enabled`        | Enable ingress                                | `false`                             |
| `serviceMonitor.enabled` | Enable Prometheus ServiceMonitor              | `false`                             |

### Full Values Reference

See [values.yaml](values.yaml) for the complete list of configurable values with descriptions.

## Upgrading

```bash
helm upgrade jellyfin-db-sync jellyfin-db-sync/jellyfin-db-sync -f values.yaml
```

## Uninstalling

```bash
helm uninstall jellyfin-db-sync
```

⚠️ **Note**: PVC is not deleted by default. Delete manually if needed:

```bash
kubectl delete pvc jellyfin-db-sync
```

## Troubleshooting

### Check logs

```bash
kubectl logs -f -l app.kubernetes.io/name=jellyfin-db-sync
```

### Check health endpoints

```bash
# Liveness probe
kubectl exec -it deploy/jellyfin-db-sync -- curl -s http://localhost:8080/healthz

# Readiness probe
kubectl exec -it deploy/jellyfin-db-sync -- curl -s http://localhost:8080/readyz
```

**Note:** The health endpoints are `/healthz` (liveness) and `/readyz` (readiness), not `/health`.

### Access dashboard locally

```bash
kubectl port-forward svc/jellyfin-db-sync 8080:8080
open http://localhost:8080
```

### Verify configuration

```bash
kubectl exec -it deploy/jellyfin-db-sync -- cat /config/config.yaml
```

## License

MIT License - see [LICENSE](../../LICENSE) for details.
