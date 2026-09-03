PYTHON := .venv/bin/python

run:
	.venv/bin/uvicorn app.main:app --port 8000

seed:
	$(PYTHON) scripts/seed_demo.py

test:
	$(PYTHON) -m pytest
