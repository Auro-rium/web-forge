.PHONY: install test serve demo-offline bench report docker-build help

PY ?= python
REPEAT ?= 3

help:
	@echo "targets: install test serve demo-offline bench report docker-build"

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
