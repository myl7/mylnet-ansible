.PHONY: format format-check lint lint-mypy lint-ansible test

YAML_FILES := $(shell find . -type f -name '*.yaml')
PY_FILES := $(shell find . -type f -name '*.py')

# mypy resolves imports with its own interpreter, so it cannot see the venv
# ansible runs in, and plugins/filter/proxy.py imports ansible. Take that
# interpreter from the ansible CLI, which is a console script of that venv.
ANSIBLE_PYTHON := $(shell head -1 "$$(command -v ansible)" | cut -c3-)

format:
	prettier --write $(YAML_FILES)
	ruff check --fix $(PY_FILES)

format-check:
	prettier --check $(YAML_FILES)
	ruff check $(PY_FILES)

lint: lint-ansible lint-mypy

lint-ansible:
	find playbooks -maxdepth 1 -type f -name '*.yaml' ! -name 'secrets.yaml' -print0 | xargs -0 ansible-lint

lint-mypy:
	mypy --check-untyped-defs --python-executable $(ANSIBLE_PYTHON) $(PY_FILES)

test:
	python3 -m unittest discover -s tests
