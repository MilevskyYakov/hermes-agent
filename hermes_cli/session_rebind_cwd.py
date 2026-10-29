"""Explicit recovery of pinned chat metadata after a directory/worktree move."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from agent.instruction_contract import rebind_instruction_cwd


def sessions_rebind_cwd(db, args):
    from hermes_cli.sessions_cmd import _confirm_prompt

    session_id = args.session_id
    row = db.get_session(session_id)
    if row is None:
        print(f"Session '{session_id}' not found. Use the exact stored ID, not the TUI tab ID.")
        return 1
    home = Path(db.db_path).parent.resolve()
    try:
        config = json.loads(row.get("model_config") or "{}")
        if not isinstance(config, dict):
            raise ValueError("Session model_config must be an object; inspect it before recovery")
        snapshot = config.get("_instruction_contract")
        rebound = rebind_instruction_cwd(snapshot, home, args.cwd)
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}")
        return 1
    target = rebound["cwd"]
    print(f"Pinned chat: {session_id}\nSaved directory: {snapshot['cwd']}\nTarget directory: {target}")
    print("Target project rules match exactly. Saved rules, messages and system prompt will not change.")
    if getattr(args, "dry_run", False):
        print("Dry run: no metadata changed.")
        return 0
    if not getattr(args, "yes", False) and not _confirm_prompt(
        "Close the affected chat before applying. Rebind its directory? [y/N] "
    ):
        print("Aborted: no metadata changed.")
        return 1
    holder = f"pid={os.getpid()}:rebind-cwd={uuid.uuid4().hex}"
    if not db.try_acquire_session_turn_lease(session_id, holder):
        print("Error: session is busy. Finish/stop the turn and close the chat before retrying.")
        return 1
    try:
        # Confirmation may take arbitrarily long: recheck both metadata and files under the turn lease.
        latest = db.get_session(session_id)
        if latest is None or any(latest[k] != row[k] for k in ("cwd", "model_config")):
            raise ValueError("Session metadata changed during confirmation; nothing was rebound")
        rebound = rebind_instruction_cwd(snapshot, home, args.cwd)
        if rebound["cwd"] != target:
            raise ValueError("Target directory changed during confirmation; nothing was rebound")
        backup_dir = home / "backups"
        backup_dir.mkdir(exist_ok=True)
        fd, backup = tempfile.mkstemp(prefix="instruction-cwd-", suffix=".json", dir=backup_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"session_id": session_id, "cwd": row["cwd"], "instruction_contract": snapshot},
                      stream, ensure_ascii=False, indent=2)
        config["_instruction_contract"] = rebound

        def update(conn):
            lease_key = db._session_turn_lease_key_on_conn(conn, session_id)
            return conn.execute(
                "UPDATE sessions SET cwd = ?, model_config = ?, git_branch = NULL, git_repo_root = NULL, "
                "git_metadata_generation = COALESCE(git_metadata_generation, 0) + 1 "
                "WHERE id = ? AND model_config IS ? AND cwd IS ? "
                "AND EXISTS (SELECT 1 FROM session_turn_leases "
                "WHERE conversation_id = ? AND holder = ? AND expires_at > ?)",
                (target, json.dumps(config, ensure_ascii=False), session_id, row["model_config"], row["cwd"],
                 lease_key, holder, time.time()),
            ).rowcount == 1

        if not db._execute_write(update):
            raise ValueError("Session metadata changed or turn lease was lost before commit; nothing was rebound")
        actual = db.get_session(session_id)
        if actual is None or actual["cwd"] != target or json.loads(actual["model_config"]) != config:
            raise RuntimeError("Cwd rebind could not be verified; do not resume until inspected")
        print(f"Rebound directory. Original metadata backup: {backup}")
        print("Resume the closed chat from the target directory. No running process was restarted.")
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Error: {exc}")
        return 1
    finally:
        db.release_session_turn_lease(session_id, holder)
