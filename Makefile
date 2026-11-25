.PHONY: install test serve fixtures docker-build docker-up docker-down docker-logs help

PY ?= python

help:
	@echo "targets: install test serve fixtures docker-build docker-up docker-down docker-logs"

install:  ## Python deps + Chromium for Playwright
	$(PY) -m pip install -r requirements.txt
	$(PY) -m playwright install chromium

test:  ## offline test suite (MockLLM + fixture site, no network, no spend)
	$(PY) -m pytest -q

serve:  ## web UI + API at http://localhost:8000
	$(PY) -m webforge serve

fixtures:  ## serve the offline test site (forms, data pages) at http://localhost:8765
	$(PY) -m webforge serve-fixtures

docker-build:
	docker build -t webforge-backend .

docker-up:  ## build + start in Docker, then open http://localhost:8000 and paste your Backboard key
	docker compose up --build

docker-down:  ## stop the container (data volumes are kept; `docker compose down -v` also wipes them)
	docker compose down

docker-logs:
	docker compose logs -f
