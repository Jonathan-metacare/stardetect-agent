import pytest

from stardetect_agent.system.ixsmi import parse_ixsmi_csv

REAL_MR_V100_SAMPLE = (
    "GPU-2288a720-d353-57bb-9737-23898011931b, Iluvatar MR-V100, "
    "32768, 17586, 15182, 57, 0, 54"
)


def test_parse_real_mr_v100_csv() -> None:
    devices = parse_ixsmi_csv(REAL_MR_V100_SAMPLE)

    assert len(devices) == 1
    assert devices[0].to_dict() == {
        "uuid": "GPU-2288a720-d353-57bb-9737-23898011931b",
        "name": "Iluvatar MR-V100",
        "memory_total_mb": 32768,
        "memory_used_mb": 17586,
        "memory_free_mb": 15182,
        "temperature_c": 57.0,
        "gpu_utilization_percent": 0.0,
        "memory_utilization_percent": 54.0,
    }


def test_parse_multiple_gpus_and_na_values() -> None:
    output = "\n".join(
        [
            "GPU-one, Iluvatar MR-V100, 32768, 100, 32668, 45, 10, 1",
            "GPU-two, Iluvatar MR-V100, 32768, N/A, 30000, N/A, 0.5, N/A",
        ]
    )

    devices = parse_ixsmi_csv(output)

    assert len(devices) == 2
    assert devices[1].memory_used_mb is None
    assert devices[1].temperature_c is None
    assert devices[1].gpu_utilization_percent == 0.5
    assert devices[1].memory_utilization_percent is None


@pytest.mark.parametrize(
    "output",
    [
        "GPU-one, Iluvatar MR-V100, 32768",
        "GPU-one, Iluvatar MR-V100, bad, 100, 32668, 45, 10, 1",
    ],
)
def test_parse_rejects_malformed_rows(output: str) -> None:
    with pytest.raises(ValueError):
        parse_ixsmi_csv(output)


def test_parse_ignores_blank_lines() -> None:
    assert parse_ixsmi_csv("\n  \n") == []
