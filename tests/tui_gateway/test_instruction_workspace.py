"""Removed worktrees preserve pinned rules or request a directory before any model call."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from agent.instruction_contract import persist_instructions, prepare_instructions
from agent.runtime_cwd import _SESSION_CWD
from hermes_state import SessionDB
from tests.agent.test_instruction_contract import _agent, _migration_home
import tui_gateway.server as server


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _pinned_worktree(tmp_path, monkeypatch, *, nested):
    home, _ = _migration_home(tmp_path, monkeypatch, ["shared-rule"], pin=True)
    repo = tmp_path / "project"
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "AGENTS.md")
    _git(repo, "commit", "-m", "initial rules")
    worktree = (repo if nested else tmp_path) / "removed-worktree"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree))
    monkeypatch.setenv("TERMINAL_CWD", str(worktree))
    db = SessionDB(home / "state.db")
    db.create_session("workspace-chat", source="tui", cwd=str(worktree))
    agent, notices = _agent(home, "workspace-chat", db)
    prompt = agent._build_system_prompt()
    agent._cached_system_prompt = prompt
    persist_instructions(agent)
    db.update_system_prompt("workspace-chat", prompt)
    db.append_message("workspace-chat", "user", "Continue my task.")
    db.append_message("workspace-chat", "assistant", "Previous reply.")
    session = {"session_key": "workspace-chat", "cwd": str(worktree), "source": "tui",
               "agent": agent, "history": db.get_messages("workspace-chat"), "history_lock": threading.RLock()}
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_persist_session_cwd_and_schedule_git_meta",
                        lambda sess, cwd: db.update_session_cwd(sess["session_key"], cwd))
    monkeypatch.setattr(server, "_persist_session_git_meta", lambda *args: None)
    monkeypatch.setattr(server, "_is_local_terminal_backend", lambda: True)
    monkeypatch.setattr(server, "_sessions", {"ui-chat": session})
    from tui_gateway import git_probe
    git_probe.invalidate()
    _git(repo, "worktree", "remove", str(worktree))
    return home, repo, worktree, db, agent, notices, session, prompt


@pytest.mark.parametrize("restart", [False, True])
def test_deleted_worktree_continues_automatically_without_replacing_rules(tmp_path, monkeypatch, restart):
    home, repo, worktree, db, agent, notices, session, prompt = _pinned_worktree(tmp_path, monkeypatch, nested=True)
    try:
        before = db.get_session_model_config_value("workspace-chat", "_instruction_contract")
        history = db.get_messages("workspace-chat")
        healed = server._display_session_cwd(session)
        assert Path(healed).resolve() == repo.resolve()
        monkeypatch.setenv("TERMINAL_CWD", healed)
        if restart:
            agent, notices = _agent(home, "workspace-chat", db)
        prepare_instructions(agent, continuing=True)
        persist_instructions(agent)
        after = db.get_session_model_config_value("workspace-chat", "_instruction_contract")
        assert after["cwd"] == str(repo.resolve())
        assert after["skills"] == before["skills"] and after["project"] == before["project"]
        assert db.get_session("workspace-chat")["system_prompt"] == prompt
        assert db.get_messages("workspace-chat") == history
        assert any("directory" in notice.lower() and str(worktree) in notice for notice in notices)
        notices.clear()
        prepare_instructions(agent, continuing=True)
        assert not notices  # A successful transition must not prompt/warn on every turn.
        from agent.conversation_compression import _rebuild_system_prompt_at_boundary
        rebuilt = _rebuild_system_prompt_at_boundary(agent, "")
        assert before["skills"][0] in rebuilt and before["project"] in rebuilt
    finally:
        db.close()


@pytest.mark.parametrize("answer", ["valid", "cancel", "timeout", "different_rules"])
def test_directory_selection_precedes_turn_preparation_and_cancellation_is_safe(tmp_path, monkeypatch, answer):
    home, repo, worktree, db, agent, notices, session, _ = _pinned_worktree(tmp_path, monkeypatch, nested=False)
    token = _SESSION_CWD.set("")
    state = None
    try:
        snapshot = db.get_session_model_config_value("workspace-chat", "_instruction_contract")
        history = db.get_messages("workspace-chat")
        healed = server._display_session_cwd(session)
        assert Path(healed).resolve() != repo.resolve()  # The old bug's existing-but-ruleless parent.
        monkeypatch.setenv("TERMINAL_CWD", healed)
        other = tmp_path / "different-project"
        other.mkdir()
        (other / "AGENTS.md").write_text("Different project: never deploy.")
        from tools.clarify_tool import TIMEOUT_RESPONSE
        reply = {"valid": str(repo), "cancel": "", "timeout": TIMEOUT_RESPONSE,
                 "different_rules": str(other)}[answer]
        questions = []
        def choose(question, choices):
            questions.append((question, choices))
            return reply
        agent.clarify_callback = choose
        for name in ("_wire_callbacks", "_apply_pending_model_switch", "_sync_agent_model_with_config",
                     "_sync_agent_compression_with_config", "_sync_bot_capabilities"):
            monkeypatch.setattr(server, name, lambda *args: None)
        monkeypatch.setattr(server, "_start_turn_voice", lambda: (None, False))
        monkeypatch.setattr(server, "_pending_reaction_notes", lambda *args: "")
        monkeypatch.setattr(server, "_hud_surface_note", lambda *args: "")
        events = []
        monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: events.append((event, payload)))
        state = server._TurnRun(agent, None, None, True)
        prepared = server._prepare_turn_input("ui-chat", session, state, "Continue my task.", [])
        assert len(questions) == 1
        assert str(worktree) in questions[0][0]
        if answer == "valid":
            assert prepared is not None
            assert session["cwd"] == str(repo.resolve())
            from agent.runtime_cwd import resolve_context_cwd
            assert Path(resolve_context_cwd()).resolve() == repo.resolve()
            prepare_instructions(agent, continuing=True)
            persist_instructions(agent)
            assert db.get_session_model_config_value("workspace-chat", "_instruction_contract")["cwd"] == str(repo.resolve())
        else:
            assert prepared is None  # _run_prompt_submit cannot invoke the model on this branch.
            assert state.result["interrupted"] is True
            assert any(event == "message.complete" and data["status"] == "interrupted" for event, data in events)
            assert not any(event == "error" for event, _ in events)
            assert session["cwd"] == healed
            assert db.get_session_model_config_value("workspace-chat", "_instruction_contract") == snapshot
        assert db.get_messages("workspace-chat") == history
    finally:
        if state is not None:
            server._finish_turn("ui-chat", session, state)
        _SESSION_CWD.reset(token)
        db.close()
