import json

import pytest

from mcperf.api import chat
from mcperf.api.chat import ChatSession, ChatSettings, Transcript, classify_frame


@pytest.mark.parametrize(
    "frame, kind, category",
    [
        ({"message": "Endpoint request timed out"}, "info", "GW_TIMEOUT"),
        ({"type": "THREAD_TITLE", "thread_title": "t"}, "info", "THREAD_TITLE"),
        ({"body": {"type": "PROMPT_STATUS", "message": "STARTED"}}, "info", "STARTED"),
        ({"body": {"type": "REASONING"}}, "reasoning", "REASONING"),
        ({"body": {"type": "TOOL_RESULT"}}, "info", "TOOL_RESULT"),
        ({"body": {"type": "PROMPT_STATUS", "message": "ENDED"}}, "ended", "ENDED"),
        ({"body": {"type": "SOMETHING_NEW"}}, "info", "SOMETHING_NEW"),
        ({}, "info", "UNKNOWN"),
    ],
)
def test_classify_frame(frame, kind, category):
    result = classify_frame(frame)
    assert (result.kind, result.category) == (kind, category)


def test_classify_terminal_frames():
    error = classify_frame({"type": "PROMPT_STATUS", "message": {"message": "ERROR", "error": "no access"}})
    assert error.kind == "error" and "no access" in str(error.error)

    rejected = classify_frame({"type": "PROMPT_STATUS", "message": "REJECTED", "code": "QUOTA",
                               "display_message": "Quota hit"})
    assert rejected.kind == "error" and "Quota hit" in str(rejected.error)

    timeout = classify_frame({"type": "PROMPT_STATUS", "message": "REJECTED",
                              "code": "SESSION_TIME_LIMIT_REACHED", "thread_id": "th-1"})
    assert (timeout.kind, timeout.thread_id) == ("session_timeout", "th-1")


class FakeSocket:
    """Plays back one scripted list of frames per connection."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data))

    def settimeout(self, _seconds):
        pass

    def recv(self):
        return json.dumps(self.frames.pop(0))

    def close(self):
        pass


@pytest.fixture
def sockets(monkeypatch):
    opened = []
    script = []

    def create_connection(_url, timeout):
        sock = FakeSocket(script.pop(0))
        opened.append(sock)
        return sock

    monkeypatch.setattr(chat.websocket, "create_connection", create_connection)
    return script, opened


def make_session(max_retries=2):
    settings = ChatSettings(ws_base_url="wss://x", timeout=5, queries=("q1",),
                            tenant_scope=({"t1": "Tenant 1"},), max_session_retries=max_retries)
    metrics = []
    session = ChatSession(settings, lambda name, _start, exc: metrics.append((name, exc)), Transcript(None), "u")
    return session, metrics


REASONING = {"body": {"type": "REASONING"}}
ENDED = {"body": {"type": "PROMPT_STATUS", "message": "ENDED"}}
SESSION_LIMIT = {"type": "PROMPT_STATUS", "message": "REJECTED", "code": "SESSION_TIME_LIMIT_REACHED",
                 "thread_id": "th-9"}


def test_successful_call_records_ttft_and_full_response(sockets):
    script, opened = sockets
    script.append([REASONING, REASONING, ENDED])
    session, metrics = make_session()
    assert session.run("wss://x") == 0
    assert metrics == [("[Chat] time_to_first_token", None), ("[Chat] full_response", None)]
    payload = opened[0].sent[0]
    assert payload["query"] == "q1" and payload["thread_id"] == ""
    assert payload["tenant_scope"] == [{"t1": "Tenant 1"}]


def test_session_limit_continues_on_same_thread(sockets):
    script, opened = sockets
    script += [[SESSION_LIMIT], [ENDED]]
    session, metrics = make_session()
    assert session.run("wss://x") == 1
    assert opened[1].sent[0]["query"] == "yes Continue"
    assert opened[1].sent[0]["thread_id"] == "th-9"
    assert metrics[-1] == ("[Chat] full_response", None)


def test_session_limit_fails_after_max_retries(sockets):
    script, _opened = sockets
    script += [[SESSION_LIMIT]] * 3
    session, metrics = make_session(max_retries=2)
    assert session.run("wss://x") == 2
    name, error = metrics[-1]
    assert name == "[Chat] full_response" and "SESSION_TIME_LIMIT_REACHED" in str(error)
