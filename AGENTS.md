# Repository Guidelines

## Project Structure & Module Organization

This repository is the Python backend for authentication, RBAC, and daily/multi-day route planning.

- `src/auth/` contains sessions, users, roles, and authentication HTTP adapters.
- `src/planning/` is a Clean Architecture bounded context: `domain`, `application`, `infrastructure`, `presentation`, and `entrypoint`.
- `tests/unit/` contains isolated unit tests; `tests/integration/` contains database-backed tests.
- `analytics/` stores the technical specifications and implementation plan; `docs/` stores diagrams and API documentation.
- `conf/`, `docker-compose.yml`, `.env.example`, and `.env.docker.example` configure local and container execution.

Keep domain rules independent from SQLAlchemy, FastAPI, and external geoservices. Put adapters in `infrastructure` and HTTP concerns in `presentation`.

## Build, Test, and Development Commands

Use Python 3.12 and the project-managed `uv` environment.

- `make infra-up` starts PostgreSQL, Valhalla, and Nominatim.
- `make migrate` applies Alembic migrations; `make migrate-down` rolls back one revision.
- `make app` runs FastAPI through Gunicorn with reload enabled.
- `make test` runs the test suite with coverage and parallel workers.
- `make up` / `make down` starts or stops the complete Docker Compose stack.
- `make seed-planning-demo` loads demonstration planning data.

For focused checks, use `uv run pytest tests/unit/path/to/test_file.py -q`. Run `uv run ruff check .` and `uv run ruff format --check .` before submitting changes.

## Coding Style & Naming Conventions

Use four spaces, type hints, and 88-character lines. Follow Ruff and isort configuration in `pyproject.toml`; use `snake_case` for modules/functions, `PascalCase` for classes, and descriptive domain names such as `PlanningValidator`. Prefer async APIs for I/O and immutable dataclasses for value-oriented domain entities.

## Testing Guidelines

Tests use pytest and pytest-asyncio. Name files `test_*.py`, classes `Test*`, and functions `test_*`. Add unit coverage for every business rule and integration coverage for transaction, migration, and PostgreSQL behavior. Keep external geoservices out of unit tests by using `STATIC_TEST` or test doubles.

## Commit & Pull Request Guidelines

Recent commits use short, imperative-style prefixes such as `feat:`, `refactor:`, and `fix:`; follow that convention (for example, `feat: validate planning snapshots`). Pull requests should explain the behavior change, link the relevant specification or issue, list migrations and configuration changes, and include test commands/results. Include API examples or screenshots when changing HTTP responses or planning UI contracts.

## Security & Configuration Tips

Never commit `.env` files, credentials, session secrets, or geocoder data. Start from the example environment files. Review migration SQL and project scoping carefully: planning and administrative queries must enforce `project_id` ownership and must not mutate job lifecycle status during a planning run.
