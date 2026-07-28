# Build locally, deploy on openEuler

## Recommended for this server: upload source and build natively

Because the CoreX base image already exists on the AArch64 openEuler server,
the simplest deployment is to upload the source package and build it there.
This avoids publishing the CoreX image and avoids cross-compilation.

The source archive does not contain the CoreX image or model files. On the
server, confirm the existing base image first:

```bash
docker image inspect \
  installer:4.4.0-ubuntu-20.04-py3.10-llm-aarch64-10.2-full
```

Extract an uploaded archive into a versioned directory:

```bash
mkdir -p /opt/stardetect-agent-qwen3-20260728

tar -xzf /tmp/stardetect-agent-corex-4.4.0-qwen3-sampling-20260728.tar.gz \
  -C /opt/stardetect-agent-qwen3-20260728 \
  --strip-components=1

cd /opt/stardetect-agent-qwen3-20260728
```

After enabling vLLM tool calling and creating `stardetect-net` as documented
below, build and start the Agent:

```bash
docker build -t stardetect-agent:corex-4.4.0-sampling .

docker run -d \
  --name stardetect-agent \
  --restart unless-stopped \
  --network stardetect-net \
  --device /dev/iluvatar0:/dev/iluvatar0 \
  --publish 8001:8001 \
  --env LLM_BASE_URL=http://llm:8000/v1 \
  --env LLM_MODEL=qwen3 \
  --env LLM_API_KEY=dummy \
  --env IXSMI_PATH=/usr/local/corex-4.4.0/bin/ixsmi \
  --env IXSMI_TIMEOUT_SECONDS=5 \
  --env GPU_SAMPLING_INTERVAL_SECONDS=0.2 \
  --env GPU_SAMPLING_WINDOW_SECONDS=5 \
  --env AGENT_PORT=8001 \
  stardetect-agent:corex-4.4.0-sampling

docker ps --filter name=stardetect-agent
docker logs --tail=100 stardetect-agent
```

This path uses plain Docker because the target server does not have the Docker
Compose plugin. If an older `stardetect-agent` container exists, remove that
exact container before running the new one:

```bash
docker rm -f stardetect-agent
```

For this deployment path, ignore the private-registry and Buildx sections. They
remain documented as an alternative for future CI-based image distribution.

---

## Alternative: build locally and deploy through a registry

In this alternative workflow, the source repository is required only on the
local build machine. The openEuler server pulls the finished image and does not
build the project.

There are three distinct steps:

1. Once, publish the server-only CoreX base image to an authorized private registry.
2. On the local development machine, cross-build and push the Agent image.
3. On the openEuler server, enable vLLM tools, pull the Agent image, and run it.

Replace these examples with the actual private registry and namespace:

```text
<registry>/<namespace>/corex-installer:4.4.0-py3.10-aarch64
<registry>/<namespace>/stardetect-agent:corex-4.4.0
```

Do not publish the CoreX image to a public registry. Confirm that redistribution
to the selected private registry is permitted by its license.

## 1. Publish the CoreX base image once

Run this section on the openEuler server, where the base image already exists:

```bash
docker image inspect \
  installer:4.4.0-ubuntu-20.04-py3.10-llm-aarch64-10.2-full

docker login <registry>

docker tag \
  installer:4.4.0-ubuntu-20.04-py3.10-llm-aarch64-10.2-full \
  <registry>/<namespace>/corex-installer:4.4.0-py3.10-aarch64

docker push \
  <registry>/<namespace>/corex-installer:4.4.0-py3.10-aarch64
```

This is a one-time prerequisite. It is not repeated for each Agent release.

## 2. Cross-build and push the Agent locally

Run this section from the repository root on the local development machine.
Docker Buildx must be available, and the builder must be able to pull the private
CoreX base image and Python packages.

```bash
docker login <registry>

docker buildx create \
  --name stardetect-arm64-builder \
  --driver docker-container \
  --use

docker buildx inspect --bootstrap
```

If the builder already exists:

```bash
docker buildx use stardetect-arm64-builder
```

Build a Linux AArch64 image and push it directly to the registry:

```bash
docker buildx build \
  --platform linux/arm64 \
  --build-arg COREX_BASE_IMAGE=<registry>/<namespace>/corex-installer:4.4.0-py3.10-aarch64 \
  --tag <registry>/<namespace>/stardetect-agent:corex-4.4.0 \
  --push \
  .
```

`--push` is required: a `docker-container` Buildx builder does not automatically
place the result in the local Docker image store. On an Intel development
machine the build uses QEMU and is slower; on Apple Silicon, `linux/arm64`
matches the CPU architecture but still targets Linux rather than macOS.

Inspect the published architecture:

```bash
docker buildx imagetools inspect \
  <registry>/<namespace>/stardetect-agent:corex-4.4.0
```

The manifest must contain `linux/arm64`.

## 3. Enable Qwen tool calling on the server

No project files are needed for this step. The existing LLM container is
`0754f3408644`.

Stop only the old vLLM process:

```bash
docker exec 0754f3408644 \
  pkill -f '/usr/local/corex-4.4.0/lib64/python3/dist-packages/bin/vllm serve'
```

Start vLLM with Qwen3 Hermes tool parsing and the stable served model name
`qwen3`:

```bash
docker exec -d 0754f3408644 bash -lc \
  'vllm serve /models/Qwen3-4B \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name qwen3 \
    --max-model-len 2048 \
    --tensor-parallel-size 1 \
    --gpu-memory-utilization 0.5 \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --reasoning-parser qwen3 \
    >/nvme/vllm-tool-calls.log 2>&1'
```

Check readiness:

```bash
docker exec 0754f3408644 tail -f /nvme/vllm-tool-calls.log
```

Press `Ctrl+C` after startup completes; this stops only `tail`.

```bash
curl http://127.0.0.1:8000/v1/models
```

## 4. Create the shared network on the server

No project files are needed:

```bash
docker network inspect stardetect-net >/dev/null 2>&1 ||
  docker network create stardetect-net
```

Connect the running LLM container with the DNS alias `llm`:

```bash
docker network connect --alias llm stardetect-net 0754f3408644
```

If Docker reports that the endpoint already exists, inspect the network and
confirm both the container and alias:

```bash
docker network inspect stardetect-net
```

## 5. Pull and run the Agent on the server

The repository is not required. Log in and pull the cross-built image:

```bash
docker login <registry>
docker pull <registry>/<namespace>/stardetect-agent:corex-4.4.0
```

First deployment:

```bash
docker run -d \
  --name stardetect-agent \
  --restart unless-stopped \
  --network stardetect-net \
  --device /dev/iluvatar0:/dev/iluvatar0 \
  --publish 8001:8001 \
  --env LLM_BASE_URL=http://llm:8000/v1 \
  --env LLM_MODEL=qwen3 \
  --env LLM_API_KEY=dummy \
  --env IXSMI_PATH=/usr/local/corex-4.4.0/bin/ixsmi \
  --env IXSMI_TIMEOUT_SECONDS=5 \
  --env GPU_SAMPLING_INTERVAL_SECONDS=0.2 \
  --env GPU_SAMPLING_WINDOW_SECONDS=5 \
  --env AGENT_PORT=8001 \
  <registry>/<namespace>/stardetect-agent:corex-4.4.0
```

For an upgrade, pull the new tag, replace only the Agent container, and rerun
the command above:

```bash
docker rm -f stardetect-agent
```

Alternatively, copy only `compose.deploy.yaml` to the server, set
`AGENT_IMAGE`, and deploy:

```bash
export AGENT_IMAGE=<registry>/<namespace>/stardetect-agent:corex-4.4.0
docker compose -f compose.deploy.yaml up -d
```

## 6. Validate

Check the container and its connection to vLLM:

```bash
docker ps --filter name=stardetect-agent
docker logs --tail=100 stardetect-agent

docker exec stardetect-agent \
  curl -s http://llm:8000/v1/models
```

Check MR-V100 access:

```bash
docker exec stardetect-agent \
  /usr/local/corex-4.4.0/bin/ixsmi \
  --query-gpu=uuid,name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu,utilization.memory \
  --format=csv,nounits,noheader
```

Check the Agent APIs:

```bash
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8001/api/tools
curl http://127.0.0.1:8001/api/telemetry/snapshot
```

Test the Agent tool call:

```bash
curl http://127.0.0.1:8001/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"当前 GPU 温度、利用率和显存是多少？"}'
```

The response must contain `get_gpu_status` in `tool_calls`. A plain-text answer
without a tool call does not pass deployment validation.

The GPU response must also show `sampling.active: true` and a
`utilization_window` for each device. After about five seconds of uptime,
`sample_count` should be close to 25 with the default 0.2-second interval.

## Troubleshooting

### The local build cannot pull the CoreX base image

Confirm the base image was pushed to the private registry, the local machine is
logged in, and the registry repository name matches `COREX_BASE_IMAGE`.

### `exec format error`

The Agent was built for the wrong architecture. Rebuild with:

```text
--platform linux/arm64
```

Then confirm the registry manifest with `docker buildx imagetools inspect`.

### Agent cannot resolve `llm`

```bash
docker network inspect stardetect-net
```

Both containers must be present, and the LLM container must have alias `llm`.
If the LLM container is already attached without that alias, reconnect only
this additional network:

```bash
docker network disconnect stardetect-net llm-qwen-test
docker network connect --alias llm stardetect-net llm-qwen-test
```

The host port 8000 and the default bridge remain available while this additional
network attachment is replaced.

### `ixsmi_not_found` or no GPU

Confirm that the Agent uses the CoreX base image and maps the device:

```bash
docker inspect stardetect-agent --format '{{json .HostConfig.Devices}}'
docker exec stardetect-agent /usr/local/corex-4.4.0/bin/ixsmi -L
```

### Model answers without tool calls

```bash
docker exec 0754f3408644 ps -ef
```

The vLLM command must contain both `--enable-auto-tool-choice` and
`--tool-call-parser hermes`.
