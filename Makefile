.PHONY: bootstrap bootstrap-service format lint typecheck test test-unit test-integration test-property test-e2e test-security \
        schemas bench-smoke demo demo-reset docs paper docker-build docker-smoke db-migrate db-current ci clean

VENV := .venv
PY := $(VENV)/bin/python

bootstrap:
	uv venv --python 3.12 $(VENV)
	uv pip install -e ".[dev]" --python $(PY)

bootstrap-service:
	uv venv --python 3.12 $(VENV)
	uv pip install -e ".[dev,service]" --python $(PY)

format:
	$(PY) -m ruff format src tests examples

lint:
	$(PY) -m ruff check src tests examples

typecheck:
	$(PY) -m mypy src

test:
	$(PY) -m pytest tests/ -q

test-unit:
	$(PY) -m pytest tests/unit -q

test-integration:
	$(PY) -m pytest tests/integration -q

test-property:
	$(PY) -m pytest tests/property -q

test-e2e:
	@echo "Playwright web-UI e2e tests are not implemented in this session (no web dashboard); see STATUS.md."

test-security:
	$(PY) -m pytest tests/unit/test_agent_skills.py tests/unit/test_foundation.py -k "traversal or corruption or tamper" -q

schemas:
	$(PY) -c "import json,glob; [json.load(open(f)) for f in glob.glob('spec/**/*.json', recursive=True)]; print('All spec JSON files parse as valid JSON.')"

bench-smoke:
	$(PY) -m skillrewind.bench.cli generate --preset smoke --seed 42 --output .runs/cases
	$(PY) -m skillrewind.bench.cli run --method static-multitrace --cases .runs/cases --output .runs/run-smoke
	$(PY) -m skillrewind.bench.cli score --run .runs/run-smoke
	$(PY) -m skillrewind.bench.cli report --run .runs/run-smoke --format markdown

demo:
	$(PY) examples/poisoned-descendant/run_demo.py

demo-reset:
	@if [ "$(FORCE)" = "1" ]; then \
		rm -rf .skillrewind-demo; \
		echo "Removed .skillrewind-demo"; \
	else \
		read -p "Remove ./.skillrewind-demo ? [y/N] " ans; \
		if [ "$$ans" = "y" ] || [ "$$ans" = "Y" ]; then rm -rf .skillrewind-demo; echo "Removed .skillrewind-demo"; \
		else echo "Aborted."; fi \
	fi

docs:
	@echo "Static docs live under docs/. No documentation build tool is wired in this session."

paper:
	cd paper && latexmk -pdf -interaction=nonstopmode -halt-on-error -outdir=build main.tex

docker-build:
	@echo "Docker images are not implemented in this session; see STATUS.md."

docker-smoke:
	@echo "Docker images are not implemented in this session; see STATUS.md."

db-migrate:
	@if [ -z "$$SKILLREWIND_DATABASE_URL" ]; then \
		echo "SKILLREWIND_DATABASE_URL is not set. Example: export SKILLREWIND_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/skillrewind"; \
		exit 1; \
	fi
	$(PY) -m alembic upgrade head

db-current:
	@if [ -z "$$SKILLREWIND_DATABASE_URL" ]; then \
		echo "SKILLREWIND_DATABASE_URL is not set."; exit 1; \
	fi
	$(PY) -c "from skillrewind.persistence.service.engine import build_engine, schema_current; import os; e = build_engine(os.environ['SKILLREWIND_DATABASE_URL']); ok, detail = schema_current(e); print(detail); raise SystemExit(0 if ok else 1)"

ci: lint typecheck test schemas bench-smoke

clean:
	rm -rf build dist *.egg-info src/*.egg-info paper/build paper/rendered paper/rendered-final .runs .skillrewind-demo
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
