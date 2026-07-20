from stardetect_agent.jetson.tegrastats import parse_tegrastats

SAMPLE = (
    "RAM 1545/31919MB (lfb 7400x4MB) SWAP 2/15959MB (cached 1MB) "
    "CPU [0%@1190,1%@1190] EMC_FREQ 1%@408 GR3D_FREQ 12%@318 "
    "AO@38C GPU@39.5C CPU@40C thermal@38.8C GPU 120/100 CPU 468/450 "
    "SOC 937/900 CV 0/0 VDDRQ 312/234 SYS5V 1458/1400"
)


def test_parse_tegrastats_memory_gpu_temperature_and_power() -> None:
    snapshot = parse_tegrastats(SAMPLE)

    assert snapshot.ram == {
        "used_mb": 1545,
        "total_mb": 31919,
        "available_mb": 30374,
        "used_percent": 4.84,
    }
    assert snapshot.swap == {
        "used_mb": 2,
        "total_mb": 15959,
        "cached_mb": 1,
        "used_percent": 0.01,
    }
    assert snapshot.gpu == {"gr3d_load_percent": 12, "gr3d_frequency_mhz": 318}
    assert snapshot.temperatures["GPU"] == 39.5
    assert snapshot.temperatures["CPU"] == 40.0
    assert snapshot.power_rails["GPU"] == {"instant_mw": 120, "average_mw": 100}
    assert snapshot.power_rails["SYS5V"] == {"instant_mw": 1458, "average_mw": 1400}


def test_parse_tegrastats_handles_empty_output() -> None:
    snapshot = parse_tegrastats("")

    assert snapshot.raw == ""
    assert snapshot.ram is None
    assert snapshot.swap is None
    assert snapshot.gpu is None
    assert snapshot.temperatures == {}
    assert snapshot.power_rails == {}

