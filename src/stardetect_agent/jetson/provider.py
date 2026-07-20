import shutil
import subprocess
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import psutil

from stardetect_agent.config import get_settings
from stardetect_agent.jetson.tegrastats import TegrastatsSnapshot, parse_tegrastats

CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def default_command_runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


class JetsonMetricsProvider:
    def __init__(
        self,
        tegrastats_path: str | None = None,
        timeout_seconds: float | None = None,
        command_runner: CommandRunner = default_command_runner,
    ) -> None:
        settings = get_settings()
        self._tegrastats_path = tegrastats_path or settings.tegrastats_path
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.tegrastats_timeout_seconds
        )
        self._command_runner = command_runner

    def get_system_snapshot(self) -> dict[str, Any]:
        tegrastats = self.read_tegrastats()
        return {
            "available": True,
            "tegrastats": self._serialize_tegrastats(tegrastats),
            "memory": self.get_memory_status(tegrastats),
            "storage": self.get_storage_status("/"),
            "temperatures": self.get_temperature_status(tegrastats),
            "power": self.get_power_status(tegrastats),
        }

    def get_memory_status(self, tegrastats: TegrastatsSnapshot | None = None) -> dict[str, Any]:
        snapshot = tegrastats or self.read_tegrastats()
        virtual_memory = psutil.virtual_memory()
        swap_memory = psutil.swap_memory()
        return {
            "available": True,
            "system": {
                "total_mb": _bytes_to_mb(virtual_memory.total),
                "available_mb": _bytes_to_mb(virtual_memory.available),
                "used_mb": _bytes_to_mb(virtual_memory.used),
                "used_percent": virtual_memory.percent,
            },
            "swap": {
                "total_mb": _bytes_to_mb(swap_memory.total),
                "used_mb": _bytes_to_mb(swap_memory.used),
                "free_mb": _bytes_to_mb(swap_memory.free),
                "used_percent": swap_memory.percent,
            },
            "tegrastats_ram": snapshot.ram if snapshot else None,
            "tegrastats_swap": snapshot.swap if snapshot else None,
            "gpu": snapshot.gpu if snapshot else None,
        }

    def get_storage_status(self, path: str = "/") -> dict[str, Any]:
        usage = shutil.disk_usage(path)
        return {
            "available": True,
            "path": path,
            "total_gb": _bytes_to_gb(usage.total),
            "used_gb": _bytes_to_gb(usage.used),
            "free_gb": _bytes_to_gb(usage.free),
            "used_percent": round((usage.used / usage.total) * 100, 2) if usage.total else None,
        }

    def get_temperature_status(
        self,
        tegrastats: TegrastatsSnapshot | None = None,
    ) -> dict[str, Any]:
        snapshot = tegrastats or self.read_tegrastats()
        sysfs = self._read_sysfs_temperatures()
        return {
            "available": bool((snapshot and snapshot.temperatures) or sysfs),
            "tegrastats": snapshot.temperatures if snapshot else {},
            "sysfs": sysfs,
        }

    def get_power_status(self, tegrastats: TegrastatsSnapshot | None = None) -> dict[str, Any]:
        snapshot = tegrastats or self.read_tegrastats()
        rails = snapshot.power_rails if snapshot else {}
        return {
            "available": bool(rails),
            "rails": rails,
            "unit": "mW",
            "note": None if rails else "No power rail readings were found in tegrastats output.",
        }

    def read_tegrastats(self) -> TegrastatsSnapshot | None:
        try:
            completed = self._command_runner(
                [self._tegrastats_path, "--interval", "1000"],
                self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            output = _decode_timeout_output(exc.stdout)
            return parse_tegrastats(output) if output else None
        except (FileNotFoundError, PermissionError):
            return None
        if completed.returncode not in {0, 124, -15, -9} and not completed.stdout:
            return None
        if not completed.stdout:
            return None
        return parse_tegrastats(completed.stdout)

    def _read_sysfs_temperatures(self) -> dict[str, float]:
        root = Path("/sys/class/thermal")
        readings: dict[str, float] = {}
        if not root.exists():
            return readings
        for zone in root.glob("thermal_zone*"):
            type_path = zone / "type"
            temp_path = zone / "temp"
            if not temp_path.exists():
                continue
            try:
                name = (
                    type_path.read_text(encoding="utf-8").strip()
                    if type_path.exists()
                    else zone.name
                )
                raw_temp = int(temp_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            readings[name] = round(raw_temp / 1000, 2)
        return readings

    @staticmethod
    def _serialize_tegrastats(snapshot: TegrastatsSnapshot | None) -> dict[str, Any]:
        if snapshot is None:
            return {
                "available": False,
                "raw": "",
                "error": "tegrastats unavailable, timed out, or produced no output",
            }
        return {
            "available": True,
            "raw": snapshot.raw,
            "ram": snapshot.ram,
            "swap": snapshot.swap,
            "gpu": snapshot.gpu,
            "temperatures": snapshot.temperatures,
            "power_rails": snapshot.power_rails,
        }


@lru_cache(maxsize=1)
def get_metrics_provider() -> JetsonMetricsProvider:
    return JetsonMetricsProvider()


def _bytes_to_mb(value: int) -> int:
    return round(value / 1024 / 1024)


def _bytes_to_gb(value: int) -> float:
    return round(value / 1024 / 1024 / 1024, 2)


def _decode_timeout_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output
