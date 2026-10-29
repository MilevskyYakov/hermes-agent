"""Recover a pinned workspace before building a turn, using the existing directory and question UI."""
from __future__ import annotations

from pathlib import Path

from agent.instruction_contract import _home, _validate, rebind_instruction_cwd
from tools.clarify_tool import TIMEOUT_RESPONSE


def ensure_instruction_workspace(agent, cwd, *, choose, move, cancelled):
    if getattr(agent, "skip_context_files", False):
        return True
    db = getattr(agent, "_session_db", None)
    snapshot = (db.get_session_model_config_value(agent.session_id, "_instruction_contract") if db
                else getattr(agent, "_instruction_contract_snapshot", None))
    if snapshot is None:
        return True
    if not isinstance(snapshot, dict):
        raise ValueError("Invalid instruction snapshot; refusing current files")
    home = _home(agent)
    _validate(snapshot, home, check_cwd=False)
    if snapshot.get("legacy"):
        return True
    current = Path(cwd).expanduser().resolve()
    if current.is_dir() and str(current) == snapshot["cwd"]:
        return True
    try:
        rebind_instruction_cwd(snapshot, home, current)
        return True  # The agent's normal turn lease will persist the checked metadata-only transition.
    except (ValueError, OSError):
        pass  # A path with different/unavailable rules needs user input, never silent adoption.
    warn = agent._emit_warning
    if choose is None or cancelled():
        warn("Request paused before model execution: select a directory matching this chat's saved project rules.")
        return False
    question = (
        "This chat's working directory changed, and the current project rules do not match its saved rules.\n"
        f"Saved directory: {snapshot['cwd']}\nCurrent directory: {current}\n"
        "Enter an existing directory with the same project rules (absolute path or ~/...). "
        "Leave blank to pause; use a new chat with a handoff for different project rules."
    )
    try:
        answer = choose(question, None)
        if cancelled() or not isinstance(answer, str) or not answer.strip() or answer == TIMEOUT_RESPONSE:
            warn("Workspace selection cancelled. No model call or task action started; send the request again when ready.")
            return False
        selected = Path(answer.strip()).expanduser()
        if not selected.is_absolute():
            raise ValueError("Enter an absolute directory path or ~/...")
        rebound = rebind_instruction_cwd(snapshot, home, selected)
        move(rebound["cwd"])
        return True
    except (ValueError, OSError) as exc:
        warn(f"Request paused before model execution: {exc}")
        return False
