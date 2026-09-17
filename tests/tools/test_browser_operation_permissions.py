"""Browser discovery/execution must not bootstrap or silently share a tab."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import browser_tool as bt
from tools import browser_tool_install as install
from tools import browser_tool_session as session
from tools import browser_use_cli as cli


def test_uvx_is_not_an_installed_browser(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name, path=None: "/fake/uvx" if name == "uvx" else None)
    assert cli._find_cli_unpatched() is None


def test_agent_browser_discovery_never_installs(monkeypatch):
    monkeypatch.setattr(bt, "_agent_browser_resolved", False)
    monkeypatch.setattr(install, "_agent_browser_candidates", lambda path: iter(()))
    monkeypatch.setattr(install, "_resolve_npx_bin", lambda: "/fake/npx")
    bootstrap = Mock(return_value=False)
    monkeypatch.setattr("hermes_cli.dep_ensure.ensure_dependency", bootstrap)
    for validate in (False, True):
        with pytest.raises(FileNotFoundError):
            install._find_agent_browser(validate=validate)
    bootstrap.assert_not_called()


def test_missing_chromium_blocks_before_bootstrap(monkeypatch):
    monkeypatch.setattr(install, "_find_agent_browser", lambda: "/fake/agent-browser")
    monkeypatch.setattr(install, "_requires_real_termux_browser_install", lambda path: False)
    monkeypatch.setattr(install, "_chromium_installed", lambda: False)
    monkeypatch.setattr("tools.browser_tool_cloud._is_local_mode", lambda: True)
    monkeypatch.setattr("tools.browser_tool_cloud._get_browser_engine", lambda: "auto")
    bootstrap = Mock(return_value=True)
    monkeypatch.setattr(install, "_maybe_autoinstall_chromium", bootstrap)
    assert session._browser_command_preflight()["success"] is False
    bootstrap.assert_not_called()


@pytest.mark.parametrize("failure", ["pid", "create", "switch", "missing_target", None])
def test_shared_tab_preamble_fails_closed(tmp_path, monkeypatch, failure):
    pid = tmp_path / "daemon.pid"
    pid.write_text("" if failure == "pid" else "12345")
    monkeypatch.setitem(sys.modules, "browser_harness", SimpleNamespace(_ipc=SimpleNamespace(pid_path=lambda name: pid)))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("BU_NAME", "owned-test")
    create = Mock(return_value={} if failure == "missing_target" else {"targetId": "owned"})
    switch = Mock()
    if failure == "create":
        create.side_effect = RuntimeError("offline creation failure")
    if failure == "switch":
        switch.side_effect = RuntimeError("offline switch failure")
    namespace = {"cdp": create, "switch_tab": switch}
    payload = cli._OWN_TAB_PREAMBLE + "\npayload_ran = True\n"
    if failure:
        with pytest.raises(RuntimeError):
            exec(payload, namespace)
        assert "payload_ran" not in namespace
        assert not list(tmp_path.glob("hermes-bu-owntab-*"))
    else:
        exec(payload, namespace)
        assert namespace["payload_ran"] is True
        switch.assert_called_once_with("owned")
        exec(payload, namespace)
        create.assert_called_once()
