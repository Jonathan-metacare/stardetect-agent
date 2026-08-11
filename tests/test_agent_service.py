from typing import Any

from langchain_core.messages import AIMessage

from stardetect_agent.agent.service import AgentService
from stardetect_agent.observability import TaskLLMCallback


class RecordingAgent:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None
        self.config: dict[str, Any] | None = None

    def invoke(
        self, payload: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, list[AIMessage]]:
        self.payload = payload
        self.config = config
        return {"messages": [AIMessage(content="图片已分析")]} 


def test_invoke_forwards_image_as_openai_multimodal_content() -> None:
    graph = RecordingAgent()
    service = AgentService.__new__(AgentService)
    service._agent = graph
    service._llm_base_url = "http://llm-vl:8000/v1"
    service._llm_model = "qwen3-vl"

    result = service.invoke(
        "请描述图片中的告警",
        image_url="https://example.com/alert.png",
    )

    assert result == {"answer": "图片已分析", "tool_calls": []}
    assert graph.payload == {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请描述图片中的告警"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/alert.png"},
                    },
                ],
            }
        ]
    }
    assert graph.config is not None
    assert len(graph.config["callbacks"]) == 1


def test_invoke_keeps_plain_text_content_compatible() -> None:
    graph = RecordingAgent()
    service = AgentService.__new__(AgentService)
    service._agent = graph
    service._llm_base_url = "http://llm-vl:8000/v1"
    service._llm_model = "qwen3-vl"

    service.invoke("当前 GPU 温度是多少？")

    assert graph.payload == {
        "messages": [{"role": "user", "content": "当前 GPU 温度是多少？"}]
    }


def test_llm_callback_logs_usage_without_response_content(monkeypatch: object) -> None:
    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        "stardetect_agent.observability.log_event",
        lambda event, **fields: events.append({"event": event, **fields}),
    )  # type: ignore[attr-defined]
    callback = TaskLLMCallback("task-1", "qwen3-vl")
    callback.on_chat_model_start({}, [[]], run_id="run-1")
    response = type("Response", (), {"llm_output": {"token_usage": {"total_tokens": 12}}})()
    callback.on_llm_end(response, run_id="run-1")

    assert events[0]["event"] == "llm_call_started"
    assert events[1]["event"] == "llm_call_completed"
    assert events[1]["task_id"] == "task-1"
    assert events[1]["total_tokens"] == 12
    assert isinstance(events[1]["duration_ms"], int)
