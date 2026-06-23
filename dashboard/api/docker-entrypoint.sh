#!/bin/sh
# Entrypoint for the sleepctl API and daemon containers.
# Generates a persistent JWT_SECRET on first run and stores it in the shared
# /data volume so the API and daemon share the same secret across restarts.
set -e

SECRET_FILE="/data/.jwt_secret"

# Ensure the data directory exists (volume may not be pre-created)
mkdir -p /data

if [ -z "$JWT_SECRET" ]; then
    if [ -f "$SECRET_FILE" ]; then
        # Restore from persisted file
        JWT_SECRET=$(cat "$SECRET_FILE")
    else
        # Generate a new 64-char hex secret and persist it
        JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        echo "$JWT_SECRET" > "$SECRET_FILE"
        chmod 600 "$SECRET_FILE"
        echo "[entrypoint] Generated new JWT_SECRET and saved to $SECRET_FILE"
    fi
    export JWT_SECRET
fi

exec "$@"
