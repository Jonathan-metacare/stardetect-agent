import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TegrastatsSnapshot:
    raw: str
    ram: dict[str, Any] | None = None
    swap: dict[str, Any] | None = None
    gpu: dict[str, Any] | None = None
    temperatures: dict[str, float] = field(default_factory=dict)
    power_rails: dict[str, dict[str, int]] = field(default_factory=dict)


RAM_RE = re.compile(r"RAM\s+(?P<used>\d+)/(?P<total>\d+)MB")
SWAP_RE = re.compile(r"SWAP\s+(?P<used>\d+)/(?P<total>\d+)MB(?:\s+\(cached\s+(?P<cached>\d+)MB\))?")
GR3D_RE = re.compile(r"GR3D_FREQ\s+(?P<load>\d+)%@(?P<freq>\d+)")
TEMP_RE = re.compile(r"(?P<name>[A-Za-z0-9_]+)@(?P<temp>-?\d+(?:\.\d+)?)C")
POWER_RE = re.compile(
    r"(?P<name>[A-Z][A-Z0-9_]+)\s+"
    r"(?P<instant>\d+)(?:mW)?/(?P<average>\d+)(?:mW)?"
)


def parse_tegrastats(output: str) -> TegrastatsSnapshot:
    line = output.strip().splitlines()[0] if output.strip() else ""
    return TegrastatsSnapshot(
        raw=line,
        ram=_parse_ram(line),
        swap=_parse_swap(line),
        gpu=_parse_gpu(line),
        temperatures=_parse_temperatures(line),
        power_rails=_parse_power_rails(line),
    )


def _parse_ram(line: str) -> dict[str, Any] | None:
    match = RAM_RE.search(line)
    if not match:
        return None
    used = int(match.group("used"))
    total = int(match.group("total"))
    return {
        "used_mb": used,
        "total_mb": total,
        "available_mb": max(total - used, 0),
        "used_percent": _percent(used, total),
    }


def _parse_swap(line: str) -> dict[str, Any] | None:
    match = SWAP_RE.search(line)
    if not match:
        return None
    used = int(match.group("used"))
    total = int(match.group("total"))
    cached = match.group("cached")
    return {
        "used_mb": used,
        "total_mb": total,
        "cached_mb": int(cached) if cached is not None else None,
        "used_percent": _percent(used, total),
    }


def _parse_gpu(line: str) -> dict[str, Any] | None:
    match = GR3D_RE.search(line)
    if not match:
        return None
    return {
        "gr3d_load_percent": int(match.group("load")),
        "gr3d_frequency_mhz": int(match.group("freq")),
    }


def _parse_temperatures(line: str) -> dict[str, float]:
    return {match.group("name"): float(match.group("temp")) for match in TEMP_RE.finditer(line)}


def _parse_power_rails(line: str) -> dict[str, dict[str, int]]:
    rails: dict[str, dict[str, int]] = {}
    for match in POWER_RE.finditer(line):
        name = match.group("name")
        if name in {"RAM", "SWAP"}:
            continue
        rails[name] = {
            "instant_mw": int(match.group("instant")),
            "average_mw": int(match.group("average")),
        }
    return rails


def _percent(used: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round((used / total) * 100, 2)
