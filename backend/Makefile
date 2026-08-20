.PHONY: lint format typecheck boundaries test ci dev up down migrate rebuild-graph

lint:
	uv run ruff format --check .
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

boundaries:
	uv run lint-imports

test:
	uv run pytest -q

ci: lint typecheck boundaries test

dev:
	uv run uvicorn devloop.api.app:app --reload --port 8100

up:
	docker compose up -d

down:
	docker compose down

migrate:
	uv run alembic upgrade head

rebuild-graph:
	uv run devloop rebuild-graph
