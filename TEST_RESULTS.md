# Test Results

Results for the deployed service at **https://confluencr-assessment-22vd.onrender.com**, recorded on 2026-11-03 against commit `cef293e`. Every result below can be reproduced with the commands shown.

## At a glance

The same checks were run against the live deployment and against the identical stack running locally in Docker.

| Requirement (from the brief) | Live (Render free tier) | Local (Docker) |
| --- | --- | --- |
| Health check `GET /` | ✅ | ✅ |
| One webhook → `PROCESSED` after ~30 seconds | ✅ | ✅ |
| Duplicate webhooks → one transaction | ✅ | ✅ |
| No lost transactions | ✅ 50 of 50 processed | ✅ 161 of 161 processed, each exactly once |
| Invalid payload → `422`, unknown ID → `404` | ✅ | ✅ |
| Webhook under 500ms, one request at a time | ✅ p95 222ms | ✅ |
| Webhook under 500ms, 20 concurrent requests | ❌ p95 5,241ms | ✅ p95 120ms |

The one difference between the columns is the hosting tier's CPU allowance, not the code. See [Performance](#performance) for the measurements and the controlled CPU experiment.

## Functional checks on the live service

`scripts/acceptance_check.py` exercises the deployed API end to end:

```bash
python scripts/acceptance_check.py --base-url https://confluencr-assessment-22vd.onrender.com --wait-seconds 90
```

| Check | Result |
| --- | --- |
| `GET /` returns `HEALTHY` with a UTC `Z` timestamp | ✅ |
| `POST /v1/webhooks/transactions` returns `202` | ✅ |
| Transaction is immediately queryable as `PROCESSING` with `processed_at: null` | ✅ |
| Query response is a one-element array with the specified fields | ✅ |
| Sending the same webhook again returns `202` and does not create a second row | ✅ |
| Status becomes `PROCESSED` after the 30-second processing delay | ✅ |
| A duplicate sent after processing leaves `processed_at` unchanged | ✅ |
| Unknown transaction ID returns `404` | ✅ |
| Invalid payload returns `422` | ✅ |

**15 of 15 checks passed.**

## Automated test suite

CI runs on every push ([workflow](./.github/workflows/ci.yml)) against a real PostgreSQL database. The end-to-end job also uses a real Redis and Celery worker.

| Step | Result |
| --- | --- |
| Lint and format (`ruff`) | ✅ |
| Unit and integration tests | ✅ 51 passed |
| Latency test | ✅ 1 passed |
| End-to-end test (API → Redis → worker → PostgreSQL) | ✅ 1 passed |

The reliability scenarios are tested directly:

| Scenario | Test |
| --- | --- |
| Duplicate webhook → `202`, no second row | `tests/integration/test_webhook_ingest.py::test_duplicate_webhook_returns_202_without_duplicate_processing` |
| Concurrent duplicates race safely | `tests/integration/test_webhook_ingest.py::test_concurrent_duplicate_webhooks_are_idempotent` |
| Task redelivered after completion → no-op | `tests/integration/test_processing.py::test_redelivery_after_completion_is_skipped` |
| Two workers cannot process the same transaction | `tests/integration/test_processing.py::test_active_lease_prevents_duplicate_processing` |
| A crashed worker's transaction is recovered | `tests/integration/test_reconcile.py::test_reconcile_redispatches_expired_lease` |
| Redis unavailable at accept time → still `202`, processed later | `tests/integration/test_reconcile.py::test_dispatch_failure_returns_202_and_reconcile_recovers` |
| A stale worker cannot overwrite a newer claim | `tests/integration/test_transactions_service.py::test_release_allows_reclaim_and_rejects_stale_token` |

Run locally with `pytest -q`; add `-m e2e` for the end-to-end test.

## Performance

**How to read these numbers.** Each run sends a batch of webhooks and sorts their response times. **p50** is the median. **p95** is the time that 95% of requests beat, so it shows what the slowest requests experienced. The 500ms target is measured at p95.

### Live service

The client was in India and the service runs in Render's Singapore region, so these times include the real network round trip.

```bash
python scripts/load_test.py --base-url https://confluencr-assessment-22vd.onrender.com --count 50 --concurrency 20 --poll --poll-timeout 480
```

| Webhooks sent at the same time | Sent | Accepted (`202`) | p50 | p95 |
| --- | --- | --- | --- | --- |
| 1 | 10 | 10 | 181ms | **222ms** ✅ |
| 2 | 8 | 8 | 287ms | 1,503ms |
| 5 | 20 | 20 | 474ms | 811ms |
| 10 | 40 | 40 | 982ms | 1,892ms |
| 20 | 50 | 50 | 2,942ms | 5,241ms |

Every webhook was accepted with no errors, and all 50 in the 20-at-once run reached `PROCESSED`. Concurrency increased response time but never caused a lost or failed transaction.

### Why bursts are slower on the current host

The service currently runs on Render's free tier, which allocates about **0.1 CPU** to the instance. Latency rises by about 95ms for each additional concurrent request. That linear pattern means requests are waiting for CPU time: each request is cheap, but on a tenth of a core they have to take turns.

To confirm that CPU is the only factor, the identical stack was run locally in Docker and the API's CPU was capped to match each hosting tier:

| CPU available to the API | Equivalent | p50 | p95 | Under 500ms |
| --- | --- | --- | --- | --- |
| Unrestricted | Developer machine | 102ms | 120ms | ✅ |
| 0.5 CPU | Render Starter | 222ms | 372ms | ✅ |
| 0.1 CPU | Render Free (current) | 4,499ms | 5,803ms | ❌ |

All three runs used the same code, database and test (50 webhooks, 20 at a time). At 0.1 CPU the local result matches the live one, and with 0.5 CPU the same burst stays under 500ms. Moving to a paid instance is a one-line change in [`render.yaml`](./render.yaml) (`plan: starter`) and needs no code changes. The architecture also allows the worker to run as a separate service, so it would no longer share CPU with the API.

Reproduce it:

```bash
docker compose up -d --build
docker compose run --rm api alembic upgrade head
docker update --cpus 0.1 confluencr-assessment-api-1   # 0.5 for Starter, 0 to remove the cap
python scripts/load_test.py --base-url http://localhost:8000 --count 50 --concurrency 20 --timeout 30
```

## Free-tier notes

- **Idle spin-down:** free web services sleep after a period without traffic, and the first request after that takes several seconds to wake the instance. An external scheduler pings `GET /` every few minutes to keep it awake.
- **Database lifetime:** the free PostgreSQL instance expires about 30 days after creation.
