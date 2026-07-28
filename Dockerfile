ARG COREX_BASE_IMAGE=installer:4.4.0-ubuntu-20.04-py3.10-llm-aarch64-10.2-full
FROM ${COREX_BASE_IMAGE}

WORKDIR /opt/stardetect-agent

COPY pyproject.toml README.md ./
COPY src ./src

RUN python3 -m pip install --no-cache-dir .

ENV LLM_BASE_URL=http://llm:8000/v1 \
    LLM_MODEL=qwen3 \
    LLM_API_KEY=dummy \
    IXSMI_PATH=/usr/local/corex-4.4.0/bin/ixsmi \
    IXSMI_TIMEOUT_SECONDS=5 \
    GPU_SAMPLING_INTERVAL_SECONDS=0.2 \
    GPU_SAMPLING_WINDOW_SECONDS=5 \
    AGENT_PORT=8001

EXPOSE 8001

CMD ["stardetect-agent"]
