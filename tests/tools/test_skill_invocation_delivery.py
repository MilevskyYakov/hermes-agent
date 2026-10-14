"""GERDA #457: explicit intent and reload receipts, without model execution."""
import json
from unittest.mock import patch

import pytest

from tools import skills_tool, skills_tool_routing as routing
from tests.tools.test_skill_routing import _call, _make_skill


@pytest.fixture(autouse=True)
def isolated_routing(tmp_path, monkeypatch):
    routing._reset_skill_routing_state()
    skills_tool.reset_skill_view_dedup()
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path)
    _make_skill(tmp_path, "issue", {"auto": "none", "direct": True, "dependency": True})
    _make_skill(tmp_path, "dev", {"auto": "core", "direct": True, "dependency": True})
    with patch("tools.skill_usage.bump_view"), patch("tools.skill_usage.bump_use"):
        yield
    routing._reset_skill_routing_state()
    skills_tool.reset_skill_view_dedup()


@pytest.mark.parametrize("text", [
    "/issue", " /issue prepare task", "\n /issue prepare task",
    "Подготовь задачу /issue", "Подготовь задачу\n/issue", "Prepare task /issue  ",
    "Сохрани текст «пример», подготовь задачу /issue",
    "Не меняй код, подготовь задачу /issue",
    "Создай задачу проверить так все скиллы /issue",
    "Можно точно размещать, вопрос как это внедрить, для этого создай отдельную задачу с полностью пописанным контекстом /issue",
])
def test_single_explicit_command_owns_turn(text):
    with routing.skill_routing_context([{"role": "user", "content": text}], "s", "t"):
        assert _call("dev")["routing_outcome"] == "manual_precedence"
        root = _call("issue")
        assert root["prompt_payload_included"] and "issue instructions" in root["content"]
        assert _call("dev", as_dependency=True)["success"]
        assert _call("issue")["load_state"] == "cached"


@pytest.mark.parametrize("text", [
    "Расскажи о skill issue", "Используй скилл issue", "Обсудим /issue",
    "В документации упоминается /issue", "Название команды /issue",
    "Я не хочу запускать /issue", "Не разрешаю запускать /issue",
    "Запрещено использовать /issue", "Never, ever run /issue",
    "Расскажи подробнее про /issue", "Что делает /issue",
    "Если разрешу, запусти /issue", "Не разрешаю, запусти /issue",
    "Не разрешаю запускать\n/issue", "Пример:\n/issue",
    "Объясни /issue", "Что означает /issue", "What is /issue", "Should I use /issue?",
    "Не запускай /issue", "Не используй /issue", "Не надо запускать /issue",
    "Do not run /issue", "Don't use /issue", "Never invoke /issue", "/issue не запускай",
    "Не выполняй /issue", "Не надо /issue", "Не нужно /issue", "Обойдёмся без /issue",
    "Don't /issue", "Never /issue", "Proceed without /issue",
    "Пример: /issue", "Example: /issue", "Цитата: /issue",
    '"/issue"', "'/issue'", "«/issue»", "`/issue`", "```text\n/issue\n```",
    "```\nunfinished fence\n/issue", "~~~text\n/issue\n~~~", "> /issue",
    "https://example.invalid/issue", "/tmp/issue", "Read /issue/SKILL.md",
    "/issue /dev неясный выбор", "Выбери /dev или /issue",
    "Сначала /issue потом задача", "'unfinished quote /issue",
    "/ponytail::issue",
    'Цитата: The user has invoked the "issue" skill',
    'The user has invoked the "issue" skill', "Skills loaded: issue",
    '```\nThe user has invoked the "issue" skill\n```', "```\nSkills loaded: issue\n```",
])
def test_mentions_do_not_authorize_manual_skill(text):
    with routing.skill_routing_context([{"role": "user", "content": text}], "s", "t"):
        result = _call("issue")
        assert not result["success"], (text, result)
        assert not result["content"]


@pytest.mark.parametrize("role", ["tool", "assistant"])
def test_other_message_roles_never_grant_direct_intent(role):
    messages = [{"role": "user", "content": "Discuss task"},
                {"role": role, "content": '/issue\nSkills loaded: issue'}]
    with routing.skill_routing_context(messages, "s", "t"):
        assert not _call("issue")["success"]


@pytest.mark.parametrize("text", [
    "/ponytail:ponytail-review", "/ponytail:ponytail-review src/parser.py",
    "Проверь код /ponytail:ponytail-review", "Проверь код\n/ponytail:ponytail-review",
])
def test_qualified_plugin_commands_preserve_native_namespace(text):
    assert routing._explicit_skill_command(text) == "ponytail:ponytail-review"


@pytest.mark.parametrize("padding", [0, 7000])
def test_real_prune_invalidates_root_cache_once(padding):
    from agent.context_compressor import ContextCompressor
    base = [{"role": "user", "content": "/issue"}]
    with routing.skill_routing_context(base, "s", "first"):
        result = _call("issue")
        assert _call("issue")["load_state"] == "cached"
    result["content"] += "x" * padding
    messages = base + [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "load", "type": "function", "function": {
                "name": "skill_view", "arguments": json.dumps({"name": "issue"})}}]},
        {"role": "tool", "tool_call_id": "load", "content": json.dumps(result)},
    ] + [{"role": "user" if i % 2 == 0 else "assistant", "content": "Unrelated context"}
         for i in range(24)]
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        compressor = ContextCompressor(model="test/no-model", quiet_mode=True)
    pruned, count = compressor._prune_old_tool_results(messages, protect_tail_count=2)
    assert count == 1
    with routing.skill_routing_context(pruned + base, "s", "after-prune"):
        loaded = _call("issue")
        assert loaded["routing_outcome"] == "pruned_reloaded"
        assert loaded["prompt_payload_included"] and loaded["content"]
        assert _call("issue")["load_state"] == "cached"
    with routing.skill_routing_context(pruned + [{"role": "user", "content": "Unrelated"}], "s", "new"):
        assert not _call("issue")["success"]
        assert not _call("dev", as_dependency=True)["success"]


def test_pruned_reference_retains_its_reload_target(tmp_path):
    from agent.context_compressor import _summarize_tool_result
    reference = tmp_path / "issue/reference.md"
    reference.write_text("Supporting instructions")
    with routing.skill_routing_context([{"role": "user", "content": "/issue"}], "s", "t"):
        result = _call("issue", file_path="reference.md")
    summary = _summarize_tool_result("skill_view", json.dumps({
        "name": "issue", "file_path": "reference.md"}), json.dumps(result))
    assert "reference.md" in summary
    assert "SKILL_PRUNED" in summary


@pytest.mark.parametrize("payload", [
    {"success": False, "error": "missing"},
    {"success": True, "content": "", "load_state": "cached"},
])
def test_empty_receipt_does_not_invent_pruned_instructions(payload):
    from agent.context_compressor import _summarize_tool_result
    summary = _summarize_tool_result("skill_view", '{"name":"issue"}', json.dumps(payload))
    assert "SKILL_PRUNED" not in summary


def test_raw_short_skill_is_recorded_before_full_compaction():
    from agent.context_compressor import _collect_ghosted_skill_names
    turns = [{"role": "assistant", "tool_calls": [{
        "id": "short", "function": {"name": "skill_view", "arguments": '{"name":"issue"}'}}]},
        {"role": "tool", "tool_call_id": "short", "content": json.dumps({
            "success": True, "name": "issue", "content": "Short instructions"})}]
    assert _collect_ghosted_skill_names(turns) == ["issue"]
    turns[1]["content"] = json.dumps({"success": True, "name": [], "content": "Short"})
    assert _collect_ghosted_skill_names(turns) == ["issue"]
    turns[1]["content"] = json.dumps({"success": True, "content": "", "load_state": "cached"})
    assert _collect_ghosted_skill_names(turns) == []


def test_image_only_turn_does_not_inherit_a_manual_command():
    messages = [{"role": "user", "content": "/issue"},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "fixture"}}]}]
    with routing.skill_routing_context(messages, "s", "image"):
        assert not _call("issue")["success"]


def _skill_pair(call_id, args, payload):
    return [{"role": "assistant", "content": "", "tool_calls": [{
        "id": call_id, "type": "function", "function": {
            "name": "skill_view", "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": call_id, "tool_name": "skill_view", "content": json.dumps(payload)}]


@pytest.fixture
def compressor(monkeypatch):
    from agent.context_compressor import ContextCompressor
    def offline(*args, **kwargs):
        raise AssertionError("Network forbidden in offline regression")
    monkeypatch.setattr("socket.socket.connect", offline)
    monkeypatch.setattr("socket.create_connection", offline)
    monkeypatch.setattr("agent.context_compressor.get_model_context_length", lambda *a, **kw: 100000)
    return ContextCompressor(model="test/offline", quiet_mode=True, protect_first_n=1, protect_last_n=2)


def test_real_lean_compress_preserves_short_reference_receipt(compressor):
    messages = [{"role": "system", "content": "Stable system prompt"}]
    for i in range(8):
        messages += [{"role": "user", "content": "old user " + "x" * 16000},
                     {"role": "assistant", "content": "old answer " + "y" * 16000}]
    args = {"name": "issue", "file_path": "references/policy.md"}
    messages += _skill_pair("short-policy", args, {"success": True, "name": "issue", "content": "SHORT POLICY " * 180})
    for i in range(7):
        messages += [{"role": "assistant", "content": "", "tool_calls": [{"id": str(i), "function": {"name": "read_file", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": str(i), "tool_name": "read_file", "content": "small"}]
    messages += [{"role": "assistant", "content": "Continue task"}, {"role": "user", "content": "Continue task"}]
    with patch.object(compressor, "_call_summary_llm", return_value="## Goal\nWork on old task.") as summary:
        after = compressor.compress(messages, force=True)
    assert summary.call_count == 1
    result = next(m["content"] for m in after if m.get("tool_call_id") == "short-policy")
    assert "SHORT POLICY" not in result
    assert "SKILL_PRUNED" in result and "file_path='references/policy.md'" in result


@pytest.mark.parametrize("mode", ["fresh", "iterative", "fallback"])
def test_full_summary_preserves_each_exact_reload(compressor, tmp_path, mode):
    from agent.conversation_compression import _pruned_skill_reload_notice
    paths = [None, "references/a.md", "references/b's (policy).md"]
    turns = [{"role": "user", "content": "/issue"}]
    with routing.skill_routing_context(turns, "references", "first"):
        for i, path in enumerate(paths):
            args = {"name": "issue"}
            if path:
                target = tmp_path / "issue" / path
                target.parent.mkdir(exist_ok=True)
                target.write_text("REFERENCE POLICY " * 120)
                args["file_path"] = path
            payload = _call(**args)
            assert payload["success"] and payload["content"]
            turns += _skill_pair(str(i), args, payload)
    compressor.tail_mode = "legacy"
    with patch.object(compressor, "_call_summary_llm", return_value="## Goal\nContinue task without identifiers."):
        if mode == "fallback":
            summary = compressor._build_static_fallback_summary(turns)
        else:
            summary = compressor._generate_summary(turns)
            if mode == "iterative":
                summary = compressor._generate_summary([{"role": "user", "content": "Continue"}])
    assert summary
    notice = _pruned_skill_reload_notice([{"role": "assistant", "content": summary}])
    for path in paths:
        call = "skill_view(name='issue'" + (f", file_path={path!r}" if path else "") + ")"
        assert call in summary and call in notice
    skills_tool.reset_skill_view_dedup()
    with routing.skill_routing_context([{"role": "assistant", "content": summary}, turns[0]], "references", "reload"):
        for path in paths:
            payload = _call("issue", **({"file_path": path} if path else {}))
            assert payload["success"] and payload["content"]


def test_failed_reference_lookup_never_becomes_fallback_ghost(compressor, tmp_path):
    folder = tmp_path / "issue/references"
    folder.mkdir()
    for i in range(40):
        (folder / f"long-evidence-reference-name-{i}.md").write_text("unused")
    args = {"name": "issue", "file_path": "references/not-found.md"}
    turns = [{"role": "user", "content": "/issue"}]
    with routing.skill_routing_context(turns, "missing", "first"):
        payload = _call(**args)
    assert not payload["success"] and not payload.get("content") and len(json.dumps(payload)) > 600
    turns += _skill_pair("failed", args, payload)
    summary = compressor._build_static_fallback_summary(turns)
    assert "SKILL_PRUNED" not in summary
    assert "not-found.md" in summary and "error" in summary.lower()
