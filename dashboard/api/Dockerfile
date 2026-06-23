FROM python:3.11-slim

# System deps: only what PBKDF2/stdlib needs (already built-in) + tini for proper init
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the entire repo so both `sleepctl` (root) and `dashboard/` are available
COPY . .

# Install sleepctl package from repo root first (editable mode requires the source tree)
RUN pip install --no-cache-dir -e .

# Install dashboard API runtime deps
RUN pip install --no-cache-dir -r dashboard/api/requirements.txt

# Working directory the app expects
WORKDIR /app/dashboard/api

ENV PYTHONPATH=/app:/app/dashboard/api
ENV SLEEPCTL_DB=/data/sleepctl.db

# Entrypoint script: generate & persist JWT_SECRET if not already set
COPY dashboard/api/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
