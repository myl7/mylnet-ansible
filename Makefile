.PHONY: format format-check lint test

YAML_FILES := $(shell find . -type f -name '*.yaml')
PY_FILES := $(shell find . -type f -name '*.py')

format:
	prettier --write $(YAML_FILES)
	ruff check --fix $(PY_FILES)

format-check:
	prettier --check $(YAML_FILES)
	ruff check $(PY_FILES)

lint:
	mypy --check-untyped-defs $(PY_FILES)

test:
	python3 -m unittest discover -s tests
