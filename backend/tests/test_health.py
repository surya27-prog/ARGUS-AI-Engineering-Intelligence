from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_200():
    response = client.get("/health")
    assert response.status_code == 200


def test_health_payload():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"
    assert "postgres" in body
    assert "neo4j" in body


def test_health_reports_stores_up():
    """Requires the compose stack to be running."""
    body = client.get("/health").json()
    assert body["postgres"] == "up"
    assert body["neo4j"] == "up"


def test_root_redirects_to_docs_hint():
    body = client.get("/").json()
    assert body["docs"] == "/docs"
