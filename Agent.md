# Stardetect Agent Deployment Runbook

This document explains how to package this project, send it to a Jetson Orin,
run it on the Orin, and verify the FastAPI -> LangChain -> Ollama -> Jetson
tools chain.

## 1. What This Agent Runs

Target device:

- Jetson Orin
- Ubuntu 20.04
- JetPack 5.x / L4T R35.3.1
- ARM64
- Python 3.11.15
- Ollama model: `qwen3.5:4b`

Service chain:

```text
HTTP API -> FastAPI -> LangChain agent -> Ollama qwen3.5:4b -> Jetson tools
```

Main endpoints:

- `GET /health`
- `GET /api/tools`
- `GET /api/telemetry/snapshot`
- `POST /api/chat`

## 2. Package on Your Development Machine

From the project root:

```bash
cd /Users/baibing/development/code/ai/stardetect-agent
```

Create a source package for transfer:

```bash
mkdir -p dist
tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='dist' \
  -czf dist/stardetect-agent-src.tar.gz .
```

Check the package:

```bash
ls -lh dist/stardetect-agent-src.tar.gz
tar -tzf dist/stardetect-agent-src.tar.gz | head
```

Alternative if the Orin is reachable over SSH and you prefer direct sync:

```bash
rsync -av \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  ./ orin-user@ORIN_IP:/home/orin-user/stardetect-agent/
```

Replace `orin-user` and `ORIN_IP` with your real SSH user and Jetson IP.

## 3. Send the Package to Jetson Orin

Copy the tarball:

```bash
scp dist/stardetect-agent-src.tar.gz orin-user@ORIN_IP:/home/orin-user/
```

On the Orin:

```bash
ssh orin-user@ORIN_IP
mkdir -p ~/stardetect-agent
tar -xzf ~/stardetect-agent-src.tar.gz -C ~/stardetect-agent
cd ~/stardetect-agent
```

## 4. Prepare Orin Runtime

Confirm Python:

```bash
python3.11 --version
```

Expected:

```text
Python 3.11.15
```

Create and activate the virtual environment:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
```

Install the agent:

```bash
pip install -e .
```

For development checks on Orin, install dev dependencies too:

```bash
pip install -e ".[dev]"
```

Create local configuration:

```bash
cp .env.example .env
```

Default `.env` values:

```bash
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:4b
```

## 5. Verify Jetson and Ollama Prerequisites

Check Ollama:

```bash
ollama list | grep qwen3.5:4b
curl http://127.0.0.1:11434/api/tags
```

If the model is missing:

```bash
ollama pull qwen3.5:4b
```

Check Jetson telemetry command:

```bash
which tegrastats
tegrastats --interval 1000
```

Stop `tegrastats` with `Ctrl+C` after one or two lines.

The API can still start if `tegrastats` is unavailable, but telemetry fields will
report unavailable or partial data.

## 6. Run the API

Start the service:

```bash
. .venv/bin/activate
uvicorn stardetect_agent.api.main:app --host 0.0.0.0 --port 8000
```

Equivalent command:

```bash
stardetect-agent
```

Keep this terminal open while testing.

## 7. Verify Locally on Orin

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Expected shape:

```json
{
  "status": "ok",
  "ollama_model": "qwen3.5:4b",
  "ollama_base_url": "http://127.0.0.1:11434"
}
```

List registered tools:

```bash
curl http://127.0.0.1:8000/api/tools
```

Expected tool names:

- `get_system_snapshot`
- `get_memory_status`
- `get_storage_status`
- `get_temperature_status`
- `get_power_status`

Read telemetry directly, without the LLM:

```bash
curl http://127.0.0.1:8000/api/telemetry/snapshot
```

Ask the agent to use Jetson tools:

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"当前 GPU 温度、内存、存储和功耗是多少？"}'
```

Successful response shape:

```json
{
  "answer": "...",
  "tool_calls": [
    {
      "name": "get_system_snapshot",
      "args": {},
      "id": "..."
    }
  ]
}
```

## 8. Telemetry Sources and Calculations

The agent exposes telemetry through LangChain tools, but the values are read from
local Jetson/Linux sources. The main aggregation endpoint is:

```bash
curl http://127.0.0.1:8000/api/telemetry/snapshot
```

### Temperature

Primary source:

```bash
tegrastats --interval 1000
```

The parser reads fields like:

```text
GPU@45.531C CPU@51.25C Tboard@40C
```

It extracts `name@valueC` and returns the value in Celsius.

Fallback/additional source:

```text
/sys/class/thermal/thermal_zone*/type
/sys/class/thermal/thermal_zone*/temp
```

The sysfs temperature value is usually in millicelsius:

```text
celsius = temp / 1000
```

### Memory

System RAM and swap source:

```python
psutil.virtual_memory()
psutil.swap_memory()
```

This is Linux system memory information exposed through `psutil`.

The provider also parses `tegrastats` memory fields:

```text
RAM 18968/62795MB
SWAP 0/31398MB (cached 0MB)
```

Calculations:

```text
available_mb = total_mb - used_mb
used_percent = used_mb / total_mb * 100
```

GPU/GR3D status is parsed from `tegrastats` when present:

```text
GR3D_FREQ 0%@611
```

### Storage

Storage source:

```python
shutil.disk_usage("/")
```

This reads the root filesystem usage. The provider returns:

```text
total_gb
used_gb
free_gb
used_percent
```

Calculations:

```text
gb = bytes / 1024 / 1024 / 1024
used_percent = used / total * 100
```

### Power

First, the provider tries to parse power rails from `tegrastats` if they exist:

```text
VDD_GPU_SOC 716mW/716mW
VIN_SYS_5V0 2772mW/2772mW
```

On the tested Jetson Orin, `tegrastats` did not expose power rails, so the
provider reads INA3221 sysfs nodes instead. It scans:

```text
/sys/class/hwmon/hwmon*
/sys/bus/i2c/drivers/ina3221/**/hwmon*
/sys/bus/i2c/drivers/ina3221x/**/iio:device*
/sys/bus/i2c/devices/**/hwmon*
/sys/devices/platform/**/hwmon*
```

The confirmed Orin paths were:

```text
/sys/devices/platform/c240000.i2c/i2c-1/1-0040/hwmon/hwmon3
/sys/devices/platform/c240000.i2c/i2c-1/1-0041/hwmon/hwmon4
```

For each power rail, the provider reads:

```text
inN_label   -> rail name, for example VDD_GPU_SOC
inN_input   -> voltage in mV
currN_input -> current in mA
```

Power calculation:

```text
instant_mw = voltage_mv * current_ma / 1000
instant_w = instant_mw / 1000
total_instant_w = sum(valid rail instant_w)
```

The tested Orin exposes one special channel mapping:

```text
in7_label / in7_input -> curr4_input
```

The provider skips non-rail labels:

```text
NC
sum of shunt voltages
```

If ordinary users cannot read the INA3221 files, run the service with sufficient
permissions during validation:

```bash
sudo -E .venv/bin/uvicorn stardetect_agent.api.main:app --host 0.0.0.0 --port 8000
```

## 9. Verify from Another Machine

From your laptop or another machine on the same network:

```bash
curl http://ORIN_IP:8000/health
```

If this fails but local Orin checks pass, inspect network/firewall settings and
confirm the API was started with `--host 0.0.0.0`.

## 10. Run Tests on Orin

Install dev dependencies first:

```bash
pip install -e ".[dev]"
```

Run checks:

```bash
pytest
ruff check .
```

Expected:

```text
13 passed
All checks passed!
```

## 11. Optional Background Run

For a simple background run during development:

```bash
nohup .venv/bin/uvicorn stardetect_agent.api.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  > stardetect-agent.log 2>&1 &
```

Check logs:

```bash
tail -f stardetect-agent.log
```

Stop it:

```bash
pkill -f 'uvicorn stardetect_agent.api.main:app'
```

For production, prefer a `systemd` service that runs the venv executable from
the project directory.

## 12. Troubleshooting

If `/api/chat` fails:

- Confirm Ollama is running: `curl http://127.0.0.1:11434/api/tags`
- Confirm the model exists: `ollama list | grep qwen3.5:4b`
- Confirm `.env` has the right `OLLAMA_BASE_URL` and `OLLAMA_MODEL`

If telemetry is empty or partial:

- Run `tegrastats --interval 1000` manually
- Confirm the process user can read `/sys/class/thermal`
- For Orin power rails, check INA3221 sysfs nodes:

```bash
find /sys/bus/i2c/drivers/ina3221 -maxdepth 4 -type f \
  \( -name 'in*_label' -o -name 'in*_input' -o -name 'curr*_input' \) \
  -print 2>/dev/null
```

- Typical Orin rails include `VDD_GPU_SOC`, `VDD_CPU_CV`, and `VIN_SYS_5V0`
- Use `/api/telemetry/snapshot` to separate Jetson tool issues from LLM issues

If remote access fails:

- Confirm `uvicorn` was started with `--host 0.0.0.0`
- Test locally first: `curl http://127.0.0.1:8000/health`
- Check the Jetson IP address and firewall rules
