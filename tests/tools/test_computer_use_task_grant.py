"""Task-scoped opt-in regression tests for computer_use."""

import json
from unittest.mock import patch


def test_capture_without_task_grant_fails_before_backend_start():
    from tools.computer_use import tool as cu_tool

    with patch(
        "tools.approval.request_session_task_approval", return_value="deny"
    ), patch.object(cu_tool, "_get_backend") as get_backend:
        result = json.loads(
            cu_tool.handle_computer_use(
                {"action": "capture", "mode": "ax"}, session_id="task-a"
            )
        )

    assert result["action"] == "task_grant"
    assert "explicit approval" in result["error"]
    get_backend.assert_not_called()


def test_one_task_grant_covers_safe_actions_but_keeps_mutation_gate():
    from tools.computer_use import tool as cu_tool

    prompts = []

    def approve(action, _args, _summary):
        prompts.append(action)
        return "approve_session"

    cu_tool.set_approval_callback(approve)
    with patch.dict(
        "os.environ", {"HERMES_COMPUTER_USE_BACKEND": "noop"}, clear=False
    ):
        capture = json.loads(
            cu_tool.handle_computer_use(
                {"action": "capture", "mode": "ax"}, session_id="task-b"
            )
        )
        apps = json.loads(
            cu_tool.handle_computer_use(
                {"action": "list_apps"}, session_id="task-b"
            )
        )
        click = json.loads(
            cu_tool.handle_computer_use(
                {"action": "click", "element": 1}, session_id="task-b"
            )
        )

    assert capture["mode"] == "ax"
    assert apps["count"] == 0
    assert click["ok"] is True
    assert prompts == ["task_grant", "click"]


def test_cleanup_revokes_task_grant_and_yolo_never_creates_one():
    from tools.computer_use import tool as cu_tool

    prompts = []

    def approve(action, _args, _summary):
        prompts.append(action)
        return "approve_session"

    cu_tool.set_approval_callback(approve)
    with patch.dict(
        "os.environ", {"HERMES_COMPUTER_USE_BACKEND": "noop"}, clear=False
    ):
        cu_tool.handle_computer_use(
            {"action": "capture", "mode": "ax"}, session_id="task-c"
        )
        assert cu_tool.release_computer_use_session("task-c") is True
        cu_tool.handle_computer_use(
            {"action": "capture", "mode": "ax"}, session_id="task-c"
        )

    assert prompts == ["task_grant", "task_grant"]

    cu_tool.release_computer_use_session("task-c")
    cu_tool.set_approval_callback(None)
    with patch(
        "tools.approval.is_approval_bypass_active_for_session", return_value=True
    ), patch(
        "tools.approval.request_session_task_approval", return_value="deny"
    ), patch.object(cu_tool, "_get_backend") as get_backend:
        result = json.loads(
            cu_tool.handle_computer_use(
                {"action": "capture", "mode": "ax"}, session_id="task-yolo"
            )
        )

    assert result["action"] == "task_grant"
    get_backend.assert_not_called()
