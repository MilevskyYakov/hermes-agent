"""Session-scoped skill routing for skill_view."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Skill-routing state is session-scoped and payload-free: only skill names,
# source fingerprints, turn ids, and prune-marker counts are retained.
_SKILL_ROUTE_CONTEXT: ContextVar[dict | None] = ContextVar(
    "skill_route_context", default=None
)
_SKILL_ROUTE_LOCK = threading.Lock()
_SKILL_ROUTE_SESSIONS: OrderedDict[str, dict] = OrderedDict()
# ponytail: process-local receipts; reload the owner after restart or bounded eviction.
_SKILL_PRELOADS: OrderedDict[tuple, list[dict]] = OrderedDict()
_SKILL_ROUTE_SESSION_LIMIT = 512


def _source_fingerprint(payload: dict):
    from tools.skills_tool_dedup import _skill_view_fingerprint
    source = payload.get("_source_path")
    return _skill_view_fingerprint({"_source_path": str(Path(source).resolve())}) if source else None


def _activation(payload: dict) -> dict:
    for key in ("metadata", "gerda", "activation"):
        value = payload.get(key)
        payload = value if isinstance(value, dict) else {}
    return payload


def _preload_key(message: str) -> tuple:
    from hermes_constants import get_hermes_home
    return str(get_hermes_home()), hashlib.sha256(message.encode("utf-8")).hexdigest()


def register_skill_preload(message: str, payload: dict | None = None, *, blocks=()) -> str:
    """Remember actual runtime-rendered loads without retaining their prompt bodies."""
    with _SKILL_ROUTE_LOCK:
        entries = [entry for block in blocks for entry in _SKILL_PRELOADS.get(_preload_key(block), [])]
        if payload and (fingerprint := _source_fingerprint(payload)):
            entries.append({"name": payload["name"], "fingerprint": fingerprint})
        if entries:
            key = _preload_key(message)
            _SKILL_PRELOADS[key] = entries
            _SKILL_PRELOADS.move_to_end(key)
            while len(_SKILL_PRELOADS) > _SKILL_ROUTE_SESSION_LIMIT:
                _SKILL_PRELOADS.popitem(last=False)
    return message


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


@contextmanager
def skill_routing_context(messages: list, session_id: str, turn_id: str, *, agent=None):
    """Bind trusted per-turn routing input for ``skill_view`` enforcement."""
    user_text = ""
    transcript = []
    for message in messages or []:
        text = _message_text(message.get("content")) if isinstance(message, dict) else ""
        if text:
            transcript.append(text)
        if isinstance(message, dict) and message.get("role") == "user" and text:
            user_text = text

    joined = "\n".join(transcript)
    pruned_counts: dict[str, int] = {}
    for name in re.findall(r"reload with skill_view\(name='([^']+)'\)", joined):
        pruned_counts[name] = pruned_counts.get(name, 0) + 1

    direct_skill = None
    match = re.match(r"\s*/([A-Za-z0-9_-]+)(?:\s|$)", user_text)
    if match:
        direct_skill = match.group(1)
    else:
        invoked = re.search(
            r'The user has invoked the "([A-Za-z0-9_-]+)" skill', user_text
        )
        if invoked:
            direct_skill = invoked.group(1)
        else:
            stacked = re.search(r"^Skills loaded: ([A-Za-z0-9_-]+)", user_text, re.MULTILINE)
            if stacked:
                direct_skill = stacked.group(1)

    context = {
        "session_id": session_id or "",
        "turn_id": turn_id or "",
        "direct_skill": direct_skill,
        "pruned_counts": pruned_counts,
        "agent": agent,
    }
    token = _SKILL_ROUTE_CONTEXT.set(context)
    try:
        # A quoted invocation header is not proof that instructions were loaded.
        with _SKILL_ROUTE_LOCK:
            preloads = list(_SKILL_PRELOADS.get(_preload_key(user_text), []))
        owner = None
        for entry in preloads:
            from tools.skills_tool import skill_view
            result = skill_view(entry["name"], preprocess=False)
            payload = json.loads(result)
            if not payload.get("success") or _source_fingerprint(payload) != entry["fingerprint"]:
                continue
            activation = _activation(payload)
            is_workflow = not activation.get("always") and (
                activation.get("auto") == "core" or (
                    activation.get("auto") == "none" and (activation.get("direct") or activation.get("slash"))
                )
            )
            if owner is None and is_workflow:
                owner = payload["name"]
                context["direct_skill"] = owner
            _skill_route_finish(
                entry["name"], result, None,
                as_dependency=bool(owner and payload["name"] != owner and activation.get("dependency")),
            )
        yield
    finally:
        _SKILL_ROUTE_CONTEXT.reset(token)


def _reset_skill_routing_state() -> None:
    """Test helper: clear bounded process-local routing state."""
    with _SKILL_ROUTE_LOCK:
        _SKILL_ROUTE_SESSIONS.clear()
        _SKILL_PRELOADS.clear()


def _route_session(context: dict) -> dict:
    session_id = context.get("session_id") or "__unscoped__"
    state = _SKILL_ROUTE_SESSIONS.setdefault(
        session_id,
        {
            "loaded": set(),
            "fingerprints": {},
            "reloaded_pruned": {},
            "turns": OrderedDict(),
            "loading": set(),
        },
    )
    _SKILL_ROUTE_SESSIONS.move_to_end(session_id)
    while len(_SKILL_ROUTE_SESSIONS) > _SKILL_ROUTE_SESSION_LIMIT:
        _SKILL_ROUTE_SESSIONS.popitem(last=False)
    return state


def _normalized_skill_name(name: str) -> str:
    return str(name or "").strip().lower().replace("_", "-")


def _routing_result(name: str, load_state: str, outcome: str, *, error: str | None = None) -> str:
    payload = {
        "success": error is None,
        "name": name,
        "content": "",
        "load_state": load_state,
        "routing_outcome": outcome,
        "prompt_payload_included": False,
    }
    if error:
        payload["error"] = error
    logger.info("skill_routing skill=%s outcome=%s", name, outcome)
    return json.dumps(payload, ensure_ascii=False)


def _skill_route_precheck(
    name: str, file_path: str | None, *, as_dependency: bool = False
) -> str | None:
    context = _SKILL_ROUTE_CONTEXT.get()
    if not context or file_path:
        return None
    requested = _normalized_skill_name(name)
    with _SKILL_ROUTE_LOCK:
        state = _route_session(context)
        if requested in state["loading"]:
            return _routing_result(name, "loading", "duplicate_loading")
        state["loading"].add(requested)
    return None


def _skill_route_finish(
    name: str, result: str, file_path: str | None, *, as_dependency: bool = False
) -> str:
    context = _SKILL_ROUTE_CONTEXT.get()
    if not context:
        return result
    requested = _normalized_skill_name(name)
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        payload = {}

    if file_path and not (isinstance(payload, dict) and payload.get("_skill_root")):
        return result

    with _SKILL_ROUTE_LOCK:
        state = _route_session(context)
        state["loading"].discard(requested)
        if not isinstance(payload, dict) or not payload.get("success"):
            return result

        canonical = _normalized_skill_name(payload.get("name") or name)
        activation = _activation(payload)
        auto = str(activation.get("auto") or "").lower()
        dependency = activation.get("dependency") is True
        direct_skill = _normalized_skill_name(context.get("direct_skill") or "")
        slash_aliases = {
            _normalized_skill_name(str(alias).lstrip("/"))
            for alias in (activation.get("slash") or [])
        }
        direct_match = bool(direct_skill) and direct_skill in {
            requested,
            canonical,
            *slash_aliases,
        }
        turn_id = context.get("turn_id") or "__turn__"
        workflow = state["turns"].get(turn_id)

        outcome = "legacy_loaded"
        if as_dependency:
            if not dependency:
                return _routing_result(
                    canonical,
                    "blocked",
                    "dependency_not_allowed",
                    error=f"Skill '{canonical}' does not declare dependency activation.",
                )
            if not workflow:
                return _routing_result(
                    canonical,
                    "blocked",
                    "dependency_context_required",
                    error=f"Dependency '{canonical}' requires an already-selected workflow.",
                )
            outcome = "dependency_loaded"
        elif auto == "core":
            if direct_skill and not direct_match:
                return _routing_result(
                    canonical,
                    "blocked",
                    "manual_precedence",
                    error=f"Auto-core '{canonical}' blocked: direct /{direct_skill} invocation owns this turn.",
                )
            if workflow and workflow != canonical:
                return _routing_result(
                    canonical,
                    "blocked",
                    "second_core_blocked",
                    error=f"Workflow '{workflow}' already owns this turn; auto-core '{canonical}' was not loaded.",
                )
            state["turns"][turn_id] = canonical
            outcome = "core_selected"
        elif activation.get("always"):
            outcome = "capability_loaded"
        elif dependency and auto == "none" and not (
            activation.get("direct") or activation.get("slash")
        ):
            return _routing_result(
                canonical,
                "blocked",
                "dependency_flag_required",
                error=f"Capability '{canonical}' requires as_dependency=true.",
            )
        elif auto == "none" and (activation.get("direct") or activation.get("slash")):
            if not direct_match:
                return _routing_result(
                    canonical,
                    "blocked",
                    "manual_trigger_required",
                    error=f"Manual skill '{canonical}' requires exact slash/direct invocation.",
                )
            if workflow and workflow != canonical:
                return _routing_result(
                    canonical,
                    "blocked",
                    "workflow_already_selected",
                    error=f"Workflow '{workflow}' already owns this turn.",
                )
            state["turns"][turn_id] = canonical
            outcome = "manual_selected"
        prune_counts = context.get("pruned_counts") or {}
        prune_count = max(int(prune_counts.get(name, 0)), int(prune_counts.get(canonical, 0)))
        previous_prune_count = int(state["reloaded_pruned"].get(canonical, 0))
        fingerprint = _source_fingerprint(payload)
        cached = bool(fingerprint and state["fingerprints"].get(canonical) == fingerprint
                      and prune_count <= previous_prune_count
                      and not payload.get("setup_needed"))
        state["loaded"].update({requested, canonical})
        state["fingerprints"][canonical] = fingerprint
        if prune_count > previous_prune_count:
            state["reloaded_pruned"][requested] = prune_count
            state["reloaded_pruned"][canonical] = prune_count
            outcome = "pruned_reloaded"
        while len(state["turns"]) > 64:
            state["turns"].popitem(last=False)

    if cached:
        return _routing_result(name, "cached", "duplicate_cached")
    payload["load_state"] = "loaded"
    payload["routing_outcome"] = outcome
    payload["prompt_payload_included"] = True
    if outcome in {"core_selected", "manual_selected"} and context.get("agent") is not None:
        try:
            from agent.session_toolsets import select_core_preset

            select_core_preset(context["agent"], canonical)
        except Exception:
            logger.debug("core toolset preset selection failed", exc_info=True)
    logger.info("skill_routing skill=%s outcome=%s", canonical, outcome)
    return json.dumps(payload, ensure_ascii=False)
