# Repository Guidelines

## Project Structure & Architecture

This Python 3.12 FastAPI service provides authentication/RBAC and engineer-route
planning. Source code lives in `src/`: `auth/` and `planning/` each follow Clean
Architecture layers—`domain`, `application`, `infrastructure`, `presentation`,
and `entrypoint`. Keep domain code independent of framework and database code;
depend on application interfaces and wire concrete adapters in `entrypoint/ioc/`.

Tests are in `tests/unit/` and `tests/integration/`. Database migrations are in
`alembic/`; deployment configuration is in `conf/`, `Dockerfile`, and
`docker-compose.yml`. Use `scripts/` for operational helpers and `docs/` for
project documentation. CSV demo inputs belong in `data/`.

## Build, Test, and Development Commands

- `uv sync --all-groups` installs locked runtime, development, and test dependencies.
- `make infra-up` starts PostgreSQL, Valhalla, and Nominatim for local development.
- `make app` runs the FastAPI application through Gunicorn with reload enabled.
- `make migrate` applies Alembic migrations; create one with `make migrate-create NAME='add_jobs_table'`.
- `make test` runs pytest in parallel with coverage output.
- `make up` / `make down` start or stop the complete Docker Compose stack.

## Coding Style & Naming Conventions

Use four-space indentation, Python type hints, and `snake_case` for modules,
functions, and variables; use `PascalCase` for classes. Keep HTTP schemas and
handlers in `presentation/http/`, use cases/services in `application/`, and SQLAlchemy
implementations in `infrastructure/`. Ruff enforces an 88-character line length and
selects `E`, `F`, `I`, and `B`; isort uses the Black profile. Before committing, run
`uv run pre-commit run --all-files`.

## Testing Guidelines

Write pytest tests named `test_*.py`, with test functions named `test_*`. Place
fast business-rule tests under the matching `tests/unit/<layer>/` path; reserve
`tests/integration/` for real adapter/database behavior. Cover changed validation,
authorization, and planning edge cases. Run a focused test with
`uv run pytest tests/unit/application/test_multiday_planning.py` and the full suite
with `make test`.

## Commits, Pull Requests, and Configuration

Recent history uses concise conventional-style subjects such as `feat: ...` and
`refactor: ...`; follow that pattern and keep each commit scoped. Pull requests
should explain the behavioral change, list validation performed, link the issue when
available, and include API examples or screenshots for externally visible changes.

Never commit populated `.env` files, credentials, or private keys. Start from
`.env.example` or `.env.docker.example`; document any new required setting there.
