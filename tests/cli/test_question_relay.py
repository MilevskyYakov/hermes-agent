"""Classic CLI/Orca callback and real turn runner: one synthetic effect, no model/network."""
import asyncio
import queue
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import yaml

from cli import HermesCLI
from hermes_constants import get_hermes_home
from hermes_state_registry import acquire
from hermes_question_relay import RelayStore
from gateway.question_relay import deliver_pending, handle_reply
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from tests.tui_gateway.test_question_relay import Transport


def test_classic_cli_real_turn_both_directions_and_duplicate(monkeypatch):
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"question_relay": {"sessions": {
        "classic": {"task": "synthetic classic", "chat_id": "101", "user_id": "101"}}}}))
    db = acquire()
    db.create_session("classic", "cli")
    store = RelayStore(db)
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "classic"
    cli._session_db = db
    cli._clarify_state = None
    cli._clarify_freetext = False
    cli._clarify_multi_base = None
    cli._clarify_prefill = ""
    cli._clarify_deadline = None
    cli._ring_bell = MagicMock()
    ready = queue.Queue()
    cli._paint_now = lambda: ready.put(True) if cli._clarify_state else None
    cli._persist_prompt_summary = MagicMock()
    cli._flush_credit_notices = MagicMock()
    cli.conversation_history = [{"role": "user", "content": "Synthetic task"}]
    effects = []

    def run_conversation(**kwargs):
        answer = cli._clarify_callback("Colour?", ["BLUE", "GREEN"])
        effects.append(answer)
        return {"completed": True, "final_response": "DONE " + answer, "messages": []}

    cli.agent = MagicMock()
    cli.agent.run_conversation.side_effect = run_conversation
    transport = Transport()
    with ThreadPoolExecutor(1) as pool:
        turn = SimpleNamespace(voice_prefix="", stream_callback=None, result=None)
        running = pool.submit(cli._chat_run_agent, turn, "Synthetic task")
        ready.get(timeout=5)
        rid = store.for_session("classic")[-1]["request_id"]
        asyncio.run(deliver_pending(store, transport))
        mid = store.get(rid)["message_id"]
        event = MessageEvent(text="BLUE", source=SessionSource(platform=Platform.TELEGRAM, chat_id="101", user_id="101"),
                             reply_to_message_id=mid, reply_to_text=transport.messages[mid], reply_to_is_own_message=True)
        assert "accepted" in asyncio.run(handle_reply(event))
        running.result(timeout=5)
        assert effects == ["BLUE"]
        assert turn.result["final_response"] == "DONE BLUE"
        assert cli._clarify_state is None
        cli._persist_prompt_summary.assert_any_call("?", "Clarify", "Colour?", "BLUE")
        assert "duplicate" in asyncio.run(handle_reply(event))
        assert effects == ["BLUE"]
        asyncio.run(deliver_pending(store, transport))
        assert "DONE BLUE" in transport.messages[mid]
        while not ready.empty():
            ready.get_nowait()
        next_turn = SimpleNamespace(voice_prefix="", stream_callback=None, result=None)
        running = pool.submit(cli._chat_run_agent, next_turn, "Second synthetic task")
        ready.get(timeout=5)
        state = cli._clarify_state
        cli._clarify_batch_lock(state, "GREEN")  # Real keyboard submission handler.
        running.result(timeout=5)
        assert effects == ["BLUE", "GREEN"]
        asyncio.run(deliver_pending(store, transport))
        assert any("DONE GREEN" in text for text in transport.messages.values())
    assert cli.agent.run_conversation.call_count == 2
    db.close()
