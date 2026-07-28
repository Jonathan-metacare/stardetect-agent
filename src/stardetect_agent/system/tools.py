import json
from typing import Any

from langchain.tools import tool

from stardetect_agent.system.provider import get_metrics_provider


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


@tool
def get_system_snapshot() -> str:
    """Return container CPU, memory, and storage plus physical Iluvatar GPU status."""
    return _json(get_metrics_provider().get_system_snapshot())


@tool
def get_cpu_status() -> str:
    """Return CPU utilization visible to the agent container."""
    return _json(get_metrics_provider().get_cpu_status())


@tool
def get_memory_status() -> str:
    """Return RAM and swap usage visible to the agent container."""
    return _json(get_metrics_provider().get_memory_status())


@tool
def get_storage_status(path: str = "/") -> str:
    """Return storage usage for a path inside the agent container."""
    return _json(get_metrics_provider().get_storage_status(path))


@tool
def get_gpu_status() -> str:
    """Return MR-V100 status plus current, 5-second average, and peak utilization."""
    return _json(get_metrics_provider().get_gpu_status())


@tool
def get_power_status() -> str:
    """Return GPU power availability; never estimate unsupported power values."""
    return _json(get_metrics_provider().get_power_status())


def build_system_tools() -> list[Any]:
    return [
        get_system_snapshot,
        get_cpu_status,
        get_memory_status,
        get_storage_status,
        get_gpu_status,
        get_power_status,
    ]


def list_tool_metadata() -> list[dict[str, str]]:
    return [
        {
            "name": tool_obj.name,
            "description": tool_obj.description or "",
        }
        for tool_obj in build_system_tools()
    ]
