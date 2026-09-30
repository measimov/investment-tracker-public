.PHONY: backend-test backend-lint backend-format backend-format-check frontend-build frontend-format-check

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
