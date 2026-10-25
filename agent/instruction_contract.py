"""Opt-in version pinning around native skills.auto_load and project discovery.

No separate instruction loader or state store. Memory/tools keep their native lifecycle.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

_KEY = "_instruction_contract"
_PAYLOAD = ("version", "home", "cwd", "skills", "project", "sources", "soul_loaded")


def _home(agent) -> Path:
    from agent.system_prompt import _agent_home
    return (_agent_home(agent) or get_hermes_home()).resolve()


@contextmanager
def _profile(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _pin_enabled(home) -> bool:
    from hermes_cli.config import load_config_readonly
    with _profile(home):
        settings = load_config_readonly().get("instructions", {})
    if not isinstance(settings, dict) or not isinstance(settings.get("pin", False), bool):
        raise ValueError("instructions.pin must be a boolean")
    if settings.get("file"):
        raise ValueError("instructions.file was replaced by skills.auto_load with instructions.pin")
    return settings.get("pin", False)


def _cwd() -> str:
    from agent.runtime_cwd import resolve_context_cwd
    return str(Path(resolve_context_cwd() or os.getcwd()).resolve())


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _checksum(snapshot) -> str:
    return _hash({key: snapshot[key] for key in _PAYLOAD})


def _sources(agent, home) -> str:
    """Validate required native skill payloads without executing their preprocessing.

    Ordinary auto_load is best-effort. Pinning explicitly makes its configured skills
    mandatory: do not bless a skipped, blocked, empty or truncated contract.
    """
    from agent.prompt_builder import _get_context_file_max_chars, _scan_context_content
    from agent.skill_commands import _disabled_skill_names, _load_skill_payload, resolve_auto_load_skills
    from agent.skill_utils import parse_frontmatter
    from hermes_cli.config import load_config_readonly
    from utils import is_truthy_value

    with _profile(home):
        config = load_config_readonly()
        names = resolve_auto_load_skills(config)
        if not names or is_truthy_value(os.environ.get("HERMES_IGNORE_RULES")):
            raise ValueError("Pinned contract requires non-empty, enabled skills.auto_load")
        disabled = _disabled_skill_names()
        budget = _get_context_file_max_chars(getattr(getattr(agent, "context_compressor", None), "context_length", None))
        sources = []
        for name in names:
            loaded = _load_skill_payload(name)
            if not loaded or name in disabled or loaded[2] in disabled:
                raise ValueError("Required auto_load contract is unavailable or disabled")
            payload, directory, canonical = loaded
            content = payload.get("content", "")
            _, body = parse_frontmatter(content)
            if not body.strip() or len(content) > budget:
                raise ValueError("Required auto_load contract is empty or exceeds the context budget")
            if _scan_context_content(content, "auto_load contract") != content:
                raise ValueError("Required auto_load contract was blocked by the security scanner")
            sources.append((canonical, str(directory), _hash(content)))
        return _hash([sources, config.get("skills", {})])


def _validate(snapshot, home):
    if snapshot.get("version") != 2 or snapshot.get("home") != str(home):
        raise ValueError("Instruction snapshot version/profile mismatch; start a new chat")
    if snapshot.get("legacy") is True:
        if "legacy_skills" in snapshot and (
            not isinstance(snapshot["legacy_skills"], str)
            or _hash(snapshot["legacy_skills"]) != snapshot.get("legacy_skills_sha256")
        ):
            raise ValueError("Legacy skill snapshot checksum/shape mismatch; start a new chat")
        return
    try:
        valid = (
            all(isinstance(snapshot[k], str) for k in ("cwd", "project", "sources"))
            and isinstance(snapshot["soul_loaded"], bool)
            and isinstance(snapshot["skills"], list) and len(snapshot["skills"]) == 3
            and isinstance(snapshot["skills"][0], str)
            and all(isinstance(xs, list) and all(isinstance(x, str) for x in xs) for xs in snapshot["skills"][1:])
            and _checksum(snapshot) == snapshot.get("sha256")
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Instruction snapshot checksum/shape mismatch; start a new chat")
    if snapshot["cwd"] != _cwd():
        raise ValueError("Project changed for a pinned chat; start a new chat with a handoff")


def prepare_instructions(agent, *, continuing: bool = False) -> None:
    if getattr(agent, "skip_context_files", False):
        return
    home = _home(agent)
    session_id = getattr(agent, "session_id", "")
    if getattr(agent, "_instruction_contract_session", session_id) != session_id:
        agent._instruction_contract_snapshot = None
        agent._instruction_contract_dirty = False
        agent._auto_load_skills_resolved = False
    agent._instruction_contract_session = session_id
    snapshot = getattr(agent, "_instruction_contract_snapshot", None)
    if snapshot is None and getattr(agent, "_instruction_contract_checked", None) != session_id:
        db = getattr(agent, "_session_db", None)
        snapshot = db.get_session_model_config_value(session_id, _KEY) if db else None
        if snapshot is not None and not isinstance(snapshot, dict):
            raise ValueError("Invalid instruction snapshot; refusing current files")
        agent._instruction_contract_snapshot = snapshot
        agent._instruction_contract_checked = session_id
    # Enabling pinning in an already-running unpinned chat is still a legacy case.
    # The DB lookup cache must not turn a later config change into a silent adoption.
    if snapshot is None and continuing and _pin_enabled(home):
        snapshot = {"version": 2, "home": str(home), "legacy": True}
        agent._instruction_contract_snapshot = snapshot
        agent._instruction_contract_dirty = True
    if snapshot is not None:
        _validate(snapshot, home)
        if snapshot.get("legacy") and "legacy_skills" not in snapshot:
            from agent.system_prompt import recover_legacy_auto_load
            prompt = getattr(agent, "_cached_system_prompt", None)
            db = getattr(agent, "_session_db", None)
            if prompt is None and db is not None:
                prompt = (db.get_session(session_id) or {}).get("system_prompt")
            if isinstance(prompt, str):
                snapshot["legacy_skills"] = recover_legacy_auto_load(prompt)
                snapshot["legacy_skills_sha256"] = _hash(snapshot["legacy_skills"])
                agent._instruction_contract_dirty = True
        _warn_if_changed(agent, snapshot, home)


def _project(agent, soul_loaded):
    from agent.system_prompt import _context_files_part
    length = getattr(getattr(agent, "context_compressor", None), "context_length", None)
    return "\n\n".join(_context_files_part(agent, length, soul_loaded))


def _warn_if_changed(agent, snapshot, home):
    if snapshot.get("legacy"):
        key = "unknown"
        message = "Instruction version unknown for this existing chat. Use a new chat with a handoff to enable pinned instructions."
    else:
        try:
            key = _hash([_pin_enabled(home), _sources(agent, home), _project(agent, snapshot["soul_loaded"])])
            expected = _hash([True, snapshot["sources"], snapshot["project"]])
            if key == expected:
                if "warned" in snapshot:
                    snapshot.pop("warned")
                    agent._instruction_contract_dirty = True
                return
        except (ValueError, OSError):
            key = "unavailable"
        message = (
            "Global or project instructions changed or are unavailable. This chat keeps its saved contract "
            "through resume and compaction. Start a new chat with a handoff to adopt the update."
        )
    if snapshot.get("warned") != key:
        agent._emit_warning(message)
        snapshot["warned"] = key
        agent._instruction_contract_dirty = True


def instruction_context(agent, *, soul_loaded=False):
    """Return stable skill blocks and project blocks from native loaders or saved bytes."""
    from agent.system_prompt import _auto_load_parts
    if agent.skip_context_files:
        return [], []
    prepare_instructions(agent)
    snapshot = getattr(agent, "_instruction_contract_snapshot", None)
    if isinstance(snapshot, dict) and snapshot.get("legacy"):
        return [snapshot.get("legacy_skills", "")], [_project(agent, soul_loaded)]
    if snapshot is None:
        home = _home(agent)
        if not _pin_enabled(home):
            return _auto_load_parts(agent), [_project(agent, soul_loaded)]
        sources = _sources(agent, home)
        blocks = _auto_load_parts(agent)
        result = getattr(agent, "_auto_load_skills_result", ("", [], []))
        if not blocks or not result[1] or result[2]:
            raise ValueError("Required auto_load contract was not fully loaded")
        snapshot = {
            "version": 2, "home": str(home), "cwd": _cwd(), "skills": list(result),
            "project": _project(agent, soul_loaded), "sources": sources, "soul_loaded": soul_loaded,
        }
        snapshot["sha256"] = _checksum(snapshot)
        agent._instruction_contract_snapshot = snapshot
        agent._instruction_contract_dirty = True
    agent._auto_load_skills_result = tuple(snapshot["skills"])
    agent._auto_load_skills_resolved = True
    return [snapshot["skills"][0]], [snapshot["project"]]


def persist_instructions(agent) -> None:
    if getattr(agent, "_instruction_contract_dirty", False) is not True:
        return
    db = getattr(agent, "_session_db", None)
    if db is None:
        return  # In-memory callers cannot promise cross-process resume.
    snapshot = agent._instruction_contract_snapshot
    db.patch_session_model_config(agent.session_id, {_KEY: snapshot})
    if db.get_session_model_config_value(agent.session_id, _KEY) != snapshot:
        raise RuntimeError("Instruction snapshot was not persisted; refusing to continue")
    agent._instruction_contract_dirty = False
