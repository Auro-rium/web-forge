.PHONY: install test serve demo-offline bench report docker-build docker-up docker-down docker-logs help

PY ?= python
REPEAT ?= 3

help:
	@echo "targets: install test serve demo-offline bench report docker-build docker-up docker-down docker-logs"

install:  ## Python deps + Chromium for Playwright
	$(PY) -m pip install -r requirements.txt
	$(PY) -m playwright install chromium

test:  ## offline test suite (MockLLM + fixture site, no network, no spend)
	$(PY) -m pytest -q

serve:  ## web UI + API at http://localhost:8000
	$(PY) -m webforge serve

demo-offline:  ## serve the offline fixture site at http://localhost:8765
	$(PY) -m webforge serve-fixtures

bench:  ## LIVE benchmark, both modes (spends real money; see WEBFORGE_BUDGET_USD)
	$(PY) -m webforge bench run --mode cold --repeat $(REPEAT)
	$(PY) -m webforge bench run --mode learned --repeat $(REPEAT)

report:  ## usage: make report COLD=runs/bench_cold_<ts>.jsonl LEARNED=runs/bench_learned_<ts>.jsonl
	$(PY) -m webforge bench report $(COLD) $(LEARNED)

docker-build:
	docker build -t webforge-backend .

docker-up:  ## build + start in Docker, then open http://localhost:8000 and paste your Backboard key
	docker compose up --build

docker-down:  ## stop the container (data volumes are kept; `docker compose down -v` also wipes them)
	docker compose down

docker-logs:
	docker compose logs -f
