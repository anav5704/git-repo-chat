set dotenv-load := true

up:
	docker compose up -d --wait

down:
	docker compose down

migrate:
	uv run python -m db.migrate

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright

test:
	uv run pytest
