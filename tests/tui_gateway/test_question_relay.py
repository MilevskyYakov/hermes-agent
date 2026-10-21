"""Exercise the real TUI wait/RPC, gateway auth/dispatch and SQLite with no network/model."""
import asyncio
import json
import queue
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import yaml
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionSource, build_session_key


class Transport(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="synthetic"), Platform.TELEGRAM)
        self.messages = {}

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "private"}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        mid = str(len(self.messages) + 1)
        self.messages[mid] = content
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.messages[message_id] = content
        return SendResult(success=True, message_id=message_id)


@pytest.mark.parametrize("automatic", [False, True])
def test_tui_telegram_questions_share_one_owner_and_stop(monkeypatch, tmp_path, automatic):
    from hermes_constants import get_hermes_home
    from hermes_state_registry import acquire
    from hermes_question_relay import RelayStore
    from gateway.question_relay import deliver_pending, handle_reply
    from gateway.run import GatewayRunner
    from tui_gateway import server
    from tui_gateway.question_relay import finish, tick

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    route = {"task": "synthetic", "chat_id": "101", "user_id": "101"}
    project = tmp_path / "synthetic-project"
    subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
    config = {"question_relay": {"sessions": {"alpha": route, "beta": route}}}
    if automatic:
        config = {"question_relay": {"auto": {"enabled": True,
            "since": datetime.now(timezone.utc).isoformat(), "chat_id": "101", "user_id": "101"}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "101")
    db = acquire()
    store = RelayStore(db)
    for sid in ("alpha", "beta"):
        db.create_session(sid, "tui", cwd=str(project))
        db.set_session_title(sid, "Synthetic task " + sid)
    monkeypatch.setattr(server, "_db", db)
    events = queue.Queue()
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: events.put((event, sid, payload)))
    sessions = {sid: {"session_key": sid, "history_lock": threading.RLock(), "agent": None,
                      "profile_home": str(home), "running": True} for sid in ("alpha", "beta")}
    monkeypatch.setattr(server, "_sessions", sessions)
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: 10)
    runner = GatewayRunner(GatewayConfig())
    runner._run_agent = AsyncMock(side_effect=AssertionError("must not create a second executor"))
    transport = Transport()
    runner.adapters[Platform.TELEGRAM] = transport
    transport.set_message_handler(runner._handle_message)

    def next_question():
        while True:
            event, sid, payload = events.get(timeout=5)
            if event == "clarify.request":
                return sid, payload["request_id"]

    def event(mid, text, user="101", chat="101"):
        return MessageEvent(text=text, source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat,
                            user_id=user, chat_type="dm"), message_id="reply", reply_to_message_id=mid,
                            reply_to_text=transport.messages[mid], reply_to_is_own_message=True)

    with ThreadPoolExecutor(2) as pool:
        alpha = pool.submit(server._clarify_block, "alpha", "Colour?", ["BLUE", "RED"])
        beta = pool.submit(server._clarify_block, "beta", "Shape?", ["ROUND", "SQUARE"])
        requests = dict([next_question(), next_question()])
        asyncio.run(deliver_pending(store, transport, config))
        am = store.get(requests["alpha"])["message_id"]
        bm = store.get(requests["beta"])["message_id"]
        assert "unauthorized" in asyncio.run(handle_reply(event(am, "RED", user="999")))
        assert "unauthorized" in asyncio.run(handle_reply(event(am, "RED", chat="102")))
        assert store.get(requests["alpha"])["answers"] == {}
        # Both native guards: even a busy ordinary Telegram conversation cannot swallow the reply.
        reply = event(am, "BLUE")
        transport._active_sessions[build_session_key(reply.source)] = asyncio.Event()
        asyncio.run(transport.handle_message(reply))
        assert alpha.result(timeout=5) == "BLUE"
        late = server._methods["clarify.respond"]("late", {"request_id": requests["alpha"], "answer": "RED"})
        assert "expired" in late["error"]["message"]
        assert not beta.done()
        local = server._methods["clarify.respond"]("rpc-1", {"request_id": requests["beta"], "answer": "ROUND"})
        assert local["result"]["status"] == "ok"
        assert beta.result(timeout=5) == "ROUND"
        assert "conflict" in asyncio.run(runner._handle_message(event(am, "RED")))
        finish(sessions["alpha"], "DONE BLUE")
        asyncio.run(deliver_pending(store, transport, config))
        assert "DONE BLUE" in transport.messages[am]
        assert "Decision: ROUND" in transport.messages[bm]
        # A stop targeted at beta never interrupts alpha or creates a new turn.
        assert "stopped" in asyncio.run(runner._handle_message(event(bm, "/stop")))
        tick("beta", sessions["beta"])
        assert sessions["beta"]["_turn_cancel_requested"] is True
        assert not sessions["alpha"].get("_turn_cancel_requested")
        assert "stopped" in asyncio.run(runner._handle_message(event(bm, "SQUARE")))
    runner._run_agent.assert_not_awaited()
    received = []
    while not events.empty():
        received.append(events.get_nowait())
    resolved = {sid: payload["answers"] for kind, sid, payload in received if kind == "clarify.resolved"}
    assert resolved == {"alpha": {"q1": "BLUE"}, "beta": {"q1": "ROUND"}}
    assert not any(kind == "clarify.expire" for kind, _, _ in received)
    db.close()


@pytest.mark.parametrize("automatic", [False, True])
def test_batch_revocation_and_recovery_fence(monkeypatch, tmp_path, automatic):
    from hermes_constants import get_hermes_home
    from hermes_state_registry import acquire
    from hermes_question_relay import RelayStore
    from tui_gateway import server
    from tui_gateway.question_relay import resume_note

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    route = {"task": "batch", "chat_id": "101", "user_id": "101"}
    config = home / "config.yaml"
    project = tmp_path / "project"
    subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
    config_data = {"question_relay": {"sessions": {"batch": route}}}
    if automatic:
        config_data = {"question_relay": {"auto": {"enabled": True,
            "since": datetime.now(timezone.utc).isoformat(), "chat_id": "101", "user_id": "101"}}}
    config.write_text(yaml.safe_dump(config_data))
    db = acquire()
    db.create_session("batch", "tui", cwd=str(project))
    store = RelayStore(db)
    monkeypatch.setattr(server, "_db", db)
    session = {"session_key": "batch", "profile_home": str(home), "history_lock": threading.RLock(), "agent": None, "running": True}
    monkeypatch.setattr(server, "_sessions", {"batch": session})
    events = queue.Queue()
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: events.put((event, payload)))
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: 10)
    questions = [{"qid": "a", "question": "A?", "choices": [], "multi_select": False},
                 {"qid": "b", "question": "B?", "choices": ["B1", "B2"], "multi_select": True}]
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(server._clarify_block, "batch", "", [], questions=questions)
        while True:
            kind, payload = events.get(timeout=5)
            if kind == "clarify.request":
                rid = payload["request_id"]
                break
        assert "forbidden" in server._methods["clarify.respond"]("rpc", {"session_id": "foreign", "request_id": rid, "question_id": "a", "answer": "X"})["error"]["message"]
        response = server._methods["clarify.respond"]("rpc", {"session_id": "batch", "request_id": rid, "question_id": "a", "answer": "A1"})
        assert response["result"]["remaining"] == ["b"]
        assert not waiting.done()
        # Removing the explicit grant stops the original waiter; no answer/approval is invented.
        config.write_text("question_relay: {sessions: {}}\n")
        assert waiting.result(timeout=5) == ""
        assert session["_turn_cancel_requested"] is True
        assert store.get(rid)["answers"] == {"a": "A1"}
        assert store.answer(rid, "b", '["B1"]') == "stopped"
        with pytest.raises(RuntimeError, match="recovery required"):
            resume_note({"session_key": "batch", "profile_home": str(home)})
        with pytest.raises(ValueError, match="still alive"):
            store.recover("batch", rid, "reviewed synthetic effects")
    # A reply committed between the last read and expiry must not be lost.
    db.create_session("deadline", "tui")
    server._sessions["deadline"] = {"session_key": "deadline", "profile_home": str(home)}
    config.write_text(yaml.safe_dump({"question_relay": {"sessions": {"deadline": route}}}))
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: 0)
    original_expire = RelayStore.expire
    def answer_before_expire(self, request_id):
        assert self.answer(request_id, "q1", "BLUE") == "accepted"
        original_expire(self, request_id)
    monkeypatch.setattr(RelayStore, "expire", answer_before_expire)
    assert server._clarify_block("deadline", "Colour?", ["BLUE"]) == "BLUE"
    db.close()
