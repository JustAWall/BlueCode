PY ?= .venv/bin/python
PREFIX ?= $(HOME)/.local

.PHONY: run test lint venv install uninstall

venv:
	python3 -m venv .venv
	.venv/bin/pip install -q -e '.[dev]'

install: venv
	mkdir -p $(PREFIX)/bin
	sed 's|@ROOT@|$(PWD)|' packaging/bluecode-launcher.sh > $(PREFIX)/bin/bluecode
	chmod +x $(PREFIX)/bin/bluecode
	@echo "installed $(PREFIX)/bin/bluecode"

uninstall:
	rm -f $(PREFIX)/bin/bluecode

run:
	$(PY) -m bluecode

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m pyflakes bluecode tests
