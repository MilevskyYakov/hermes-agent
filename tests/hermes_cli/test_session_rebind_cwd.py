"""Explicit cwd recovery preserves pinned rules and the conversation, without model calls."""
from __future__ import annotations

import json
import os
import pytest

from agent.instruction_contract import persist_instructions, prepare_instructions
from agent.system_prompt import build_system_prompt
from hermes_state import SessionDB
from tests.agent.test_instruction_contract import _agent, _migration_home


def _command(monkeypatch, args):
    import hermes_cli.main as main
    monkeypatch.setattr("sys.argv", ["hermes", "sessions", "rebind-cwd", *args])
    try:
        main.main()
    except SystemExit as exc:
        return exc.code or 0
    return 0


def _setup(tmp_path, monkeypatch):
    home, _ = _migration_home(tmp_path, monkeypatch, ["shared-rule"], pin=True)
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
    db = SessionDB(home / "state.db")
    db.create_session("pinned-chat", source="tui", cwd=str(tmp_path / "project"),
                      model_config={"unrelated": "preserve"})
    agent, _ = _agent(home, "pinned-chat", db)
    prompt = build_system_prompt(agent)
    persist_instructions(agent)
    db.update_system_prompt("pinned-chat", prompt)
    db.append_message("pinned-chat", "user", "Keep my history.")
    db.append_message("pinned-chat", "assistant", "Saved reply.")
    target = tmp_path / "replacement"
    target.mkdir()
    (target / "AGENTS.md").write_text("Project rule: preserve local checks.")
    return home, db, target, prompt


@pytest.mark.parametrize("removed", [False, True])
def test_explicit_rebind_preserves_rules_history_and_resume(tmp_path, monkeypatch, capsys, removed):
    home, db, target, prompt = _setup(tmp_path, monkeypatch)
    try:
        before = db.get_session("pinned-chat")
        old_snapshot = db.get_session_model_config_value("pinned-chat", "_instruction_contract")
        history = db.get_messages("pinned-chat")
        if removed:
            (tmp_path / "project" / "AGENTS.md").unlink()
            (tmp_path / "project").rmdir()
        monkeypatch.setenv("TERMINAL_CWD", str(target))
        from agent.instruction_contract import _validate
        with pytest.raises(ValueError, match="rebind-cwd"):
            _validate(old_snapshot, home)
        # Preview and declined/noninteractive confirmation cannot change metadata.
        assert _command(monkeypatch, ["pinned-chat", "--cwd", str(target), "--dry-run"]) == 0
        assert db.get_session("pinned-chat")["model_config"] == before["model_config"]
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert _command(monkeypatch, ["pinned-chat", "--cwd", str(target)]) != 0
        assert db.get_session("pinned-chat")["cwd"] == before["cwd"]
        assert _command(monkeypatch, ["pinned-chat", "--cwd", str(target), "--yes"]) == 0
        after = db.get_session("pinned-chat")
        config = json.loads(after["model_config"])
        snapshot = config["_instruction_contract"]
        assert snapshot["cwd"] == str(target.resolve()) == after["cwd"]
        assert {k: v for k, v in snapshot.items() if k not in {"cwd", "sha256"}} == {
            k: v for k, v in old_snapshot.items() if k not in {"cwd", "sha256"}}
        assert config["unrelated"] == "preserve"
        assert after["system_prompt"] == before["system_prompt"]
        assert db.get_messages("pinned-chat") == history
        backups = list((home / "backups").glob("instruction-cwd-*.json"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text())["instruction_contract"] == old_snapshot
        assert backups[0].stat().st_mode & 0o077 == 0
        resumed, _ = _agent(home, "pinned-chat", db)
        prepare_instructions(resumed, continuing=True)
        from agent.conversation_loop import _restore_or_build_system_prompt
        _restore_or_build_system_prompt(resumed, "", history)
        # Native resume refreshes the environment header when cwd changes, not the pinned rule bytes.
        assert f"Current working directory: {target}" in resumed._cached_system_prompt
        assert snapshot["skills"][0] in resumed._cached_system_prompt
        assert snapshot["project"] in resumed._cached_system_prompt
        from agent.conversation_compression import _rebuild_system_prompt_at_boundary
        rebuilt = _rebuild_system_prompt_at_boundary(resumed, "")
        assert snapshot["skills"][0] in rebuilt and snapshot["project"] in rebuilt
        assert "resume" in capsys.readouterr().out.lower()
    finally:
        db.close()


@pytest.mark.parametrize("failure", [
    "rules", "missing", "corrupt", "legacy", "unknown", "busy", "race", "rules_race", "profile", "lease_lost",
])
def test_rebind_refuses_unsafe_or_unconfirmed_changes(tmp_path, monkeypatch, capsys, failure):
    home, db, target, _ = _setup(tmp_path, monkeypatch)
    holder = f"pid={os.getpid()}:turn=test"
    try:
        session_id = "pinned-chat"
        if failure == "rules":
            (target / "AGENTS.md").write_text("Different project: never deploy.")
        elif failure == "missing":
            target = target / "missing"
        elif failure in {"corrupt", "legacy", "profile"}:
            snapshot = db.get_session_model_config_value(session_id, "_instruction_contract")
            if failure == "corrupt":
                snapshot["skills"][0] += "corruption"
            elif failure == "legacy":
                snapshot = {"version": 2, "home": str(home), "legacy": True}
            else:
                snapshot["home"] = str(home / "foreign-profile")
            db.patch_session_model_config(session_id, {"_instruction_contract": snapshot})
        elif failure == "unknown":
            session_id = "not-a-session"
        elif failure == "busy":
            assert db.try_acquire_session_turn_lease(session_id, holder)
        elif failure == "race":
            def confirm(_):
                db.patch_session_model_config(session_id, {"concurrent": "keep"})
                return "yes"
            monkeypatch.setattr("builtins.input", confirm)
        elif failure == "rules_race":
            def confirm(_):
                (target / "AGENTS.md").write_text("Changed while waiting for confirmation.")
                return "yes"
            monkeypatch.setattr("builtins.input", confirm)
        elif failure == "lease_lost":
            import hermes_cli.session_rebind_cwd as recovery
            original = recovery.tempfile.mkstemp
            def lose_lease(**kwargs):
                db._execute_write(lambda conn: conn.execute("DELETE FROM session_turn_leases"))
                return original(**kwargs)
            monkeypatch.setattr(recovery.tempfile, "mkstemp", lose_lease)
        before = db.get_session("pinned-chat")
        args = [session_id, "--cwd", str(target)]
        if failure not in {"race", "rules_race"}:
            args += ["--yes"]
        assert _command(monkeypatch, args) != 0
        reason = {
            "rules": "project rules differ", "missing": "not a directory", "corrupt": "checksum",
            "legacy": "verified instruction snapshot", "unknown": "not found", "busy": "busy",
            "race": "metadata changed",
            "rules_race": "project rules differ", "profile": "profile mismatch", "lease_lost": "lease was lost",
        }[failure]
        assert reason in capsys.readouterr().out
        after = db.get_session("pinned-chat")
        assert after["cwd"] == before["cwd"]
        if failure == "race":
            assert json.loads(after["model_config"])["concurrent"] == "keep"
            assert db.get_session_model_config_value("pinned-chat", "_instruction_contract") == json.loads(
                before["model_config"])["_instruction_contract"]
        else:
            assert after["model_config"] == before["model_config"]
    finally:
        db.release_session_turn_lease("pinned-chat", holder)
        db.close()
