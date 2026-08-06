# SpaceZenith 星载 APP_1

`app_1` 是供 AI 单机平台启动的 C++17 历史任务 APP。它只调用本机已经运行的
Stardetect Agent，不访问 GPU 设备；它通过平台提供的 Unix domain socket 上报任务遥测。

## 指令模式

平台必须按其接口约定传入十个参数（`argc == 10`）。本 APP 使用：

| `argv[1]` | 任务 | `argv[2]` 预置素材编号 | 输出 |
| --- | --- | --- | --- |
| `1` | 图像识别 | `1` 对应 `raw/image1.jpg`；后续可增加 `image2.jpg` 等 JPEG/PNG | `argv[6]`：UTF-8 JSON |
| `2` | 文字 prompt 回复 | `1` 对应 `raw/prompt1.txt`；`2` 对应 `raw/prompt2.txt` | `argv[6]`：UTF-8 JSON |

APP 只读取 `argv[3]` 对应 APP 工作目录的上级 `raw/` 目录，绝不读取平台传入的
`argv[4]`、`argv[5]` 输入通道。`argv[7]` 在本版保留但不使用。`argv[8]` 是平台
Unix domain socket server 的路径；`argv[9]` 必须是 `0` 到 `255` 的十进制设备码。收到
`SIGTERM` 时程序会停止当前处理、尝试写入取消结果、发送取消遥测并退出；不会创建子进程。

## 平台遥测

平台软件必须先创建 `argv[8]` 指向的 Unix domain socket server，APP_1 作为 client
连接。遥测链路异常不会中断图像识别或文字任务，APP 会将告警写到标准输出，并在下一个
周期尝试重连。

APP 发送 1 字节对齐、固定 1066 字节的 `InnerTeleFrame`：`source_device` 为 `argv[9]`，
`cmd` 固定为 `0x00`，`length` 以小端序写入 `1`，`data[0]` 为状态码，其余 `data` 字节为
零。运行中每 500ms 上报一次，并在退出前额外上报一次终态：

| `data[0]` | 含义 |
| --- | --- |
| `0x00` | 运行中 |
| `0x01` | 成功 |
| `0x02` | 失败 |
| `0x03` | 因 `SIGTERM` 取消 |

每帧均完整发送固定结构体；有效数据长度为 1 字节，因此符合平台对遥测有效数据不超过
60 字节的限制。

## 构建与安装

在目标 AArch64/openEuler 主机原生构建：

```bash
cd spacezenith
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
cmake --install build --prefix "$PWD"
```

安装后可执行文件为 `app_1/bin/app_1`。平台应将整个 `spacezenith` 目录作为
算法根目录部署；空目录以 `.gitkeep` 保留，平台会负责清理 `app_1/tmp/`。

## Agent 配置

编辑 `app_1/lib/app_1.conf`：

```ini
agent_base_url=http://127.0.0.1:8001
docker_path=docker
llm_container=llm-qwen3-vl
agent_container=stardetect-agent
llm_health_url=http://127.0.0.1:8003/v1/models
agent_health_url=http://127.0.0.1:8001/health
backend_start_timeout_ms=180000
backend_poll_interval_ms=1000
connect_timeout_ms=3000
request_timeout_ms=60000
max_image_bytes=8388608
max_prompt_bytes=65536
```

只支持 `http://` 地址。星载 APP 与 Agent 默认处于同一主机网络命名空间；若部署
位置不同，修改 `agent_base_url` 后无需重新编译。

## 后端容器生命周期

每次任务开始时，APP 会依次执行以下操作：

1. `docker inspect` 检查 `llm_container`；若它原先停止，执行 `docker start`，并等待
   `llm_health_url` 返回 HTTP 2xx。
2. 对 `agent_container` 执行相同操作，等待 `agent_health_url` 返回 HTTP 2xx。
3. 调用 Agent 完成图像或文字任务。
4. 任务成功、失败或收到 `SIGTERM` 后，按 Agent、LLM 的顺序停止**本次 APP 启动的**容器。

若容器在任务开始前已经运行，APP 不会停止它。默认配置对应当前部署的
`llm-qwen3-vl`（主机端口 8003）和 `stardetect-agent`（主机端口 8001）。

运行 APP 的平台用户必须有执行 Docker CLI 的权限，通常要求为 `root` 或已加入 Docker
socket 对应用户组；否则结果 JSON 会返回 `backend_docker_failed`。

## 结果格式

成功和失败都会尽力覆盖写入 `argv[6]`。成功例：

```json
{
  "mode": "text_prompt",
  "input": {"preset_id": 2, "path": "/opt/spacezenith/raw/prompt2.txt"},
  "answer": "当前 GPU 温度为 59°C。",
  "tool_calls": [{"name": "get_gpu_status", "args": {}, "id": "..."}],
  "timestamp": "2026-08-05T12:00:00Z"
}
```

文字模式不会在 APP 中硬编码任何 prompt 或工具规则；是否调用工具完全由所选
`raw/promptN.txt` 的内容和 Agent 决定。返回的 `tool_calls` 会原样保留在结果 JSON 中。

## 本地验证

```bash
python3 tests/test_app.py
```

测试会临时编译 APP，并以本地 mock Agent、mock Docker 和 Unix socket server 覆盖图像
JPEG/PNG、预置 prompt、Docker 启停及既有容器保护、遥测帧、参数错误、HTTP 错误、超时和
SIGTERM 收尾行为。
