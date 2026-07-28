import math
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from threading import Event, Lock, Thread
from typing import Any

import psutil

from stardetect_agent.config import get_settings
from stardetect_agent.system.ixsmi import IXSMI_QUERY_FIELDS, parse_ixsmi_csv

CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class GpuSample:
    monotonic_at: float
    sampled_at: str
    device: dict[str, Any]


def default_command_runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


class SystemMetricsProvider:
    def __init__(
        self,
        ixsmi_path: str | None = None,
        timeout_seconds: float | None = None,
        sampling_interval_seconds: float | None = None,
        sampling_window_seconds: float | None = None,
        command_runner: CommandRunner = default_command_runner,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        settings = get_settings()
        self._ixsmi_path = ixsmi_path or settings.ixsmi_path
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.ixsmi_timeout_seconds
        )
        self._sampling_interval_seconds = (
            sampling_interval_seconds
            if sampling_interval_seconds is not None
            else settings.gpu_sampling_interval_seconds
        )
        self._sampling_window_seconds = (
            sampling_window_seconds
            if sampling_window_seconds is not None
            else settings.gpu_sampling_window_seconds
        )
        if self._sampling_interval_seconds <= 0:
            raise ValueError("sampling_interval_seconds must be greater than zero")
        if self._sampling_window_seconds <= 0:
            raise ValueError("sampling_window_seconds must be greater than zero")

        self._command_runner = command_runner
        self._monotonic_clock = monotonic_clock
        self._history_limit = max(
            2,
            math.ceil(self._sampling_window_seconds / self._sampling_interval_seconds) + 2,
        )
        self._histories: dict[str, deque[GpuSample]] = {}
        self._latest_error: dict[str, Any] | None = None
        self._state_lock = Lock()
        self._collect_lock = Lock()
        self._stop_event = Event()
        self._sampling_thread: Thread | None = None

    def start_sampling(self) -> None:
        """Start the process-wide daemon sampler if it is not already running."""
        with self._state_lock:
            if self._sampling_thread is not None and self._sampling_thread.is_alive():
                return
            self._stop_event.clear()
            thread = Thread(
                target=self._sampling_loop,
                name="ixsmi-sampler",
                daemon=True,
            )
            self._sampling_thread = thread
            thread.start()

    def stop_sampling(self) -> None:
        """Stop the sampler during application shutdown."""
        with self._state_lock:
            thread = self._sampling_thread
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout=self._timeout_seconds + self._sampling_interval_seconds + 1)
        with self._state_lock:
            if self._sampling_thread is thread and not thread.is_alive():
                self._sampling_thread = None

    def get_system_snapshot(self) -> dict[str, Any]:
        gpu = self.get_gpu_status()
        return {
            "available": True,
            "scope": "mixed",
            "scope_details": {
                "cpu": "container",
                "memory": "container",
                "storage": "container",
                "gpu": "device",
                "power": "device",
            },
            "cpu": self.get_cpu_status(),
            "memory": self.get_memory_status(),
            "storage": self.get_storage_status("/"),
            "gpu": gpu,
            "power": self.get_power_status(),
        }

    def get_cpu_status(self) -> dict[str, Any]:
        try:
            used_percent = psutil.cpu_percent(interval=None)
        except OSError as exc:
            return {
                "available": False,
                "scope": "container",
                "error": str(exc) or "CPU utilization could not be read",
            }
        return {
            "available": True,
            "scope": "container",
            "logical_cpu_count": psutil.cpu_count(logical=True),
            "physical_cpu_count": psutil.cpu_count(logical=False),
            "used_percent": used_percent,
        }

    def get_memory_status(self) -> dict[str, Any]:
        try:
            virtual_memory = psutil.virtual_memory()
        except OSError as exc:
            return {
                "available": False,
                "scope": "container",
                "error": str(exc) or "memory usage could not be read",
            }
        try:
            swap_memory = psutil.swap_memory()
            swap = {
                "available": True,
                "total_mb": _bytes_to_mb(swap_memory.total),
                "used_mb": _bytes_to_mb(swap_memory.used),
                "free_mb": _bytes_to_mb(swap_memory.free),
                "used_percent": swap_memory.percent,
            }
        except OSError as exc:
            swap = {
                "available": False,
                "error": str(exc) or "swap usage could not be read",
            }
        return {
            "available": True,
            "scope": "container",
            "system": {
                "total_mb": _bytes_to_mb(virtual_memory.total),
                "available_mb": _bytes_to_mb(virtual_memory.available),
                "used_mb": _bytes_to_mb(virtual_memory.used),
                "used_percent": virtual_memory.percent,
            },
            "swap": swap,
        }

    def get_storage_status(self, path: str = "/") -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            return {
                "available": False,
                "scope": "container",
                "path": path,
                "error": str(exc),
            }
        return {
            "available": True,
            "scope": "container",
            "path": path,
            "total_gb": _bytes_to_gb(usage.total),
            "used_gb": _bytes_to_gb(usage.used),
            "free_gb": _bytes_to_gb(usage.free),
            "used_percent": round((usage.used / usage.total) * 100, 2) if usage.total else None,
        }

    def sample_gpu(self) -> dict[str, Any]:
        """Collect one ixsmi sample, store it, and return the current window."""
        with self._collect_lock:
            result = self._query_gpu()
            if not result["available"]:
                with self._state_lock:
                    self._latest_error = result
                return self._build_gpu_status()

            monotonic_at = self._monotonic_clock()
            sampled_at = datetime.now(timezone.utc).isoformat()
            with self._state_lock:
                self._latest_error = None
                for index, raw_device in enumerate(result["devices"]):
                    device = dict(raw_device)
                    device["index"] = index
                    key = str(device.get("uuid") or f"index:{index}")
                    history = self._histories.setdefault(
                        key,
                        deque(maxlen=self._history_limit),
                    )
                    history.append(
                        GpuSample(
                            monotonic_at=monotonic_at,
                            sampled_at=sampled_at,
                            device=device,
                        )
                    )
                self._prune_histories_locked(monotonic_at)
            return self._build_gpu_status()

    def get_gpu_status(self) -> dict[str, Any]:
        """Return the latest sample plus rolling statistics for the configured window."""
        now = self._monotonic_clock()
        with self._state_lock:
            self._prune_histories_locked(now)
            has_samples = any(self._histories.values())
        if not has_samples:
            return self.sample_gpu()
        return self._build_gpu_status()

    def get_power_status(self) -> dict[str, Any]:
        return {
            "available": False,
            "scope": "device",
            "source": "ixsmi",
            "reason": "unsupported_by_ixsmi_query",
            "note": "CoreX 4.4.0 rejected the power.draw query; power is not estimated.",
        }

    def _sampling_loop(self) -> None:
        while not self._stop_event.is_set():
            self.sample_gpu()
            self._stop_event.wait(self._sampling_interval_seconds)

    def _query_gpu(self) -> dict[str, Any]:
        command = [
            self._ixsmi_path,
            f"--query-gpu={','.join(IXSMI_QUERY_FIELDS)}",
            "--format=csv,nounits,noheader",
        ]
        try:
            completed = self._command_runner(command, self._timeout_seconds)
        except FileNotFoundError:
            return _gpu_error("ixsmi_not_found", f"ixsmi was not found at {self._ixsmi_path}")
        except PermissionError:
            return _gpu_error("ixsmi_permission_denied", f"cannot execute {self._ixsmi_path}")
        except subprocess.TimeoutExpired:
            return _gpu_error(
                "ixsmi_timeout",
                f"ixsmi exceeded the {self._timeout_seconds:g} second timeout",
            )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            return _gpu_error(
                "ixsmi_failed",
                detail or f"ixsmi exited with status {completed.returncode}",
            )
        try:
            devices = parse_ixsmi_csv(completed.stdout)
        except ValueError as exc:
            return _gpu_error("ixsmi_invalid_output", str(exc))
        if not devices:
            return _gpu_error("ixsmi_no_devices", "ixsmi returned no GPU rows")
        return {
            "available": True,
            "devices": [device.to_dict() for device in devices],
        }

    def _build_gpu_status(self) -> dict[str, Any]:
        now = self._monotonic_clock()
        with self._state_lock:
            self._prune_histories_locked(now)
            histories = {
                key: list(history)
                for key, history in self._histories.items()
                if history
            }
            latest_error = dict(self._latest_error) if self._latest_error else None
            sampling_active = (
                self._sampling_thread is not None and self._sampling_thread.is_alive()
            )

        if not histories:
            if latest_error is not None:
                latest_error["sampling"] = self._sampling_metadata(
                    active=sampling_active,
                    last_error=latest_error,
                )
                return latest_error
            return {
                **_gpu_error("ixsmi_no_samples", "no GPU samples are available"),
                "sampling": self._sampling_metadata(active=sampling_active),
            }

        devices = [
            self._device_window(history, now)
            for _, history in sorted(histories.items(), key=lambda item: item[0])
        ]
        return {
            "available": True,
            "scope": "device",
            "source": "ixsmi",
            "devices": devices,
            "sampling": self._sampling_metadata(
                active=sampling_active,
                last_error=latest_error,
            ),
            "error": None,
        }

    def _device_window(self, history: list[GpuSample], now: float) -> dict[str, Any]:
        latest = history[-1]
        device = dict(latest.device)
        device["sampled_at"] = latest.sampled_at
        device["sample_age_seconds"] = round(max(0.0, now - latest.monotonic_at), 3)
        device["utilization_window"] = {
            "window_seconds": self._sampling_window_seconds,
            "sample_count": len(history),
            "gpu_utilization_percent": _summarize_samples(
                history,
                "gpu_utilization_percent",
            ),
            "memory_utilization_percent": _summarize_samples(
                history,
                "memory_utilization_percent",
            ),
            "temperature_c": _summarize_samples(history, "temperature_c"),
        }
        return device

    def _sampling_metadata(
        self,
        *,
        active: bool,
        last_error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "active": active,
            "interval_seconds": self._sampling_interval_seconds,
            "window_seconds": self._sampling_window_seconds,
            "last_error": (
                {
                    "reason": last_error.get("reason"),
                    "error": last_error.get("error"),
                }
                if last_error
                else None
            ),
        }

    def _prune_histories_locked(self, now: float) -> None:
        cutoff = now - self._sampling_window_seconds
        empty_keys: list[str] = []
        for key, history in self._histories.items():
            while history and history[0].monotonic_at < cutoff:
                history.popleft()
            if not history:
                empty_keys.append(key)
        for key in empty_keys:
            del self._histories[key]


@lru_cache(maxsize=1)
def get_metrics_provider() -> SystemMetricsProvider:
    return SystemMetricsProvider()


def _gpu_error(reason: str, detail: str) -> dict[str, Any]:
    return {
        "available": False,
        "scope": "device",
        "source": "ixsmi",
        "devices": [],
        "reason": reason,
        "error": detail,
    }


def _summarize_samples(history: list[GpuSample], field: str) -> dict[str, float | None]:
    current = history[-1].device.get(field)
    values = [
        float(sample.device[field])
        for sample in history
        if sample.device.get(field) is not None
    ]
    return {
        "current": float(current) if current is not None else None,
        "average": round(sum(values) / len(values), 2) if values else None,
        "max": round(max(values), 2) if values else None,
    }


def _bytes_to_mb(value: int) -> int:
    return round(value / 1024 / 1024)


def _bytes_to_gb(value: int) -> float:
    return round(value / 1024 / 1024 / 1024, 2)
