"""Routing/load invariants for portable skill activation metadata."""

import json
from unittest.mock import patch

import pytest

from tools.skills_tool import (
    _reset_skill_routing_state,
    _skill_view_with_bump,
    reset_skill_view_dedup,
    skill_routing_context,
)


def _make_skill(skills_dir, name, activation):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {name} workflow.\n"
        "metadata:\n"
        "  gerda:\n"
        "    activation:\n"
        + "".join(f"      {key}: {json.dumps(value)}\n" for key, value in activation.items())
        + "---\n\n"
        f"# {name}\n\n{name} instructions.\n",
        encoding="utf-8",
    )


def _call(name, *, as_dependency=False):
    return json.loads(
        _skill_view_with_bump(
            {"name": name, "as_dependency": as_dependency}, task_id="task"
        )
    )


@pytest.fixture(autouse=True)
def _routing_state():
    _reset_skill_routing_state()
    reset_skill_view_dedup()
    with (
        patch("tools.skill_usage.bump_view"),
        patch("tools.skill_usage.bump_use"),
    ):
        yield
    _reset_skill_routing_state()
    reset_skill_view_dedup()


def test_only_one_auto_core_per_turn(tmp_path):
    _make_skill(tmp_path, "hub", {"auto": "core", "direct": True})
    _make_skill(tmp_path, "dev", {"auto": "core", "direct": True})

    messages = [{"role": "user", "content": "Разбери текущий Хаб"}]
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        messages, "session", "turn"
    ):
        first = _call("hub")
        second = _call("dev")

    assert first["routing_outcome"] == "core_selected"
    assert second["success"] is False
    assert second["routing_outcome"] == "second_core_blocked"
    assert second["prompt_payload_included"] is False


def test_selected_core_drives_session_toolset_without_second_classifier(tmp_path):
    _make_skill(tmp_path, "dev", {"auto": "core", "direct": True})
    agent = object()

    with (
        patch("tools.skills_tool.SKILLS_DIR", tmp_path),
        patch("agent.session_toolsets.select_core_preset") as select_preset,
        skill_routing_context(
            [{"role": "user", "content": "Исправь код"}],
            "session",
            "turn",
            agent=agent,
        ),
    ):
        result = _call("dev")

    assert result["routing_outcome"] == "core_selected"
    select_preset.assert_called_once_with(agent, "dev")


def test_manual_workflow_can_select_matching_session_preset(tmp_path):
    _make_skill(
        tmp_path,
        "imagegen",
        {"auto": "none", "direct": True, "slash": ["imagegen"]},
    )
    agent = object()

    with (
        patch("tools.skills_tool.SKILLS_DIR", tmp_path),
        patch("agent.session_toolsets.select_core_preset") as select_preset,
        skill_routing_context(
            [{"role": "user", "content": "/imagegen"}],
            "session",
            "turn",
            agent=agent,
        ),
    ):
        result = _call("imagegen")

    assert result["routing_outcome"] == "manual_selected"
    select_preset.assert_called_once_with(agent, "imagegen")


def test_bare_issue_url_selects_core_not_manual_issue_skill(tmp_path):
    _make_skill(tmp_path, "dev", {"auto": "core", "direct": True})
    _make_skill(tmp_path, "issue", {"auto": "none", "direct": True})

    messages = [{"role": "user", "content": "https://github.com/acme/repo/issues/1"}]
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        messages, "session", "turn"
    ):
        manual = _call("issue")
        core = _call("dev")

    assert manual["routing_outcome"] == "manual_trigger_required"
    assert core["routing_outcome"] == "core_selected"


def test_direct_grill_wins_over_auto_core(tmp_path):
    _make_skill(tmp_path, "grill", {"auto": "none", "direct": True})
    _make_skill(tmp_path, "dev", {"auto": "core", "direct": True})

    messages = [{
        "role": "user",
        "content": (
            '[IMPORTANT: The user has invoked the "grill" skill, indicating they want '
            "you to follow its instructions. The full skill content is loaded below.]"
        ),
    }]
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        messages, "session", "turn"
    ):
        core = _call("dev")
        manual = _call("grill")

    assert core["routing_outcome"] == "manual_precedence"
    assert manual["routing_outcome"] == "manual_selected"


def test_explicit_taskfinish_loads(tmp_path):
    _make_skill(tmp_path, "taskfinish", {"auto": "none", "direct": True})

    messages = [{"role": "user", "content": "/taskfinish"}]
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        messages, "session", "turn"
    ):
        result = _call("taskfinish")

    assert result["success"] is True
    assert result["routing_outcome"] == "manual_selected"


def test_always_capability_loads_without_workflow(tmp_path):
    _make_skill(
        tmp_path,
        "caveman",
        {"auto": "none", "always": ["hermes"], "direct": True},
    )

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        [{"role": "user", "content": "Обычная задача"}], "session", "turn"
    ):
        result = _call("caveman")

    assert result["routing_outcome"] == "capability_loaded"


def test_dependency_does_not_consume_second_core_slot(tmp_path):
    _make_skill(tmp_path, "hub", {"auto": "core", "direct": True})
    _make_skill(tmp_path, "helper", {"auto": "none", "dependency": True})
    _make_skill(tmp_path, "dev", {"auto": "core", "direct": True})

    messages = [{"role": "user", "content": "Проверь Хаб"}]
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        messages, "session", "turn"
    ):
        assert (
            _call("helper", as_dependency=True)["routing_outcome"]
            == "dependency_context_required"
        )
        assert _call("hub")["routing_outcome"] == "core_selected"
        assert _call("helper", as_dependency=True)["routing_outcome"] == "dependency_loaded"
        assert _call("dev")["routing_outcome"] == "second_core_blocked"


def test_duplicate_load_is_cached_without_prompt_payload(tmp_path):
    _make_skill(tmp_path, "hub", {"auto": "core", "direct": True})
    _make_skill(tmp_path, "dev", {"auto": "core", "direct": True})
    messages = [{"role": "user", "content": "Проверь Хаб"}]

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        messages, "session", "turn"
    ):
        first = _call("hub")
        second = _call("hub")

    assert first["prompt_payload_included"] is True
    assert "hub instructions" in first["content"]
    assert second["load_state"] == "cached"
    assert second["content"] == ""
    assert second["prompt_payload_included"] is False

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        messages, "session", "turn-2"
    ):
        assert _call("hub")["load_state"] == "cached"
        assert _call("dev")["routing_outcome"] == "second_core_blocked"


def test_cached_manual_still_requires_direct_trigger(tmp_path):
    _make_skill(tmp_path, "taskfinish", {"auto": "none", "direct": True})

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        [{"role": "user", "content": "/taskfinish"}], "session", "turn-1"
    ):
        assert _call("taskfinish")["routing_outcome"] == "manual_selected"

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        [{"role": "user", "content": "Обычная задача"}], "session", "turn-2"
    ):
        result = _call("taskfinish")

    assert result["routing_outcome"] == "manual_trigger_required"
    assert result["prompt_payload_included"] is False


def test_pruned_skill_reloads_once_per_new_marker(tmp_path):
    _make_skill(tmp_path, "hub", {"auto": "core", "direct": True})
    base = [{"role": "user", "content": "Проверь Хаб"}]

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        base, "session", "turn-1"
    ):
        assert _call("hub")["load_state"] == "loaded"
        assert _call("hub")["load_state"] == "cached"

    marker = (
        "[SKILL_PRUNED: content lost in compression; "
        "reload with skill_view(name='hub')]"
    )
    messages = [{"role": "user", "content": marker}, *base]
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        messages, "session", "turn-2"
    ):
        reloaded = _call("hub")
        cached = _call("hub")

    assert reloaded["routing_outcome"] == "pruned_reloaded"
    assert reloaded["prompt_payload_included"] is True
    assert cached["load_state"] == "cached"
