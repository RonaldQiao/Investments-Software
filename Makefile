PYTHON := .venv/bin/python

setup:
	/opt/homebrew/bin/python3.12 -m venv .venv
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
