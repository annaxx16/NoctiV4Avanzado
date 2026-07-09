from fastapi.testclient import TestClient

from umbra.api.app import app


def test_health_returns_ok():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_endpoint():
    with TestClient(app) as client:
        response = client.get("/version")
    assert response.status_code == 200
    data = response.json()
    assert "version" in data
    assert data["mode"] in {"sim", "paper", "live"}
