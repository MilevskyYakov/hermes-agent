"""Tests for external skill directories (skills.external_dirs config)."""

import json
import os
from unittest.mock import patch

import pytest


@pytest.fixture
def external_skills_dir(tmp_path):
    """Create a temp dir with a sample external skill."""
    ext_dir = tmp_path / "external-skills"
    skill_dir = ext_dir / "my-external-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-external-skill\ndescription: A skill from an external directory\n---\n\n# My External Skill\n\nDo external things.\n"
    )
    return ext_dir


@pytest.fixture
def hermes_home(tmp_path):
    """Create a minimal HERMES_HOME with config."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    return home


class TestGetExternalSkillsDirs:
    def test_empty_config(self, hermes_home):
        (hermes_home / "config.yaml").write_text("skills:\n  external_dirs: []\n")
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_external_skills_dirs
            result = get_external_skills_dirs()
        assert result == []


    def test_valid_dir_returned(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_external_skills_dirs
            result = get_external_skills_dirs()
        assert len(result) == 1
        assert result[0] == external_skills_dir.resolve()






class TestGetAllSkillsDirs:
    def test_local_always_first(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_all_skills_dirs
            result = get_all_skills_dirs()
        assert result[0] == hermes_home / "skills"
        assert result[1] == external_skills_dir.resolve()


class TestExternalSkillsInFindAll:
    def test_external_skills_found(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        local_skills = hermes_home / "skills"
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import _find_all_skills
            skills = _find_all_skills()
        names = [s["name"] for s in skills]
        assert "my-external-skill" in names

    def test_local_takes_precedence(self, hermes_home, external_skills_dir):
        """If the same skill name exists locally and externally, local wins."""
        local_skills = hermes_home / "skills"
        local_skill = local_skills / "my-external-skill"
        local_skill.mkdir(parents=True)
        (local_skill / "SKILL.md").write_text(
            "---\nname: my-external-skill\ndescription: Local version\n---\n\nLocal.\n"
        )
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import _find_all_skills
            skills = _find_all_skills()
        matching = [s for s in skills if s["name"] == "my-external-skill"]
        assert len(matching) == 1
        assert matching[0]["description"] == "Local version"


@pytest.mark.parametrize("support_dir", ["provenance", "adapters"])
def test_support_docs_do_not_shadow_skills(hermes_home, external_skills_dir, monkeypatch, support_dir):
    from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
    from tools import skills_tool  # Register the real skill_view handler.
    from tools.registry import registry

    package = external_skills_dir / "my-external-skill"
    support = package / support_dir / "example"
    support.mkdir(parents=True)
    (support / "my-external-skill.md").write_text("Support document.\n", encoding="utf-8")
    (support / "SKILL.md").write_text("---\nname: embedded-instruction\n---\nSupport only.\n", encoding="utf-8")
    category_skill = external_skills_dir / support_dir / "category-skill"
    category_skill.mkdir(parents=True)
    (category_skill / "SKILL.md").write_text("---\nname: category-skill\n---\nStandalone.\n", encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        json.dumps({"skills": {"external_dirs": [str(external_skills_dir)]}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.chdir(hermes_home)
    clear_skills_system_prompt_cache(clear_snapshot=True)
    try:
        result = registry.dispatch("skill_view", {"name": "my-external-skill"})
        assert isinstance(result, str)
        loaded = json.loads(result)
        assert loaded["success"], loaded
        assert loaded["_source_path"] == str(package / "SKILL.md")
        result = registry.dispatch("skill_view", {
            "name": "my-external-skill", "file_path": f"{support_dir}/example/my-external-skill.md",
        })
        assert isinstance(result, str)
        reference = json.loads(result)
        assert reference["success"] and "Support document." in reference["content"]
        prompt = build_skills_system_prompt()
        assert "embedded-instruction" not in prompt
        assert "- category-skill" in prompt
    finally:
        clear_skills_system_prompt_cache(clear_snapshot=True)


class TestExternalSkillView:
    def test_skill_view_finds_external(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        local_skills = hermes_home / "skills"
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import skill_view
            result = json.loads(skill_view("my-external-skill"))
        assert result["success"] is True
        assert "external things" in result["content"]


@pytest.mark.parametrize("source", ["local", "external", "project"])
def test_activation_labels_survive_discovery_and_snapshot(hermes_home, tmp_path, monkeypatch, source):
    from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache

    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    directory = {
        "local": hermes_home / "skills",
        "external": tmp_path / "external",
        "project": project / ".hermes/skills",
    }[source]
    cases = {
        "core": ({"auto": "core", "direct": True, "dependency": True}, "[core]"),
        "manual": ({"auto": "none", "direct": True, "dependency": True}, "[manual]"),
        "always": ({"auto": "none", "always": ["hermes"], "direct": True}, "[capability]"),
        "helper": ({"auto": "none", "dependency": True}, "[capability]"),
        "legacy": ({}, ""),
    }
    for name, (activation, _) in cases.items():
        target = directory / name / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            "---\n" + json.dumps({"name": name, "description": f"Use {name}.",
                                  "metadata": {"gerda": {"activation": activation}}})
            + "\n---\nInstructions.\n", encoding="utf-8",
        )
    (hermes_home / "config.yaml").write_text(json.dumps({"skills": {
        "external_dirs": [str(directory)] if source == "external" else [],
        "trusted_project_dirs": [str(project)],
    }}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.chdir(project)
    clear_skills_system_prompt_cache(clear_snapshot=True)
    try:
        first = build_skills_system_prompt()
        assert build_skills_system_prompt() == first
        clear_skills_system_prompt_cache()
        assert build_skills_system_prompt() == first
        for name, (_, label) in cases.items():
            line = next(line for line in first.splitlines() if line.strip().startswith(f"- {name}:"))
            prefix = "[project] " if source == "project" else ""
            expected = f"{prefix}{label} Use {name}." if label else f"{prefix}Use {name}."
            assert line.strip() == f"- {name}: {expected}"
    finally:
        clear_skills_system_prompt_cache(clear_snapshot=True)
