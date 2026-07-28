from functools import lru_cache
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APIStatusError

from stardetect_agent.config import get_settings
from stardetect_agent.system.tools import build_system_tools

SYSTEM_PROMPT = """You are an operations assistant for an openEuler system with
a Phytium S5000C CPU and Iluvatar MR-V100 GPU.

Use the provided tools whenever the user asks about CPU, GPU, RAM, swap,
storage, temperature, utilization, or power. CPU, memory, and storage values
have container scope. GPU values have physical-device scope. State the scope
when it matters and never describe container values as host values.

Do not invent hardware metrics. If a metric is unavailable, say so and use the
tool's reason. In particular, GPU power is unavailable when ixsmi does not
support a power query; never estimate it from other fields. For GPU utilization,
prefer the 5-second `utilization_window` average and maximum, and label the
single `current` value as an instantaneous sample. Keep answers concise and
operationally useful.
"""


class AgentUpstreamError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AgentService:
    def __init__(self) -> None:
        settings = get_settings()
        self._llm_base_url = settings.llm_base_url
        self._llm_model = settings.llm_model
        model = ChatOpenAI(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            temperature=0,
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": False,
                }
            },
        )
        self._agent = create_agent(
            model=model,
            tools=build_system_tools(),
            system_prompt=SYSTEM_PROMPT,
        )

    def invoke(self, message: str) -> dict[str, Any]:
        try:
            result = self._agent.invoke({"messages": [{"role": "user", "content": message}]})
        except APIConnectionError as exc:
            raise AgentUpstreamError(
                "llm_connection_failed",
                f"Cannot connect to LLM endpoint {self._llm_base_url}",
            ) from exc
        except APIStatusError as exc:
            raise AgentUpstreamError(
                "llm_request_failed",
                (
                    f"LLM model {self._llm_model!r} returned HTTP "
                    f"{exc.status_code}: {exc.message}"
                ),
            ) from exc
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
