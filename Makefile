.PHONY: setup lint unit integration live test cov done api migrate web web-dev
setup:        ; pip install -r requirements-dev.txt
lint:         ; ruff check .
unit:         ; pytest -m unit
integration:  ; pytest -m integration
live:         ; RUN_LIVE=1 pytest -m live -s
test:         ; pytest
cov:          ; pytest -m "not live" --cov --cov-report=term-missing
done: lint unit integration cov
	@echo "Automated gate passed. Now tick docs/DEFINITION_OF_DONE.md by hand."
api:          ; uvicorn app.api.main:app --reload
migrate:      ; alembic upgrade head
web:          ; cd frontend && npm install && npm run build
web-dev:      ; cd frontend && npm install && npm run dev
