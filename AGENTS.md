# AGENTS.md

## Project

Transaction webhook service: accept webhooks quickly, persist durably in PostgreSQL, and process asynchronously with Celery and Redis. Deployed on Render.

## Read order

1. `ASSESSMENT_QUESTION.md`
2. `SPEC.md`
3. `PLAN.md`
4. `CLAUDE.md`

If the documents conflict, stop and ask before changing code.

## Current status

Phases 0–7 are tracked in `PLAN.md`. Work on one phase at a time and update its checkboxes as tasks complete.

Free Render has no background workers, so Phase 6 runs API + Celery worker + beat in one free web service via `app/render_start.py`. A keepalive workflow and the free-tier platform limits are documented in `README.md`.

## Commands

```powershell
# Local infrastructure
docker compose up -d postgres redis

# Install (Python 3.12)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Database
alembic upgrade head

# API
uvicorn app.main:app --reload

# Worker (Windows: run beat as a separate process)
celery -A app.celery_app.celery_app worker --loglevel=INFO --pool=threads --concurrency=4
celery -A app.celery_app.celery_app beat --loglevel=INFO

# Render-style supervisor (migrations + API + worker + beat)
python -m app.render_start

# Quality
ruff check .
ruff format --check .
pytest -q
pytest -m e2e -q
```

## Invariants

The full list is in `CLAUDE.md`. The most important:

- PostgreSQL is the source of truth; Redis is only transport.
- Webhook path: validate, durable commit, return `202`; no 30-second work and no blocking dispatch.
- Idempotency is enforced by the database unique constraint.
- Public status values are only `PROCESSING` and `PROCESSED`.
- The transaction query returns a one-element JSON array.
- Never hold a DB session during sleeps or network calls.
- Timestamps are UTC with `Z`; money uses `Decimal`/`NUMERIC(18,2)`.
- Reconciliation is mandatory for undispatched and lease-expired rows.

## Workflow

- State the intended change, implement it in small steps, then run lint and tests.
- Update `PLAN.md` and any affected docs.
- Do not commit, push, or create a PR unless the user explicitly asks.
