"""Hidden skills are offer-filtered, not disabled for explicit loads."""
import json

import pytest
import yaml


@pytest.mark.parametrize("platform", ["cli", "telegram"])
def test_hidden_offer_surfaces_preserve_explicit_bundle_load(tmp_path, monkeypatch, platform):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PLATFORM", platform)
    monkeypatch.chdir(tmp_path)
    skills = tmp_path / "skills"
    names = {"visible-probe", "hidden-probe", "platform-probe", "disabled-probe"}
    for name in names:
        directory = skills / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Probe skill\n---\nPayload for {name}.\n",
            encoding="utf-8",
        )
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"skills": {
        "hidden": '["hidden-probe"]',
        "platform_hidden": {"telegram": ["platform-probe"]},
        "disabled": ["disabled-probe"],
    }}), encoding="utf-8")
    bundles = tmp_path / "skill-bundles"
    bundles.mkdir()
    (bundles / "probe.yaml").write_text(yaml.safe_dump({
        "name": "probe", "skills": ["hidden-probe/SKILL", "disabled-probe/SKILL"],
    }), encoding="utf-8")

    from agent.prompt_builder import build_skills_system_prompt
    from agent.skill_bundles import build_bundle_invocation_message, reload_bundles
    from agent.skill_commands import scan_skill_commands
    from tools.skills_tool import _find_all_skills, skills_list, skill_view

    offered = names - {"hidden-probe", "disabled-probe"}
    if platform == "telegram":
        offered.remove("platform-probe")
    assert {s["name"] for s in json.loads(skills_list())["skills"]} == offered
    assert set(scan_skill_commands()) == {f"/{name}" for name in offered}
    prompt = build_skills_system_prompt()
    for name in names:
        assert (name in prompt) == (name in offered)
    assert {s["name"] for s in _find_all_skills(skip_disabled=True)} == names
    assert json.loads(skill_view("hidden-probe", preprocess=False))["success"]
    assert not json.loads(skill_view("disabled-probe", preprocess=False))["success"]
    reload_bundles()
    result = build_bundle_invocation_message("/probe", platform=platform)
    assert result is not None
    message, loaded, missing = result
    assert loaded == ["hidden-probe"]
    assert "hidden-probe/SKILL" not in missing
    assert "Payload for hidden-probe." in message
    assert "Payload for disabled-probe." not in message
