from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.session_lifecycle import (
    build_handoff,
    checkpoint_if_due,
    is_safe_boundary,
    transition_if_due,
    validate_handoff,
)


def test_handoff_is_structurally_complete_without_raw_tool_results():
    messages = [
        {"role": "user", "content": "Fix session lifecycle"},
        {
            "role": "assistant",
            "content": "Use existing continuation.",
            "tool_calls": [
                {
                    "id": "1",
                    "function": {
                        "name": "patch",
                        "arguments": '{"path":"agent/session_lifecycle.py"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "very large raw log"},
    ]

    handoff = build_handoff(messages, "task-316")
    validate_handoff(handoff)

    assert handoff["goal"] == "Fix session lifecycle"
    assert handoff["changed_files"] == ["agent/session_lifecycle.py"]
    assert "very large raw log" not in str(handoff)


def test_missing_required_handoff_field_blocks_transition():
    with pytest.raises(ValueError, match="missing handoff fields: next_step"):
        validate_handoff({
            "goal": "g",
            "decisions": [],
            "changed_files": [],
            "checks": [],
            "blocker": "",
            "task_identity": "task",
        })


def test_checkpoint_is_not_rewritten_after_agent_restart():
    class DB:
        def __init__(self):
            self.model_config = None
            self.writes = 0

        def flush_token_counts(self):
            pass

        def get_session(self, _session_id):
            return {"api_call_count": 50, "model_config": self.model_config}

        def update_session_meta(self, _session_id, model_config, model=None):
            self.model_config = model_config
            self.writes += 1

    db = DB()
    base = {
        "session_lifecycle_enabled": True,
        "session_lifecycle_checkpoint_calls": 50,
        "session_api_calls": 0,
        "session_id": "same-session",
        "model": "same-model",
        "_session_db": db,
        "_session_init_model_config": {},
        "_lifecycle_checkpoint_written": False,
    }
    messages = [{"role": "user", "content": "long task"}]

    assert checkpoint_if_due(SimpleNamespace(**base), messages, "task") is True
    assert checkpoint_if_due(SimpleNamespace(**base), messages, "task") is False
    assert db.writes == 1


@pytest.mark.parametrize("unsafe", ["mutation", "test", "deploy", "auth"])
def test_transition_is_blocked_during_unsafe_operation(unsafe, monkeypatch):
    monkeypatch.setattr(
        "tools.process_registry.process_registry.has_active_processes",
        lambda _task: False,
    )
    agent = SimpleNamespace(
        _executing_tools=False,
        _lifecycle_unsafe_operation=unsafe,
        _active_compression_lock_holder=None,
    )
    assert not is_safe_boundary(agent, [], "task")


def test_transition_rotates_on_safe_boundary_and_preserves_identity(monkeypatch):
    monkeypatch.setattr(
        "tools.process_registry.process_registry.has_active_processes",
        lambda _task: False,
    )
    persisted = []

    class DB:
        def flush_token_counts(self):
            pass

        def get_session(self, _session_id):
            return {"api_call_count": 2, "model_config": None}

        def update_session_meta(self, session_id, model_config, model=None):
            persisted.append((session_id, model_config, model))

    def compress(messages, system_message, **kwargs):
        assert kwargs["task_id"] == "task-316"
        assert kwargs["force_session_rotation"] is True
        agent.session_id = "child"
        return list(messages), system_message

    agent = SimpleNamespace(
        session_lifecycle_enabled=True,
        session_lifecycle_transition_calls=2,
        session_api_calls=2,
        session_id="parent",
        model="same-model",
        _session_db=DB(),
        _session_init_model_config={},
        _executing_tools=False,
        _lifecycle_unsafe_operation=None,
        _active_compression_lock_holder=None,
        _compress_context=compress,
        _emit_status=lambda _message: None,
    )
    messages = [{"role": "user", "content": "continue this"}]

    _, _, rotated = transition_if_due(agent, messages, "system", "task-316")

    assert rotated is True
    assert agent.session_id == "child"
    assert agent.model == "same-model"
    assert agent.session_api_calls == 2
    assert agent._lifecycle_call_offset == 2
    assert [row[0] for row in persisted] == ["parent", "child"]
