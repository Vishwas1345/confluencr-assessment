#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Result:
    transaction_id: str
    status: int | None
    latency_ms: float
    error: str | None


def request(
    method: str,
    url: str,
    payload: dict | None = None,
    *,
    timeout: float,
) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_object = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request_object, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(str(exc)) from exc


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def send_one(base_url: str, timeout: float) -> Result:
    transaction_id = f"load_{uuid4().hex}"
    payload = {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }
    started = time.perf_counter()
    try:
        status, _ = request(
            "POST",
            f"{base_url}/v1/webhooks/transactions",
            payload,
            timeout=timeout,
        )
        error = None
    except RuntimeError as exc:
        status = None
        error = str(exc)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return Result(transaction_id, status, elapsed_ms, error)


def wait_until_processed(
    base_url: str,
    transaction_id: str,
    *,
    timeout: float,
    deadline: float,
) -> bool:
    url = f"{base_url}/v1/transactions/{urllib.parse.quote(transaction_id)}"
    while time.monotonic() < deadline:
        try:
            status, body = request("GET", url, timeout=timeout)
        except RuntimeError:
            status, body = None, None
        if (
            status == 200
            and isinstance(body, list)
            and body
            and body[0].get("status") == "PROCESSED"
        ):
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test the transaction webhook endpoint.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-p95-ms", type=float, default=500.0)
    parser.add_argument("--poll", action="store_true", help="Wait for every accepted transaction.")
    parser.add_argument("--poll-timeout", type=float, default=120.0)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")

    base_url = args.base_url.rstrip("/")
    started = time.perf_counter()
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(send_one, base_url, args.timeout) for _ in range(args.count)]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started

    latencies = [result.latency_ms for result in results]
    accepted = sum(1 for result in results if result.status == 202)
    non_202 = sum(1 for result in results if result.status != 202)
    errors = sum(1 for result in results if result.error is not None)

    print(f"count={len(results)}")
    print(f"accepted_202={accepted}")
    print(f"non_202={non_202}")
    print(f"errors={errors}")
    print(f"total_seconds={elapsed:.3f}")
    p95 = percentile(latencies, 0.95)
    print(f"latency_ms_p50={percentile(latencies, 0.50):.2f}")
    print(f"latency_ms_p95={p95:.2f}")
    print(f"latency_ms_max={max(latencies):.2f}")

    failed = non_202 > 0 or errors > 0 or p95 > args.max_p95_ms
    if args.poll:
        deadline = time.monotonic() + args.poll_timeout
        processed = 0
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [
                executor.submit(
                    wait_until_processed,
                    base_url,
                    result.transaction_id,
                    timeout=args.timeout,
                    deadline=deadline,
                )
                for result in results
            ]
            for future in as_completed(futures):
                processed += int(future.result())
        print(f"processed={processed}")
        if processed != len(results):
            failed = True

    if failed:
        print("LOAD FAIL")
        return 1
    print("LOAD PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
