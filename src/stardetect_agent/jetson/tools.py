import json
from typing import Any

from langchain.tools import tool

from stardetect_agent.jetson.provider import get_metrics_provider


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


@tool
def get_system_snapshot() -> str:
    """Return a full Jetson Orin telemetry snapshot."""
    return _json(get_metrics_provider().get_system_snapshot())


@tool
def get_memory_status() -> str:
    """Return Jetson Orin RAM, swap, and GPU/GR3D memory-related status."""
    return _json(get_metrics_provider().get_memory_status())


@tool
def get_storage_status(path: str = "/") -> str:
    """Return storage usage for a filesystem path on the Jetson Orin."""
    return _json(get_metrics_provider().get_storage_status(path))


@tool
def get_temperature_status() -> str:
    """Return Jetson Orin temperature readings from tegrastats and thermal sysfs."""
    return _json(get_metrics_provider().get_temperature_status())


@tool
def get_power_status() -> str:
    """Return Jetson Orin power rail readings when available."""
    return _json(get_metrics_provider().get_power_status())


def build_jetson_tools() -> list[Any]:
    return [
        get_system_snapshot,
        get_memory_status,
        get_storage_status,
        get_temperature_status,
        get_power_status,
    ]


def list_tool_metadata() -> list[dict[str, str]]:
    return [
        {
            "name": tool_obj.name,
            "description": tool_obj.description or "",
        }
        for tool_obj in build_jetson_tools()
    ]
