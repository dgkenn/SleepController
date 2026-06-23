# ── Stage 1: build ──────────────────────────────────────────────────────────
FROM node:22-slim AS builder

WORKDIR /app

# Copy package manifests first for better layer caching
COPY dashboard/web/package*.json ./

RUN npm ci --prefer-offline

# Copy the full web source tree
COPY dashboard/web/ ./

ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
# Supports two Next.js output modes:
#   - `output: 'standalone'` in next.config.js  → runs via `node server.js` (smaller image)
#   - default (no standalone)                    → runs via `npm start`
#
# To enable the leaner standalone mode, add to dashboard/web/next.config.js:
#   module.exports = { output: 'standalone' }
FROM node:22-slim AS runner

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# Copy the full build output so both standalone and non-standalone work
COPY --from=builder /app ./

EXPOSE 3000

# Use npm start so this works regardless of whether standalone mode is configured.
# If standalone is enabled the `start` script in package.json should be
# `node .next/standalone/server.js` — Next.js sets that up automatically.
CMD ["npm", "start"]
