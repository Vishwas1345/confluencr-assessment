from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_healthy_with_utc_timestamp() -> None:
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "HEALTHY"
    assert set(body) == {"status", "current_time"}
    assert body["current_time"].endswith("Z")
    datetime.fromisoformat(body["current_time"].replace("Z", "+00:00"))
