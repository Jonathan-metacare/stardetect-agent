from functools import lru_cache
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_ollama import ChatOllama

from stardetect_agent.config import get_settings
from stardetect_agent.jetson.tools import build_jetson_tools

SYSTEM_PROMPT = """You are a Jetson Orin operations assistant.

Use the provided Jetson tools whenever the user asks about GPU, RAM, swap,
storage, temperatures, voltage, current, or power. Do not invent hardware
metrics. If a metric is unavailable, say that it could not be read and include
the available tool details. Keep answers concise and operationally useful.
"""


class AgentService:
    def __init__(self) -> None:
        settings = get_settings()
        model = ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=0,
        )
        self._agent = create_agent(
            model=model,
            tools=build_jetson_tools(),
            system_prompt=SYSTEM_PROMPT,
        )

    def invoke(self, message: str) -> dict[str, Any]:
        result = self._agent.invoke({"messages": [{"role": "user", "content": message}]})
        messages = result.get("messages", [])
        answer = _last_ai_content(messages)
        return {"answer": answer, "tool_calls": _collect_tool_calls(messages)}


def _last_ai_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.content:
            return str(message.content)
        if getattr(message, "type", None) == "ai" and getattr(message, "content", None):
            return str(message.content)
    return ""


def _collect_tool_calls(messages: list[Any]) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for message in messages:
        for call in getattr(message, "tool_calls", []) or []:
            calls.append(
                {
                    "name": call.get("name"),
                    "args": call.get("args", {}),
                    "id": call.get("id"),
                }
            )
    return calls


@lru_cache(maxsize=1)
def get_agent_service() -> AgentService:
    return AgentService()

