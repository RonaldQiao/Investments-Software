PYTHON := .venv/bin/python
SYSTEM_PYTHON ?= $(shell command -v python3.12 || command -v python3.11 || command -v python3)

setup:
	$(SYSTEM_PYTHON) -m venv .venv
	$(PYTHON) -m pip install -r requirements.txt

run:
	.venv/bin/uvicorn app.main:app --port 8000

seed:
	$(PYTHON) scripts/seed_demo.py --history 60

test:
	$(PYTHON) -m pytest

lint:
	.venv/bin/ruff check .

backup:
	$(PYTHON) scripts/backup.py

backfill-benchmark:
	$(PYTHON) scripts/backfill_benchmark.py

install-agent:
	bash scripts/install_launchd.sh

uninstall-agent:
	bash scripts/uninstall_launchd.sh
