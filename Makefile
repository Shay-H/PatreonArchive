# Convenience wrapper around docker compose for the Patreon Archive.
# Usage: make deploy | up | down | logs | restart | ps | build

.PHONY: deploy up down logs restart ps build check-env prune-raw

# Guard: refuse to start without a .env file.
check-env:
	@test -f .env || { \
		echo "ERROR: .env not found. Run: cp .env.example .env  (then edit it)"; \
		exit 1; \
	}

# Build + start (the everyday command).
deploy: check-env
	./deploy.sh

up: check-env
	docker compose up -d --build

down:
	docker compose down

build: check-env
	docker compose build

restart:
	docker compose restart

logs:
	docker compose logs -f

ps:
	docker compose ps

# One-off: delete downloaded image media under data/raw (keeps the .patreon-dl
# status caches and the DB). Syncs auto-prune already; use this to clear the
# pre-existing media in one shot.
prune-raw:
	docker compose exec app python -m app.cleanup
