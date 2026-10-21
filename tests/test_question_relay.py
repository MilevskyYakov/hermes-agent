"""GERDA2.0 #588: durable decisions, not a second executor or a last-chat mailbox."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from hermes_state import SessionDB
from hermes_question_relay import RelayStore, route_for


def test_decisions_are_addressed_atomic_and_survive_reopen(tmp_path):
    import pytest
    path = tmp_path / "state.db"
    db = SessionDB(path)
    store = RelayStore(db)
    route = {"task": "synthetic", "chat_id": "101", "user_id": "101"}
    for sid in ("alpha", "beta"):
        db.create_session(sid, "tui")
        store.create(sid, sid, "owner-" + sid, route, [{"qid": "q1", "question": "Colour?", "choices": ["BLUE", "RED"]}])
    store.delivery("alpha", "sent", "11")
    store.delivery("beta", "sent", "12")
    assert store.reply("101", "", "999", "11", "BLUE") == "forbidden"
    assert store.reply("102", "", "101", "11", "BLUE") == "unknown"
    assert store.reply("101", "topic", "101", "11", "BLUE") == "unknown"
    assert store.reply("101", "", "101", "13", "BLUE") == "unknown"
    with pytest.raises(ValueError, match="already has an owner"):
        store.create("other-owner", "alpha", "other", route, [{"qid": "q1", "question": "Hijack?"}])
    peer_db = SessionDB(path)
    peer = RelayStore(peer_db)
    with ThreadPoolExecutor(2) as pool:
        attempts = [pool.submit(s.reply, "101", "", "101", "11", a) for s, a in [(store, "BLUE"), (peer, "RED")]]
        results = [f.result() for f in attempts]
    assert sorted(results) == ["accepted", "conflict"]
    decision = store.get("alpha")["answers"]["q1"]
    assert store.reply("101", "", "101", "11", decision) == "duplicate"
    assert store.get("beta")["answers"] == {}
    assert store.consume("alpha", "wrong-owner") is None
    with ThreadPoolExecutor(2) as pool:
        attempts = [pool.submit(s.consume, "alpha", "owner-alpha") for s in (store, peer)]
        consumed = [f.result() for f in attempts]
    assert consumed.count({"q1": decision}) == 1 and consumed.count(None) == 1
    peer_db.close()
    assert store.consume("alpha", "owner-alpha") is None
    assert store.reply("101", "", "101", "12", "/stop") == "stopped"
    assert store.reply("101", "", "101", "12", "BLUE") == "stopped"
    db.create_session("timeout", "tui")
    store.create("late", "timeout", "owner", route, [{"qid": "q1", "question": "Timed out?"}])
    store.expire("late")
    assert store.answer("late", "q1", "YES") == "expired"
    db.close()
    db = SessionDB(path)
    store = RelayStore(db)
    assert store.get("alpha")["answers"] == {"q1": decision}
    assert store.consume("alpha", "new-process") is None
    assert store.get("beta")["status"] == "stopped"
    assert route_for({"question_relay": {"sessions": {"alpha": route}}}, "beta") is None
    db.close()


def test_delivery_failure_is_not_success_or_a_blind_retry(tmp_path):
    from gateway.question_relay import deliver_pending

    db = SessionDB(tmp_path / "state.db")
    db.create_session("alpha", "tui")
    store = RelayStore(db)
    route = {"task": "synthetic", "chat_id": "101", "user_id": "101"}
    store.create("one", "alpha", "owner", route, [{"qid": "q1", "question": "Colour?", "choices": []}])

    class Transport:
        calls = 0
        async def send(self, **kwargs):
            self.calls += 1
            raise TimeoutError("delivery outcome unknown")

    transport = Transport()
    asyncio.run(deliver_pending(store, transport))
    asyncio.run(deliver_pending(store, transport))
    assert transport.calls == 1
    assert store.get("one")["delivery"] == "unknown"
    assert store.get("one")["status"] == "waiting"
    assert store.consume("one", "owner") is None
    store.create("offline", "alpha", "owner", route, [{"qid": "q1", "question": "Gateway offline?"}])
    assert store.get("offline")["delivery"] == "queued"
    assert store.answer("offline", "q1", "terminal still works") == "accepted"
    assert store.consume("offline", "owner") == {"q1": "terminal still works"}
    db.close()


def test_dead_owner_recovery_preserves_decision_without_replaying_effect(tmp_path, monkeypatch):
    import argparse
    import subprocess
    import sys
    import pytest
    from hermes_cli.subcommands.sessions import build_sessions_parser
    from hermes_cli.sessions_cmd import cmd_sessions
    from tui_gateway import server
    from tui_gateway.question_relay import resume_note
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    import yaml
    (home / "config.yaml").write_text(yaml.safe_dump({"question_relay": {"sessions": {
        "crashed": {"task": "synthetic", "chat_id": "101", "user_id": "101"}}}}))
    effect = tmp_path / "effect.txt"
    # A real terminated process owns the row; no forged PID/liveness fixture.
    child = subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
from hermes_state import SessionDB
from hermes_question_relay import RelayStore
db = SessionDB()
db.create_session('crashed', 'tui')
s = RelayStore(db)
s.create('question', 'crashed', 'child-owner', {'task': 'synthetic', 'chat_id': '101', 'user_id': '101'},
         [{'qid': 'q1', 'question': 'Colour?', 'choices': []}])
s.answer('question', 'q1', 'BLUE')
assert s.consume('question', 'child-owner') == {'q1': 'BLUE'}
Path(sys.argv[1]).write_text('performed once')
db.close()
""", str(effect)], capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    db = SessionDB()
    store = RelayStore(db)
    monkeypatch.setattr(server, "_db", db)
    session = {"session_key": "crashed", "profile_home": str(home)}
    with pytest.raises(RuntimeError, match="recovery required"):
        resume_note(session)
    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(), cmd_sessions=cmd_sessions)
    args = parser.parse_args(['sessions', 'questions', 'crashed', '--recover', 'question',
                              '--effects-reviewed', 'Verified effect.txt contains performed once; reuse it'])
    assert args.func(args) == 0
    assert args.func(args) == 0  # Repeat is a no-op, not another execution grant.
    assert store.get('question')['status'] == 'reviewed'
    note = resume_note(session)
    assert 'BLUE' in note and 'do not repeat completed actions' in note
    assert effect.read_text() == 'performed once'
    assert store.consume('question', 'new-owner') is None
    db.close()
