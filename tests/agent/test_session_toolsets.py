from types import SimpleNamespace
from unittest.mock import patch

from agent.session_toolsets import (
    PRESET_TOOLSETS,
    expand_full_toolset,
    preset_tool_metrics,
    rebuild_prompt_if_dirty,
    restore_preset_after_registry_refresh,
    select_core_preset,
    start_session_bootstrap,
)


def _schema(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _agent(*, platform="cli", db=None):
    tools = [
        _schema("skill_view"),
        _schema("skills_list"),
        _schema("clarify"),
        _schema("terminal"),
        _schema("read_file"),
        _schema("web_search"),
        _schema("image_generate"),
        _schema("computer_use"),
    ]
    return SimpleNamespace(
        tools=tools,
        valid_tool_names={tool["function"]["name"] for tool in tools},
        enabled_toolsets=["hermes-cli"],
        disabled_toolsets=[],
        platform=platform,
        session_id="session",
        _session_db=db,
        _cached_system_prompt=None,
        _build_system_prompt=lambda _system: "rebuilt",
    )


def _defs(enabled_toolsets=None, **_kwargs):
    names = {
        "coding": ["terminal", "read_file", "skill_view", "skills_list", "clarify"],
        "web": ["web_search"],
        "browser": [],
        "file": ["read_file"],
        "skills": ["skill_view", "skills_list"],
        "todo": [],
        "memory": [],
        "session_search": [],
        "clarify": ["clarify"],
        "code_execution": [],
        "delegation": [],
        "vision": [],
        "image_gen": ["image_generate"],
        "terminal": ["terminal"],
        "cronjob": [],
        "computer_use": ["computer_use"],
    }
    selected = []
    for toolset in enabled_toolsets or []:
        selected.extend(names.get(toolset, []))
    return [_schema(name) for name in dict.fromkeys(selected)]


def test_new_session_bootstraps_and_full_escape_hatch_restores_configured_tools():
    agent = _agent()

    assert start_session_bootstrap(agent, has_history=False) is True
    assert agent._session_toolset_preset == "bootstrap"
    assert agent.valid_tool_names == {"skill_view", "skills_list", "clarify", "tool_expand"}

    result = expand_full_toolset(agent, "need terminal")

    assert '"preset": "full"' in result
    assert agent.valid_tool_names == {
        "skill_view", "skills_list", "clarify", "terminal", "read_file",
        "web_search", "image_generate", "computer_use",
    }


def test_luna_tool_expansion_requests_fresh_sol_continuation():
    agent = _agent()
    start_session_bootstrap(agent, has_history=False)
    agent._model_route_tier = "luna_max"

    result = expand_full_toolset(agent, "scope became broad")

    assert '"escalation": "sol_medium"' in result
    assert agent._model_escalation_requested is True
    assert agent._session_toolset_preset == "bootstrap"


def test_core_selection_reuses_existing_router_and_never_widens_configured_surface():
    agent = _agent()
    start_session_bootstrap(agent, has_history=False)

    with patch("model_tools.get_tool_definitions", side_effect=_defs):
        assert select_core_preset(agent, "dev") is True

    assert agent._session_toolset_preset == "dev"
    assert agent.valid_tool_names == {
        "terminal", "read_file", "skill_view", "skills_list", "clarify",
    }
    assert "image_generate" not in agent.valid_tool_names


def test_unknown_core_keeps_bootstrap_for_full_fallback():
    agent = _agent()
    start_session_bootstrap(agent, has_history=False)

    assert select_core_preset(agent, "unknown") is False
    assert agent._session_toolset_preset == "bootstrap"
    assert "tool_expand" in agent.valid_tool_names


def test_gateway_style_resume_restores_persisted_preset():
    db = SimpleNamespace(get_session=lambda _sid: {
        "system_prompt": "prefix\n[Session toolset preset: visual]\nsuffix"
    })
    agent = _agent(platform="telegram", db=db)

    with patch("model_tools.get_tool_definitions", side_effect=_defs):
        assert start_session_bootstrap(agent, has_history=True) is True

    assert agent._session_toolset_preset == "visual"
    assert agent.valid_tool_names == {
        "image_generate", "read_file", "web_search", "skill_view",
        "skills_list", "clarify",
    }


def test_platform_safety_excludes_noninteractive_workers():
    agent = _agent(platform="cron")

    assert start_session_bootstrap(agent, has_history=False) is False
    assert "terminal" in agent.valid_tool_names


def test_prompt_rebuild_persists_after_late_expansion():
    calls = []
    db = SimpleNamespace(update_system_prompt=lambda sid, prompt: calls.append((sid, prompt)))
    agent = _agent(db=db)
    start_session_bootstrap(agent, has_history=False)
    expand_full_toolset(agent, "need terminal")

    assert rebuild_prompt_if_dirty(agent, None) == "rebuilt"
    assert calls == [("session", "rebuilt")]
    assert rebuild_prompt_if_dirty(agent, None) is None


def test_registry_refresh_keeps_visible_preset_and_updates_full_fallback():
    agent = _agent()
    start_session_bootstrap(agent, has_history=False)
    previous = set(agent.valid_tool_names)
    agent.tools = list(agent._session_full_tools) + [_schema("mcp_new")]
    agent.valid_tool_names = {
        tool["function"]["name"] for tool in agent.tools
    }

    restore_preset_after_registry_refresh(agent, previous)

    assert agent.valid_tool_names == previous
    assert "mcp_new" in {
        tool["function"]["name"] for tool in agent._session_full_tools
    }


def test_every_preset_reports_smaller_schema_than_full_on_representative_surface():
    agent = _agent()

    with patch("model_tools.get_tool_definitions", side_effect=_defs):
        full = preset_tool_metrics(agent, "full")
        for preset in PRESET_TOOLSETS:
            metrics = preset_tool_metrics(agent, preset)
            assert metrics["tool_count"] < full["tool_count"]
            assert metrics["json_bytes"] < full["json_bytes"]
