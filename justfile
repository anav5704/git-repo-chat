set dotenv-load := true

# Start Postgres and wait until its healthcheck passes.
up:
	docker compose up -d --wait

# Stop Postgres. The named volume keeps the data.
down:
	docker compose down

# Apply numbered SQL migrations. A second run does not reapply them.
migrate:
	uv run python -m db.migrate

# Lint and typecheck.
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright

# Run tests.
test:
	uv run pytest
