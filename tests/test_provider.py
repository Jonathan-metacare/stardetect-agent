import subprocess

from stardetect_agent.jetson.provider import JetsonMetricsProvider


def test_provider_returns_none_when_tegrastats_missing() -> None:
    def missing_runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(command[0])

    provider = JetsonMetricsProvider(command_runner=missing_runner)

    assert provider.read_tegrastats() is None
    snapshot = provider.get_system_snapshot()
    assert snapshot["tegrastats"]["available"] is False
    assert snapshot["power"]["available"] is False


def test_storage_status_has_expected_schema() -> None:
    provider = JetsonMetricsProvider()

    status = provider.get_storage_status("/")

    assert status["available"] is True
    assert status["path"] == "/"
    assert status["total_gb"] > 0
    assert status["used_percent"] is not None

