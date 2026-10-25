"""Opt-in, profile-owned instructions pinned across turns, resume and compaction.

Project discovery remains native. Only sessions started with instructions.file acquire
this contract; legacy sessions are reported as unknown, never retroactively stamped.
Snapshots use existing session metadata, not a second state store or prompt parser.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from hermes_constants import (
    get_hermes_home, reset_hermes_home_override, set_hermes_home_override,
)

_KEY = "_instruction_contract"


def _home(agent) -> Path:
    from agent.system_prompt import _agent_home
    return (_agent_home(agent) or get_hermes_home()).resolve()


def _configured_source(home: Path) -> Path | None:
    from hermes_cli.config import load_config_readonly
    token = set_hermes_home_override(home)
    try:
        settings = load_config_readonly().get("instructions", {})
    finally:
        reset_hermes_home_override(token)
    if not isinstance(settings, dict):
        raise ValueError("instructions must be a mapping")
    value = settings.get("file", "")
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("instructions.file must be a non-empty path string")
    path = Path(value).expanduser()
    # Keep the symlink spelling: an approved cutover may change its target.
    return path if path.is_absolute() else home / path


def _read_source(path: Path, home: Path, context_length=None) -> tuple[str, str]:
    from agent.prompt_builder import (
        _get_context_file_max_chars, _read_text_with_timeout, _scan_context_content,
    )
    token = set_hermes_home_override(home)
    try:
        budget = _get_context_file_max_chars(context_length)
        try:
            text = _read_text_with_timeout(path)
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Cannot read instructions.file ({type(exc).__name__}); no new contract loaded") from exc
    finally:
        reset_hermes_home_override(token)
    if text is None or not text.strip():
        raise ValueError("instructions.file is empty or timed out; no new contract loaded")
    text = text.removeprefix("\ufeff")
    if len(text) > budget:
        raise ValueError("instructions.file exceeds the context-file budget; refusing to truncate the contract")
    if _scan_context_content(text, "instructions.file") != text:
        raise ValueError("instructions.file was blocked by the context security scanner")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_snapshot(value: dict, home: Path) -> None:
    if value.get("version") != 1 or value.get("home") != str(home):
        raise ValueError("Instruction snapshot version/profile mismatch; start a new chat")
    if value.get("legacy") is True:
        return
    if not all(isinstance(value.get(k), str) for k in ("source", "text", "sha256", "project")):
        raise ValueError("Invalid instruction snapshot; refusing to replace it with current files")
    if hashlib.sha256(value["text"].encode("utf-8")).hexdigest() != value["sha256"]:
        raise ValueError("Instruction snapshot checksum mismatch; start a new chat")


def prepare_instructions(agent, *, continuing: bool = False) -> None:
    """Recover metadata before any prompt rebuild, then notify without changing messages."""
    if getattr(agent, "skip_context_files", False):
        return
    home = _home(agent)
    session_id = getattr(agent, "session_id", "")
    bound = getattr(agent, "_instruction_contract_session", session_id)
    if isinstance(bound, str) and bound != session_id:
        agent._instruction_contract_snapshot = None
        agent._instruction_contract_dirty = False
    agent._instruction_contract_session = session_id
    snapshot = getattr(agent, "_instruction_contract_snapshot", None)
    if not isinstance(snapshot, dict):
        db = getattr(agent, "_session_db", None)
        checked = getattr(agent, "_instruction_contract_checked", None) == session_id
        stored = db.get_session_model_config_value(session_id, _KEY) if db and not checked else None
        if isinstance(stored, dict):
            _validate_snapshot(stored, home)
            snapshot = dict(stored)
            agent._instruction_contract_snapshot = snapshot
        elif stored is not None:
            raise ValueError("Invalid instruction snapshot; refusing to load current files")
        elif _configured_source(home) and continuing:
            snapshot = {"version": 1, "home": str(home), "legacy": True}
            agent._instruction_contract_snapshot = snapshot
        agent._instruction_contract_checked = session_id
    if isinstance(snapshot, dict):
        _validate_snapshot(snapshot, home)
        _warn_if_changed(agent, snapshot, home)


def _warn_if_changed(agent, snapshot: dict, home: Path) -> None:
    if snapshot.get("legacy"):
        key = "unknown"
        message = (
            "Instruction version unknown for this existing chat. No new global contract was injected. "
            "Its existing project-context behavior is unchanged; use a new chat with a handoff "
            "to enable pinned instructions."
        )
    else:
        try:
            source = _configured_source(home)
            if source is None:
                key = "disabled"
            else:
                _, digest = _read_source(source, home, getattr(getattr(agent, "context_compressor", None), "context_length", None))
                key = str(source) + ":" + digest
                if str(source) == snapshot["source"] and digest == snapshot["sha256"]:
                    if "warned" in snapshot:
                        snapshot.pop("warned")
                        agent._instruction_contract_dirty = True
                    return
        except ValueError:
            key = "unreadable"
        message = (
            "Global instructions changed or are unavailable. This chat keeps its saved contract "
            "through resume and compaction. Start a new chat with a handoff to adopt the update."
        )
    if snapshot.get("warned") != key:
        agent._emit_warning(message)
        snapshot["warned"] = key
        agent._instruction_contract_dirty = True


def instruction_context(agent, build_project, context_length=None) -> list[str]:
    """Compose global + local instructions; cache only this managed slice, not memory/tools."""
    if agent.skip_context_files:
        return []
    prepare_instructions(agent)
    snapshot = getattr(agent, "_instruction_contract_snapshot", None)
    if isinstance(snapshot, dict) and snapshot.get("legacy"):
        return build_project()
    if not isinstance(snapshot, dict):
        home = _home(agent)
        source = _configured_source(home)
        if source is None:
            return build_project()
        text, digest = _read_source(source, home, context_length)
        snapshot = {
            "version": 1, "home": str(home), "source": str(source),
            "text": text, "sha256": digest, "project": "\n\n".join(build_project()),
        }
        agent._instruction_contract_snapshot = snapshot
        agent._instruction_contract_dirty = True
    block = (
        "# Global instruction contract\n\n"
        f"Source: {snapshot['source']}\nSHA-256: {snapshot['sha256']}\n\n"
        + snapshot["text"]
    )
    return [block, snapshot["project"]]


def persist_instructions(agent) -> None:
    """Call after the session row exists, before any model call/compaction can advance it."""
    if getattr(agent, "_instruction_contract_dirty", False) is not True:
        return
    db = getattr(agent, "_session_db", None)
    if db is None:
        return  # In-memory callers have no resume guarantee; live surfaces provide SessionDB.
    snapshot = agent._instruction_contract_snapshot
    db.patch_session_model_config(agent.session_id, {_KEY: snapshot})
    if db.get_session_model_config_value(agent.session_id, _KEY) != snapshot:
        raise RuntimeError("Instruction snapshot was not persisted; refusing to continue")
    agent._instruction_contract_dirty = False
