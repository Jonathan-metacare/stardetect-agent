import subprocess
from threading import Event

import pytest

import stardetect_agent.system.provider as provider_module
from stardetect_agent.system.provider import SystemMetricsProvider

SAMPLE = (
    "GPU-2288a720-d353-57bb-9737-23898011931b, Iluvatar MR-V100, "
    "32768, 17586, 15182, 57, 0, 54\n"
)


def _completed(
    stdout: str = SAMPLE,
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ixsmi"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_gpu_status_runs_expected_query() -> None:
    observed: dict[str, object] = {}

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["timeout"] = timeout
        return _completed()

    provider = SystemMetricsProvider(
        ixsmi_path="/usr/local/corex-4.4.0/bin/ixsmi",
        timeout_seconds=4,
        command_runner=runner,
    )

    status = provider.get_gpu_status()

    assert status["available"] is True
    assert status["scope"] == "device"
    assert status["devices"][0]["name"] == "Iluvatar MR-V100"
    assert status["devices"][0]["memory_total_mb"] == 32768
    assert observed["timeout"] == 4
    assert observed["command"] == [
        "/usr/local/corex-4.4.0/bin/ixsmi",
        (
            "--query-gpu=uuid,name,memory.total,memory.used,memory.free,"
            "temperature.gpu,utilization.gpu,utilization.memory"
        ),
        "--format=csv,nounits,noheader",
    ]


def test_gpu_status_reports_current_average_and_peak_over_window() -> None:
    outputs = iter(
        [
            (
                "GPU-window, Iluvatar MR-V100, 32768, 17586, 15182, "
                "55, 0, 54\n"
            ),
            (
                "GPU-window, Iluvatar MR-V100, 32768, 17600, 15168, "
                "56, 80, 60\n"
            ),
            (
                "GPU-window, Iluvatar MR-V100, 32768, 17590, 15178, "
                "57, 20, 56\n"
            ),
        ]
    )

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return _completed(stdout=next(outputs))

    provider = SystemMetricsProvider(
        sampling_interval_seconds=0.5,
        sampling_window_seconds=5,
        command_runner=runner,
    )
    provider.sample_gpu()
    provider.sample_gpu()
    status = provider.sample_gpu()

    device = status["devices"][0]
    window = device["utilization_window"]
    assert device["gpu_utilization_percent"] == 20.0
    assert window["window_seconds"] == 5
    assert window["sample_count"] == 3
    assert window["gpu_utilization_percent"] == {
        "current": 20.0,
        "average": 33.33,
        "max": 80.0,
    }
    assert window["memory_utilization_percent"] == {
        "current": 56.0,
        "average": 56.67,
        "max": 60.0,
    }
    assert window["temperature_c"] == {
        "current": 57.0,
        "average": 56.0,
        "max": 57.0,
    }
    assert status["sampling"] == {
        "active": False,
        "interval_seconds": 0.5,
        "window_seconds": 5,
        "last_error": None,
    }


def test_background_sampler_collects_and_stops_cleanly() -> None:
    two_samples = Event()
    call_count = 0

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            two_samples.set()
        return _completed()

    provider = SystemMetricsProvider(
        timeout_seconds=0.1,
        sampling_interval_seconds=0.01,
        sampling_window_seconds=1,
        command_runner=runner,
    )
    provider.start_sampling()
    try:
        assert two_samples.wait(timeout=1)
        status = provider.get_gpu_status()
        assert status["sampling"]["active"] is True
        assert status["devices"][0]["utilization_window"]["sample_count"] >= 2
    finally:
        provider.stop_sampling()

    assert provider.get_gpu_status()["sampling"]["active"] is False


@pytest.mark.parametrize(
    ("exception", "reason"),
    [
        (FileNotFoundError("ixsmi"), "ixsmi_not_found"),
        (PermissionError("ixsmi"), "ixsmi_permission_denied"),
        (subprocess.TimeoutExpired(["ixsmi"], 5), "ixsmi_timeout"),
    ],
)
def test_gpu_status_handles_command_errors(exception: Exception, reason: str) -> None:
    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise exception

    status = SystemMetricsProvider(command_runner=runner).get_gpu_status()

    assert status["available"] is False
    assert status["scope"] == "device"
    assert status["reason"] == reason
    assert status["devices"] == []


def test_gpu_status_handles_nonzero_exit() -> None:
    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return _completed(stdout="", stderr="driver unavailable", returncode=2)

    status = SystemMetricsProvider(command_runner=runner).get_gpu_status()

    assert status["available"] is False
    assert status["reason"] == "ixsmi_failed"
    assert status["error"] == "driver unavailable"


def test_gpu_status_handles_invalid_and_empty_output() -> None:
    outputs = [
        ("too,few,fields", "ixsmi_invalid_output"),
        ("\n", "ixsmi_no_devices"),
    ]
    for output, expected_reason in outputs:
        provider = SystemMetricsProvider(
            command_runner=lambda command, timeout, value=output: _completed(stdout=value)
        )

        status = provider.get_gpu_status()

        assert status["available"] is False
        assert status["reason"] == expected_reason


def test_snapshot_labels_container_and_device_scopes() -> None:
    provider = SystemMetricsProvider(
        command_runner=lambda command, timeout: _completed(),
    )

    snapshot = provider.get_system_snapshot()

    assert snapshot["scope"] == "mixed"
    assert snapshot["cpu"]["scope"] == "container"
    assert snapshot["memory"]["scope"] == "container"
    assert snapshot["storage"]["scope"] == "container"
    assert snapshot["gpu"]["scope"] == "device"
    assert snapshot["power"]["scope"] == "device"
    assert snapshot["power"]["available"] is False
    assert snapshot["power"]["reason"] == "unsupported_by_ixsmi_query"


def test_storage_status_has_expected_schema() -> None:
    status = SystemMetricsProvider().get_storage_status("/")

    assert status["available"] is True
    assert status["scope"] == "container"
    assert status["path"] == "/"
    assert status["total_gb"] > 0
    assert status["used_percent"] is not None


def test_memory_status_degrades_when_swap_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provider_module.psutil,
        "swap_memory",
        lambda: (_ for _ in ()).throw(OSError("swap unavailable")),
    )

    status = SystemMetricsProvider().get_memory_status()

    assert status["available"] is True
    assert status["scope"] == "container"
    assert status["swap"] == {
        "available": False,
        "error": "swap unavailable",
    }
