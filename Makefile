.PHONY: backend-test backend-lint backend-format backend-format-check frontend-build frontend-format-check

ARCHITECTURE_PYTHON ?= .venv-architecture/bin/python
CI_REPORT_ROOT ?= .architecture/ci-reports
.PHONY: architecture architecture-build architecture-test

architecture-build:
	cd frontend && npm run architecture:build

architecture: architecture-build
	$(ARCHITECTURE_PYTHON) -m tools.architecture.server --report-dir "$(CI_REPORT_ROOT)"

architecture-test:
	$(ARCHITECTURE_PYTHON) -m pytest tools/architecture -q
	node --test tools/architecture/frontend_index.test.mjs tools/architecture/frontend_metrics.test.mjs

backend-test:
	python -m pytest backend

backend-lint:
	ruff check backend

backend-format-check:
	ruff format --check backend

backend-format:
	ruff format backend
	ruff check --fix backend

frontend-build:
	cd frontend && npm run build

frontend-format-check:
	cd frontend && npm run format:check
