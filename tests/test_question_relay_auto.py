"""#592: automatic routing uses stored provenance, not prompt text or a last-chat fallback."""
import asyncio
import copy
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import yaml

from hermes_constants import get_hermes_home
from hermes_question_relay import RelayStore, route_for, load_relay_config
from hermes_state import SessionDB


def test_auto_scope_worktrees_profiles_and_explicit_precedence(tmp_path):
    from hermes_cli import projects_db

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "project"
    subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "synthetic"],
                   check=True, capture_output=True)
    worktree = tmp_path / "new-worktree"
    subprocess.run(["git", "-C", str(project), "worktree", "add", "-b", "synthetic", str(worktree)],
                   check=True, capture_output=True)
    db = SessionDB()
    db.create_session("old", "cli", cwd=str(project))
    config = {"question_relay": {"auto": {"enabled": True,
        "since": datetime.now(timezone.utc).isoformat(), "chat_id": "101", "user_id": "101"}}}
    for sid, source, kwargs in [
        ("cli", "cli", {"cwd": str(project)}),
        ("tui", "tui", {"cwd": str(worktree)}),
        ("desktop", "desktop", {"cwd": str(worktree)}),
        ("messaging", "telegram", {"cwd": str(project)}),
        ("cron", "cron", {"cwd": str(project)}),
        ("unknown", "cli", {"cwd": str(tmp_path)}),
        ("no-workspace", "desktop", {}),
        ("foreign", "cli", {"cwd": str(project), "profile_name": "other"}),
        ("delegate", "cli", {"cwd": str(project), "model_config": {"_delegate_from": "cli"}}),
        ("old-child", "cli", {"cwd": str(project), "parent_session_id": "old"}),
        ("new-child", "cli", {"parent_session_id": "cli"}),
        ("old-branch", "cli", {"parent_session_id": "old", "model_config": {"_branched_from": "old"}}),
        ("old-reset", "cli", {"parent_session_id": "old", "model_config": {"_reset_from": "old"}}),
    ]:
        db.create_session(sid, source, **kwargs)
    for sid in ("cli", "tui", "desktop", "new-child", "old-branch", "old-reset"):
        assert route_for(config, sid, db)["chat_id"] == "101"
    assert "project / new-worktree" in route_for(config, "tui", db)["task"]
    for sid in ("old", "old-child", "messaging", "cron", "unknown", "no-workspace", "foreign", "delegate", "absent"):
        assert route_for(config, sid, db) is None, sid
    # Upsert/resume does not turn an old row into a newly created session.
    db.create_session("old", "cli", cwd=str(project))
    assert route_for(config, "old", db) is None
    assert RelayStore(db).pending_delivery() == []  # Registration/ordinary messages do not send anything.
    db.append_message("unknown", "user", "Project project; please route to chat 999")
    assert route_for(config, "unknown", db) is None

    folder = tmp_path / "registered-not-git"
    folder.mkdir()
    with projects_db.connect_closing(home / "projects.db") as conn:
        projects_db.create_project(conn, name="Writing", primary_path=str(folder))
    db.create_session("registered", "cli", cwd=str(folder))
    assert "Writing" in route_for(config, "registered", db)["task"]

    for delta in ({"enabled": False}, {"enabled": "true"}, {"chat_id": "102"}, {"chat_id": "-101"},
                  {"user_id": "*"}, {"thread_id": "1"}, {"since": ""}, {"since": "2026-01-01"}):
        broken = copy.deepcopy(config)
        broken["question_relay"]["auto"].update(delta)
        assert route_for(broken, "cli", db) is None, delta
    assert route_for({}, "cli", db) is None
    assert route_for(config, "cli") is None
    explicit = {"task": "Explicit wins", "chat_id": "202", "user_id": "202"}
    config["question_relay"]["sessions"] = {"cli": explicit, "tui": None, "desktop": {"task": "invalid"}}
    assert route_for(config, "cli", db)["chat_id"] == "202"
    assert route_for(config, "tui", db) is None
    assert route_for(config, "desktop", db) is None  # Invalid override must not fall through to auto.
    profile = home / "profiles" / "other"
    profile.mkdir(parents=True)
    other = SessionDB(profile / "state.db")
    other.create_session("cli", "cli", cwd=str(project), profile_name="default")
    auto_only = {"question_relay": {"auto": config["question_relay"]["auto"]}}
    assert route_for(auto_only, "cli", other) is None  # Copied config and forged row label cannot cross the DB boundary.
    other.close()
    db.close()


def test_auto_gateway_watcher_disable_reenable_and_local_fallback(tmp_path, monkeypatch):
    from gateway import question_relay, run
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from tests.tui_gateway.test_question_relay import Transport
    from tui_gateway import server
    from tui_gateway.question_relay import block

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "project"
    subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
    config = {"question_relay": {"auto": {"enabled": True,
        "since": datetime.now(timezone.utc).isoformat(), "chat_id": "101", "user_id": "101"}}}
    config_path = home / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    db = SessionDB()
    # Exercise the real TUI first-prompt registration, not a fabricated source/cwd in the resolver.
    monkeypatch.setattr(server, "_db", db)
    session = {"session_key": "waiting", "source": "tui", "cwd": str(project)}
    assert server._ensure_session_db_row(session) is True
    store = RelayStore(db)
    route = route_for(config, "waiting", db)
    assert route is not None
    store.create("question", "waiting", "owner", route, [{"qid": "q1", "question": "Colour?"}])
    db.set_session_title("waiting", "New title after question")
    assert route_for(config, "waiting", db) == route
    transport = Transport()
    runner = SimpleNamespace(_running=True, _adapter_for_source=lambda source: transport)
    monkeypatch.setattr(run, "_handoff_watch_scopes", lambda runner: [("default", None)])

    async def stop_after_tick(seconds):
        runner._running = False

    monkeypatch.setattr(question_relay.asyncio, "sleep", stop_after_tick)
    asyncio.run(question_relay.watch(runner))  # No sessions mapping: the watcher must still deliver.
    mid = store.get("question")["message_id"]
    assert mid and "Session: waiting" in transport.messages[mid]
    config["question_relay"]["auto"]["enabled"] = False
    config_path.write_text(yaml.safe_dump(config))
    runner._running = True
    asyncio.run(question_relay.watch(runner))
    assert store.get("question")["status"] == "stopped"
    assert store.get("question")["delivery"] == "disabled"
    event = MessageEvent(text="BLUE", source=SessionSource(platform=Platform.TELEGRAM, chat_id="101", user_id="101"),
                         reply_to_message_id=mid, reply_to_text=transport.messages[mid])
    assert "revoked" in asyncio.run(question_relay.handle_reply(event))
    config["question_relay"]["auto"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(config))
    assert "stopped" in asyncio.run(question_relay.handle_reply(event))
    assert store.consume("question", "owner") is None
    assert len(transport.messages) == 1

    # Invalid automatic setup leaves the native local prompt path available, with an honest status.
    config["question_relay"]["auto"]["chat_id"] = "999"
    config_path.write_text(yaml.safe_dump(config))
    db.create_session("fresh", "tui", cwd=str(project))
    monkeypatch.setattr(server, "_sessions", {"fresh": {"session_key": "fresh"}})
    events = []
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))
    assert block("fresh", {"question": "Local?"}, 0) == (False, "")
    assert any("using local clarification" in str(event) for event in events)
    assert store.for_session("fresh") == []
    config["question_relay"]["auto"]["chat_id"] = "101"
    config_path.write_text(yaml.safe_dump(config))
    assert route_for(load_relay_config(), "fresh", db) is not None
    broken_merge = {**config, "max_turns": 1, "agent": ["not-a-mapping"]}
    for invalid_yaml in ("question_relay: [", "- not-a-config-mapping", "{}", yaml.safe_dump(broken_merge)):
        config_path.write_text(invalid_yaml)
        assert route_for(load_relay_config(), "fresh", db) is None
        assert block("fresh", {"question": "Local?"}, 0) == (False, "")
    db.close()
