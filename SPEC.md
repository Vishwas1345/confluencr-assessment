# Specification — Transaction Webhook Processing Service

| Field | Value |
| --- | --- |
| Status | Draft for implementation |
| Source | [`ASSESSMENT_QUESTION.md`](./ASSESSMENT_QUESTION.md) |
| Scope | Backend assessment for Confluencr |
| Last updated | 2026-11-02 |

## 1. Summary

Build a Python service that receives transaction webhooks from an external payment processor, acknowledges them within 500ms, and processes them asynchronously. Processing simulates external API work with a 30-second delay. The service must be idempotent, durable across restarts, observable through a status endpoint, and deployed publicly on Render.

## 2. Goals

- **G1 — Fast acknowledgement:** Accept a valid webhook and return `202 Accepted` within 500ms.
- **G2 — Reliable asynchronous processing:** Process each transaction after a simulated 30-second delay without blocking the API.
- **G3 — Exactly-once effect:** Multiple deliveries of the same `transaction_id` produce one persisted transaction and one effective processing run.
- **G4 — Durability:** An acknowledged webhook is never lost due to worker crashes, Redis failures, or service restarts.
- **G5 — Verifiability:** Health and transaction-status endpoints expose enough state for an external tester to verify behavior.
- **G6 — Delivery quality:** Public GitHub repository, automated tests, deployment configuration, and a README explaining how to run, test, and the technical choices.

## 3. Non-goals

- Webhook signature verification or authentication (not specified in the brief).
- Real money movement, ledger accounting, or reconciliation.
- Currency conversion, FX validation, or locale-specific amount rules.
- A user interface or dashboard.
- Multi-region deployment, horizontal sharding, or high-availability guarantees beyond the assessment.
- Long-term retention, archival, or analytics.

## 4. Actors and use cases

| Actor | Use case |
| --- | --- |
| Payment processor | Sends transaction webhooks, including retries/duplicates. |
| Grader / operator | Calls the health endpoint, sends webhooks, queries transaction status, verifies timing and idempotency. |
| Background worker | Claims accepted transactions, simulates 30 seconds of work, and records the final state. |
| Reconciliation sweeper | Re-dispatches committed but unprocessed transactions after failures or restarts. |

## 5. Functional requirements

### FR-1 — Webhook ingestion

`POST /v1/webhooks/transactions` MUST:

- Accept a JSON object with these required fields:
  - `transaction_id` — non-empty string.
  - `source_account` — non-empty string.
  - `destination_account` — non-empty string.
  - `amount` — positive number, stored with decimal precision.
  - `currency` — non-empty string (ISO 4217 code expected; example values are `INR` and `USD`).
- Reject malformed or invalid payloads with `422 Unprocessable Entity` and a descriptive error body. No transaction row is created for a rejected payload.
- On first receipt, durably persist the transaction with status `PROCESSING`, `created_at` set to the current UTC time, and `processed_at` set to `null`.
- Commit the transaction before returning `202 Accepted`.
- Return `202 Accepted` with a simple acknowledgement body for both first-time and duplicate webhooks.
- Never perform the 30-second simulated work in the request path.
- Complete the request within 500ms. The 500ms budget covers validation, the durable write, and dispatch; it excludes client-side network latency.
- Return a non-2xx status if the transaction cannot be durably persisted, because the processor must retry. Once the transaction is committed, dispatch failure MUST NOT convert the response into an error; reconciliation handles it.

**Chosen acknowledgement body:**

```json
{
  "status": "ACCEPTED",
  "transaction_id": "txn_abc123def456"
}
```

### FR-2 — Health check

`GET /` MUST return `200 OK` with:

```json
{
  "status": "HEALTHY",
  "current_time": "2024-01-15T10:30:00Z"
}
```

- `current_time` MUST be the current UTC time in ISO 8601 format with a `Z` suffix.
- The endpoint MUST NOT depend on the database, Redis, or worker availability; it is a liveness check, not a readiness check.

### FR-3 — Transaction status query

`GET /v1/transactions/{transaction_id}` MUST:

- Return `200 OK` with a JSON **array** containing exactly one transaction object when found. This array shape intentionally matches the assessment brief.
- Include exactly these fields:

| Field | Type | Notes |
| --- | --- | --- |
| `transaction_id` | string | The submitted identifier. |
| `source_account` | string | As accepted. |
| `destination_account` | string | As accepted. |
| `amount` | number | Decimal value; JSON number. |
| `currency` | string | As accepted. |
| `status` | string | `PROCESSING` or `PROCESSED` only. |
| `created_at` | string | UTC ISO 8601 with `Z`. |
| `processed_at` | string or null | UTC ISO 8601 with `Z` when processed; `null` otherwise. |

- Return `404 Not Found` with `{"detail": "Transaction not found"}` for an unknown identifier.
- Expose no internal retry, lease, or error fields.

### FR-4 — Background processing

The worker MUST:

- Receive a reference to a committed transaction and process it asynchronously.
- Apply a 30-second simulated delay before recording success. The delay MUST be configurable through an environment variable so acceptance tests can run quickly; the production/default value MUST be `30`.
- Transition the transaction from `PROCESSING` to `PROCESSED` and set `processed_at` to the completion time.
- Process different transactions concurrently. A slow transaction MUST NOT block unrelated transactions.
- Be safe under at-least-once task delivery: redelivered tasks for an already `PROCESSED` transaction are no-ops.
- Retry transient processing failures a bounded number of times with backoff, logging failures. A transaction is not considered lost while it remains `PROCESSING` and is eligible for reconciliation.
- Leave `created_at` unchanged.

### FR-5 — Idempotency

- A database-level unique constraint on `transaction_id` MUST enforce idempotency. Application-level "check then insert" alone is not acceptable.
- Concurrent or sequential duplicate webhooks MUST:
  - Return `202 Accepted`.
  - Produce no additional transaction row.
  - Not reset `created_at`, `processed_at`, or status.
  - Not cause a second effective processing run.
- Duplicate handling MUST be race-safe under concurrent requests.
- A duplicate that arrives after processing completes MUST return `202` and leave the transaction `PROCESSED`.
- Task-level idempotency is also required because Celery/Redis may deliver a task more than once.

### FR-6 — Persistent storage

- PostgreSQL is the system of record for transaction data.
- Data MUST survive web-service restarts, worker restarts, and redeployments.
- Redis is transport only. Loss of Redis data MUST NOT lose committed transactions; the reconciliation sweeper re-dispatches them from PostgreSQL.

### FR-7 — Failure recovery and reconciliation

- A reconciliation process MUST run periodically and at service/worker startup.
- It MUST find committed transactions that are `PROCESSING` but not progressing (for example, never dispatched, or whose worker lease expired) and re-dispatch them idempotently.
- The lease/staleness threshold MUST be longer than the maximum expected processing duration to avoid racing an active 30-second task.
- Worker crashes MUST NOT leave a transaction permanently stuck.
- A task redelivered after successful completion MUST be a no-op.

### FR-8 — Observability

- Log structured events including `transaction_id`, `event`, and relevant timestamps for:
  - Webhook accepted (first receipt vs duplicate).
  - Task dispatched, started, retried, failed, and completed.
  - Reconciliation performed.
- Webhook request handling should log its server-side duration.
- Logs MUST NOT include secrets or full connection strings.

### FR-9 — Configuration

Configuration MUST come from environment variables, at minimum:

- `DATABASE_URL` — PostgreSQL connection string.
- `REDIS_URL` — Redis/Celery broker URL.
- `PROCESSING_DELAY_SECONDS` — simulated delay, default `30`.
- Worker concurrency and reconciliation interval as needed.
- `.env.example` documents required variables; real secrets are never committed.

## 6. API contracts

### 6.1 `POST /v1/webhooks/transactions`

Request:

```json
{
  "transaction_id": "txn_abc123def456",
  "source_account": "acc_user_789",
  "destination_account": "acc_merchant_456",
  "amount": 1500,
  "currency": "INR"
}
```

Success — `202 Accepted`:

```json
{
  "status": "ACCEPTED",
  "transaction_id": "txn_abc123def456"
}
```

- Same response for a duplicate `transaction_id`.
- Invalid payload — `422 Unprocessable Entity`:
  ```json
  {
    "detail": [
      {
        "loc": ["body", "transaction_id"],
        "msg": "Field required",
        "type": "missing"
      }
    ]
  }
  ```
- Persistence unavailable — `503 Service Unavailable` (the processor may retry).

### 6.2 `GET /`

Success — `200 OK`:

```json
{
  "status": "HEALTHY",
  "current_time": "2024-01-15T10:30:00Z"
}
```

### 6.3 `GET /v1/transactions/{transaction_id}`

Success while processing — `200 OK`:

```json
[
  {
    "transaction_id": "txn_abc123def456",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500.0,
    "currency": "INR",
    "status": "PROCESSING",
    "created_at": "2024-01-15T10:30:00Z",
    "processed_at": null
  }
]
```

Success after processing — `200 OK`:

```json
[
  {
    "transaction_id": "txn_abc123def456",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500.0,
    "currency": "INR",
    "status": "PROCESSED",
    "created_at": "2024-01-15T10:30:00Z",
    "processed_at": "2024-01-15T10:30:30Z"
  }
]
```

Unknown transaction — `404 Not Found`:

```json
{
  "detail": "Transaction not found"
}
```

## 7. Data model

Logical table: `transactions`

| Column | Type | Constraints / notes |
| --- | --- | --- |
| `transaction_id` | `VARCHAR` / `TEXT` | Primary key or unique; idempotency key. |
| `source_account` | `TEXT` | Not null. |
| `destination_account` | `TEXT` | Not null. |
| `amount` | `NUMERIC(18, 2)` | Not null; must be positive. |
| `currency` | `TEXT` | Not null. |
| `status` | `TEXT` | Not null; check in (`PROCESSING`, `PROCESSED`). |
| `created_at` | `TIMESTAMPTZ` | Not null; set when first accepted. |
| `processed_at` | `TIMESTAMPTZ` | Null until processing completes. |
| `dispatched_at` | `TIMESTAMPTZ` | Internal; last successful enqueue time; null if not yet dispatched. |
| `processing_started_at` | `TIMESTAMPTZ` | Internal; set when a worker claims the task. |
| `processing_attempts` | `INTEGER` | Internal; default 0. |
| `last_error` | `TEXT` | Internal; last failure summary. |

Indexes:

- Unique index on `transaction_id` (idempotency).
- Index on `(status, dispatched_at)` or an equivalent for the reconciliation query.

Implementation may add internal columns, but the public API shape is fixed by section 6.

## 8. State model

| From | Event | To | Side effects |
| --- | --- | --- | --- |
| — | First webhook accepted | `PROCESSING` | Row inserted, `created_at` set, dispatch attempted. |
| `PROCESSING` | Duplicate webhook | `PROCESSING` | None; row unchanged. |
| `PROCESSING` | Worker starts task | `PROCESSING` | `processing_started_at` set; 30s delay begins. |
| `PROCESSING` | Task completes | `PROCESSED` | `processed_at` set, `last_error` cleared. |
| `PROCESSING` | Task fails transiently | `PROCESSING` | Attempt count incremented, retry scheduled, error logged. |
| `PROCESSING` | Worker crash / lease expires | `PROCESSING` | Reconciliation re-dispatches. |
| `PROCESSED` | Duplicate webhook or redelivered task | `PROCESSED` | No-op. |

There is no public `FAILED` or `PENDING` state. The status endpoint returns only `PROCESSING` or `PROCESSED`, as required by the brief.

## 9. Non-functional requirements

| Area | Requirement |
| --- | --- |
| Latency | Webhook `202` response within 500ms under the assessment load; target p95 well below it. |
| Concurrency | Webhook requests are not serialized behind 30-second processing; distinct transactions process concurrently. |
| Durability | An accepted transaction survives restarts and Redis loss. |
| Idempotency | At most one row and one effective processing run per `transaction_id`, including under concurrency. |
| Availability | The deployed web instance must not cold-start on the grading request; the Render service must be kept warm. |
| Security | Input validation; no secrets in the repository; configuration via environment. Auth/signature verification is out of scope per the brief. |
| Observability | Structured logs for acceptance, dispatch, processing, completion, and reconciliation. |
| Portability | Runs locally and in Docker with PostgreSQL and Redis. |
| Timestamps | All stored and returned timestamps are UTC, ISO 8601, `Z` suffix. |
| Currency/amount | Amount stored as decimal, not float; currency preserved as submitted. |

## 10. Acceptance criteria

| ID | Criterion | Verification |
| --- | --- | --- |
| AC-1 | `GET /` returns `200` with `status: HEALTHY` and a valid UTC ISO timestamp. | API test. |
| AC-2 | A valid webhook returns `202` with an acknowledgement, and the transaction is queryable immediately as `PROCESSING` with `processed_at: null`. | API test. |
| AC-3 | A single transaction transitions to `PROCESSED` after approximately 30 seconds, with `processed_at` set. | Integration test with real worker or shortened delay. |
| AC-4 | Sending the same webhook 5 times (including concurrently) yields one row, consistently returns `202`, preserves `created_at`, and processes once. | Integration test. |
| AC-5 | Under load, each webhook responds in under 500ms and all accepted transactions eventually process. | Load test. |
| AC-6 | If dispatch/worker availability fails after commit, the transaction is eventually picked up and processed after recovery; it is not lost. | Failure-injection test. |
| AC-7 | A task delivered twice for the same transaction (or delivered after `PROCESSED`) does not change `processed_at` or re-run processing. | Worker unit/integration test. |
| AC-8 | Invalid payloads return `422` and create no transaction row. | API test. |
| AC-9 | An unknown transaction ID returns `404`. | API test. |
| AC-10 | Transaction data survives a web and worker restart. | Persistence test. |
| AC-11 | `GET /v1/transactions/{id}` returns the array-of-one shape with exactly the specified fields. | Contract test. |

### Traceability

| Assessment requirement | Spec requirements | Acceptance criteria |
| --- | --- | --- |
| Webhook endpoint | FR-1 | AC-2, AC-8 |
| Health check endpoint | FR-2 | AC-1 |
| Query endpoint | FR-3 | AC-2, AC-9, AC-11 |
| 202 + 500ms | FR-1, NFR latency | AC-2, AC-5 |
| Background 30s processing | FR-4 | AC-3 |
| Idempotency | FR-5 | AC-4, AC-7 |
| Persistent storage | FR-6 | AC-10, AC-3 |
| Reliability / no lost transactions | FR-7, FR-6 | AC-6 |
| Deployed service + repo + README | FR-9, FR-10 | Deployment verification |

## 11. Selected technical direction

| Decision | Choice | Rationale |
| --- | --- | --- |
| Language/framework | Python 3.12 + FastAPI | Fast, typed, easy validation and OpenAPI docs. |
| Persistence | PostgreSQL | Durable system of record; unique constraint supports race-safe idempotency. |
| Background processing | Celery + Redis broker | User-selected stack; separates processing from API; Celery handles retries and worker lifecycle. |
| Idempotency | PostgreSQL unique constraint + `INSERT ... ON CONFLICT DO NOTHING` | Race-safe at the database level. |
| Dispatch reliability | Database row as the queue source of truth + reconciliation sweeper | Covers the commit-then-enqueue gap and Redis/worker failures. |
| Deployment | Render web service + Render worker + Render PostgreSQL + Render Key Value | Single platform for all runtime components. |
| Containerization | Docker for web and worker | Reproducible local and cloud execution. |
| Testing | pytest + HTTP client + Celery test mode/worker + test database | Verifies API contract and asynchronous behavior. |

Detailed module layout, task breakdown, and step-by-step implementation order belong in `PLAN.md` (to be written next).

## 12. Risks, assumptions, and open questions

1. **Render cold starts:** Free Render web services spin down after inactivity. The first grading request could exceed 500ms while the instance boots. The deployment must use an always-on instance or an external keep-alive mechanism. This needs to be settled before deployment.
2. **Render resource limits:** The web, worker, Redis, and PostgreSQL services must fit the selected Render plan. Worker concurrency must support multiple simultaneous 30-second tasks without exhausting memory.
3. **At-least-once delivery:** Celery/Redis can redeliver tasks. The worker must claim and complete idempotently; this is a hard requirement, not an optimization.
4. **Commit/enqueue gap:** A crash or Redis outage between the database commit and task enqueue is possible. The reconciliation sweeper is mandatory.
5. **30-second concurrency:** A blocking sleep in a small prefork worker pool limits throughput. Worker pool/concurrency strategy will be verified against AC-5. Threads or a cooperative pool may be required.
6. **Example inconsistency:** The brief's webhook example uses `amount: 1500, currency: INR`, while its query example uses `amount: 150.50, currency: USD`. These are treated as illustrative; the service echoes the accepted values.
7. **Array response:** The query endpoint deliberately returns an array with one object to match the brief exactly.
8. **Authentication:** The brief does not require webhook signatures or authentication. This is documented as out of scope. If desired later, an HMAC signature check can be added without changing the storage model.
9. **Latency measurement:** 500ms is measured server-side for requests that reach the service, under the expected grading load. Client network time and platform cold starts are excluded by definition but must still be mitigated operationally.
10. **Free-tier durability:** Render free PostgreSQL/Key Value limitations (if used) could affect durability or availability. The final plan should prefer persistent, non-expiring resources for the graded window.

## 13. Deliverables

- Public GitHub repository containing the working application.
- `README.md` with:
  - Architecture overview.
  - Local setup and run instructions (Docker and native).
  - How to run the test suite.
  - How to exercise the deployed endpoints (curl examples).
  - Explanation of technical choices, idempotency, durability, and trade-offs.
- Deployment configuration for Render (for example, `render.yaml` or documented dashboard steps).
- `Dockerfile`(s) for the API and worker.
- `.env.example`.
- Automated tests mapped to the acceptance criteria.
- `ASSESSMENT_QUESTION.md`, `SPEC.md`, and the forthcoming `PLAN.md` for traceability.
