PYTHON ?= python3
DATA_MANIFEST := data/generated/manifest.json
GENERATOR_SOURCES := $(addprefix src/tailsampler/,fleet.py traffic.py otlp.py model.py)
export PYTHONPATH := src

.PHONY: setup lock data train evaluate diagrams serve test lint clean

setup:
	$(PYTHON) -m pip install -c requirements.lock -e ".[dev]"

lock:
	$(PYTHON) scripts/freeze_lock.py

data: $(DATA_MANIFEST)

$(DATA_MANIFEST): configs/simulation.toml $(GENERATOR_SOURCES)
	$(PYTHON) -m tailsampler.cli generate

train: data
	$(PYTHON) -m tailsampler.cli train

evaluate: train
	$(PYTHON) -m tailsampler.cli evaluate
	$(PYTHON) scripts/render_results.py

diagrams:
	$(PYTHON) scripts/render_figures.py

serve:
	$(PYTHON) -m tailsampler.cli serve

test:
	$(PYTHON) -m pytest

lint:
	ruff check .
	ruff format --check .

clean:
	rm -rf .pytest_cache .ruff_cache build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
