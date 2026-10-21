"""Real process loss and native recovery, isolated from accounts, models and gateway services."""
import argparse
import asyncio
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from hermes_constants import get_hermes_home
from hermes_question_relay import RelayStore
from hermes_state_registry import acquire
from gateway.question_relay import deliver_pending
from tests.tui_gateway.test_question_relay import Transport


@pytest.mark.parametrize("surface", ["cli", "tui"])
@pytest.mark.parametrize("crash_after_consume", [False, True])
def test_recovery_rebinds_one_owner_and_publishes_result(tmp_path, monkeypatch, surface, crash_after_consume):
    from hermes_cli.subcommands.sessions import build_sessions_parser
    from hermes_cli.sessions_cmd import cmd_sessions
    from hermes_cli import question_relay as cli_relay
    from tui_gateway import server, question_relay as tui_relay

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    route = {"task": "synthetic recovery", "chat_id": "101", "user_id": "101"}
    config = home / "config.yaml"
    config.write_text(yaml.safe_dump({"question_relay": {"sessions": {"crashed": route}}}))
    effect = tmp_path / "effects.log"
    # The already completed action and its receipt survive an actual abrupt exit.
    child = subprocess.run([sys.executable, "-c", r"""
import os, sys
from pathlib import Path
from hermes_state import SessionDB
from hermes_question_relay import RelayStore
db = SessionDB()
db.create_session('crashed', 'cli')
Path(sys.argv[1]).write_text('effect completed\n')
db.append_message('crashed', 'assistant', 'Completed effect; receipt: effect completed')
s = RelayStore(db)
s.create('question', 'crashed', 'old-owner',
         {'task': 'synthetic recovery', 'chat_id': '101', 'user_id': '101'},
         [{'qid': 'q1', 'question': 'Colour?', 'choices': ['BLUE', 'GREEN']}])
s.delivery('question', 'sent', 'message')
assert s.reply('101', '', '101', 'message', 'BLUE') == 'accepted'
if sys.argv[2] == 'yes':
    assert s.consume('question', 'old-owner') == {'q1': 'BLUE'}
os._exit(23)
""", str(effect), "yes" if crash_after_consume else "no"],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 23, child.stderr
    assert effect.read_text().splitlines() == ["effect completed"]
    db = acquire()
    store = RelayStore(db)
    old_pid = json.loads(store.get("question")["owner_process"])["pid"]
    monkeypatch.setattr(server, "_db", db)
    instance = (SimpleNamespace(session_id="crashed", _session_db=db, agent=MagicMock()) if surface == "cli"
                else {"session_key": "crashed", "profile_home": str(home)})
    resume = cli_relay.resume_note if surface == "cli" else tui_relay.resume_note
    with pytest.raises(RuntimeError, match="recovery required"):
        resume(instance)
    assert store.get("question")["answers"] == {"q1": "BLUE"}
    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(), cmd_sessions=cmd_sessions)
    args = parser.parse_args(["sessions", "questions", "crashed", "--recover", "question",
                              "--effects-reviewed", "Receipt checked: effect completed once; reuse it, never replay"])
    assert args.func(args) == 0
    assert args.func(args) == 0
    assert store.reply("101", "", "101", "message", "/revoke") == "stopped"
    with pytest.raises(RuntimeError, match="recovery required"):
        resume(instance)
    assert args.func(args) == 0  # A new explicit review is necessary after revocation.
    config.write_text("question_relay: {sessions: {}}\n")
    with pytest.raises(RuntimeError, match="route"):
        resume(instance)
    assert store.get("question")["status"] == "reviewed"
    config.write_text(yaml.safe_dump({"question_relay": {"sessions": {"crashed": route}}}))
    rival = (SimpleNamespace(session_id="crashed", _session_db=db) if surface == "cli"
             else {"session_key": "crashed", "profile_home": str(home)})
    def try_resume(candidate):
        try:
            return candidate, resume(candidate)
        except RuntimeError as exc:
            assert "recovery required" in str(exc)
            return candidate, None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(try_resume, (instance, rival)))
    winners = [(candidate, note) for candidate, note in results if note is not None]
    assert len(winners) == 1
    instance, note = winners[0]
    assert "BLUE" in note and "never replay" in note and "not new execution permission" in note
    owner = instance._question_relay_owner if surface == "cli" else instance["_question_relay_owner"]
    assert store.get("question")["owner"] == owner
    assert store.get("question")["status"] == "consumed"
    assert json.loads(store.get("question")["owner_process"])["pid"] != old_pid
    rival = next(candidate for candidate, note in results if note is None)
    with pytest.raises(RuntimeError, match="recovery required"):
        resume(rival)
    assert store.consume("question", owner) is None
    # Complete via the real surface hook without calling the completed external action again.
    if surface == "cli":
        cli_relay.finish(instance, {"completed": True, "final_response": "DONE BLUE"})
    else:
        tui_relay.finish(instance, "DONE BLUE")
    transport = Transport()
    asyncio.run(deliver_pending(store, transport))
    assert "DONE BLUE" in transport.messages["message"]
    assert "State: complete" in transport.messages["message"]
    assert store.reply("101", "", "101", "message", "BLUE") == "duplicate"
    assert store.reply("101", "", "101", "message", "GREEN") == "conflict"
    assert effect.read_text().splitlines() == ["effect completed"]
    assert resume(instance) == ""
    db.close()
