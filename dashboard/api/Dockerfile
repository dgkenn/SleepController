FROM python:3.11-slim

# System deps: tini for proper init + git to fetch pyEight (live Eight Sleep control)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the entire repo so both `sleepctl` (root) and `dashboard/` are available
COPY . .

# Install sleepctl + the async deps the live Eight Sleep client needs (aiohttp/httpx/dateutil)
RUN pip install --no-cache-dir -e ".[eightsleep]"

# Install dashboard API runtime deps
RUN pip install --no-cache-dir -r dashboard/api/requirements.txt

# Vendor the community pyEight library (lukas-clarke OAuth2 fork) for live Pod control.
# Best-effort: the image still builds (simulator mode) if the clone is unavailable; the live
# client lazy-imports pyeight and raises a clear error only if live mode is actually used.
RUN git clone --depth 1 https://github.com/lukas-clarke/pyEight.git /opt/pyEight || \
    echo "[build] pyEight not vendored; live Pod mode unavailable (simulator still works)"

# Working directory the app expects
WORKDIR /app/dashboard/api

# /opt/pyEight on the path so `import pyeight` works in live mode
ENV PYTHONPATH=/app:/app/dashboard/api:/opt/pyEight
ENV SLEEPCTL_DB=/data/sleepctl.db

# Entrypoint script: generate & persist JWT_SECRET if not already set
COPY dashboard/api/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
