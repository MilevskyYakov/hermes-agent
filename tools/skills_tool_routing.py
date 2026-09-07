"""Session-scoped skill routing for skill_view."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

# Skill-routing state is session-scoped and payload-free: only skill names,
# turn ids, routing outcomes, and prune-marker counts are retained.
_SKILL_ROUTE_CONTEXT: ContextVar[dict | None] = ContextVar(
    "skill_route_context", default=None
)
_SKILL_ROUTE_LOCK = threading.Lock()
_SKILL_ROUTE_SESSIONS: OrderedDict[str, dict] = OrderedDict()
_SKILL_ROUTE_SESSION_LIMIT = 512


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

    token = _SKILL_ROUTE_CONTEXT.set(
        {
            "session_id": session_id or "",
            "turn_id": turn_id or "",
            "direct_skill": direct_skill,
            "pruned_counts": pruned_counts,
            "agent": agent,
        }
    )
    try:
        yield
    finally:
        _SKILL_ROUTE_CONTEXT.reset(token)


def _reset_skill_routing_state() -> None:
    """Test helper: clear bounded process-local routing state."""
    with _SKILL_ROUTE_LOCK:
        _SKILL_ROUTE_SESSIONS.clear()


def _route_session(context: dict) -> dict:
    session_id = context.get("session_id") or "__unscoped__"
    state = _SKILL_ROUTE_SESSIONS.setdefault(
        session_id,
        {
            "loaded": set(),
            "roles": {},
            "triggers": {},
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


def _skill_route_precheck(name: str, file_path: str | None) -> str | None:
    context = _SKILL_ROUTE_CONTEXT.get()
    if not context or file_path:
        return None
    requested = _normalized_skill_name(name)
    with _SKILL_ROUTE_LOCK:
        state = _route_session(context)
        prune_count = int((context.get("pruned_counts") or {}).get(name, 0))
        reloaded_count = int(state["reloaded_pruned"].get(requested, 0))
        if requested in state["loaded"] and prune_count <= reloaded_count:
            role = state["roles"].get(requested)
            turn_id = context.get("turn_id") or "__turn__"
            workflow = state["turns"].get(turn_id)
            direct_skill = _normalized_skill_name(context.get("direct_skill") or "")
            triggers = state["triggers"].get(requested, {requested})
            if role == "core":
                if direct_skill and direct_skill not in triggers:
                    return _routing_result(
                        name,
                        "blocked",
                        "manual_precedence",
                        error=f"Auto-core '{requested}' blocked: direct /{direct_skill} invocation owns this turn.",
                    )
                if workflow and workflow != requested:
                    return _routing_result(
                        name,
                        "blocked",
                        "second_core_blocked",
                        error=f"Workflow '{workflow}' already owns this turn.",
                    )
                state["turns"][turn_id] = requested
            elif role == "manual":
                if direct_skill not in triggers:
                    return _routing_result(
                        name,
                        "blocked",
                        "manual_trigger_required",
                        error=f"Manual skill '{requested}' requires exact slash/direct invocation.",
                    )
                if workflow and workflow != requested:
                    return _routing_result(
                        name,
                        "blocked",
                        "workflow_already_selected",
                        error=f"Workflow '{workflow}' already owns this turn.",
                    )
                state["turns"][turn_id] = requested
            return _routing_result(name, "cached", "duplicate_cached")
        if requested in state["loading"]:
            return _routing_result(name, "loading", "duplicate_loading")
        state["loading"].add(requested)
    return None


def _skill_route_pruned_reload_pending(name: str, file_path: str | None) -> bool:
    context = _SKILL_ROUTE_CONTEXT.get()
    if not context or file_path:
        return False
    requested = _normalized_skill_name(name)
    with _SKILL_ROUTE_LOCK:
        state = _route_session(context)
        prune_count = int((context.get("pruned_counts") or {}).get(name, 0))
        reloaded_count = int(state["reloaded_pruned"].get(requested, 0))
        return requested in state["loaded"] and prune_count > reloaded_count


def _skill_route_finish(
    name: str, result: str, file_path: str | None, *, as_dependency: bool = False
) -> str:
    context = _SKILL_ROUTE_CONTEXT.get()
    if not context or file_path:
        return result
    requested = _normalized_skill_name(name)
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        payload = {}

    with _SKILL_ROUTE_LOCK:
        state = _route_session(context)
        state["loading"].discard(requested)
        if not isinstance(payload, dict) or not payload.get("success"):
            return result

        canonical = _normalized_skill_name(payload.get("name") or name)
        raw_metadata = payload.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        raw_gerda = metadata.get("gerda")
        gerda: dict[str, Any] = raw_gerda if isinstance(raw_gerda, dict) else {}
        raw_activation = gerda.get("activation")
        activation: dict[str, Any] = (
            raw_activation if isinstance(raw_activation, dict) else {}
        )
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
        role = "legacy"
        if auto == "core":
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
            role = "core"
        elif activation.get("always"):
            outcome = "capability_loaded"
            role = "capability"
        elif dependency and as_dependency and workflow:
            outcome = "dependency_loaded"
            role = "capability"
        elif dependency and as_dependency:
            return _routing_result(
                canonical,
                "blocked",
                "dependency_context_required",
                error=f"Dependency '{canonical}' requires an already-selected workflow.",
            )
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
            role = "manual"
        state["loaded"].update({requested, canonical})
        triggers = {requested, canonical, *slash_aliases}
        state["roles"].update({requested: role, canonical: role})
        state["triggers"].update({requested: triggers, canonical: triggers})
        prune_count = int((context.get("pruned_counts") or {}).get(name, 0))
        previous_prune_count = int(state["reloaded_pruned"].get(requested, 0))
        if prune_count > previous_prune_count:
            state["reloaded_pruned"][requested] = prune_count
            state["reloaded_pruned"][canonical] = prune_count
            outcome = "pruned_reloaded"
        while len(state["turns"]) > 64:
            state["turns"].popitem(last=False)

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
