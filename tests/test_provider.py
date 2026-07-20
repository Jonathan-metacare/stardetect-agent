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


def test_power_status_reads_orin_ina3221_hwmon_nodes(tmp_path) -> None:
    hwmon = tmp_path / "sys" / "bus" / "i2c" / "drivers" / "ina3221" / "1-0040" / "hwmon" / "hwmon0"
    hwmon.mkdir(parents=True)
    (hwmon / "in1_label").write_text("VDD_GPU_SOC\n", encoding="utf-8")
    (hwmon / "in1_input").write_text("19500\n", encoding="utf-8")
    (hwmon / "curr1_input").write_text("120\n", encoding="utf-8")
    (hwmon / "in2_label").write_text("VDD_CPU_CV\n", encoding="utf-8")
    (hwmon / "in2_input").write_text("19500\n", encoding="utf-8")
    (hwmon / "curr2_input").write_text("80\n", encoding="utf-8")

    provider = JetsonMetricsProvider(
        ina3221_root=tmp_path / "sys" / "bus" / "i2c" / "drivers" / "ina3221",
        hwmon_root=tmp_path / "missing-hwmon",
        ina3221x_root=tmp_path / "missing-ina3221x",
    )

    status = provider.get_power_status()

    assert status["available"] is True
    assert status["rails"]["VDD_GPU_SOC"]["instant_mw"] == 2340
    assert status["rails"]["VDD_GPU_SOC"]["instant_w"] == 2.34
    assert status["rails"]["VDD_GPU_SOC"]["voltage_mv"] == 19500
    assert status["rails"]["VDD_GPU_SOC"]["voltage_v"] == 19.5
    assert status["rails"]["VDD_GPU_SOC"]["current_ma"] == 120
    assert status["rails"]["VDD_GPU_SOC"]["current_a"] == 0.12
    assert status["rails"]["VDD_CPU_CV"]["instant_mw"] == 1560
    assert status["total_instant_mw"] == 3900
    assert status["total_instant_w"] == 3.9
    assert "Total readable rail power: 3.9 W" in status["summary"]


def test_power_status_reads_i2c_devices_hwmon_nodes(tmp_path) -> None:
    hwmon = tmp_path / "sys" / "bus" / "i2c" / "devices" / "1-0040" / "hwmon" / "hwmon2"
    hwmon.mkdir(parents=True)
    (hwmon / "in3_label").write_text("VIN_SYS_5V0\n", encoding="utf-8")
    (hwmon / "in3_input").write_text("5000\n", encoding="utf-8")
    (hwmon / "curr3_input").write_text("620\n", encoding="utf-8")

    provider = JetsonMetricsProvider(
        hwmon_root=tmp_path / "missing-hwmon",
        ina3221_root=tmp_path / "missing-ina3221",
        ina3221x_root=tmp_path / "missing-ina3221x",
        i2c_devices_root=tmp_path / "sys" / "bus" / "i2c" / "devices",
    )

    status = provider.get_power_status()

    assert status["available"] is True
    assert status["rails"]["VIN_SYS_5V0"]["instant_mw"] == 3100
    assert status["rails"]["VIN_SYS_5V0"]["instant_w"] == 3.1


def test_power_status_reads_platform_hwmon_and_skips_non_rails(tmp_path) -> None:
    hwmon3 = (
        tmp_path
        / "sys"
        / "devices"
        / "platform"
        / "c240000.i2c"
        / "i2c-1"
        / "1-0040"
        / "hwmon"
        / "hwmon3"
    )
    hwmon3.mkdir(parents=True)
    (hwmon3 / "name").write_text("ina3221\n", encoding="utf-8")
    (hwmon3 / "in1_label").write_text("VDD_GPU_SOC\n", encoding="utf-8")
    (hwmon3 / "in1_input").write_text("12008\n", encoding="utf-8")
    (hwmon3 / "curr1_input").write_text("280\n", encoding="utf-8")
    (hwmon3 / "in2_label").write_text("VDD_CPU_CV\n", encoding="utf-8")
    (hwmon3 / "in2_input").write_text("12008\n", encoding="utf-8")
    (hwmon3 / "curr2_input").write_text("40\n", encoding="utf-8")
    (hwmon3 / "in3_label").write_text("VIN_SYS_5V0\n", encoding="utf-8")
    (hwmon3 / "in3_input").write_text("4968\n", encoding="utf-8")
    (hwmon3 / "curr3_input").write_text("980\n", encoding="utf-8")
    (hwmon3 / "in7_label").write_text("sum of shunt voltages\n", encoding="utf-8")
    (hwmon3 / "in7_input").write_text("760\n", encoding="utf-8")
    (hwmon3 / "curr4_input").write_text("360\n", encoding="utf-8")

    hwmon4 = (
        tmp_path
        / "sys"
        / "devices"
        / "platform"
        / "c240000.i2c"
        / "i2c-1"
        / "1-0041"
        / "hwmon"
        / "hwmon4"
    )
    hwmon4.mkdir(parents=True)
    (hwmon4 / "in1_label").write_text("NC\n", encoding="utf-8")
    (hwmon4 / "in1_input").write_text("0\n", encoding="utf-8")
    (hwmon4 / "curr1_input").write_text("0\n", encoding="utf-8")
    (hwmon4 / "in2_label").write_text("VDDQ_VDD2_1V8AO\n", encoding="utf-8")
    (hwmon4 / "in2_input").write_text("4968\n", encoding="utf-8")
    (hwmon4 / "curr2_input").write_text("80\n", encoding="utf-8")

    provider = JetsonMetricsProvider(
        hwmon_root=tmp_path / "missing-hwmon",
        ina3221_root=tmp_path / "missing-ina3221",
        ina3221x_root=tmp_path / "missing-ina3221x",
        i2c_devices_root=tmp_path / "missing-i2c-devices",
        platform_devices_root=tmp_path / "sys" / "devices" / "platform",
    )

    status = provider.get_power_status()

    assert status["available"] is True
    assert status["rails"]["VDD_GPU_SOC"]["instant_mw"] == 3362
    assert status["rails"]["VDD_GPU_SOC"]["instant_w"] == 3.362
    assert status["rails"]["VDD_CPU_CV"]["instant_mw"] == 480
    assert status["rails"]["VIN_SYS_5V0"]["instant_mw"] == 4869
    assert status["rails"]["VDDQ_VDD2_1V8AO"]["instant_mw"] == 397
    assert status["total_instant_mw"] == 9108
    assert status["total_instant_w"] == 9.108
    assert "NC" not in status["rails"]
    assert "sum of shunt voltages" not in status["rails"]


def test_power_status_reads_legacy_ina3221x_power_nodes(tmp_path) -> None:
    iio = tmp_path / "sys" / "bus" / "i2c" / "drivers" / "ina3221x" / "0-0041" / "iio:device1"
    iio.mkdir(parents=True)
    (iio / "rail_name_0").write_text("VDD_IN\n", encoding="utf-8")
    (iio / "in_power0_input").write_text("4321\n", encoding="utf-8")
    (iio / "in_voltage0_input").write_text("5000\n", encoding="utf-8")
    (iio / "in_current0_input").write_text("864\n", encoding="utf-8")

    provider = JetsonMetricsProvider(
        hwmon_root=tmp_path / "missing-hwmon",
        ina3221_root=tmp_path / "missing-ina3221",
        ina3221x_root=tmp_path / "sys" / "bus" / "i2c" / "drivers" / "ina3221x",
    )

    status = provider.get_power_status()

    assert status["available"] is True
    assert status["rails"]["VDD_IN"] == {
        "instant_mw": 4321,
        "instant_w": 4.321,
        "voltage_mv": 5000,
        "voltage_v": 5.0,
        "current_ma": 864,
        "current_a": 0.864,
        "source": str(iio),
    }
