# sleepctl dashboard — self-hosted deployment
# Run `make up` from this directory to start the full stack.

COMPOSE   := docker compose
ENV_FILE  := .env

.PHONY: up down logs seed ps help

# ── Default target ─────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  make up    — build & start all services (safe to re-run)"
	@echo "  make down  — stop & remove containers (data volumes are kept)"
	@echo "  make logs  — tail logs for all services"
	@echo "  make seed  — populate demo sleep data (run after first `make up`)"
	@echo "  make ps    — show running containers"
	@echo ""

# ── up ────────────────────────────────────────────────────────────────────────
up: $(ENV_FILE)
	@# Auto-generate a strong DASHBOARD_PASSWORD if it is still the placeholder
	@if grep -q '^DASHBOARD_PASSWORD=changeme$$' $(ENV_FILE); then \
	    NEW_PASS=$$(python3 -c "import secrets, string; \
	        alpha = string.ascii_letters + string.digits; \
	        print(''.join(secrets.choice(alpha) for _ in range(24)))"); \
	    sed -i "s|^DASHBOARD_PASSWORD=changeme$$|DASHBOARD_PASSWORD=$$NEW_PASS|" $(ENV_FILE); \
	    echo ""; \
	    echo "  ┌─────────────────────────────────────────────┐"; \
	    echo "  │  Generated dashboard password (save this!): │"; \
	    echo "  │  $$NEW_PASS"; \
	    echo "  └─────────────────────────────────────────────┘"; \
	    echo ""; \
	fi
	$(COMPOSE) up -d --build
	@echo ""
	@echo "  ✓ Stack is up.  Find your LAN IP with:"
	@echo "    hostname -I | awk '{print \$$1}'"
	@echo ""
	@LAN_IP=$$(hostname -I 2>/dev/null | awk '{print $$1}' || echo "<your-LAN-IP>"); \
	 echo "  Open on iPhone:  https://$$LAN_IP/"; \
	 echo ""
	@echo "  ── iPhone PWA install steps ──────────────────────────────────────"
	@echo "  1. Open Safari and navigate to https://<LAN-IP>/"
	@echo "  2. When Safari shows a certificate warning, tap 'Show Details'"
	@echo "     then 'visit this website' and confirm — this is Caddy's local CA."
	@echo "     (Optional: install the root cert once for a green lock — see README.md)"
	@echo "  3. Log in with the credentials in deploy/.env"
	@echo "  4. Tap the Share icon (□↑) → 'Add to Home Screen' → 'Add'"
	@echo "  5. The dashboard icon appears on your home screen as a full-screen app."
	@echo "  ──────────────────────────────────────────────────────────────────"
	@echo ""

# Create .env from the example template if it doesn't exist yet
$(ENV_FILE):
	@echo "  → .env not found — copying from .env.example"
	cp .env.example $(ENV_FILE)

# ── down ──────────────────────────────────────────────────────────────────────
down:
	$(COMPOSE) down

# ── logs ──────────────────────────────────────────────────────────────────────
logs:
	$(COMPOSE) logs -f

# ── seed ──────────────────────────────────────────────────────────────────────
seed:
	@echo "Seeding demo sleep data..."
	$(COMPOSE) exec api python3 -m app.seed
	@echo "Done.  Refresh the dashboard to see 21 nights of demo data."

# ── ps ────────────────────────────────────────────────────────────────────────
ps:
	$(COMPOSE) ps
