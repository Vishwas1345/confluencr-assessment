import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import redis

PROJECT_ROOT = Path(__file__).resolve().parents[2]
E2E_REDIS_URL = "redis://localhost:6379/9"

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http(url: str, process: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Process exited with code {process.returncode} before {url} was ready"
            )
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}")


def _wait_for_log(
    path: Path,
    needle: str,
    process: subprocess.Popen,
    timeout: float = 20.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Worker exited with code {process.returncode}")
        if path.exists() and needle in path.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {needle!r} in {path}")


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def test_webhook_is_processed_by_real_worker() -> None:
    redis_client = redis.Redis.from_url(E2E_REDIS_URL)
    try:
        redis_client.ping()
    except redis.RedisError:
        pytest.skip("Redis is not available")
    redis_client.flushdb()

    database_url = os.environ["DATABASE_URL"]
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "e2e",
            "DATABASE_URL": database_url,
            "TEST_DATABASE_URL": database_url,
            "REDIS_URL": E2E_REDIS_URL,
            "PROCESSING_DELAY_SECONDS": "1",
            "PROCESSING_LEASE_SECONDS": "10",
            "DISPATCH_STALE_SECONDS": "5",
            "RECONCILE_INTERVAL_SECONDS": "1",
            "WORKER_CONCURRENCY": "2",
            "PYTHONUNBUFFERED": "1",
        }
    )
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        api_log_path = Path(temp_dir) / "api.log"
        worker_log_path = Path(temp_dir) / "worker.log"
        api_process = None
        worker_process = None

        try:
            with api_log_path.open("w", encoding="utf-8") as api_log:
                api_process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--log-level",
                        "warning",
                    ],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=api_log,
                    stderr=subprocess.STDOUT,
                )
                _wait_for_http(f"{base_url}/", api_process)

            with worker_log_path.open("w", encoding="utf-8") as worker_log:
                worker_process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "celery",
                        "-A",
                        "app.celery_app.celery_app",
                        "worker",
                        "--loglevel=INFO",
                        "--pool=threads",
                        "--concurrency=2",
                    ],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                )
            _wait_for_log(worker_log_path, "ready.", worker_process)

            transaction_id = f"e2e_{uuid4().hex}"
            payload = {
                "transaction_id": transaction_id,
                "source_account": "acc_user_789",
                "destination_account": "acc_merchant_456",
                "amount": 1500,
                "currency": "INR",
            }
            with httpx.Client(base_url=base_url, timeout=5.0) as client:
                response = client.post("/v1/webhooks/transactions", json=payload)
                assert response.status_code == 202

                deadline = time.monotonic() + 30
                row = None
                while time.monotonic() < deadline:
                    query = client.get(f"/v1/transactions/{transaction_id}")
                    assert query.status_code == 200
                    rows = query.json()
                    assert len(rows) == 1
                    row = rows[0]
                    if row["status"] == "PROCESSED":
                        break
                    time.sleep(0.25)

                assert row is not None
                assert row["status"] == "PROCESSED"
                assert row["processed_at"] is not None
                processed_at = row["processed_at"]

                duplicate = client.post("/v1/webhooks/transactions", json=payload)
                assert duplicate.status_code == 202
                duplicate_query = client.get(f"/v1/transactions/{transaction_id}")
                duplicate_rows = duplicate_query.json()
                assert len(duplicate_rows) == 1
                assert duplicate_rows[0]["processed_at"] == processed_at
        except Exception:
            if api_log_path.exists():
                print("API LOG:\n" + api_log_path.read_text(encoding="utf-8", errors="replace"))
            if worker_log_path.exists():
                print(
                    "WORKER LOG:\n" + worker_log_path.read_text(encoding="utf-8", errors="replace")
                )
            raise
        finally:
            if worker_process is not None:
                _terminate(worker_process)
            if api_process is not None:
                _terminate(api_process)
            redis_client.flushdb()
