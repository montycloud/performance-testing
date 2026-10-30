"""AI Chat over WebSocket.

One chat call = open a WebSocket, send one query, read frames until
PROMPT_STATUS/ENDED. If the server rejects with SESSION_TIME_LIMIT_REACHED, a
fresh connection sends "yes Continue" on the same thread, up to
max_session_retries times.

Two pseudo-requests go into Locust's stats (request_type "WS"):
  [Chat] time_to_first_token   first REASONING frame after the query was sent
  [Chat] full_response         whole call including continuations

classify_frame() holds the protocol knowledge and has no I/O, so it is unit-tested
directly. ChatSession does the socket work and takes its metric and transcript
sinks as arguments.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websocket

from mcperf.core.redact import redact_ws_url

logger = logging.getLogger("perf_test.chat")

CONTINUE_QUERY = "yes Continue"

# A metric sink: (name, start_monotonic, exception_or_None) -> None
FireMetric = Callable[[str, float, BaseException | None], None]


# ----------------------------------------------------------------------
# Settings and input files
# ----------------------------------------------------------------------


def load_queries(path: Path) -> tuple[str, ...]:
    """Non-empty, non-comment lines of a chat queries file."""
    if not path.exists():
        raise FileNotFoundError(f"Chat queries file not found: {path} (chat.queries_file)")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    queries = tuple(line for line in lines if line and not line.startswith("#"))
    if not queries:
        raise ValueError(f"Chat queries file is empty: {path}")
    logger.info("Loaded %d chat quer(y/ies) from %s", len(queries), path)
    return queries


def load_tenant_scope(path: Path) -> tuple[dict[str, str], ...]:
    """Tenant.json → [{ID: Name}, ...]; every chat call sends all of them as tenant_scope."""
    if not path.exists():
        raise FileNotFoundError(f"Tenants file not found: {path} (chat.tenants_file)")
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"Tenants file must be a JSON list: {path}")
    by_name = {e["Name"]: e for e in entries if isinstance(e, dict) and e.get("Name")}
    logger.info("Loaded %d tenant(s) from %s", len(by_name), path)
    return tuple({e["ID"]: e["Name"]} for e in by_name.values() if e.get("ID"))


@dataclass(frozen=True)
class ChatSettings:
    ws_base_url: str = ""
    timeout: float = 120.0
    model_id: str = ""
    temperature: float = 0.1
    top_p: float = 1.0
    top_k: int = 250
    max_session_retries: int = 5
    queries: tuple[str, ...] = ()
    tenant_scope: tuple[dict[str, str], ...] = ()
    transcript_log: Path | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any], resolve: Callable[[str], Path]) -> ChatSettings:
        """Reads the queries and tenants files; call only when chat is enabled."""
        transcript = str(d.get("transcript_log", "") or "").strip()
        return cls(
            ws_base_url=str(d.get("ws_base_url", "")).rstrip("/"),
            timeout=float(d.get("timeout_seconds", 120)),
            model_id=str(d.get("model_id", "")),
            temperature=float(d.get("temperature", 0.1)),
            top_p=float(d.get("top_p", 1)),
            top_k=int(d.get("top_k", 250)),
            max_session_retries=int(d.get("max_session_retries", 5)),
            queries=load_queries(resolve(str(d.get("queries_file", "./chat_queries.txt")))),
            tenant_scope=load_tenant_scope(resolve(str(d.get("tenants_file", "./Tenant.json")))),
            transcript_log=resolve(transcript) if transcript else None,
        )

    def payload(self, query: str, thread_id: str) -> dict[str, Any]:
        return {
            "query": query,
            "thread_id": thread_id,
            "metadata": {
                "model_id": self.model_id,
                "temperature": self.temperature,
                "top_p": self.top_p,
                "top_k": self.top_k,
            },
            "tenant_scope": list(self.tenant_scope),
        }


# ----------------------------------------------------------------------
# Transcript and session-timeout bookkeeping
# ----------------------------------------------------------------------


class Transcript:
    """Optional per-message log file (chat.transcript_log). No-op when path is None."""

    def __init__(self, path: Path | None):
        self._logger: logging.Logger | None = None
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(f"perf_test.chat_transcript.{path}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = logging.FileHandler(path, mode="a", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
            self._logger.addHandler(handler)
        logger.info("Chat transcript logging enabled -> %s", path)

    def write(self, who: str, direction: str, category: str, elapsed_s: float, detail: str,
              max_len: int = 500) -> None:
        if self._logger is None:
            return
        if len(detail) > max_len:
            detail = f"{detail[:max_len]}...(+{len(detail) - max_len} more chars)"
        self._logger.info("%-28s | %-4s | %-11s | +%8.3fs | %s", who, direction, category, elapsed_s, detail)


class SessionTimeoutStats:
    """Per-user count of SESSION_TIME_LIMIT_REACHED continuations, shown in the report."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def add(self, who: str, count: int) -> None:
        if count:
            with self._lock:
                self._counts[who] = self._counts.get(who, 0) + count

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


# ----------------------------------------------------------------------
# Protocol
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """What one server frame means for the call in progress.

    kind: "info" (keep reading), "reasoning", "ended", "error", "session_timeout"
    """

    kind: str
    category: str
    error: BaseException | None = None
    thread_id: str | None = None


def classify_frame(frame: dict[str, Any]) -> Frame:
    if frame.get("message") == "Endpoint request timed out":
        # API Gateway notice; the backend is still working.
        return Frame("info", "GW_TIMEOUT")
    if frame.get("type") == "THREAD_TITLE":
        return Frame("info", "THREAD_TITLE")

    top_message = frame.get("message")
    if frame.get("type") == "PROMPT_STATUS":
        # Top-level PROMPT_STATUS/ERROR (e.g. inaccessible tenant_scope org id):
        # no "body" wrapper and "message" is an object. Terminal.
        if isinstance(top_message, dict) and top_message.get("message") == "ERROR":
            return Frame("error", "PROMPT_ERROR",
                         RuntimeError(f"Chat PROMPT_STATUS ERROR: {top_message.get('error', top_message)}"))
        if top_message == "REJECTED":
            code = frame.get("code", "")
            if code == "SESSION_TIME_LIMIT_REACHED":
                return Frame("session_timeout", "PROMPT_REJECTED", thread_id=frame.get("thread_id", ""))
            return Frame("error", "PROMPT_REJECTED", RuntimeError(
                f"Chat PROMPT_STATUS REJECTED (code={code}): {frame.get('display_message') or code}"))

    body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
    body_type = body.get("type")
    if body_type == "PROMPT_STATUS" and body.get("message") == "STARTED":
        return Frame("info", "STARTED")
    if body_type == "REASONING":
        return Frame("reasoning", "REASONING")
    if body_type == "TOOL_RESULT":
        return Frame("info", "TOOL_RESULT")
    if body_type == "PROMPT_STATUS" and body.get("message") == "ENDED":
        return Frame("ended", "ENDED")
    return Frame("info", body_type or "UNKNOWN")


@dataclass(frozen=True)
class AttemptResult:
    ended: bool
    error: BaseException | None = None
    # Set only on SESSION_TIME_LIMIT_REACHED: the thread to continue on.
    continue_thread_id: str | None = None


# ----------------------------------------------------------------------
# Session
# ----------------------------------------------------------------------


class ChatSession:
    def __init__(self, settings: ChatSettings, fire_metric: FireMetric, transcript: Transcript, who: str):
        self.settings = settings
        self.fire_metric = fire_metric
        self.transcript = transcript
        self.who = who

    def run(self, url: str) -> int:
        """One chat call with continuations. Returns the number of continuations used."""
        s = self.settings
        t_start = time.monotonic()
        query = random.choice(s.queries)
        thread_id = ""
        continuations = 0
        result = AttemptResult(ended=False)

        for attempt in range(s.max_session_retries + 1):
            result = self.attempt(url, query, thread_id, attempt)
            if result.ended or result.continue_thread_id is None:
                break
            if attempt >= s.max_session_retries:
                result = AttemptResult(False, RuntimeError(
                    f"SESSION_TIME_LIMIT_REACHED after {s.max_session_retries} continuation attempt(s)"))
                logger.error("[%s] Max session continuations reached (%d); failing.",
                             self.who, s.max_session_retries)
                break
            continuations += 1
            thread_id, query = result.continue_thread_id, CONTINUE_QUERY
            logger.info("[%s] Sending session continuation %d/%d (thread_id=%s).",
                        self.who, continuations, s.max_session_retries, thread_id)
            self.transcript.write(self.who, "SYS", "SESSION_CONTINUE", time.monotonic() - t_start,
                                  f"continuation={continuations}/{s.max_session_retries} thread_id={thread_id}")

        elapsed = time.monotonic() - t_start
        if result.ended:
            self.fire_metric("[Chat] full_response", t_start, None)
            self.transcript.write(self.who, "SYS", "DONE", elapsed,
                                  f"status=SUCCESS session_continuations={continuations}")
        else:
            error = result.error or Exception("Chat call did not complete")
            self.fire_metric("[Chat] full_response", t_start, error)
            self.transcript.write(self.who, "SYS", "DONE", elapsed,
                                  f"status=FAILED session_continuations={continuations} error={result.error}")
        return continuations

    def attempt(self, url: str, query: str, thread_id: str, attempt: int) -> AttemptResult:
        """One WebSocket connection: send *query*, read until a terminal frame or timeout."""
        s, who, log = self.settings, self.who, self.transcript.write
        connect_start = time.monotonic()
        try:
            ws = websocket.create_connection(url, timeout=s.timeout)
        except Exception as exc:
            logger.error("[%s] Chat WebSocket connect failed (attempt %d): %s", who, attempt, exc)
            log(who, "SYS", "CONNECT_FAIL", time.monotonic() - connect_start,
                f"attempt={attempt} url={redact_ws_url(url)} error={exc}")
            return AttemptResult(False, exc)
        log(who, "SYS", "CONNECTED", time.monotonic() - connect_start,
            f"attempt={attempt} url={redact_ws_url(url)}")

        t_send = time.monotonic()
        ttft_recorded = False
        timeout_error = TimeoutError(f"No PROMPT_STATUS/ENDED frame within {s.timeout}s")
        try:
            ws.send(json.dumps(s.payload(query, thread_id)))
            log(who, "SEND", "QUERY", 0.0, f"attempt={attempt} query={query}")
            deadline = t_send + s.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._timed_out(attempt, t_send, timeout_error)
                ws.settimeout(remaining)
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    return self._timed_out(attempt, t_send, timeout_error)
                if not raw:
                    continue

                elapsed = time.monotonic() - t_send
                try:
                    frame = classify_frame(json.loads(raw))
                except (ValueError, TypeError, AttributeError):
                    logger.warning("[%s] Chat: non-JSON frame received; ignoring.", who)
                    log(who, "RECV", "NON_JSON", elapsed, str(raw))
                    continue
                log(who, "RECV", frame.category, elapsed, raw)

                if frame.kind == "reasoning" and not ttft_recorded:
                    ttft_recorded = True
                    self.fire_metric("[Chat] time_to_first_token", t_send, None)
                elif frame.kind == "ended":
                    return AttemptResult(True)
                elif frame.kind == "error":
                    logger.error("[%s] Chat failed: %s", who, frame.error)
                    return AttemptResult(False, frame.error)
                elif frame.kind == "session_timeout":
                    logger.warning("[%s] SESSION_TIME_LIMIT_REACHED (attempt %d); thread_id=%s",
                                   who, attempt, frame.thread_id)
                    return AttemptResult(False, continue_thread_id=frame.thread_id)
                elif frame.category == "GW_TIMEOUT":
                    logger.warning("[%s] Chat received 'Endpoint request timed out'; still listening.", who)
        except Exception as exc:
            logger.error("[%s] Chat WebSocket error (attempt %d): %s", who, attempt, exc)
            log(who, "SYS", "ERROR", time.monotonic() - t_send, f"attempt={attempt} {exc}")
            return AttemptResult(False, exc)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _timed_out(self, attempt: int, t_send: float, error: TimeoutError) -> AttemptResult:
        logger.error("[%s] Chat timed out waiting for ENDED frame (attempt %d).", self.who, attempt)
        self.transcript.write(self.who, "RECV", "TIMEOUT", time.monotonic() - t_send,
                              f"attempt={attempt} No ENDED frame within {self.settings.timeout}s")
        return AttemptResult(False, error)
