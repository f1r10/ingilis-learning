# Language Learning Platform - developer shortcuts
.PHONY: up down logs build seed migrate revision pyc lint typecheck fmt test test-integration verify-phase12

up:            ## start the full stack
	docker compose up --build

down:          ## stop the stack
	docker compose down

logs:          ## tail backend + worker logs
	docker compose logs -f backend worker

seed:          ## run minimal dev seed (admin + languages + branding)
	docker compose exec backend python -m scripts.seed

migrate:       ## apply migrations
	docker compose exec backend alembic upgrade head

revision:      ## autogenerate a migration: make revision MSG="add x"
	docker compose exec backend alembic revision --autogenerate -m "$(MSG)"

pyc:           ## byte-compile backend to catch syntax errors
	cd backend && python -m compileall -q app migrations scripts

lint:          ## ruff over the backend
	cd backend && ruff check app

fmt:
	cd backend && ruff format app

typecheck:     ## tsc no-emit over the frontend
	cd frontend && npm run typecheck

test:          ## fast developer tests (skips anything needing Postgres/Redis/MinIO)
	cd backend && pytest tests --ignore=tests/integration

test-integration: ## backend E2E tests against services you started yourself (skips when one is missing)
	cd backend && pytest tests/integration -v

verify-phase12: ## MANDATORY Phase 1-2 acceptance gate (starts PG/Redis/MinIO, migrates, seeds, runs everything)
	bash scripts/verify_phase12.sh
