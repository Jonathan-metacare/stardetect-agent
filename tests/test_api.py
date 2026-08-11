import logging

from fastapi.testclient import TestClient

from stardetect_agent.agent.service import AgentUpstreamError, get_agent_service
from stardetect_agent.api.main import app
from stardetect_agent.system.provider import get_metrics_provider


class FakeProvider:
    def get_system_snapshot(self) -> dict[str, object]:
        return {
            "available": True,
            "scope": "mixed",
            "memory": {"available": True},
            "storage": {"available": True},
            "gpu": {"available": True},
            "power": {"available": False},
        }


class FakeAgentService:
    def invoke(
        self, message: str, image_url: str | None = None, *, task_id: str = "unscoped"
    ) -> dict[str, object]:
        return {
            "answer": f"mock answer: {message}; image={image_url}",
            "tool_calls": [{"name": "get_system_snapshot", "args": {}, "id": "mock"}],
        }


class FailingAgentService:
    def invoke(
        self, message: str, image_url: str | None = None, *, task_id: str = "unscoped"
    ) -> dict[str, object]:
        raise AgentUpstreamError(
            "llm_connection_failed",
            "Cannot connect to LLM endpoint http://llm-vl:8000/v1",
        )


def test_health_endpoint() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["model_backend"] == "openai_compatible"
    assert response.json()["model"] == "qwen3-vl"
    assert response.json()["base_url"] == "http://llm-vl:8000/v1"


def test_tools_endpoint() -> None:
    client = TestClient(app)

    response = client.get("/api/tools")

    assert response.status_code == 200
    names = {item["name"] for item in response.json()["tools"]}
    assert "get_system_snapshot" in names
    assert "get_gpu_status" in names
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
    assert response.json()["answer"] == "mock answer: GPU 温度是多少？; image=None"
    assert response.json()["tool_calls"][0]["name"] == "get_system_snapshot"


def test_chat_endpoint_forwards_safe_task_id() -> None:
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        headers={"X-SpaceZenith-Task-ID": "20260810T120000Z-123"},
        json={"message": "hello"},
    )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.headers["X-SpaceZenith-Task-ID"] == "20260810T120000Z-123"


def test_chat_audit_log_does_not_contain_message_or_image_url(caplog: object) -> None:
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()
    client = TestClient(app)
    caplog.set_level(logging.INFO, logger="stardetect_agent.audit")  # type: ignore[attr-defined]

    response = client.post(
        "/api/chat",
        headers={"X-SpaceZenith-Task-ID": "task-safe"},
        json={"message": "private prompt text", "image_url": "https://example.com/private.png"},
    )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    rendered = caplog.text  # type: ignore[attr-defined]
    assert "private prompt text" not in rendered
    assert "https://example.com/private.png" not in rendered
    assert '"has_image":true' in rendered


def test_chat_endpoint_accepts_image_url() -> None:
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={
            "message": "请描述这张图片",
            "image_url": "https://example.com/status.png",
        },
    )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["answer"] == (
        "mock answer: 请描述这张图片; image=https://example.com/status.png"
    )


def test_chat_endpoint_rejects_unsupported_image_url() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "请描述这张图片", "image_url": "file:///etc/passwd"},
    )

    assert response.status_code == 422


def test_chat_endpoint_returns_diagnostic_502_for_upstream_failure() -> None:
    app.dependency_overrides[get_agent_service] = lambda: FailingAgentService()
    client = TestClient(app)

    response = client.post("/api/chat", json={"message": "GPU 温度是多少？"})

    app.dependency_overrides.clear()
    assert response.status_code == 502
    assert response.json() == {
        "detail": {
            "code": "llm_connection_failed",
            "message": "Cannot connect to LLM endpoint http://llm-vl:8000/v1",
        }
    }
