"""Shared runtime guidance must preserve the task's permission boundaries."""
from agent.coding_context import CODING_AGENT_GUIDANCE
from agent.prompt_builder import SKILLS_GUIDANCE, _render_skills_index, build_memory_guidance


def test_all_skill_write_guidance_requires_explicit_permission():
    blocks = [
        SKILLS_GUIDANCE,
        build_memory_guidance(True, True),
        build_memory_guidance(False, True),
        _render_skills_index({"general": [("sample", "Sample skill")]}, {}, None, None),
    ]
    for block in blocks:
        assert "explicit permission" in block
    assert "fix it with skill_manage" not in blocks[-1]
    assert "update it before finishing" not in blocks[-1]


def test_retry_guidance_requires_diagnosis_not_an_attempt_count():
    assert "three attempts" not in CODING_AGENT_GUIDANCE
    assert "reassess the diagnosis" in CODING_AGENT_GUIDANCE
    assert "missing permission" in CODING_AGENT_GUIDANCE
