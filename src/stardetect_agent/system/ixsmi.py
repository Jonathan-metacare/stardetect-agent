import csv
from dataclasses import asdict, dataclass
from io import StringIO
from typing import Any

IXSMI_QUERY_FIELDS = (
    "uuid",
    "name",
    "memory.total",
    "memory.used",
    "memory.free",
    "temperature.gpu",
    "utilization.gpu",
    "utilization.memory",
)


@dataclass(frozen=True)
class IxsmiDevice:
    uuid: str | None
    name: str | None
    memory_total_mb: int | None
    memory_used_mb: int | None
    memory_free_mb: int | None
    temperature_c: float | None
    gpu_utilization_percent: float | None
    memory_utilization_percent: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_ixsmi_csv(output: str) -> list[IxsmiDevice]:
    """Parse headerless, unitless ixsmi CSV output."""
    devices: list[IxsmiDevice] = []
    for line_number, row in enumerate(csv.reader(StringIO(output), skipinitialspace=True), start=1):
        if not row or not any(value.strip() for value in row):
            continue
        if len(row) != len(IXSMI_QUERY_FIELDS):
            raise ValueError(
                f"ixsmi row {line_number} has {len(row)} fields; "
                f"expected {len(IXSMI_QUERY_FIELDS)}"
            )
        values = [value.strip() for value in row]
        devices.append(
            IxsmiDevice(
                uuid=_optional_text(values[0]),
                name=_optional_text(values[1]),
                memory_total_mb=_optional_int(values[2]),
                memory_used_mb=_optional_int(values[3]),
                memory_free_mb=_optional_int(values[4]),
                temperature_c=_optional_float(values[5]),
                gpu_utilization_percent=_optional_float(values[6]),
                memory_utilization_percent=_optional_float(values[7]),
            )
        )
    return devices


def _optional_text(value: str) -> str | None:
    return None if _is_unavailable(value) else value


def _optional_int(value: str) -> int | None:
    if _is_unavailable(value):
        return None
    try:
        return int(float(value))
    except ValueError as exc:
        raise ValueError(f"invalid numeric ixsmi value: {value!r}") from exc


def _optional_float(value: str) -> float | None:
    if _is_unavailable(value):
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"invalid numeric ixsmi value: {value!r}") from exc


def _is_unavailable(value: str) -> bool:
    return value.strip().upper() in {"", "N/A", "NA", "NOT SUPPORTED"}
