FROM python:3.12-slim

# Don't write .pyc files; unbuffered logs for docker logs -f.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Run as a non-root user; the data volume is owned by it.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

VOLUME ["/app/data"]

ENV STATE_DB_PATH=/app/data/se_hardcover.sqlite3 \
    HEARTBEAT_PATH=/app/data/heartbeat

# Healthcheck: the heartbeat file must have been touched in the last ~2 cycles.
HEALTHCHECK --interval=5m --timeout=10s --start-period=1m --retries=3 \
    CMD test "$(find /app/data/heartbeat -mmin -125 2>/dev/null)" || exit 1

ENTRYPOINT ["se-hardcover"]
CMD ["watch"]
