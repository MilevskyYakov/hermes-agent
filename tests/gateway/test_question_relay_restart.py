"""Restart the real relay transport worker against a local durable mailbox, never Telegram."""
import json
import subprocess
import sys
from datetime import datetime, timezone

import pytest
import yaml

from hermes_constants import get_hermes_home
from hermes_state_registry import acquire
from hermes_question_relay import RelayStore, route_for


@pytest.mark.parametrize("failure", ["offline", "uncertain_send", "uncertain_edit"])
@pytest.mark.parametrize("automatic", [False, True])
def test_gateway_loss_never_resends_or_invents_execution(tmp_path, failure, automatic):
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "project"
    subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
    config = {"question_relay": {"sessions": {"original": {"task": "restart", "chat_id": "101", "user_id": "101"}}}}
    if automatic:
        config = {"question_relay": {"auto": {"enabled": True,
            "since": datetime.now(timezone.utc).isoformat(), "chat_id": "101", "user_id": "101"}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    db = acquire()
    db.create_session("original", "cli", cwd=str(project))
    store = RelayStore(db)
    store.create("question", "original", "owner", route_for(config, "original", db),
                 [{"qid": "q1", "question": "Colour?", "choices": ["BLUE", "GREEN"]}])
    mailbox = tmp_path / "mailbox.json"
    mailbox.write_text(json.dumps({"sends": 0, "edits": 0}))
    worker = r'''
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from hermes_state import SessionDB
from hermes_question_relay import RelayStore
from hermes_cli.config import load_config_readonly
from gateway.question_relay import deliver_pending
path, mode = Path(sys.argv[1]), sys.argv[2]
class Transport:
    async def send(self, **kwargs):
        box = json.loads(path.read_text())
        box.update(sends=box['sends'] + 1, content=kwargs['content'])
        path.write_text(json.dumps(box))
        if mode == 'uncertain_send':
            os._exit(23)
        return SimpleNamespace(success=True, message_id='card')
    async def edit_message(self, **kwargs):
        assert kwargs['message_id'] == 'card'
        box = json.loads(path.read_text())
        box.update(edits=box['edits'] + 1, content=kwargs['content'])
        path.write_text(json.dumps(box))
        if mode == 'uncertain_edit':
            raise TimeoutError('reply lost after edit')
        return SimpleNamespace(success=True)
if mode == 'offline':
    os._exit(23)
db = SessionDB()
asyncio.run(deliver_pending(RelayStore(db), Transport(), load_config_readonly()))
db.close()
'''
    def run(mode):
        result = subprocess.run([sys.executable, "-c", worker, str(mailbox), mode], capture_output=True, text=True, timeout=30)
        assert result.returncode == (23 if mode in {"offline", "uncertain_send"} else 0), result.stderr

    if failure == "offline":
        run("offline")
        assert store.get("question")["delivery"] == "queued"
        assert json.loads(mailbox.read_text())["sends"] == 0
        assert store.consume("question", "owner") is None
        run("normal")
    elif failure == "uncertain_send":
        run("uncertain_send")
        assert store.get("question")["delivery"] == "sending"
        run("normal")
        assert json.loads(mailbox.read_text())["sends"] == 1
        assert store.get("question")["message_id"] is None
        assert store.reply("101", "", "101", "card", "BLUE") == "unknown"
        assert store.consume("question", "owner") is None
        # Local input still works; uncertain remote delivery is never relabelled successful.
        assert store.answer("question", "q1", "BLUE") == "accepted"
        assert store.consume("question", "owner") == {"q1": "BLUE"}
        store.finish("original", "DONE BLUE")
        run("normal")
        assert "DONE BLUE" not in json.loads(mailbox.read_text())["content"]
        assert store.get("question")["delivered_revision"] < store.get("question")["revision"]
        db.close()
        return
    else:
        run("normal")
    assert store.reply("101", "", "101", "card", "BLUE") == "accepted"
    assert store.consume("question", "owner") == {"q1": "BLUE"}
    assert store.consume("question", "owner") is None
    store.finish("original", "DONE BLUE")
    if failure == "uncertain_edit":
        run("uncertain_edit")
        assert store.get("question")["delivered_revision"] < store.get("question")["revision"]
    run("normal")
    box = json.loads(mailbox.read_text())
    assert box["sends"] == 1
    assert "DONE BLUE" in box["content"] and "State: complete" in box["content"]
    assert store.get("question")["delivered_revision"] == store.get("question")["revision"]
    assert store.reply("101", "", "101", "card", "BLUE") == "duplicate"
    assert store.reply("101", "", "101", "card", "GREEN") == "conflict"
    db.close()
