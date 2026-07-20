# Stardetect Jetson Agent

FastAPI + LangChain agent for Jetson Orin telemetry. The service exposes a local
HTTP API backed by Ollama `qwen3.5:4b` and Jetson hardware tools.

## Target Runtime

- Jetson Orin
- Ubuntu 20.04
- JetPack 5.x / L4T R35.3.1
- ARM64
- Python 3.11.15
- Ollama model: `qwen3.5:4b`

## Setup on Orin

```bash
python3.11 --version
python3.11 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
```

Confirm Ollama and the model are ready:

```bash
ollama list | grep qwen3.5:4b
curl http://127.0.0.1:11434/api/tags
```

## Run

```bash
uvicorn stardetect_agent.api.main:app --host 0.0.0.0 --port 8000
```

or:

```bash
stardetect-agent
```

## API

Health:

```bash
curl http://127.0.0.1:8000/health
```

Direct telemetry snapshot:

```bash
curl http://127.0.0.1:8000/api/telemetry/snapshot
```

List agent tools:

```bash
curl http://127.0.0.1:8000/api/tools
```

Ask the agent:

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"当前 GPU 温度、内存和功耗是多少？"}'
```

## Configuration

Environment variables:

- `OLLAMA_BASE_URL`: default `http://127.0.0.1:11434`
- `OLLAMA_MODEL`: default `qwen3.5:4b`

## Development

```bash
pytest
ruff check .
```

The Jetson telemetry layer is defensive: if `tegrastats` is missing, times out,
or returns a different format, individual fields report unavailable data instead
of crashing the API.

