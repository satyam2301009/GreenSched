
import pytest
from fastapi.testclient import TestClient

from backend.main import app, REGIONS


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_home_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "GreenSched API is running"


def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["database"] == "SQLite"


def test_regions_endpoint(client):
    response = client.get("/api/regions")
    assert response.status_code == 200

    regions = response.json()["regions"]
    assert len(regions) >= 2

    for region in regions:
        assert "region" in region
        assert "carbon_intensity_gco2_kwh" in region
        assert region["source"] in ("LIVE", "SIMULATED")


def test_submit_task_and_persist(client):
    response = client.post(
        "/api/tasks",
        json={
            "task_name": "Automated Test Task",
            "runtime_minutes": 5,
            "energy_kwh": 0.1,
        },
    )
    assert response.status_code == 200

    task = response.json()
    assert task["task_name"] == "Automated Test Task"
    assert task["selected_region"]
    assert task["execution_mode"] == "LOCAL_DEMO"
    assert task["estimated_emissions_gco2"] >= 0

    history = client.get("/api/tasks")
    assert history.status_code == 200
    assert any(
        saved["task_id"] == task["task_id"]
        for saved in history.json()["tasks"]
    )


def test_invalid_task_is_rejected(client):
    response = client.post(
        "/api/tasks",
        json={"task_name": "", "runtime_minutes": 0, "energy_kwh": -1},
    )
    assert response.status_code == 422


def test_unknown_task_returns_404(client):
    response = client.get("/api/tasks/nonexistent-task-id")
    assert response.status_code == 404


def test_demo_regions_have_intensity_values():
    for region in REGIONS:
        assert region["demo_intensity"] > 0
