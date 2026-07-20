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
        thermal_root: str | Path = "/sys/class/thermal",
        hwmon_root: str | Path = "/sys/class/hwmon",
        ina3221_root: str | Path = "/sys/bus/i2c/drivers/ina3221",
        ina3221x_root: str | Path = "/sys/bus/i2c/drivers/ina3221x",
        i2c_devices_root: str | Path = "/sys/bus/i2c/devices",
        platform_devices_root: str | Path = "/sys/devices/platform",
    ) -> None:
        settings = get_settings()
        self._tegrastats_path = tegrastats_path or settings.tegrastats_path
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.tegrastats_timeout_seconds
        )
        self._command_runner = command_runner
        self._thermal_root = Path(thermal_root)
        self._hwmon_root = Path(hwmon_root)
        self._ina3221_root = Path(ina3221_root)
        self._ina3221x_root = Path(ina3221x_root)
        self._i2c_devices_root = Path(i2c_devices_root)
        self._platform_devices_root = Path(platform_devices_root)

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
        tegrastats_rails = snapshot.power_rails if snapshot else {}
        sysfs_rails = self._read_sysfs_power_rails()
        rails = {**sysfs_rails, **tegrastats_rails}
        total_instant_mw = sum(
            rail["instant_mw"] for rail in rails.values() if isinstance(rail.get("instant_mw"), int)
        )
        total_instant_w = _mw_to_w(total_instant_mw)
        return {
            "available": bool(rails),
            "rails": rails,
            "summary": _build_power_summary(rails, total_instant_w),
            "total_instant_mw": total_instant_mw,
            "total_instant_w": total_instant_w,
            "sources": {
                "tegrastats": tegrastats_rails,
                "sysfs": sysfs_rails,
            },
            "unit": "mW",
            "note": None if rails else "No power rail readings were found in tegrastats or sysfs.",
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
        readings: dict[str, float] = {}
        if not self._thermal_root.exists():
            return readings
        for zone in self._thermal_root.glob("thermal_zone*"):
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

    def _read_sysfs_power_rails(self) -> dict[str, dict[str, Any]]:
        rails: dict[str, dict[str, Any]] = {}
        for power_dir in self._iter_power_monitor_dirs():
            for name, reading in self._read_power_monitor_dir(power_dir).items():
                rails[_unique_rail_name(name, rails)] = reading
        return rails

    def _iter_power_monitor_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        seen: set[str] = set()
        patterns = [
            (self._hwmon_root, "hwmon*"),
            (self._ina3221_root, "**/hwmon*"),
            (self._ina3221x_root, "**/iio:device*"),
            (self._i2c_devices_root, "**/hwmon*"),
            (self._platform_devices_root, "**/hwmon*"),
        ]
        for root, pattern in patterns:
            if not root.exists():
                continue
            for candidate in root.glob(pattern):
                if not candidate.is_dir():
                    continue
                key = str(candidate.resolve())
                if key in seen:
                    continue
                seen.add(key)
                dirs.append(candidate)
        return dirs

    def _read_power_monitor_dir(self, power_dir: Path) -> dict[str, dict[str, Any]]:
        rails: dict[str, dict[str, Any]] = {}
        channels = _discover_power_channels(power_dir)
        for channel in channels:
            name = _read_text(power_dir / f"in{channel}_label")
            if name is None:
                name = _read_text(power_dir / f"rail_name_{channel}")
            if _is_ignored_power_label(name):
                continue

            power_mw = _read_int(power_dir / f"in_power{channel}_input")
            if power_mw is None:
                power_uw = _read_int(power_dir / f"power{channel}_input")
                power_mw = round(power_uw / 1000) if power_uw is not None else None

            voltage_mv = _read_int(power_dir / f"in{channel}_input")
            if voltage_mv is None:
                voltage_mv = _read_int(power_dir / f"in_voltage{channel}_input")

            current_channel = _current_channel_for_voltage_channel(channel)
            current_ma = _read_int(power_dir / f"curr{current_channel}_input")
            if current_ma is None:
                current_ma = _read_int(power_dir / f"in_current{channel}_input")

            if power_mw is None and voltage_mv is not None and current_ma is not None:
                power_mw = round((voltage_mv * current_ma) / 1000)
            if power_mw is None:
                continue

            rails[name] = {
                "instant_mw": power_mw,
                "instant_w": _mw_to_w(power_mw),
                "voltage_mv": voltage_mv,
                "voltage_v": _mv_to_v(voltage_mv),
                "current_ma": current_ma,
                "current_a": _ma_to_a(current_ma),
                "source": str(power_dir),
            }
        return rails

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


def _mw_to_w(value: int | None) -> float | None:
    return round(value / 1000, 3) if value is not None else None


def _mv_to_v(value: int | None) -> float | None:
    return round(value / 1000, 3) if value is not None else None


def _ma_to_a(value: int | None) -> float | None:
    return round(value / 1000, 3) if value is not None else None


def _build_power_summary(rails: dict[str, dict[str, Any]], total_instant_w: float) -> str:
    if not rails:
        return "No power rail readings are available."
    rail_parts = [
        f"{name}: {reading['instant_w']} W"
        for name, reading in sorted(rails.items())
        if reading.get("instant_w") is not None
    ]
    return f"Total readable rail power: {total_instant_w} W; " + "; ".join(rail_parts)


def _decode_timeout_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _discover_power_channels(power_dir: Path) -> list[int]:
    channels: set[int] = set()
    for pattern in ("in*_label", "rail_name_*"):
        for path in power_dir.glob(pattern):
            suffix = path.stem.rsplit("_", maxsplit=1)[-1]
            if suffix.isdigit():
                channels.add(int(suffix))
                continue
            number = path.stem.removeprefix("in").removesuffix("_label")
            if number.isdigit():
                channels.add(int(number))
    return sorted(channels)


def _current_channel_for_voltage_channel(voltage_channel: int) -> int:
    if voltage_channel == 7:
        return 4
    return voltage_channel


def _is_ignored_power_label(name: str | None) -> bool:
    if not name:
        return True
    normalized = name.strip().lower()
    return normalized in {"nc", "not connected"} or "sum of shunt voltages" in normalized


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    value = _read_text(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _unique_rail_name(name: str, rails: dict[str, dict[str, Any]]) -> str:
    if name not in rails:
        return name
    index = 2
    while f"{name}_{index}" in rails:
        index += 1
    return f"{name}_{index}"
