# Confluencr Transaction Webhook Processing Service

A Python service that accepts transaction webhooks, acknowledges them in well under 500ms, and processes them reliably in the background after a simulated 30-second delay. PostgreSQL is the source of truth and the recovery mechanism; Redis and Celery are the asynchronous transport.

**Live:** https://confluencr-assessment-22vd.onrender.com (Render free tier, Singapore)

**Test results:** [`TEST_RESULTS.md`](./docs/TEST_RESULTS.md) records the live and local runs: 15/15 acceptance checks on the live service, duplicate prevention, no lost transactions, and latency, including a controlled experiment showing that burst latency on the free tier is caused by its 0.1 CPU allowance.

## Try it live

No setup needed; paste into a terminal (macOS/Linux shell):

```bash
BASE=https://confluencr-assessment-22vd.onrender.com
TXN=txn_demo_$(date +%s)
BODY="{\"transaction_id\":\"$TXN\",\"source_account\":\"acc_user_789\",\"destination_account\":\"acc_merchant_456\",\"amount\":1500,\"currency\":\"INR\"}"

curl -s "$BASE/"                                                                   # health
curl -s -X POST "$BASE/v1/webhooks/transactions" -H "Content-Type: application/json" -d "$BODY"   # 202 ACCEPTED
curl -s "$BASE/v1/transactions/$TXN"                                               # "status": "PROCESSING"
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/v1/webhooks/transactions" \
  -H "Content-Type: application/json" -d "$BODY"                                   # duplicate: 202, no new row
sleep 32 && curl -s "$BASE/v1/transactions/$TXN"                                   # "status": "PROCESSED"
```

Or import the Postman collection [`docs/postman/confluencr.postman_collection.json`](./docs/postman/confluencr.postman_collection.json) (11 requests, 37 assertions; [live results](./docs/postman/results-summary.json)), or run the full acceptance check (Python 3.9+, standard library only, no install):

```bash
python3 scripts/acceptance_check.py --base-url https://confluencr-assessment-22vd.onrender.com --wait-seconds 90
```

## Architecture

```text
Client
  |
  | POST /v1/webhooks/transactions
  v
FastAPI API
  |-- validate payload
  |-- INSERT ... ON CONFLICT DO NOTHING (PostgreSQL commit)
  |-- return 202
  `-- BackgroundTasks: enqueue Celery task
          |
          v
       Redis broker
          |
          v
     Celery worker (threads pool)
          |-- atomic claim: PROCESSING + lease + token
          |-- release DB session
          |-- sleep 30 seconds
          |-- token-checked completion -> PROCESSED
          `-- on failure: clear lease, record error, bounded retry
```

- **Fast path:** the webhook path validates, commits durably, returns `202`, and only then dispatches. Redis is not on the 500ms path.
- **Idempotency:** the `transactions` primary key plus `INSERT ... ON CONFLICT DO NOTHING` makes duplicate deliveries a no-op at the database level.
- **Lease and token:** a worker atomically claims a row with a UUID token and a lease. Completion requires the same token, so a slow or crashed worker cannot overwrite a newer claim.
- **Reconciliation:** a beat task runs every `RECONCILE_INTERVAL_SECONDS`, on worker startup, and on API startup. It re-dispatches rows that were committed but never dispatched, or whose lease expired.
- **Durability:** broker messages are at-least-once. A lost Redis message cannot lose a committed transaction because reconciliation reads PostgreSQL directly.
- **Concurrency:** Celery uses a threads pool (default 20 locally; the Render Blueprint sets 5) so one slow transaction does not block unrelated transactions, without relying on Unix-only prefork behavior.

## API

### Health

```bash
curl http://localhost:8000/
```

```json
{"status":"HEALTHY","current_time":"2026-11-02T12:00:00Z"}
```

### Accept a webhook

```bash
curl -X POST http://localhost:8000/v1/webhooks/transactions \
  -H "Content-Type: application/json" \
  -d '{
    "transaction_id": "txn_123",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500,
    "currency": "INR"
  }'
```

```json
{"status":"ACCEPTED","transaction_id":"txn_123"}
```

The endpoint returns `202` for both first receipt and duplicate delivery. Only a first insert schedules processing; the database unique constraint decides identity, not a read-then-write check.

### Query a transaction

```bash
curl http://localhost:8000/v1/transactions/txn_123
```

```json
[
  {
    "transaction_id": "txn_123",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500.0,
    "currency": "INR",
    "status": "PROCESSING",
    "created_at": "2026-11-02T12:00:00Z",
    "processed_at": null
  }
]
```

The response is always a one-element JSON array. Public status values are only `PROCESSING` and `PROCESSED`; internal lease, token, attempt, and error fields are never exposed.

Errors:

| Status | Meaning |
| --- | --- |
| `202` | Webhook accepted or already known |
| `404` | Unknown transaction |
| `422` | Invalid payload |
| `503` | Database could not persist the webhook |

## Local development

### Quick start (Docker, any OS)

Prerequisites: Docker with Docker Compose. This runs PostgreSQL, Redis, the API and the worker:

```bash
docker compose up -d --build
docker compose run --rm api alembic upgrade head      # create the table (first run only)
python3 scripts/acceptance_check.py --base-url http://localhost:8000 --wait-seconds 90
```

The API is at http://localhost:8000. Follow the worker with `docker compose logs -f worker`, and stop everything with `docker compose down`.

### Without Docker for the app (Python 3.12)

Run only PostgreSQL and Redis in Docker, and the API and worker natively.

macOS / Linux:

```bash
docker compose up -d postgres redis
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload                                            # terminal 1
celery -A app.celery_app.celery_app worker --loglevel=INFO --pool=threads --concurrency=4 --beat   # terminal 2
```

Windows (PowerShell):

```powershell
docker compose up -d postgres redis
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
```

Start the API:

```powershell
uvicorn app.main:app --reload
```

Start the worker (second terminal):

```powershell
celery -A app.celery_app.celery_app worker --loglevel=INFO --pool=threads --concurrency=4
```

Start beat (third terminal on Windows; Celery's `--beat` flag does not work on Windows):

```powershell
celery -A app.celery_app.celery_app beat --loglevel=INFO
```

On Linux, a single worker process can include beat with `--beat`. In this project, keep one beat instance: running multiple beat schedulers would enqueue duplicate reconciliation tasks, which are safe but noisy.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | `test` disables the API startup reconciliation trigger |
| `LOG_LEVEL` | `INFO` | Application log level |
| `PORT` | `8000` | API port |
| `DATABASE_URL` | required | PostgreSQL URL, normalized to `postgresql+psycopg://` |
| `TEST_DATABASE_URL` | optional | Test database URL |
| `REDIS_URL` | required | Celery broker URL |
| `PROCESSING_DELAY_SECONDS` | `30` | Simulated processing delay, must be positive |
| `PROCESSING_LEASE_SECONDS` | `120` | Lease duration, must exceed the delay |
| `DISPATCH_STALE_SECONDS` | `120` | Re-dispatch a dispatched row after this age if never claimed |
| `RECONCILE_INTERVAL_SECONDS` | `5` | Beat reconciliation interval |
| `WORKER_CONCURRENCY` | `20` | Worker concurrency; the Render supervisor passes it and the Blueprint sets `5` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | local defaults | Local Docker Postgres only |

Secrets belong in environment variables or Render's secret configuration, never in the repository. Logs are JSON lines and redact connection URLs, passwords, tokens, API keys, and bearer credentials.

## Tests

The default suite excludes long latency and end-to-end tests:

```powershell
ruff check .
ruff format --check .
pytest -q
```

Run the latency test (in-process, p95 under 500ms):

```powershell
pytest -m slow -q
```

Run the real end-to-end test. It starts a real Celery worker and API subprocess, needs Redis and PostgreSQL, and uses the test database:

```powershell
pytest -m e2e -q
```

The test database is created automatically. By default it is `confluencr_test` at `postgresql+psycopg://postgres:postgres@localhost:5432/confluencr_test`.

### Live acceptance and load checks

With the API and a worker running:

```powershell
python scripts/acceptance_check.py --base-url http://localhost:8000
python scripts/load_test.py --base-url http://localhost:8000 --count 50 --concurrency 20
```

`acceptance_check.py` verifies health, the full lifecycle, duplicate delivery, the one-element array contract, stable `processed_at`, `404`, and `422`. `load_test.py` reports p50, p95, and max latency; it fails on any non-`202` response or a p95 above 500ms. Use `--poll` to wait for all accepted transactions to reach `PROCESSED`.

### Acceptance criteria traceability

| Criterion | Coverage |
| --- | --- |
| AC-1 | `tests/unit/test_health.py` |
| AC-2 | `tests/integration/test_webhook_ingest.py::test_webhook_accepts_and_persists` |
| AC-3 | `tests/integration/test_processing.py`, `tests/e2e/test_end_to_end.py` |
| AC-4 | `test_duplicate_webhook_returns_202_without_duplicate_processing`, `test_concurrent_duplicate_webhooks_are_idempotent` |
| AC-5 | `tests/performance/test_webhook_latency.py`, `scripts/load_test.py` |
| AC-6 | `tests/integration/test_reconcile.py::test_dispatch_failure_returns_202_and_reconcile_recovers`, manual Redis recovery |
| AC-7 | `test_redelivery_after_completion_is_skipped`, `test_active_lease_prevents_duplicate_processing` |
| AC-8 | `tests/integration/test_webhook_ingest.py::test_invalid_webhook_returns_422_and_creates_no_row` |
| AC-9 | `tests/integration/test_query_contract.py::test_unknown_transaction_returns_404` |
| AC-10 | `tests/integration/test_persistence.py`, manual API/worker restart |
| AC-11 | `tests/integration/test_query_contract.py::test_query_returns_exact_public_contract` |

## Deployment on Render

`render.yaml` defines a free-tier Blueprint with:

1. A free Docker web service in Singapore running the API, Celery worker, and beat.
2. A free PostgreSQL database in Singapore.
3. A free Key Value (Redis-compatible) instance in Singapore, used only as the broker.

Apply it from the Render Dashboard: **New → Blueprint**, connect this repository, and apply. Render creates all three resources and wires `DATABASE_URL` and `REDIS_URL`.

The web service command is:

```text
python -m app.render_start
```

`app/render_start.py` runs `alembic upgrade head`, starts the API on `$PORT`, and supervises the Celery worker. On Linux the worker embeds beat with `--beat`; on Windows the supervisor starts beat separately. Migrations run on every start because free instances can restart at any time.

After the first deploy, set the repository variable `RENDER_BASE_URL` to the service URL (`https://confluencr-assessment-22vd.onrender.com`). This enables `.github/workflows/keepalive.yml`, which pings `/` every five minutes to keep the free web service from spinning down. The workflow is skipped while the variable is empty.

### Free-tier constraints

Render's free tier does not offer background workers, so the worker and beat must share the web service. The free tier also has these limits:

- A free web service spins down after 15 minutes without inbound traffic and takes about a minute to wake. The keepalive reduces this risk but GitHub Actions scheduling is not a hard real-time guarantee.
- Free web services get 750 instance hours per workspace per month. One always-on service uses about 744 hours in a 31-day month; a second always-on free service would exhaust the allowance.
- Free services run on 0.1 CPU / 512 MB and cannot scale beyond one instance. Do not run more than one web instance: it would duplicate workers and beat.
- Free Postgres expires 30 days after creation (1 GB, no backups). Upgrade it or recreate the database before expiry.
- Free Key Value is in-memory only and can be restarted at any time, losing queued messages. PostgreSQL retains committed transactions, so reconciliation recovers lost dispatches.
- Render explicitly recommends free instances only for prototypes, not production.

For an always-on production deployment, move the worker to a paid background worker, keep beat on that worker, and use paid Postgres/Key Value. The application code supports that split without changes.

## Reliability notes and limits

- The API returns `202` after the PostgreSQL commit, before dispatch. If dispatch fails, reconciliation retries it.
- A crash after the claim but before completion leaves a lease. When the lease expires, reconciliation re-dispatches the row. The old worker's late completion is rejected by the token check.
- Redis message loss is recoverable because PostgreSQL, not Redis, owns transaction state.
- Retries are bounded with exponential backoff; after retries are exhausted the row remains `PROCESSING` and reconciliation can dispatch it again.
- Signature verification and authentication are out of scope per the assessment brief.
- The free-tier Blueprint runs one beat instance embedded in the worker on Linux. If the deployment is ever scaled horizontally, move beat to a separate scheduler service.
- Free-tier cold starts, restarts, and the 30-day Postgres expiry are platform limits, not application-level guarantees. See [Free-tier constraints](#free-tier-constraints).

## Project documents

- [`ASSESSMENT_QUESTION.md`](./docs/ASSESSMENT_QUESTION.md) — original brief.
- [`SPEC.md`](./docs/SPEC.md) — requirements, API contract, acceptance criteria.
- [`PLAN.md`](./docs/PLAN.md) — architecture, phases, testing, deployment.
- [`TEST_RESULTS.md`](./docs/TEST_RESULTS.md) — live and local test results, latency measurements, CPU experiment.
- [`CLAUDE.md`](./CLAUDE.md) — AI engineering rules and invariants.
