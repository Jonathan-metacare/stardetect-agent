# Stardetect Iluvatar Agent

FastAPI + LangChain operations agent for an openEuler/Phytium system with an
Iluvatar MR-V100 GPU. The agent calls a Qwen model through a vLLM
OpenAI-compatible API and reads GPU telemetry from `ixsmi`.

## Runtime

- Host: openEuler 24.03 on AArch64 / Phytium S5000C
- GPU: Iluvatar MR-V100 with CoreX 4.4.0
- Python: 3.10
- LLM: Qwen3-4B served as `qwen3` by vLLM on port 8000
- Agent: separate container on port 8001

## Local development

```bash
python3.10 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
pytest
ruff check .
```

Run the API:

```bash
stardetect-agent
```

## API

```bash
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8001/api/tools
curl http://127.0.0.1:8001/api/telemetry/snapshot
curl http://127.0.0.1:8001/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"当前 GPU 温度和显存是多少？"}'
```

CPU, memory, and storage values have `scope: container`. GPU values from
`ixsmi` have `scope: device`. CoreX 4.4.0 rejects the tested `power.draw`
query, so power is explicitly unavailable and is never estimated.

The service samples `ixsmi` every 0.2 seconds in the background and retains a
rolling 5-second window. Each GPU includes `utilization_window` with `current`,
`average`, and `max` values for GPU utilization, memory utilization, and
temperature. The instantaneous fields remain for API compatibility.

## Configuration

- `LLM_BASE_URL`: default `http://llm:8000/v1`
- `LLM_MODEL`: default `qwen3`
- `LLM_API_KEY`: default `dummy`; vLLM does not require authentication here
- `IXSMI_PATH`: default `/usr/local/corex-4.4.0/bin/ixsmi`
- `IXSMI_TIMEOUT_SECONDS`: default `5`
- `GPU_SAMPLING_INTERVAL_SECONDS`: default `0.2`
- `GPU_SAMPLING_WINDOW_SECONDS`: default `5`
- `AGENT_PORT`: default `8001`

See [Agent.md](Agent.md) for the private-base-image, local Buildx,
registry-push, server-pull, and validation runbook. The server does not need the
source repository.
