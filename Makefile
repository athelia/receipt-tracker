VENV := .venv
BIN := $(VENV)/bin

.PHONY: install check lint test format lock serve

# Create the virtualenv and install pinned dependencies plus this package.
install:
	python3 -m venv $(VENV)
	$(BIN)/pip install -r requirements-dev.txt
	$(BIN)/pip install --no-deps -e .

# Lint and tests: the one command to run before committing.
check: lint test

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

test:
	$(BIN)/pytest -q

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

# Regenerate the pinned lockfiles from pyproject.toml.
lock:
	$(BIN)/pip-compile --quiet --strip-extras -o requirements.txt pyproject.toml
	$(BIN)/pip-compile --quiet --strip-extras --extra dev -o requirements-dev.txt pyproject.toml

serve:
	$(BIN)/receipt-tracker serve
