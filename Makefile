.PHONY: backend-test backend-lint backend-format frontend-build frontend-format-check

backend-test:
	python -m pytest backend

backend-lint:
	ruff check backend

backend-format:
	ruff format backend
	ruff check --fix backend

frontend-build:
	cd frontend && npm run build

frontend-format-check:
	cd frontend && npx prettier --check src e2e
