# Implementation Plan — Transaction Webhook Processing Service

| Field | Value |
| --- | --- |
| Status | Phases 0–5 complete; Phase 6 deployed and tested; Phase 7 in progress |
| Source specification | [`SPEC.md`](./SPEC.md) |
| Source brief | [`ASSESSMENT_QUESTION.md`](./ASSESSMENT_QUESTION.md) |
| Stack | Python 3.12, FastAPI, PostgreSQL, Celery, Redis, Render |
| Last updated | 2026-11-03 |

This plan turns the specification into architecture, modules, ordered tasks, and verification steps. It does not change the API contract or acceptance criteria. If implementation forces a requirement change, update `SPEC.md` first, then this plan.

## 1. Execution rules

- Execute phases in order; do not start a phase until the previous exit criteria pass.
- `AC-*` references point to `SPEC.md` section 10.
- Commit at the end of each phase with a descriptive message.

## 2. Architecture

```text
 POST webhook / GET status / GET health
                  |
                  v
       +-----------------------------+          +----------------------+
       | Render free web service     |--------->| PostgreSQL (free)    |
       | FastAPI + supervisor        |          | system of record     |
       | + Celery worker             |          +----------+-----------+
       | + beat (1 instance)         |                     ^
       +--------------+--------------+                     |
                      | best-effort dispatch after 202      |
                      v                                     |
       +-----------------------------+                      |
       | Key Value / Redis (free)    |                      |
       | Celery broker only          |                      |
       +--------------+--------------+                      |
                      v                                     |
                      +-------------------------------------+
                         worker claims/completions in-process
```

The free Render plan has no background workers, so API, worker, and beat run in one web service under `app/render_start.py`. A keepalive workflow reduces idle spin-downs. The paid deployment path splits the worker into its own service without code changes.

### 2.1 Components

| Component | Responsibility |
| --- | --- |
| FastAPI web service | Validation, idempotent insert, health, query, post-response dispatch. |
| PostgreSQL | Durable system of record; unique constraint enforces idempotency; also the reconciliation source. |
| Redis / Key Value | Celery broker. Transport only; losing it must not lose committed transactions. |
| Celery worker | Claims rows, simulates 30 seconds of work, records completion; threads allow concurrent sleeps. |
| Celery beat | Reconciliation every 5 seconds; embedded in the single worker instance. |
| Reconciliation | Re-dispatches committed rows that were never dispatched or whose lease expired. |
| Render supervisor | `app/render_start.py` runs migrations, then supervises API, worker, and beat in one free web service. |
| Render | Free web + free PostgreSQL + free Key Value in Singapore; one region. |
| GitHub Actions | Lint/tests on push; keepalive ping after deployment. |

### 2.2 Critical flows

**First webhook**

1. Pydantic validates the payload.
2. One PostgreSQL `INSERT ... ON CONFLICT (transaction_id) DO NOTHING RETURNING ...` commits.
3. If inserted, return `202` and schedule a post-response background dispatch.
4. The background task enqueues the Celery task and sets `dispatched_at`. Failure is logged and never changes the `202`.
5. The worker atomically claims the row (status `PROCESSING`, fresh lease, random token), closes the DB session, sleeps 30 seconds, then marks it `PROCESSED` only if the token still matches.

**Duplicate webhook**

1. Same validation and insert-on-conflict.
2. No row inserted, no dispatch, no state reset.
3. Returns the same `202` acknowledgement.
4. Reconciliation covers the rare case where the first delivery's dispatch was lost.

**Status query**

1. Fetch by primary key.
2. `404` if absent; otherwise a one-element JSON array with public fields only.
3. `processed_at` is null until completion.

**Recovery**

1. Reconciliation takes a transaction-scoped PostgreSQL advisory lock.
2. Finds `PROCESSING` rows that are undispatched/stale or whose lease expired.
3. Re-enqueues them and updates `dispatched_at`.
4. Claim/lease/token makes redelivery safe; a late stale completion is a no-op.

### 2.3 Key decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Acknowledgement path | Durable insert + `202`, then post-response dispatch | Redis/worker failures cannot break the 500ms path; reconciliation covers lost dispatch. |
| Idempotency | Unique constraint + `ON CONFLICT DO NOTHING` | Race-safe under concurrent duplicate deliveries. |
| Source of truth | PostgreSQL | Redis is transport; the committed row is the durable queue item. |
| Task safety | Atomic claim + lease + random token; token-checked completion | Handles at-least-once delivery, worker crashes, and stale redelivery. |
| Worker concurrency | Celery `threads` pool (default 20) | `time.sleep(30)` releases the GIL; many transactions sleep concurrently. |
| Reconciliation | Beat every 5s + worker-startup sweep + advisory lock | Recovers commit-before-dispatch gaps, Redis loss, and crashes. |
| Money | `NUMERIC(18,2)` + `Decimal`, explicit JSON number serializer | Avoids float errors; matches the contract. |
| Public status | `PROCESSING` / `PROCESSED` only | Matches the brief; lease/token/error fields stay internal. |
| Deployment | Render free web (API + worker + beat) + free PostgreSQL + free Key Value | User selected free tiers; free plan has no background worker, so a supervisor runs all processes in one service; keepalive and limitations documented. |

## 3. Project layout

```text
confluencr-assessment/
├── app/
│   ├── main.py                 # FastAPI factory, lifespan, middleware
│   ├── config.py               # pydantic-settings
│   ├── db.py                   # engine, SessionLocal, get_db
│   ├── models.py               # Transaction model
│   ├── schemas.py              # request/response models
│   ├── logging_config.py       # structured logging
│   ├── celery_app.py           # Celery config, beat schedule, signals
│   ├── render_start.py         # Render supervisor: migrations, API, worker, beat
│   ├── tasks.py                # process_transaction, reconcile_transactions
│   ├── api/health.py
│   ├── api/webhooks.py
│   ├── api/transactions.py
│   └── services/
│       ├── transactions.py     # insert, query, claim, complete, release
│       ├── dispatch.py         # enqueue + dispatched_at update
│       └── reconcile.py        # stale-row sweep
├── alembic/ + alembic.ini
├── tests/{unit,integration,e2e}/
├── scripts/{acceptance_check.py,load_test.py}
├── .github/workflows/{ci.yml,keepalive.yml}
├── Dockerfile, .dockerignore, docker-compose.yml
├── render.yaml, pyproject.toml, .env.example, .gitignore
├── CLAUDE.md, README.md, TEST_RESULTS.md
├── ASSESSMENT_QUESTION.md, SPEC.md, PLAN.md
```

## 4. Data model and migrations

### 4.1 `transactions`

Public:

- `transaction_id` — primary key, text.
- `source_account`, `destination_account`, `currency` — text, not null.
- `amount` — `NUMERIC(18,2)`, not null, `CHECK (amount > 0)`.
- `status` — text, not null, `CHECK (status IN ('PROCESSING','PROCESSED'))`.
- `created_at` — timestamptz, not null.
- `processed_at` — timestamptz, null.

Internal:

- `dispatched_at` — timestamptz, null; set after a successful enqueue.
- `processing_started_at` — timestamptz, null.
- `lease_expires_at` — timestamptz, null.
- `processing_token` — UUID, null; identifies the active claim.
- `processing_attempts` — integer, not null, default 0.
- `last_error` — text, null; truncated failure summary.

Indexes:

- Primary key on `transaction_id`.
- Partial index `(status, dispatched_at, lease_expires_at) WHERE status = 'PROCESSING'`.
- Operational index `(status, created_at)`.

### 4.2 Migration and DB rules

- Alembic with one initial migration; production never calls `create_all()`.
- Render runs `alembic upgrade head` from `app/render_start.py` before starting API/worker; migrations are idempotent and rerun after free-instance restarts.
- Tests run migrations once per session, then truncate between tests.
- SQLAlchemy 2.0 synchronous engine with psycopg 3; normalize `postgresql://` to `postgresql+psycopg://`.
- Engine: `pool_pre_ping=True`, bounded pool, `pool_recycle` below platform idle timeouts.
- Web: session per request. Worker: short session per operation; never hold a session during the 30-second sleep.
- All timestamps stored and returned as UTC (`timestamptz`, ISO 8601 `Z`).

## 5. Implementation phases

### Phase 0 — Repository foundation

- [x] Initialize git and root `.gitignore`.
- [x] Create `pyproject.toml` (runtime/dev dependencies, pytest markers, ruff config).
- [x] Create `.env.example` with every variable.
- [x] Create `Dockerfile` (`python:3.12-slim`, non-root user, install package, default web command).
- [x] Create `.dockerignore`.
- [x] Create `docker-compose.yml` with PostgreSQL 16, Redis 7, API, and worker.
- [x] Create `CLAUDE.md` with invariants, coding rules, and verification checklist (`AGENTS.md` was removed on 2026-11-03; it duplicated `CLAUDE.md` and the README).
- [x] Create README skeleton and GitHub Actions CI skeleton.
- [x] Verify `docker compose up -d postgres redis` and Python install/lint.

**Exit:** containers start, app imports, CI YAML is valid.

### Phase 1 — Configuration, database, model

- [x] `app/config.py` with `DATABASE_URL`, `REDIS_URL`, `PROCESSING_DELAY_SECONDS=30`, `PROCESSING_LEASE_SECONDS=120`, `DISPATCH_STALE_SECONDS=120`, `RECONCILE_INTERVAL_SECONDS=5`, `WORKER_CONCURRENCY=20`, `LOG_LEVEL`, `APP_ENV`; normalize DB URL.
- [x] `app/db.py`: engine, `SessionLocal`, `get_db`.
- [x] `app/models.py`: model, check constraints, indexes.
- [x] Initialize Alembic; write `0001_create_transactions`.
- [x] UTC timestamp helpers producing `Z` format.
- [x] `app/services/transactions.py`: `create_if_absent` (insert-on-conflict), `get_transaction`, `claim_transaction`, `complete_transaction`, `release_transaction`.
- [x] Test migration + insert/read + duplicate no-op.

**Exit:** migration succeeds; insert/query works; duplicate insert does not create a row.

### Phase 2 — API endpoints

- [x] `app/schemas.py`: `TransactionCreate` (required, non-empty strings, positive Decimal), `WebhookAck`, `HealthResponse`, `TransactionResponse` with explicit amount-number and timestamp-`Z` serializers.
- [x] `GET /` exactly per spec.
- [x] `POST /v1/webhooks/transactions`: `202` for new and duplicate; persist; post-response dispatch only for new rows; DB failure → `503` with rollback; log accepted/duplicate and duration.
- [x] `GET /v1/transactions/{transaction_id}`: one-element array, `404` unknown, no internal fields.
- [x] Request middleware logging method, path, status, duration.
- [x] Tests: AC-1, AC-2, AC-8, AC-9, AC-11.
- [x] Verify with curl: `202`, immediate `PROCESSING`.

**Exit:** API tests pass; row visible immediately; no 30-second work in the request path.

### Phase 3 — Celery worker and processing

- [x] `app/celery_app.py`: broker from `REDIS_URL`, no result backend, JSON/UTC, `task_acks_late`, `task_reject_on_worker_lost`, prefetch 1, Redis visibility timeout above lease, short broker timeouts.
- [x] `app/services/dispatch.py`: enqueue with `retry=False`; update `dispatched_at` in a fresh session; catch/log all errors; skip `PROCESSED`.
- [x] `app/tasks.py` `process_transaction` (bind=True): atomic claim; skip if processed/leased; close session, sleep configured delay, complete with token check; on error release lease, record error, bounded retry with backoff; log lifecycle events.
- [x] Tests: claim once, second claim blocked; token completion; stale token no-op; redelivery after `PROCESSED` no-op; delay reflected in timestamps.
- [x] Run real local worker with `PROCESSING_DELAY_SECONDS=2`.

**Exit:** real worker processes a webhook; duplicates/redelivery cannot change the completed timestamp.

### Phase 4 — Reconciliation and recovery

- [x] `app/services/reconcile.py`: transaction-scoped advisory lock; select `PROCESSING` rows that are undispatched/stale or lease-expired, ordered by `created_at`, batched; re-enqueue and update `dispatched_at`; log counts/failures.
- [x] `reconcile_transactions` Celery task.
- [x] Beat schedule + worker-startup trigger + best-effort trigger from FastAPI startup.
- [x] Failure tests: dispatch raises after commit → `202` → reconcile later dispatches; expired lease re-dispatched; active lease skipped; `PROCESSED` never re-dispatched.
- [x] Manual recovery: stop Redis/worker after accept, restart, confirm processing without a new webhook.
- [x] Verify data survives API/worker restart.

**Exit:** AC-6, AC-7, AC-10 pass; a committed transaction cannot be lost by dispatch or worker failure.

### Phase 5 — Hardening, load test, documentation

- [x] Structured logging with redaction of secrets/connection strings.
- [x] `scripts/acceptance_check.py`: health, single-transaction lifecycle, duplicate delivery, status contract, PASS/FAIL and exit code.
- [x] `scripts/load_test.py`: async unique webhooks, configurable concurrency, p50/p95/max, non-202 count, optional polling.
- [x] Latency test marked `slow`; verify p95 under 500ms locally.
- [x] E2E test with real Celery worker marked `e2e`.
- [x] CI: lint, unit/integration with PostgreSQL, optional E2E with Redis and worker.
- [x] Update README with architecture, API examples, run/test instructions, design choices.

**Exit:** full local suite, load test, and E2E pass; all acceptance criteria map to tests.

### Phase 6 — Deploy to Render

- [x] Confirm `gh auth status`; create the public repository and push.
- [x] Verify current Render Blueprint schema and plan names; `render.yaml` passes the published JSON Schema.
- [x] Use the free tier per user decision; document that free web services still spin down/idle and have no background workers.
- [x] Create `render.yaml`: free Docker web (health `/`), free PostgreSQL, free Key Value, env wiring, Singapore region.
- [x] Add `app/render_start.py` to run migrations and supervise API + Celery worker + beat in the free web service.
- [x] Add `.github/workflows/keepalive.yml` (enabled by the `RENDER_BASE_URL` repository variable).
- [x] Deploy the Blueprint and confirm the web service is healthy (https://confluencr-assessment-22vd.onrender.com).
- [x] Run acceptance and load scripts against the public URL (results in `TEST_RESULTS.md`).
- [ ] Confirm the web instance stays warm; record live URL, timings, and platform caveats in README.

**Exit:** deployed service passes the same acceptance checks as local, including duplicates, ~30-second processing, fast acknowledgement, and recovery. Free-tier platform limits are documented.

### Phase 7 — Final documentation and publication

- [ ] Finish README: overview/live URL, architecture, curl/PowerShell examples, Docker/native setup, tests, technical choices, trade-offs, limitations.
- [ ] Confirm no secrets or `.env` files are committed; `.gitignore` covers caches, venv, Celery beat schedule, local data.
- [ ] Run `ruff check`, `ruff format --check`, and the full test suite.
- [ ] Push final commit (tag optional).
- [ ] Verify public repo contains all deliverables and the live URL works.

**Exit:** a reviewer can clone, run, test, and evaluate the live service using only the README.

## 6. Testing strategy

### 6.1 Layers

| Layer | Scope | Dependencies | Default |
| --- | --- | --- | --- |
| Unit | Schemas, timestamps, health, service logic | None/mocks | Yes |
| Integration | API + real PostgreSQL, duplicate races, claim/lease, reconciliation | PostgreSQL | Yes |
| E2E | API + PostgreSQL + Redis + real worker | All services | `pytest -m e2e` |
| Load | Concurrent webhook latency | Running stack | Script |
| Failure injection | Dispatch failure, stale lease, restart | PostgreSQL, optional Redis | Yes/manual |

### 6.2 Test database rules

- PostgreSQL only (requires `ON CONFLICT`, partial indexes, `NUMERIC`, advisory locks); never SQLite.
- Run Alembic once per session; truncate `transactions` between tests.
- Override `PROCESSING_DELAY_SECONDS` to a small value (for example `0.05`).
- Mock dispatch for API tests so Redis is not required.
- Use real Redis/worker only for E2E.

### 6.3 Acceptance mapping

| AC | Test file | Assertion |
| --- | --- | --- |
| AC-1 | `tests/unit/test_health.py` | `200`, `HEALTHY`, valid `Z` timestamp. |
| AC-2 | `tests/integration/test_webhook_ingest.py` | `202`, row committed, immediate `PROCESSING`, null `processed_at`. |
| AC-3 | `tests/integration/test_processing.py` | `PROCESSED` after configured delay; `processed_at` set. |
| AC-4 | `tests/integration/test_idempotency.py` | Concurrent/sequential duplicates → one row, all `202`, `created_at` unchanged. |
| AC-5 | `tests/integration/test_latency.py`, `scripts/load_test.py` | Every webhook under 500ms at tested concurrency; all eventually process. |
| AC-6 | `tests/integration/test_recovery.py` | Dispatch failure/stale lease re-enqueued; no loss. |
| AC-7 | `tests/integration/test_recovery.py` | Redelivery after `PROCESSED` no-op; stale token cannot complete. |
| AC-8 | `tests/integration/test_webhook_ingest.py`, `tests/unit/test_schemas.py` | Invalid payload → `422`; no row. |
| AC-9 | `tests/integration/test_query_contract.py` | Unknown ID → `404`. |
| AC-10 | `tests/integration/test_recovery.py` | Data survives new sessions/engine restart. |
| AC-11 | `tests/integration/test_query_contract.py` | Array-of-one, exact fields, types, timestamp format. |

### 6.4 Commands

```powershell
docker compose up -d postgres redis
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload

# second terminal
celery -A app.celery_app.celery_app worker --loglevel=INFO --pool=threads --concurrency=4
# third terminal (Windows: beat cannot run inside the worker process)
celery -A app.celery_app.celery_app beat --loglevel=INFO

ruff check .
pytest -q
pytest -m e2e -q
python scripts/acceptance_check.py --base-url http://localhost:8000
python scripts/load_test.py --base-url http://localhost:8000 --count 50 --concurrency 20
```

## 7. Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | required | PostgreSQL URL; normalized for psycopg. |
| `REDIS_URL` | required | Celery broker. |
| `PROCESSING_DELAY_SECONDS` | `30` | Simulated work; tests use a small value. |
| `PROCESSING_LEASE_SECONDS` | `120` | Claim lease; must exceed processing delay. |
| `DISPATCH_STALE_SECONDS` | `120` | Re-dispatch queued-but-never-started tasks. |
| `RECONCILE_INTERVAL_SECONDS` | `5` | Beat/reconciliation interval. |
| `WORKER_CONCURRENCY` | `20` | Concurrent sleeping tasks. |
| `LOG_LEVEL` | `INFO` | Log verbosity. |
| `APP_ENV` | `development` | Environment label. |
| `PORT` | `8000` | Injected by Render for web. |

## 8. Deployment plan (Render)

### 8.1 Resources

| Resource | Type | Notes |
| --- | --- | --- |
| `confluencr-api` | Docker web service | Always-on, health `/`, pre-deploy migrations, injects `PORT`. |
| `confluencr-worker` | Docker background worker | One instance; Celery threads + embedded beat. |
| `confluencr-db` | PostgreSQL | System of record; same region. |
| `confluencr-redis` | Key Value / Redis | Celery broker; same region. |

Commands:

```text
# web (pre-deploy)
alembic upgrade head
# web (start)
uvicorn app.main:app --host 0.0.0.0 --port $PORT
# worker
celery -A app.celery_app.celery_app worker --loglevel=INFO --pool=threads --concurrency=20 --beat
```

### 8.2 Sequence

1. Push repository to GitHub.
2. Verify Render Blueprint schema and plan availability; configure services or dashboard equivalent.
3. Wire env vars: `DATABASE_URL`, `REDIS_URL`, `PROCESSING_DELAY_SECONDS=30`, etc.
4. Deploy database/Key Value, then web (migrations), then worker.
5. Check logs for migration/startup errors.
6. Run health check, acceptance script, and load test against the public URL.
7. Keep the web instance warm; optionally add an uptime ping.

### 8.3 Rollback

- Redeploy the previous commit from Render.
- Initial migration is additive; keep migrations backward-compatible for one release.
- If Redis is unhealthy, the API keeps accepting (`202`); rows remain `PROCESSING` and recover when Redis/worker return.

## 9. Observability and operations

- Structured logs with `transaction_id`, `event`, `status`, `duration_ms`, and error summaries.
- Log webhook accept duration server-side to audit the 500ms budget independently of client network time.
- Never log secrets or connection strings.
- Runbook:
  - **Redis down:** API still returns `202`; dispatch deferred; reconciliation recovers.
  - **Worker down:** rows stay `PROCESSING`; queued tasks remain in Redis; startup/beat recovery.
  - **Worker crash during sleep:** lease expires; reconcile re-dispatches; token check prevents stale completion.
  - **Database down:** webhook `503` (processor retries); health remains `HEALTHY` by design.
  - **Stuck `PROCESSING`:** inspect `last_error`/`processing_attempts`/lease; reconciliation should recover; manual requeue only as last resort.

## 10. Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Render free web spins down | First request exceeds 500ms | Always-on instance and/or external keep-alive; document. |
| Redis outage during dispatch | No immediate enqueue | Post-response dispatch logs failure; reconciliation re-enqueues. |
| Commit succeeds, dispatch never runs | Stuck row | Reconciliation every 5s and on startup. |
| Worker dies during sleep | Task redelivered | Lease/token claim; duplicate completion no-op. |
| Celery redelivery | Duplicate execution | Atomic claim + lease + token completion. |
| Threads pool saturation | Delayed processing under load | Concurrency 20; load test; tune to Render memory. |
| Beat embedded in one worker | No sweep while worker down | Restart runs reconciliation; advisory lock prevents overlap if scaled. |
| Decimal serialization | Amount string/precision loss | Explicit JSON number serializer; contract test. |
| Render Blueprint changes | Deploy config fails | Verify docs in Phase 6; dashboard fallback. |
| Flaky timing tests | False failures | Short configurable delay; poll with deadline. |
| Secrets committed | Security issue | `.env` ignored; Render env; review before push. |

## 11. Definition of done

- [ ] AC-1 through AC-11 pass locally.
- [ ] Deployed URL passes the acceptance script.
- [ ] Duplicates produce one row and one effective processing run.
- [ ] Processing completes approximately 30 seconds after acceptance.
- [ ] Webhook p95 under 500ms under documented load.
- [ ] Redis/worker failure recovery demonstrated.
- [ ] `ruff check` and `pytest` pass.
- [ ] README explains run, test, architecture, choices.
- [ ] Public repo contains app, tests, deployment config, specs.
- [ ] No secrets committed.

## 12. Open decisions before deployment

1. **Render budget:** always-on paid web/worker and persistent PostgreSQL/Key Value vs free tiers with cold-start/expiry risk. The 500ms requirement strongly favors always-on.
2. **Region:** Singapore (recommended for India-based testers) vs another region.
3. **Repository name:** `confluencr-assessment` (recommended) vs a different public name.
4. **Tester location:** confirm if the grader will call from a region where client-side network latency could dominate the 500ms measurement.

