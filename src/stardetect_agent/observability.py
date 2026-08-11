"""Metadata-only structured logging for task-correlated Agent events."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

AUDIT_LOGGER_NAME = "stardetect_agent.audit"


def configure_structured_logging() -> None:
    """Emit one JSON object per line to container stdout, without duplicating handlers."""
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_event(event: str, **fields: object) -> None:
    """Log safe metadata only; callers must never pass request/response bodies."""
    payload = {"event": event, **fields}
    logging.getLogger(AUDIT_LOGGER_NAME).info(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def _usage_from_response(response: Any) -> dict[str, int]:
    raw = getattr(response, "llm_output", None) or {}
    usage = raw.get("token_usage") or raw.get("usage") or {}
    if not usage:
        generations = getattr(response, "generations", []) or []
        if generations and generations[0]:
            message = getattr(generations[0][0], "message", None)
            usage = getattr(message, "usage_metadata", None) or {}
    mapping = {
        "input_tokens": ("prompt_tokens", "input_tokens"),
        "output_tokens": ("completion_tokens", "output_tokens"),
        "total_tokens": ("total_tokens",),
    }
    result: dict[str, int] = {}
    for target, candidates in mapping.items():
        value = next((usage.get(key) for key in candidates if usage.get(key) is not None), None)
        if isinstance(value, int):
            result[target] = value
    return result


class TaskLLMCallback(BaseCallbackHandler):
    """Records each ChatOpenAI turn, including tool-follow-up turns, by run id."""

    def __init__(self, task_id: str, model: str) -> None:
        self.task_id = task_id
        self.model = model
        self._started: dict[str, float] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any,
        **_: Any,
    ) -> None:
        key = str(run_id)
        self._started[key] = time.perf_counter()
        log_event("llm_call_started", task_id=self.task_id, model=self.model, run_id=key)

    def on_llm_end(self, response: Any, *, run_id: Any, **_: Any) -> None:
        key = str(run_id)
        started = self._started.pop(key, time.perf_counter())
        log_event(
            "llm_call_completed",
            task_id=self.task_id,
            model=self.model,
            run_id=key,
            duration_ms=round((time.perf_counter() - started) * 1000),
            **_usage_from_response(response),
        )

    def on_llm_error(self, error: BaseException, *, run_id: Any, **_: Any) -> None:
        key = str(run_id)
        started = self._started.pop(key, time.perf_counter())
        log_event(
            "llm_call_failed",
            task_id=self.task_id,
            model=self.model,
            run_id=key,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_type=type(error).__name__,
        )
