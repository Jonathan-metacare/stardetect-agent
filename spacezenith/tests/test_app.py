#!/usr/bin/env python3
"""Standalone integration tests for the C++ APP and a local mock Stardetect Agent."""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TELEMETRY_FRAME_SIZE = 1066


class MockTelemetryServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.frames: list[bytes] = []
        self._condition = threading.Condition()
        self._stopping = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen()
        self._server.settimeout(0.1)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._read_connection, args=(connection,), daemon=True).start()

    def _read_connection(self, connection: socket.socket) -> None:
        pending = b""
        try:
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                pending += chunk
                while len(pending) >= TELEMETRY_FRAME_SIZE:
                    frame, pending = pending[:TELEMETRY_FRAME_SIZE], pending[TELEMETRY_FRAME_SIZE:]
                    with self._condition:
                        self.frames.append(frame)
                        self._condition.notify_all()
        finally:
            connection.close()

    def wait_for_status(self, status: int, count: int = 1, timeout: float = 3.0) -> list[bytes]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while sum(frame[6] == status for frame in self.frames) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"did not receive telemetry status {status:#x}: {self.frames!r}"
                    )
                self._condition.wait(remaining)
            return list(self.frames)

    def close(self) -> None:
        self._stopping.set()
        self._server.close()
        self._thread.join(timeout=1)
        if self.path.exists():
            self.path.unlink()


class MockAgentHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    response_status = 200
    response_body: dict[str, object] = {"answer": "ok", "tool_calls": []}
    response_delay_seconds = 0.0

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        self.__class__.requests.append(
            {
                "body": json.loads(self.rfile.read(length)),
                "task_id": self.headers.get("X-SpaceZenith-Task-ID"),
            }
        )
        if self.__class__.response_delay_seconds:
            time.sleep(self.__class__.response_delay_seconds)
        body = json.dumps(self.__class__.response_body).encode()
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self) -> None:  # noqa: N802
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class AppIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build_dir = Path(tempfile.mkdtemp(prefix="spacezenith-build-"))
        subprocess.run(["cmake", "-S", str(ROOT), "-B", str(cls.build_dir)], check=True)
        subprocess.run(["cmake", "--build", str(cls.build_dir)], check=True)
        cls.executable = cls.build_dir / "app_1"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockAgentHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        MockAgentHandler.requests = []
        MockAgentHandler.response_status = 200
        MockAgentHandler.response_body = {"answer": "ok", "tool_calls": []}
        MockAgentHandler.response_delay_seconds = 0.0
        self.task_dir = tempfile.TemporaryDirectory(prefix="spacezenith-task-")
        self.work_dir = Path(self.task_dir.name) / "app_1"
        self.raw_dir = Path(self.task_dir.name) / "raw"
        self.docker_state_dir = Path(self.task_dir.name) / "docker-state"
        self.docker_state_dir.mkdir()
        self.docker_log = Path(self.task_dir.name) / "docker.log"
        self.telemetry_path = Path(self.task_dir.name) / "telemetry.sock"
        self.telemetry = MockTelemetryServer(self.telemetry_path)
        self.fake_docker = Path(self.task_dir.name) / "docker"
        for container in ("llm-qwen3-vl", "stardetect-agent"):
            (self.docker_state_dir / container).write_text("false\n", encoding="utf-8")
        self.fake_docker.write_text(
            "#!/bin/sh\n"
            f"state_dir='{self.docker_state_dir}'\n"
            f"log_file='{self.docker_log}'\n"
            "printf '%s\\n' \"$*\" >> \"$log_file\"\n"
            "case \"$1\" in\n"
            "  inspect) cat \"$state_dir/$4\" ;;\n"
            "  start) echo true > \"$state_dir/$2\" ;;\n"
            "  stop) echo false > \"$state_dir/$4\" ;;\n"
            "  logs) printf 'fake %s runtime log\\n' \"$7\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        self.fake_docker.chmod(0o755)
        (self.work_dir / "lib").mkdir(parents=True)
        self.raw_dir.mkdir()
        (self.work_dir / "lib" / "app_1.conf").write_text(
            f"agent_base_url=http://127.0.0.1:{self.server.server_port}\n"
            "request_timeout_ms=10000\n",
            encoding="utf-8",
        )
        with (self.work_dir / "lib" / "app_1.conf").open("a", encoding="utf-8") as config:
            config.write(
                f"docker_path={self.fake_docker}\n"
                "llm_container=llm-qwen3-vl\n"
                "agent_container=stardetect-agent\n"
                f"llm_health_url=http://127.0.0.1:{self.server.server_port}/v1/models\n"
                f"agent_health_url=http://127.0.0.1:{self.server.server_port}/health\n"
                "backend_start_timeout_ms=1000\n"
                "backend_poll_interval_ms=10\n"
            )
        (self.raw_dir / "prompt1.txt").write_text(
            "Translate this sentence into Chinese: The payload is ready.", encoding="utf-8"
        )
        (self.raw_dir / "prompt2.txt").write_text(
            "当前 GPU 温度、利用率和显存是多少？请使用 get_gpu_status 工具查询。", encoding="utf-8"
        )
        self.result = Path(self.task_dir.name) / "result.json"

    def tearDown(self) -> None:
        self.telemetry.close()
        self.task_dir.cleanup()

    def run_app(
        self,
        mode: str,
        preset_id: str = "1",
        telemetry_path: Path | None = None,
        device_code: str = "7",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(self.executable), mode, preset_id, str(self.work_dir), "/unused/ch1",
                "/unused/ch2", str(self.result), "/unused/result2",
                str(telemetry_path or self.telemetry_path), device_code,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

    def test_jpeg_image_request_and_result(self) -> None:
        (self.raw_dir / "image1.jpg").write_bytes(b"\xff\xd8\xff\xe0test-jpeg")
        MockAgentHandler.response_body = {"answer": "识别到测试图像", "tool_calls": []}
        completed = self.run_app("1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        request = MockAgentHandler.requests[0]["body"]
        self.assertIn("image_url", request)
        self.assertTrue(str(request["image_url"]).startswith("data:image/jpeg;base64,"))
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(result["mode"], "image_recognition")
        self.assertEqual(result["input"]["preset_id"], 1)
        self.assertEqual(result["answer"], "识别到测试图像")

    def test_telemetry_frame_and_periodic_running_status(self) -> None:
        MockAgentHandler.response_delay_seconds = 0.7
        completed = self.run_app("2")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        frames = self.telemetry.wait_for_status(0x01)
        self.assertGreaterEqual(sum(frame[6] == 0x00 for frame in frames), 2)
        for frame in frames:
            self.assertEqual(len(frame), TELEMETRY_FRAME_SIZE)
            self.assertEqual(frame[0], 7)
            self.assertEqual(frame[1], 0)
            self.assertEqual(struct.unpack_from("<I", frame, 2)[0], 1)
            self.assertTrue(all(value == 0 for value in frame[7:]))

    def test_png_image_request(self) -> None:
        (self.raw_dir / "image2.png").write_bytes(b"\x89PNG\r\n\x1a\nminimal")
        completed = self.run_app("1", "2")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        body = MockAgentHandler.requests[0]["body"]
        self.assertTrue(str(body["image_url"]).startswith("data:image/png;"))

    def test_text_prompt_reads_selected_raw_file(self) -> None:
        MockAgentHandler.response_body = {
            "answer": "GPU 温度 59°C",
            "tool_calls": [{"name": "get_gpu_status", "args": {}, "id": "call-1"}],
        }
        completed = self.run_app("2", "2")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        request = MockAgentHandler.requests[0]["body"]
        self.assertNotIn("image_url", request)
        self.assertEqual(
            request["message"],
            "当前 GPU 温度、利用率和显存是多少？请使用 get_gpu_status 工具查询。",
        )
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(result["mode"], "text_prompt")
        self.assertEqual(result["input"]["preset_id"], 2)
        self.assertEqual(result["tool_calls"][0]["name"], "get_gpu_status")

    def test_task_log_collects_stdout_docker_logs_and_header(self) -> None:
        completed = self.run_app("2", "1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        task_id = MockAgentHandler.requests[0]["task_id"]
        self.assertRegex(str(task_id), r"^\d{8}T\d{6}Z-\d+$")
        logs = list((self.work_dir / "log").glob("spacezenith-*.log"))
        self.assertEqual(len(logs), 1)
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn(f"APP task_started task_id={task_id}", text)
        self.assertIn("fake stardetect-agent runtime log", text)
        self.assertIn("fake llm-qwen3-vl runtime log", text)
        self.assertIn("APP task_completed", text)
        self.assertIn("fake stardetect-agent runtime log", completed.stdout)

    def test_only_stops_backends_started_by_this_task(self) -> None:
        completed = self.run_app("2", "1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        commands = self.docker_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            commands[:5],
            [
                "inspect --format {{.State.Running}} llm-qwen3-vl",
                "start llm-qwen3-vl",
                "inspect --format {{.State.Running}} stardetect-agent",
                "start stardetect-agent",
                "stop --time 5 stardetect-agent",
            ],
        )
        self.assertTrue(commands[5].startswith("logs --timestamps --since "))
        self.assertTrue(commands[5].endswith(" --tail 500 stardetect-agent"))
        self.assertEqual(commands[6], "stop --time 5 llm-qwen3-vl")
        self.assertTrue(commands[7].startswith("logs --timestamps --since "))
        self.assertTrue(commands[7].endswith(" --tail 500 llm-qwen3-vl"))
        self.assertIn("fake stardetect-agent runtime log", completed.stdout)
        self.assertIn("fake llm-qwen3-vl runtime log", completed.stdout)

    def test_does_not_stop_preexisting_backends(self) -> None:
        for container in ("llm-qwen3-vl", "stardetect-agent"):
            (self.docker_state_dir / container).write_text("true\n", encoding="utf-8")
        completed = self.run_app("2", "1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        commands = self.docker_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            commands[:2],
            [
                "inspect --format {{.State.Running}} llm-qwen3-vl",
                "inspect --format {{.State.Running}} stardetect-agent",
            ],
        )
        self.assertEqual(len(commands), 4)
        self.assertTrue(commands[2].endswith(" --tail 500 stardetect-agent"))
        self.assertTrue(commands[3].endswith(" --tail 500 llm-qwen3-vl"))

    def test_missing_preset_writes_error(self) -> None:
        completed = self.run_app("2", "3")
        self.assertNotEqual(completed.returncode, 0)
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(result["error"]["code"], "preset_not_found")
        self.telemetry.wait_for_status(0x02)

    def test_missing_telemetry_socket_does_not_fail_task(self) -> None:
        completed = self.run_app("2", telemetry_path=Path(self.task_dir.name) / "missing.sock")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("cannot connect telemetry socket", completed.stderr)

    def test_invalid_device_code_writes_error(self) -> None:
        for device_code in ("256", "not-a-number"):
            completed = self.run_app("2", device_code=device_code)
            self.assertNotEqual(completed.returncode, 0)
            result = json.loads(self.result.read_text(encoding="utf-8"))
            self.assertEqual(result["error"]["code"], "invalid_device_code")

    def test_bad_mode_and_http_error_write_errors(self) -> None:
        completed = self.run_app("3")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(json.loads(self.result.read_text())["error"]["code"], "unsupported_mode")

        MockAgentHandler.response_status = 502
        completed = self.run_app("2")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(json.loads(self.result.read_text())["error"]["code"], "agent_http_error")

    def test_request_timeout_writes_error(self) -> None:
        with (self.work_dir / "lib" / "app_1.conf").open("a", encoding="utf-8") as config:
            config.write("request_timeout_ms=100\n")
        MockAgentHandler.response_delay_seconds = 0.5
        completed = self.run_app("2")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(json.loads(self.result.read_text())["error"]["code"], "request_timeout")

    def test_sigterm_writes_cancelled_result(self) -> None:
        MockAgentHandler.response_delay_seconds = 0.5
        process = subprocess.Popen(
            [
                str(self.executable), "2", "1", str(self.work_dir), "/unused/ch1",
                "/unused/ch2", str(self.result), "/unused/result2", str(self.telemetry_path), "7",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 3.0
        while not MockAgentHandler.requests and time.monotonic() < deadline:
            time.sleep(0.02)
        os.kill(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143)
        self.assertEqual(json.loads(self.result.read_text())["error"]["code"], "cancelled")
        self.telemetry.wait_for_status(0x03)


if __name__ == "__main__":
    unittest.main(verbosity=2)
