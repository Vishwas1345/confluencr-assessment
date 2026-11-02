import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("app.render_start")


def _run_migrations() -> None:
    command = [sys.executable, "-m", "alembic", "upgrade", "head"]
    for attempt in range(1, 4):
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        if result.returncode == 0:
            logger.info("migrations_completed")
            return
        logger.error("migrations_failed attempt=%s code=%s", attempt, result.returncode)
        time.sleep(5)
    raise SystemExit(1)


def _start(name: str, command: list[str]) -> subprocess.Popen:
    logger.info("child_starting name=%s command=%s", name, " ".join(command))
    return subprocess.Popen(command, cwd=PROJECT_ROOT)


def _terminate(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def main() -> int:
    _run_migrations()

    concurrency = os.environ.get("WORKER_CONCURRENCY", "5")
    port = os.environ.get("PORT", "8000")
    worker_command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "app.celery_app.celery_app",
        "worker",
        "--loglevel=INFO",
        "--pool=threads",
        f"--concurrency={concurrency}",
        "--without-gossip",
        "--without-mingle",
    ]
    if os.name != "nt":
        worker_command.append("--beat")

    children: list[tuple[str, subprocess.Popen]] = []
    stop = threading.Event()

    def handle_signal(signum: int, _frame: object) -> None:
        logger.info("shutdown_requested signal=%s", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        children.append(("worker", _start("worker", worker_command)))
        if os.name == "nt":
            children.append(
                (
                    "beat",
                    _start(
                        "beat",
                        [
                            sys.executable,
                            "-m",
                            "celery",
                            "-A",
                            "app.celery_app.celery_app",
                            "beat",
                            "--loglevel=INFO",
                        ],
                    ),
                )
            )
        children.append(
            (
                "api",
                _start(
                    "api",
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        port,
                        "--proxy-headers",
                        "--forwarded-allow-ips=*",
                    ],
                ),
            )
        )

        exit_code = 0
        while not stop.is_set():
            for name, process in children:
                code = process.poll()
                if code is not None:
                    logger.error("child_exited name=%s code=%s", name, code)
                    exit_code = code or 1
                    stop.set()
                    break
            stop.wait(1.0)
        return exit_code
    finally:
        _terminate([process for _, process in children])


if __name__ == "__main__":
    raise SystemExit(main())
