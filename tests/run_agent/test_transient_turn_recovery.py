"""Regression coverage for #85426: transient outages recover the open turn."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


class ReadTimeout(Exception):
    pass


def _response(text: str):
    message = SimpleNamespace(content=text, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test-model", usage=None)


def _agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="test-key-12345678",
            base_url="https://direct.example.com/v1",
            provider="custom",
            model="test-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="tui",
        )
    agent.client = MagicMock()
    setattr(agent, "_api_max_retries", 2)
    return agent


def test_pre_delivery_outage_survives_multiple_recovery_cycles():
    agent = _agent()
    calls = []

    def api_call(_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) <= 6:
            raise ReadTimeout("temporary outage")
        return _response("recovered")

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=api_call),
        patch.object(agent, "_persist_session") as persist,
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.conversation_loop.jittered_backoff", return_value=0),
    ):
        result = agent.run_conversation("continue the task")

    assert result["completed"] is True
    assert result["final_response"] == "recovered"
    assert calls == [1, 2, 3, 4, 5, 6, 7]
    assert [m["role"] for m in result["messages"]] == ["user", "assistant"]
    persist.assert_called()


def test_interrupt_stops_transient_recovery_wait():
    agent = _agent()

    def api_call(_kwargs):
        raise ReadTimeout("temporary outage")

    def interrupt_sleep(_seconds):
        if _seconds == 0.2:
            agent._interrupt_requested = True

    def backoff(_attempt, *, base_delay=1.0, **_kwargs):
        return 60 if base_delay == 5.0 else 0

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_try_recover_primary_transport", return_value=True),
        patch("agent.conversation_loop.time.sleep", side_effect=interrupt_sleep),
        patch("agent.conversation_loop.jittered_backoff", side_effect=backoff),
    ):
        result = agent.run_conversation("continue the task")

    assert result["completed"] is False
    assert result["interrupted"] is True
    assert result["final_response"] == (
        "Operation interrupted: waiting for provider recovery."
    )


def test_background_surface_keeps_bounded_retry_policy():
    agent = _agent()
    setattr(agent, "platform", "telegram")

    with (
        patch.object(
            agent,
            "_interruptible_api_call",
            side_effect=ReadTimeout("temporary outage"),
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_try_recover_primary_transport", return_value=True) as recover,
        patch("agent.conversation_loop.time.sleep"),
        patch("agent.conversation_loop.jittered_backoff", return_value=0),
    ):
        result = agent.run_conversation("continue the task")

    assert result["completed"] is False
    assert result.get("interrupted") is not True
    assert recover.call_count == 1
