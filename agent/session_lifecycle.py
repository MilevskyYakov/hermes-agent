"""Bound long agent runs with durable checkpoints and safe session rotation."""

from __future__ import annotations

import json
import re
from typing import Any


REQUIRED_HANDOFF_FIELDS = (
    "goal",
    "decisions",
    "changed_files",
    "checks",
    "blocker",
    "next_step",
)


def build_handoff(messages: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    """Build a small deterministic handoff without copying raw tool logs."""
    users: list[str] = []
    decisions: list[str] = []
    changed_files: set[str] = set()
    checks: list[str] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "user" and isinstance(content, str) and content.strip():
            text = content.strip()
            if not text.startswith(("[CONTEXT COMPACTION", "[CONTEXT SUMMARY")):
                users.append(text[:1000])
        elif role == "assistant" and isinstance(content, str) and content.strip():
            decisions.append(content.strip()[:500])

        if role != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            raw_args = function.get("arguments") or "{}"
            try:
                args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
            except (TypeError, ValueError):
                args = {}
            if name in {"patch", "write_file"}:
                path = args.get("path")
                if isinstance(path, str) and path:
                    changed_files.add(path)
                patch_text = args.get("patch")
                if isinstance(patch_text, str):
                    changed_files.update(
                        re.findall(
                            r"^\*\*\* (?:Add|Update|Delete) File: (.+)$",
                            patch_text,
                            re.MULTILINE,
                        )
                    )
            if name == "terminal":
                command = str(args.get("command") or "")
                if re.search(
                    r"(?:^|\s)(?:pytest|ruff|mypy|ty|npm test|pnpm test|cargo test|go test|make test|build)(?:\s|$)",
                    command,
                ):
                    checks.append(command[:500])

    return {
        "goal": users[0] if users else "Continue the current task.",
        "decisions": decisions[-3:],
        "changed_files": sorted(changed_files),
        "checks": checks[-5:],
        "blocker": "",
        "next_step": "Continue from the latest completed tool result.",
        "task_identity": task_id,
    }


def validate_handoff(handoff: dict[str, Any]) -> None:
    """Reject incomplete handoffs before any session mutation."""
    missing = [field for field in REQUIRED_HANDOFF_FIELDS if field not in handoff]
    if missing:
        raise ValueError(f"missing handoff fields: {', '.join(missing)}")
    if not isinstance(handoff["goal"], str) or not handoff["goal"].strip():
        raise ValueError("handoff goal must be non-empty")
    if not isinstance(handoff["next_step"], str) or not handoff["next_step"].strip():
        raise ValueError("handoff next_step must be non-empty")
    for field in ("decisions", "changed_files", "checks"):
        if not isinstance(handoff[field], list):
            raise ValueError(f"handoff {field} must be a list")
    if not isinstance(handoff["blocker"], str):
        raise ValueError("handoff blocker must be a string")
    if (
        not isinstance(handoff.get("task_identity"), str)
        or not handoff["task_identity"]
    ):
        raise ValueError("handoff task_identity must be non-empty")


def is_safe_boundary(agent: Any, messages: list[dict[str, Any]], task_id: str) -> bool:
    """Return true only between complete tool batches with no live process."""
    if getattr(agent, "_executing_tools", False):
        return False
    if getattr(agent, "_lifecycle_unsafe_operation", None):
        return False
    if getattr(agent, "_active_compression_lock_holder", None):
        return False

    pending: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            pending.update(
                str(call.get("id"))
                for call in message.get("tool_calls") or []
                if isinstance(call, dict) and call.get("id")
            )
        elif message.get("role") == "tool" and message.get("tool_call_id"):
            pending.discard(str(message["tool_call_id"]))
    if pending:
        return False

    try:
        from tools.process_registry import process_registry

        if process_registry.has_active_processes(task_id):
            return False
    except Exception:
        return False
    return True


def _session_metadata(agent: Any) -> dict[str, Any]:
    metadata = dict(getattr(agent, "_session_init_model_config", {}) or {})
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if session_db is None or not session_id:
        return metadata
    try:
        row = session_db.get_session(session_id) or {}
        raw = row.get("model_config")
        stored = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(stored, dict):
            stored.update(metadata)
            return stored
    except Exception:
        pass
    return metadata


def _model_call_count(agent: Any) -> int:
    current_calls = max(
        0,
        int(getattr(agent, "session_api_calls", 0) or 0)
        - int(getattr(agent, "_lifecycle_call_offset", 0) or 0),
    )
    cached = getattr(agent, "_lifecycle_persisted_calls", None)
    if isinstance(cached, int):
        return cached + current_calls
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if session_db is None or not session_id:
        agent._lifecycle_persisted_calls = 0
        return current_calls
    try:
        session_db.flush_token_counts()
        row = session_db.get_session(session_id) or {}
        persisted_calls = int(row.get("api_call_count") or 0)
    except Exception:
        persisted_calls = 0
    agent._lifecycle_persisted_calls = persisted_calls
    return persisted_calls + current_calls


def _merge_previous_handoff(agent: Any, handoff: dict[str, Any]) -> dict[str, Any]:
    checkpoint = _session_metadata(agent).get("_lifecycle_checkpoint") or {}
    previous = checkpoint.get("handoff") or {}
    if not isinstance(previous, dict):
        return handoff
    if handoff["goal"] == "Continue the current task." and previous.get("goal"):
        handoff["goal"] = previous["goal"]
    for field, limit in (("decisions", 3), ("changed_files", 100), ("checks", 5)):
        values = list(dict.fromkeys([*(previous.get(field) or []), *handoff[field]]))
        handoff[field] = values[-limit:]
    if not handoff["blocker"] and isinstance(previous.get("blocker"), str):
        handoff["blocker"] = previous["blocker"]
    return handoff


def _persist_handoff(
    agent: Any, handoff: dict[str, Any], *, kind: str, calls: int
) -> None:
    metadata = _session_metadata(agent)
    metadata["_lifecycle_checkpoint"] = {
        "kind": kind,
        "model_calls": calls,
        "handoff": handoff,
    }
    agent._session_init_model_config = metadata
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if session_db is not None and session_id:
        session_db.update_session_meta(
            session_id,
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            getattr(agent, "model", None),
        )


def checkpoint_if_due(agent: Any, messages: list[dict[str, Any]], task_id: str) -> bool:
    """Persist one non-blocking checkpoint when the configured call mark is hit."""
    if not getattr(agent, "session_lifecycle_enabled", False):
        return False
    calls = _model_call_count(agent)
    threshold = int(getattr(agent, "session_lifecycle_checkpoint_calls", 50) or 50)
    existing = _session_metadata(agent).get("_lifecycle_checkpoint") or {}
    if (
        calls < threshold
        or getattr(agent, "_lifecycle_checkpoint_written", False)
        or (
            existing.get("kind") == "checkpoint"
            and int(existing.get("model_calls") or 0) >= threshold
        )
    ):
        return False
    handoff = _merge_previous_handoff(agent, build_handoff(messages, task_id))
    validate_handoff(handoff)
    _persist_handoff(agent, handoff, kind="checkpoint", calls=calls)
    agent._lifecycle_checkpoint_written = True
    return True


def transition_if_due(
    agent: Any,
    messages: list[dict[str, Any]],
    system_message: str,
    task_id: str,
) -> tuple[list[dict[str, Any]], str, bool]:
    """Rotate through the existing compression continuation seam when safe."""
    if not getattr(agent, "session_lifecycle_enabled", False):
        return messages, system_message, False
    calls = _model_call_count(agent)
    threshold = int(getattr(agent, "session_lifecycle_transition_calls", 100) or 100)
    if calls < threshold or not is_safe_boundary(agent, messages, task_id):
        return messages, system_message, False

    handoff = _merge_previous_handoff(agent, build_handoff(messages, task_id))
    validate_handoff(handoff)
    _persist_handoff(agent, handoff, kind="transition_pending", calls=calls)
    old_session_id = agent.session_id
    compressed, new_system_message = agent._compress_context(
        messages,
        system_message,
        task_id=task_id,
        focus_topic="Preserve the structured task handoff and immediate next step.",
        force_session_rotation=True,
    )
    if agent.session_id == old_session_id:
        return messages, system_message, False

    _persist_handoff(agent, handoff, kind="continued", calls=calls)
    agent._lifecycle_checkpoint_written = False
    agent._lifecycle_persisted_calls = 0
    agent._lifecycle_call_offset = int(getattr(agent, "session_api_calls", 0) or 0)
    agent._emit_status("Long task checkpoint complete — continuing in a fresh session.")
    return compressed, new_system_message, True
