# Multi-stage Dockerfile for jellyfin-db-sync

# Build stage
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN pip install --no-cache-dir hatch

# Copy project files
COPY pyproject.toml .
COPY src/ src/

# Build wheel
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# Runtime stage
FROM python:3.12-slim

WORKDIR /app

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

# Copy wheels from builder
COPY --from=builder /wheels /wheels

# Install the package
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Create directories for config and data
RUN mkdir -p /config /data && chown -R appuser:appuser /config /data

# Switch to non-root user
USER appuser

# Environment variables
ENV CONFIG_PATH=/config/config.yaml
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"

# Run the application
CMD ["jellyfin-db-sync"]
