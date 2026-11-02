#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from uuid import uuid4


class CheckFailure(RuntimeError):
    pass


def _decode(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="replace")


def request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float,
) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_object = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request_object, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())
    except urllib.error.URLError as exc:
        raise CheckFailure(f"{method} {url} failed: {exc.reason}") from exc


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise CheckFailure(f"{name}: {detail}".strip())
    print(f"PASS {name}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the live transaction lifecycle acceptance check."
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    transaction_id = f"acceptance_{uuid4().hex}"
    payload = {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }
    transaction_url = f"{base_url}/v1/transactions/{urllib.parse.quote(transaction_id)}"

    try:
        status, health = request("GET", f"{base_url}/", timeout=args.timeout)
        check("health status", status == 200 and health.get("status") == "HEALTHY", str(health))
        check(
            "health timestamp",
            isinstance(health.get("current_time"), str) and health["current_time"].endswith("Z"),
            str(health.get("current_time")),
        )

        status, ack = request(
            "POST", f"{base_url}/v1/webhooks/transactions", payload, timeout=args.timeout
        )
        check(
            "webhook accepted",
            status == 202
            and ack.get("status") == "ACCEPTED"
            and ack.get("transaction_id") == transaction_id,
            str(ack),
        )

        status, rows = request("GET", transaction_url, timeout=args.timeout)
        check(
            "query contract",
            status == 200 and isinstance(rows, list) and len(rows) == 1,
            str(rows),
        )
        row = rows[0]
        check("initial status", row.get("status") == "PROCESSING", str(row))
        check(
            "echoed fields",
            row.get("transaction_id") == transaction_id
            and row.get("source_account") == payload["source_account"]
            and row.get("destination_account") == payload["destination_account"]
            and abs(float(row.get("amount")) - 1500) < 0.001
            and row.get("currency") == "INR",
            str(row),
        )
        created_at = row.get("created_at")
        check(
            "created_at timestamp",
            isinstance(created_at, str) and created_at.endswith("Z"),
            str(created_at),
        )

        status, duplicate_ack = request(
            "POST",
            f"{base_url}/v1/webhooks/transactions",
            payload,
            timeout=args.timeout,
        )
        check("duplicate accepted", status == 202, str(duplicate_ack))
        status, rows = request("GET", transaction_url, timeout=args.timeout)
        check(
            "duplicate does not create a second row",
            status == 200 and len(rows) == 1 and rows[0].get("created_at") == created_at,
            str(rows),
        )

        deadline = time.monotonic() + args.wait_seconds
        processed_at = None
        while time.monotonic() < deadline:
            status, rows = request("GET", transaction_url, timeout=args.timeout)
            if status == 200 and len(rows) == 1 and rows[0].get("status") == "PROCESSED":
                processed_at = rows[0].get("processed_at")
                break
            time.sleep(args.poll_interval)
        check(
            "processed within timeout", processed_at is not None, "status never reached PROCESSED"
        )
        check(
            "processed_at timestamp",
            isinstance(processed_at, str) and processed_at.endswith("Z"),
            str(processed_at),
        )

        status, duplicate_ack = request(
            "POST",
            f"{base_url}/v1/webhooks/transactions",
            payload,
            timeout=args.timeout,
        )
        check("processed duplicate accepted", status == 202, str(duplicate_ack))
        status, rows = request("GET", transaction_url, timeout=args.timeout)
        check(
            "processed_at is stable",
            status == 200 and len(rows) == 1 and rows[0].get("processed_at") == processed_at,
            str(rows),
        )

        status, _ = request(
            "GET",
            f"{base_url}/v1/transactions/missing_{uuid4().hex}",
            timeout=args.timeout,
        )
        check("unknown transaction returns 404", status == 404)

        status, _ = request(
            "POST",
            f"{base_url}/v1/webhooks/transactions",
            {"transaction_id": "incomplete"},
            timeout=args.timeout,
        )
        check("invalid payload returns 422", status == 422)
    except CheckFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1

    print("ACCEPTANCE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
