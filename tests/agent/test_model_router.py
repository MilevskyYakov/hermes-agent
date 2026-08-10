from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.model_router import (
    attach_route,
    proposal_response,
    request_luna_escalation_if_due,
    route_first_task,
)


class _Response:
    def __init__(self, payload):
        self.choices = [
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))
        ]


def _call_for(payload):
    def call(**kwargs):
        assert kwargs["task"] == "model_router"
        assert "main_runtime" in kwargs
        return _Response(payload)

    return call


def _config(mode="shadow", threshold=0.8):
    return {
        "model_router": {
            "enabled": True,
            "mode": mode,
            "confidence_threshold": threshold,
            "luna_model": "gpt-5.6-luna",
            "sol_model": "gpt-5.6-sol",
        }
    }


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("mechanical_bounded", "luna_max"),
        ("normal_dev", "sol_medium"),
        ("architecture_debug", "sol_medium"),
        ("complex_large", "sol_max"),
        ("ultra_research", "sol_ultra"),
        ("ambiguous", "sol_medium"),
    ],
)
def test_shadow_route_records_proposal_but_executes_sol_medium(intent, expected):
    route = route_first_task(
        "representative task",
        config=_config(),
        main_model="gpt-5.6-sol",
        main_runtime={"provider": "openai-codex"},
        session_id="session-317",
        call=_call_for(
            {
                "intent": intent,
                "confidence": 0.95,
                "reason": "bounded reason",
                "toolset": "dev",
            }
        ),
    )

    assert route is not None
    assert route["proposed_tier"] == expected
    assert route["effective_tier"] == "sol_medium"
    assert route["effective_model"] == "gpt-5.6-sol"
    assert route["reasoning_config"] == {"effort": "medium"}
    assert route["toolset"] == "dev"
    assert route["visible_line"].startswith("Model: Sol Medium — bounded reason")


def test_low_confidence_and_invalid_output_fall_back_to_sol_medium():
    low = route_first_task(
        "task",
        config=_config(),
        main_model="gpt-5.6-sol",
        main_runtime={},
        call=_call_for(
            {
                "intent": "mechanical_bounded",
                "confidence": 0.4,
                "reason": "maybe",
                "toolset": "unknown",
            }
        ),
    )
    invalid = route_first_task(
        "task",
        config=_config(),
        main_model="gpt-5.6-sol",
        main_runtime={},
        call=lambda **_kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))]
        ),
    )

    assert low is not None
    assert invalid is not None
    assert low["effective_tier"] == low["proposed_tier"] == "sol_medium"
    assert low["reason_code"] == "low_confidence"
    assert low["toolset"] == "full"
    assert invalid["effective_tier"] == "sol_medium"
    assert invalid["reason_code"] == "router_unavailable"


def test_live_luna_route_uses_max_and_requests_fresh_session_escalation():
    route = route_first_task(
        "rename one explicit symbol",
        config=_config(mode="live"),
        main_model="gpt-5.6-sol",
        main_runtime={},
        call=_call_for(
            {
                "intent": "mechanical_bounded",
                "confidence": 0.99,
                "reason": "small bounded edit",
                "toolset": "dev",
            }
        ),
    )

    assert route is not None
    assert route["effective_tier"] == "luna_max"
    assert route["effective_model"] == "gpt-5.6-luna"
    assert route["reasoning_config"] == {"effort": "max"}
    assert route["escalation_policy"] == "fresh_linked_session"


def test_expensive_live_route_is_proposal_not_execution():
    route = route_first_task(
        "large migration",
        config=_config(mode="live"),
        main_model="gpt-5.6-sol",
        main_runtime={},
        call=_call_for(
            {
                "intent": "complex_large",
                "confidence": 0.95,
                "reason": "cross-service migration",
                "toolset": "system",
            }
        ),
    )

    assert route is not None
    assert route["proposal_required"] is True
    assert route["proposed_tier"] == "sol_max"
    assert route["effective_tier"] == "sol_medium"
    assert "no execution started" in proposal_response(route)


def test_disabled_toggle_is_immediate_rollback_without_aux_call():
    called = False

    def call(**_kwargs):
        nonlocal called
        called = True

    assert route_first_task(
        "task",
        config={"model_router": {"enabled": False}},
        main_model="gpt-5.6-sol",
        main_runtime={},
        call=call,
    ) is None
    assert called is False


def test_persisted_telemetry_excludes_prompt_and_free_form_reason():
    writes = []

    class DB:
        def update_session_meta(self, session_id, model_config, model=None):
            writes.append((session_id, json.loads(model_config), model))

    agent = SimpleNamespace(
        session_id="session-317",
        model="gpt-5.6-sol",
        _session_db=DB(),
        _session_init_model_config={"provider": "openai-codex"},
    )
    route = {
        "mode": "shadow",
        "intent": "normal_dev",
        "confidence": 0.9,
        "reason": "private prompt fragment",
        "reason_code": "normal_dev",
        "toolset": "dev",
        "proposed_tier": "sol_medium",
        "effective_tier": "sol_medium",
        "escalation_policy": "none",
    }

    attach_route(agent, route)

    stored = writes[0][1]["_model_route"]
    assert "reason" not in stored
    assert "prompt" not in json.dumps(stored)
    assert stored["reason_code"] == "normal_dev"


def test_luna_call_limit_requests_conservative_escalation():
    agent = SimpleNamespace(
        _model_route_tier="luna_max",
        _model_route={"luna_call_limit": 4},
        session_api_calls=4,
    )

    assert request_luna_escalation_if_due(agent) is True
    assert agent._model_escalation_requested is True
    assert agent._model_escalation_reason == "Luna call limit reached"
