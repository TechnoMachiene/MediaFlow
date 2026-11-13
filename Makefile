.PHONY: setup lint unit integration live test cov done api worker demo migrate reconcile prune-media web web-dev web-test \
	docker-up docker-down docker-logs docker-admin docker-demo docker-demo-down demo-check
setup:        ; pip install -r requirements-dev.txt
lint:         ; ruff check .
unit:         ; pytest -m unit
integration:  ; pytest -m integration
live:         ; RUN_LIVE=1 pytest -m live -s
test:         ; pytest
cov:          ; pytest -m "not live" --cov --cov-report=term-missing
done: lint unit integration cov
	@echo "Automated gate passed. Run 'make web-test' for the UI, then tick docs/DEFINITION_OF_DONE.md."
api:          ; uvicorn app.api.main:app --reload
worker:       ; python -m app.cli worker
# One process, no Gemini key, no PostgreSQL: SQLite + embedded worker + seeded demo accounts.
# Its own Redis prefix and database file, so it never touches your real data. GEMINI_API_KEY
# is blanked so a key in your .env can't reach the demo (demo mode refuses to run with one).
demo: web
	DEMO_MODE=1 GEMINI_API_KEY= DATABASE_URL=sqlite:///demo.sqlite3 QUEUE_PREFIX=demo \
	MEDIA_STORAGE=local MEDIA_STORAGE_DIR=demo-uploads EMBEDDED_WORKER=1 \
	uvicorn app.api.main:app --port $(or $(PORT),8082)
demo-check:   ; python3 scripts/demo_check.py $(or $(URL),http://localhost:8081)
migrate:      ; alembic upgrade head
reconcile:    ; python -m app.cli reconcile
prune-media:  ; python -m app.cli prune-media
web:          ; cd frontend && npm install && npm run build
web-dev:      ; cd frontend && npm install && npm run dev
web-test:     ; cd frontend && npm install && npm test

# ── Docker (see compose.yaml) ──────────────────────────────────────────────────────────────
DEMO_COMPOSE = docker compose -f compose.demo.yaml
docker-up:        ; docker compose up -d --build --wait && echo "MediaFlow: http://localhost:$${MEDIAFLOW_PORT:-8080}"
docker-down:      ; docker compose down
docker-logs:      ; docker compose logs -f api worker
# usage: make docker-admin EMAIL=you@example.com
docker-admin:     ; docker compose run --rm api python -m app.api.admin $(EMAIL)
docker-demo:      ; $(DEMO_COMPOSE) up -d --build --wait && $(DEMO_COMPOSE) logs api | grep -A6 "DEMO MODE" ; echo "Demo: http://localhost:$${MEDIAFLOW_DEMO_PORT:-8081}"
# Stops the demo stack and deletes its volumes (demo data is disposable; real data is untouched).
docker-demo-down: ; $(DEMO_COMPOSE) down --volumes
