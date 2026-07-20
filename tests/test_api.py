from fastapi.testclient import TestClient

from stardetect_agent.agent.service import get_agent_service
from stardetect_agent.api.main import app
from stardetect_agent.jetson.provider import get_metrics_provider


class FakeProvider:
    def get_system_snapshot(self) -> dict[str, object]:
        return {
            "available": True,
            "memory": {"available": True},
            "storage": {"available": True},
            "temperatures": {"available": False},
            "power": {"available": False},
        }


class FakeAgentService:
    def invoke(self, message: str) -> dict[str, object]:
        return {
            "answer": f"mock answer: {message}",
            "tool_calls": [{"name": "get_system_snapshot", "args": {}, "id": "mock"}],
        }


def test_health_endpoint() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["ollama_model"] == "qwen3.5:4b"


def test_tools_endpoint() -> None:
    client = TestClient(app)

    response = client.get("/api/tools")

    assert response.status_code == 200
    names = {item["name"] for item in response.json()["tools"]}
    assert "get_system_snapshot" in names
    assert "get_power_status" in names


def test_telemetry_snapshot_endpoint_with_override() -> None:
    app.dependency_overrides[get_metrics_provider] = lambda: FakeProvider()
    client = TestClient(app)

    response = client.get("/api/telemetry/snapshot")

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["available"] is True


def test_chat_endpoint_with_mock_agent() -> None:
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()
    client = TestClient(app)

    response = client.post("/api/chat", json={"message": "GPU 温度是多少？"})

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["answer"] == "mock answer: GPU 温度是多少？"
    assert response.json()["tool_calls"][0]["name"] == "get_system_snapshot"
