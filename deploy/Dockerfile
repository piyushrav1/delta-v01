# =======================================================
# Delta India Terminal - Production Dockerfile (TASK-P15-01)
# =======================================================
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# Install essential dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Install python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create unprivileged runtime user
RUN useradd -m -u 10001 delta && \
    mkdir -p /app/data /app/backups /var/log/delta-terminal && \
    chown -R delta:delta /app /var/log/delta-terminal

USER delta

EXPOSE 5000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://127.0.0.1:5000/api/v2/system/mode || exit 1

CMD ["python", "app.py"]
