"""Intent-scoped tool schemas for new interactive sessions.

A fresh session starts with routing, clarification, and one escape hatch. Once
``skill_view`` confirms the existing one-core route, Hermes swaps in that
core's preset. ``tool_expand`` restores the user's configured surface when no
core matches or a preset lacks a needed capability.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

logger = logging.getLogger("hermes.session_toolsets")

PRESET_TOOLSETS: dict[str, tuple[str, ...]] = {
    "dev": ("coding",),
    "research": (
        "web", "browser", "file", "skills", "todo", "memory",
        "session_search", "clarify", "code_execution", "delegation", "vision",
    ),
    "hub": (
        "file", "terminal", "skills", "todo", "memory", "session_search",
        "clarify", "cronjob", "web",
    ),
    "visual": (
        "vision", "image_gen", "browser", "file", "skills", "clarify", "web",
    ),
    "system": (
        "terminal", "file", "skills", "todo", "memory", "session_search",
        "clarify", "cronjob", "web",
    ),
}

CORE_PRESETS = {
    "dev": "dev",
    "research": "research",
    "hub": "hub",
    "design": "visual",
    "imagegen": "visual",
    "system": "system",
    "infra": "system",
    "feature": "dev",
    "issue": "dev",
    "pusk": "dev",
    "super-code-review": "dev",
    "taskfinish": "dev",
    "hub-retro": "hub",
    "commercial-proposal": "visual",
}

_BOOTSTRAP_NAMES = frozenset({"skills_list", "skill_view", "clarify"})
_ELIGIBLE_PLATFORMS = frozenset({
    "cli", "tui", "desktop", "telegram", "discord", "whatsapp", "slack",
    "signal", "bluebubbles", "email", "sms", "mattermost", "matrix", "dingtalk",
    "feishu", "wecom", "weixin", "qqbot", "yuanbao",
})
SESSION_TOOLSET_LOCK = threading.RLock()
_TOOL_EXPAND_NAME = "tool_expand"
_PRESET_MARKER_RE = re.compile(r"\[Session toolset preset: ([a-z-]+)\]")
_TOOL_EXPAND_SCHEMA = {
    "type": "function",
    "function": {
        "name": _TOOL_EXPAND_NAME,
        "description": (
            "Expand this session to the user's full configured Hermes tool surface. "
            "Use when no core workflow matches, or when the selected preset lacks a "
            "needed capability. Expansion keeps configured disabled-toolset and "
            "platform safety restrictions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Short capability or task reason for expansion.",
                }
            },
            "required": ["reason"],
        },
    },
}


def _tool_name(tool: dict[str, Any]) -> str:
    return str((tool.get("function") or {}).get("name") or "")


def _full_tools(agent: Any) -> list[dict[str, Any]]:
    tools = getattr(agent, "_session_full_tools", None)
    if tools is None:
        tools = list(getattr(agent, "tools", None) or [])
        agent._session_full_tools = tools
    return list(tools)


def _publish(agent: Any, tools: list[dict[str, Any]], preset: str) -> None:
    names = {_tool_name(tool) for tool in tools}
    names.discard("")
    schema_bytes = len(json.dumps(tools, ensure_ascii=False).encode("utf-8"))
    with SESSION_TOOLSET_LOCK:
        agent.tools = list(tools)
        agent.valid_tool_names = names
        agent._session_toolset_preset = preset
        agent._session_toolsets_prompt_dirty = True
        agent._tool_search_scope_cache = None
    logger.info(
        "session_toolset preset=%s tool_count=%d schema_bytes=%d",
        preset,
        len(names),
        schema_bytes,
    )


def _preset_tools(agent: Any, preset: str) -> list[dict[str, Any]]:
    from model_tools import get_tool_definitions

    requested = PRESET_TOOLSETS[preset]
    allowed = {_tool_name(tool) for tool in _full_tools(agent)}
    candidate = get_tool_definitions(
        enabled_toolsets=list(requested),
        disabled_toolsets=getattr(agent, "disabled_toolsets", None),
        quiet_mode=True,
    ) or []
    selected = [tool for tool in candidate if _tool_name(tool) in allowed]

    # Safety/routing tools remain available only when the user's configured
    # platform surface already granted them.
    selected_names = {_tool_name(tool) for tool in selected}
    for tool in _full_tools(agent):
        name = _tool_name(tool)
        if name in _BOOTSTRAP_NAMES and name not in selected_names:
            selected.append(tool)
            selected_names.add(name)
    return selected


def preset_tool_metrics(agent: Any, preset: str) -> dict[str, int]:
    if preset == "full":
        tools = _full_tools(agent)
    elif preset == "bootstrap":
        tools = [
            tool for tool in _full_tools(agent) if _tool_name(tool) in _BOOTSTRAP_NAMES
        ]
        if "skill_view" in {_tool_name(tool) for tool in tools}:
            tools.append(_TOOL_EXPAND_SCHEMA)
    else:
        tools = _preset_tools(agent, preset)
    return {
        "tool_count": len(tools),
        "json_bytes": len(json.dumps(tools, ensure_ascii=False).encode("utf-8")),
    }


def start_session_bootstrap(agent: Any, *, has_history: bool) -> bool:
    """Collapse one new interactive session before its first model request."""
    if getattr(agent, "_session_toolsets_started", False):
        return False
    agent._session_toolsets_started = True
    platform = str(getattr(agent, "platform", "") or "").strip().lower()
    if platform not in _ELIGIBLE_PLATFORMS:
        return False

    if has_history:
        db = getattr(agent, "_session_db", None)
        try:
            row = db.get_session(agent.session_id) if db is not None else None
            stored_prompt = str((row or {}).get("system_prompt") or "")
            match = _PRESET_MARKER_RE.search(stored_prompt)
            preset = match.group(1) if match else "full"
            if preset in PRESET_TOOLSETS:
                _publish(agent, _preset_tools(agent, preset), preset)
            elif preset == "bootstrap":
                bootstrap = [
                    tool for tool in _full_tools(agent)
                    if _tool_name(tool) in _BOOTSTRAP_NAMES
                ]
                bootstrap.append(_TOOL_EXPAND_SCHEMA)
                _publish(agent, bootstrap, preset)
            else:
                agent._session_toolset_preset = "full"
                full = _full_tools(agent)
                logger.info(
                    "session_toolset preset=full tool_count=%d schema_bytes=%d",
                    len(full),
                    len(json.dumps(full, ensure_ascii=False).encode("utf-8")),
                )
            agent._session_toolsets_prompt_dirty = False
            return preset != "full"
        except Exception:
            logger.debug("session toolset resume fell back to full", exc_info=True)
            return False

    full = _full_tools(agent)
    bootstrap = [tool for tool in full if _tool_name(tool) in _BOOTSTRAP_NAMES]
    if "skill_view" not in {_tool_name(tool) for tool in bootstrap}:
        logger.info(
            "session_toolset preset=full tool_count=%d schema_bytes=%d reason=no_skill_router",
            len(full),
            len(json.dumps(full, ensure_ascii=False).encode("utf-8")),
        )
        return False
    bootstrap.append(_TOOL_EXPAND_SCHEMA)
    _publish(agent, bootstrap, "bootstrap")
    # Prompt is not built yet, so no rebuild is needed for this initial swap.
    agent._session_toolsets_prompt_dirty = False
    return True


def select_core_preset(agent: Any, core_name: str) -> bool:
    """Apply preset for a core selected by the existing skill router."""
    preset = CORE_PRESETS.get(str(core_name or "").strip().lower().replace("_", "-"))
    if not preset or not getattr(agent, "_session_toolsets_started", False):
        return False
    _publish(agent, _preset_tools(agent, preset), preset)
    return True


def expand_full_toolset(agent: Any, reason: str = "") -> str:
    """Restore the user's original configured tools without widening policy."""
    if getattr(agent, "_model_route_tier", "") == "luna_max":
        agent._model_escalation_requested = True
        agent._model_escalation_reason = str(reason or "tool expansion required")[:200]
        return json.dumps({
            "success": True,
            "escalation": "sol_medium",
            "message": "Continuing in a fresh linked Sol session with a structured handoff.",
        }, ensure_ascii=False)
    full = _full_tools(agent)
    _publish(agent, full, "full")
    return json.dumps({
        "success": True,
        "preset": "full",
        "tool_count": len({_tool_name(tool) for tool in full if _tool_name(tool)}),
        "reason": str(reason or "")[:200],
        "message": "Full configured tool surface loaded. Continue the same task.",
    }, ensure_ascii=False)


def restore_preset_after_registry_refresh(
    agent: Any, previous_visible_names: set[str] | None = None
) -> None:
    """Keep a scoped session scoped after MCP/plugin registry rebuilds."""
    preset = getattr(agent, "_session_toolset_preset", "full")
    if preset == "full" or not getattr(agent, "_session_toolsets_started", False):
        return
    old_visible = previous_visible_names or {
        _tool_name(tool) for tool in getattr(agent, "tools", None) or []
    }
    was_dirty = getattr(agent, "_session_toolsets_prompt_dirty", False)
    agent._session_full_tools = list(getattr(agent, "tools", None) or [])
    if preset == "bootstrap":
        visible = [
            tool for tool in _full_tools(agent) if _tool_name(tool) in _BOOTSTRAP_NAMES
        ]
        visible.append(_TOOL_EXPAND_SCHEMA)
    else:
        visible = _preset_tools(agent, preset)
    _publish(agent, visible, preset)
    if old_visible == {_tool_name(tool) for tool in visible}:
        agent._session_toolsets_prompt_dirty = was_dirty


def rebuild_prompt_if_dirty(agent: Any, system_message: str | None) -> str | None:
    """Rebuild and persist prompt after an in-turn schema swap."""
    if not getattr(agent, "_session_toolsets_prompt_dirty", False):
        return None
    agent._session_toolsets_prompt_dirty = False
    prompt = agent._build_system_prompt(system_message)
    agent._cached_system_prompt = prompt
    db = getattr(agent, "_session_db", None)
    if db is not None:
        try:
            db.update_system_prompt(agent.session_id, prompt)
        except Exception as exc:
            logger.warning("session_toolset prompt persistence failed: %s", exc)
    return prompt
