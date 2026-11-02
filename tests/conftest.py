import os
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/confluencr_test"


def _normalize_database_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


test_database_url = _normalize_database_url(
    os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
)
os.environ["TEST_DATABASE_URL"] = test_database_url
os.environ["DATABASE_URL"] = test_database_url
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ["APP_ENV"] = "test"
os.environ.setdefault("PROCESSING_DELAY_SECONDS", "0.05")

from alembic.config import Config  # noqa: E402

from alembic import command  # noqa: E402
from app.db import engine  # noqa: E402

TEST_DATABASE_NAME = make_url(test_database_url).database
assert TEST_DATABASE_NAME is not None


def _ensure_test_database() -> None:
    parsed_url = make_url(test_database_url)
    admin_url = parsed_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

    try:
        with admin_engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": parsed_url.database},
            ).scalar()
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{parsed_url.database}"'))
    finally:
        admin_engine.dispose()


def _run_migrations() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database() -> Generator[None, None, None]:
    _ensure_test_database()
    _run_migrations()
    yield


@pytest.fixture(autouse=True)
def clean_transactions(migrate_test_database: None) -> Generator[None, None, None]:
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE transactions"))
    yield
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE transactions"))


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
