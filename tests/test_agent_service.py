from typing import Any

from langchain_core.messages import AIMessage

from stardetect_agent.agent.service import AgentService


class RecordingAgent:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    def invoke(self, payload: dict[str, Any]) -> dict[str, list[AIMessage]]:
        self.payload = payload
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
