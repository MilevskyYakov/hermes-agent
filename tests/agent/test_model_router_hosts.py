from __future__ import annotations

from agent import model_router
from gateway.run import GatewayRunner
from gateway.session_context import scoped_current_session_id
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin


_ROUTE = {
    "effective_model": "gpt-5.6-sol",
    "effective_tier": "sol_medium",
    "reasoning_config": {"effort": "medium"},
    "visible_line": "Model: Sol Medium — shadow",
}


def test_cli_routes_only_before_first_agent_initialization(monkeypatch):
    calls = []
    monkeypatch.setattr(
        model_router,
        "route_first_task",
        lambda *args, **kwargs: calls.append((args, kwargs)) or dict(_ROUTE),
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model_router": {}})
    cli = object.__new__(CLIAgentSetupMixin)
    for name, value in {
        "api_key": "",
        "base_url": "",
        "provider": "openai-codex",
        "requested_provider": "openai-codex",
        "api_mode": "codex_responses",
        "acp_command": None,
        "acp_args": [],
        "_credential_pool": None,
        "model": "gpt-5.6-sol",
        "session_id": "cli-317",
        "_resumed": False,
        "conversation_history": [],
        "service_tier": None,
    }.items():
        setattr(cli, name, value)

    first = CLIAgentSetupMixin._resolve_turn_agent_config(cli, "first task")
    second = CLIAgentSetupMixin._resolve_turn_agent_config(cli, "same session")

    assert first["model_route"]["effective_tier"] == "sol_medium"
    assert "model_route" not in second
    assert len(calls) == 1


def test_gateway_routes_once_per_session_before_agent_build(monkeypatch):
    calls = []
    monkeypatch.setattr(
        model_router,
        "route_first_task",
        lambda *args, **kwargs: calls.append((args, kwargs)) or dict(_ROUTE),
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model_router": {}})
    runner = object.__new__(GatewayRunner)
    runner._service_tier = None
    runtime = {
        "provider": "openai-codex",
        "requested_provider": "openai-codex",
        "api_mode": "codex_responses",
        "args": [],
    }

    with scoped_current_session_id("gateway-317"):
        first = GatewayRunner._resolve_turn_agent_config(
            runner, "first task", "gpt-5.6-sol", runtime
        )
        second = GatewayRunner._resolve_turn_agent_config(
            runner, "same session", "gpt-5.6-sol", runtime
        )

    assert first["model_route"]["effective_tier"] == "sol_medium"
    assert "model_route" not in second
    assert len(calls) == 1
