# CLAUDE.md — AI Engineering Guide

This file defines how AI assistants must work in this repository. Read it before changing code.

## Read order

1. `docs/ASSESSMENT_QUESTION.md` — the original brief.
2. `docs/SPEC.md` — requirements, API contract, acceptance criteria.
3. `docs/PLAN.md` — architecture, phases, verification steps.
4. This file — coding rules and invariants.

If these conflict, stop and ask. Do not silently invent requirements.

## Non-negotiable invariants

1. **Webhook path:** validate → durable PostgreSQL commit → return `202`. Do not do 30-second work in the request path. Redis dispatch happens after the response and is best-effort.
2. **Idempotency:** enforce with a database primary key/unique constraint and `INSERT ... ON CONFLICT DO NOTHING`. Never use check-then-insert.
3. **PostgreSQL is the source of truth.** Redis is only the Celery transport. Losing Redis must never lose an accepted transaction.
4. **Public status values:** only `PROCESSING` and `PROCESSED`. Never expose lease, token, retry, or error fields through the API.
5. **Query contract:** `GET /v1/transactions/{transaction_id}` returns a one-element JSON array. Keep the exact field names and types.
6. **Processing delay** comes from `PROCESSING_DELAY_SECONDS` (default `30`). Never hardcode 30 seconds in application logic.
7. **Worker safety:** claim with a lease and token; never hold a database session during the 30-second sleep. Redelivered tasks must be idempotent.
8. **Reconciliation is mandatory.** Committed rows that are never dispatched, or whose worker lease expired, must be recovered.
9. **Timestamps:** UTC only, ISO 8601 with a `Z` suffix. Never use naive datetimes or `datetime.utcnow()`.
10. **Money:** store `NUMERIC(18,2)` and use `Decimal`. Never use floats for amounts. JSON responses emit amounts as numbers.
11. **Health:** `GET /` is a liveness check and must not depend on PostgreSQL, Redis, or the worker.
12. **Duplicates:** duplicate webhooks return `202`, create no new row, and never reset `created_at`, `processed_at`, or status.

## Architecture rules

- Stack: Python 3.12, FastAPI, SQLAlchemy 2.0 (sync) with psycopg 3, Pydantic v2, Celery, Redis.
- Keep routes thin. Business logic belongs in `app/services/`.
- Configuration comes from environment variables via `pydantic-settings`. No secrets, URLs, or credentials in code.
- Use Alembic for schema changes. Never call `Base.metadata.create_all()` in production startup.
- Use short-lived DB sessions. Never hold a session or transaction open across a network call or sleep.
- Log structured events with `transaction_id`, `event`, and durations where relevant. Never log secrets or connection strings.
- Add dependencies only when necessary; prefer the standard library and existing packages. Ask before introducing a new framework or service.
- Webhook authentication/signature verification is out of scope unless the user explicitly requests it.

## Code style

- Use type hints on public functions and non-trivial internal functions.
- Keep functions small and names explicit. Prefer simple code over clever abstractions.
- Follow existing patterns before creating new ones.
- Comments only where the reason is non-obvious. Do not narrate what the code already says.
- Raise specific exceptions; never use bare `except:`. Roll back database sessions on errors.
- Use enums/constants instead of magic strings (`PROCESSING`, `PROCESSED`, event names).
- Preserve the public API contract. If a contract change is needed, update `docs/SPEC.md` first.
- No dead code, debug prints, or commented-out blocks.

## Testing rules

- Every behavior change needs a test. Test names should describe the requirement.
- Use PostgreSQL for integration tests; never SQLite.
- Never use fixed `sleep` calls in tests. Override `PROCESSING_DELAY_SECONDS` and poll with a deadline.
- Mock dispatch in API tests so Redis is not required for the default suite.
- Use real Redis and a real Celery worker only for the E2E layer.
- Test duplicate delivery, redelivery, stale leases, and recovery explicitly.
- Keep the default `pytest` suite fast; mark slow/E2E tests.
- Run `ruff check` and the relevant tests before declaring a task complete.

## Workflow

1. Read `docs/SPEC.md` and `docs/PLAN.md`; pick the next unchecked phase.
2. State what you are about to change and why.
3. Implement in small steps; keep unrelated files untouched.
4. Run lint, types (if configured), and relevant tests.
5. Update `docs/PLAN.md` checkboxes and any affected docs.
6. Report what changed, what was verified, and what remains.
7. Do not commit, push, or create a PR unless the user explicitly asks.

## Verification checklist

Before finishing any task, confirm:

- [ ] API contract unchanged unless the spec was updated.
- [ ] Idempotency still enforced by the database.
- [ ] No 30-second work in a request path.
- [ ] No session held during sleeps or network calls.
- [ ] Duplicate/redelivered tasks are safe.
- [ ] Timestamps are UTC with `Z`.
- [ ] Amounts use `Decimal`/`NUMERIC`, not floats.
- [ ] No secrets or `.env` files committed.
- [ ] Ruff and relevant tests pass.
- [ ] Docs/plan updated.

## Prohibited

- In-memory-only queues for accepted webhooks.
- Relying solely on FastAPI `BackgroundTasks` for durability.
- `time.sleep` or Celery task execution inside a web request.
- Float money, naive datetimes, `datetime.utcnow()`.
- Exposing internal columns in API responses.
- `DROP TABLE` or destructive migrations in production.
- Changing the query response from an array to a bare object.
- Committing generated caches, virtualenvs, Celery beat schedules, or secrets.
