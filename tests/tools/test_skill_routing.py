"""Routing/load invariants for portable skill activation metadata."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skills_tool import (
    _reset_skill_routing_state,
    reset_skill_view_dedup,
    skill_routing_context,
)
from tools.registry import registry


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


def _call(name, *, as_dependency=False, **kwargs):
    result = registry.dispatch(
        "skill_view", {"name": name, "as_dependency": as_dependency, **kwargs}, task_id="task"
    )
    assert isinstance(result, str)
    return json.loads(result)


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
        assert _call("helper")["routing_outcome"] == "dependency_flag_required"
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


@pytest.mark.parametrize("support_auto", ["core", "none"])
@pytest.mark.parametrize("owner_auto", ["core", "none"])
@pytest.mark.parametrize("preload", [False, True])
def test_dependency_keeps_owner_and_intrinsic_role(tmp_path, support_auto, owner_auto, preload):
    """Real external-directory discovery and dispatch, including warm session routing."""
    from hermes_constants import get_hermes_home

    external = tmp_path / "external"
    for name, auto in (("owner", owner_auto), ("support", support_auto)):
        _make_skill(external, name, {"auto": auto, "direct": True, "dependency": True})
    (get_hermes_home() / "config.yaml").write_text(
        json.dumps({"skills": {"external_dirs": [str(external)]}}), encoding="utf-8"
    )

    if preload:
        with skill_routing_context([{"role": "user", "content": "/support"}], "roles", "preload"):
            assert _call("support")["success"]

    with skill_routing_context([{"role": "user", "content": "A task"}], "roles", "no-owner"):
        assert _call("support", as_dependency=True)["routing_outcome"] == "dependency_context_required"

    with (
        patch("agent.session_toolsets.select_core_preset") as select_preset,
        skill_routing_context([{"role": "user", "content": "/owner"}], "roles", "work", agent=object()),
    ):
        assert _call("owner")["success"]
        select_preset.reset_mock()
        assert not _call("support")["success"]
        dependency = _call("support", as_dependency=True)
        assert dependency["success"], dependency
        assert dependency["load_state"] == ("cached" if preload else "loaded")
        assert dependency["prompt_payload_included"] is not preload
        if not preload:
            assert Path(dependency["_source_path"]) == external / "support/SKILL.md"
        select_preset.assert_not_called()
        assert not _call("support")["success"]
        assert _call("owner")["load_state"] == "cached"
        select_preset.assert_not_called()

    with skill_routing_context([{"role": "user", "content": "/support"}], "roles", "standalone"):
        assert _call("support")["load_state"] == "cached"
        assert not _call("owner")["success"]
        assert _call("owner", as_dependency=True)["success"]

    with skill_routing_context([{"role": "user", "content": "A task"}], "roles", "no-owner-again"):
        assert _call("support", as_dependency=True)["routing_outcome"] == "dependency_context_required"
        if support_auto == "none":
            assert _call("support")["routing_outcome"] == "manual_trigger_required"


@pytest.mark.parametrize("activation", [
    {"auto": "core", "direct": True},
    {"auto": "none", "direct": True},
    {"auto": "none", "always": ["hermes"], "direct": True},
])
def test_dependency_flag_never_grants_undeclared_permission(tmp_path, activation):
    _make_skill(tmp_path, "owner", {"auto": "core", "direct": True})
    _make_skill(tmp_path, "support", activation)
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        for turn in ("cold", "warm"):
            with skill_routing_context([{"role": "user", "content": "/owner"}], "permission", turn):
                assert _call("owner")["success"]
                denied = _call("support", as_dependency=True)
                assert denied["routing_outcome"] == "dependency_not_allowed"
                assert not denied["prompt_payload_included"]
            with skill_routing_context([{"role": "user", "content": "/support"}], "permission", turn + "-direct"):
                assert _call("support")["success"]


def test_categorized_cache_hit_selects_current_workflow(tmp_path):
    _make_skill(tmp_path / "category", "owner", {"auto": "core", "direct": True})
    _make_skill(tmp_path, "helper", {"auto": "none", "dependency": True})
    _make_skill(tmp_path, "other", {"auto": "core", "direct": True})
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        with skill_routing_context([{"role": "user", "content": "Task"}], "alias", "first"):
            assert _call("owner")["success"]
        with skill_routing_context([{"role": "user", "content": "Task"}], "alias", "second"):
            assert _call("category/owner")["load_state"] == "cached"
            assert _call("helper", as_dependency=True)["success"]
            assert _call("other")["routing_outcome"] == "second_core_blocked"


def test_changed_activation_is_not_authorized_from_cache(tmp_path):
    _make_skill(tmp_path, "owner", {"auto": "core", "direct": True})
    _make_skill(tmp_path, "helper", {"auto": "none", "direct": True, "dependency": True})
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), skill_routing_context(
        [{"role": "user", "content": "Task"}], "freshness", "turn"
    ):
        assert _call("owner")["success"]
        assert _call("helper", as_dependency=True)["success"]
        target = tmp_path / "helper/SKILL.md"
        target.write_text(target.read_text(encoding="utf-8").replace("dependency: true", "dependency: false"), encoding="utf-8")
        assert _call("helper", as_dependency=True)["routing_outcome"] == "dependency_not_allowed"


@pytest.mark.parametrize("file_path", ["SKILL.md", "./SKILL.md", "root-link.md"])
def test_root_file_routes_like_normal_skill(tmp_path, file_path):
    _make_skill(tmp_path, "manual", {"auto": "none", "direct": True})
    (tmp_path / "manual/root-link.md").symlink_to("SKILL.md")
    (tmp_path / "manual/reference.md").write_text("Supporting knowledge.", encoding="utf-8")
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        with skill_routing_context([{"role": "user", "content": "Task"}], "root-file", "cold"):
            assert _call("manual", file_path=file_path)["routing_outcome"] == "manual_trigger_required"
            assert _call("manual", file_path="reference.md")["success"]
        with skill_routing_context([{"role": "user", "content": "/manual"}], "root-file", "direct"):
            assert _call("manual", file_path=file_path)["routing_outcome"] == "manual_selected"
            assert _call("manual")["load_state"] == "cached"
        with skill_routing_context([{"role": "user", "content": "Task"}], "root-file", "warm"):
            assert _call("manual", file_path=file_path)["routing_outcome"] == "manual_trigger_required"


@pytest.mark.parametrize("surface", ["single", "stacked", "bundle", "tui"])
def test_slash_preload_owns_turn_without_duplicate_content(tmp_path, monkeypatch, surface):
    from hermes_constants import get_hermes_home
    from agent.skill_commands import build_skill_invocation_message

    _make_skill(tmp_path, "owner", {"auto": "none", "direct": True})
    _make_skill(tmp_path, "helper", {"auto": "core", "direct": True, "dependency": True})
    _make_skill(tmp_path, "style", {"auto": "none", "always": ["hermes"]})
    (get_hermes_home() / "config.yaml").write_text(json.dumps({"skills": {"external_dirs": [str(tmp_path)]}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    if surface == "stacked":
        from agent.skill_commands import build_stacked_skill_invocation_message
        built = build_stacked_skill_invocation_message(["/style", "/owner"], "Use helper", task_id="task")
        assert built is not None
        message, _, _ = built
    elif surface == "bundle":
        from agent.skill_bundles import build_bundle_invocation_message
        bundles = get_hermes_home() / "skill-bundles"
        bundles.mkdir()
        (bundles / "workflow.yaml").write_text(json.dumps({"name": "workflow", "skills": ["style", "owner"]}), encoding="utf-8")
        built = build_bundle_invocation_message("/workflow", "Use helper", task_id="task")
        assert built is not None
        message, _, _ = built
    elif surface == "tui":
        from tui_gateway import server
        response = getattr(server, "_dispatch_skill")(1, {}, {"session_key": "task"}, "owner", "Use helper")
        assert response is not None
        message = response["result"]["message"]
    else:
        message = build_skill_invocation_message("/owner", "Use helper", task_id="task")
    assert message and "owner instructions" in message
    with skill_routing_context([{"role": "user", "content": message}], "slash", "turn"):
        assert _call("helper", as_dependency=True)["success"]
        owner = _call("owner")
        assert owner["load_state"] == "cached" and not owner["prompt_payload_included"]
        assert not _call("helper")["success"]
    with skill_routing_context([{"role": "user", "content": "Unrelated task"}], "slash", "next"):
        assert _call("helper", as_dependency=True)["routing_outcome"] == "dependency_context_required"
    quoted = '[IMPORTANT: The user has invoked the "owner" skill.]'
    with skill_routing_context([{"role": "user", "content": quoted}], "slash", "quoted"):
        assert _call("helper", as_dependency=True)["routing_outcome"] == "dependency_context_required"
