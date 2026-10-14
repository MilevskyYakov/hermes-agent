"""User-dispatched plugin injections must reach the TUI, not an unread CLI queue."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from queue import SimpleQueue
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tui_gateway import server

COMMANDS = ["ponytail-review", "ponytail-audit", "ponytail-debt", "ponytail-gain", "ponytail-help"]


@pytest.fixture
def context():
    manager = PluginManager()
    return PluginContext(PluginManifest(name="ponytail", source="user"), manager), manager


def dispatch(handler, command, method):
    sid = "plugin-delivery-test"
    with patch.dict(server._sessions, {sid: {"session_key": sid, "agent": None, "running": False}}), \
         patch("hermes_cli.plugins.get_plugin_command_handler", return_value=handler), \
         patch.object(server, "_is_profile_skill_command", return_value=False), \
         patch.object(server, "_bundle_key_for", return_value=None), \
         patch.object(server, "_SlashWorker", side_effect=AssertionError("no worker")):
        params = {"session_id": sid, "command": command + " target", "name": command, "arg": "target"}
        response = server.handle_request({"id": "test", "method": method, "params": params})
        assert response is not None
        return response


@pytest.mark.parametrize("method", ["slash.exec", "command.dispatch"])
@pytest.mark.parametrize("command", COMMANDS)
def test_plugin_prompt_reaches_model_once(context, method, command):
    ctx, manager = context
    cli = SimpleNamespace(_agent_running=False, _pending_input=SimpleQueue(), _interrupt_queue=SimpleQueue())
    manager._cli_ref = cli
    calls = []
    def handler(arg):
        calls.append(arg)
        assert ctx.inject_message("/" + command + "\nTarget: " + arg)
        return "Queued for agent"
    response = dispatch(handler, command, method)
    assert response["result"]["type"] == "send"
    assert response["result"]["message"] == "/" + command + "\nTarget: target"
    assert calls == ["target"]
    assert cli._pending_input.empty() and cli._interrupt_queue.empty()


@pytest.mark.parametrize("method", ["slash.exec", "command.dispatch"])
def test_status_remains_display_only(context, method):
    response = dispatch(lambda _: "Ponytail mode: full", "ponytail", method)
    assert response["result"]["output"] == "Ponytail mode: full"
    assert response["result"].get("type") != "send"


def test_command_failure_discards_injected_prompt(context):
    ctx, _ = context
    def handler(arg):
        ctx.inject_message("must not be sent")
        raise ValueError("failed command")
    response = dispatch(handler, "ponytail-review", "command.dispatch")
    assert response.get("error")
    assert "message" not in response.get("result", {})
    assert ctx.inject_message("after command") is False


def test_command_capture_does_not_authorize_another_recipient(context):
    ctx, _ = context
    def handler(arg):
        assert ctx.inject_message("wrong recipient", session_key="other-session") is False
        return "refused"
    response = dispatch(handler, "ponytail-review", "slash.exec")
    assert response["result"].get("type") != "send"


@pytest.mark.asyncio
async def test_async_plugin_capture_survives_resolver_thread(context):
    ctx, _ = context
    async def handler(arg):
        await asyncio.sleep(0)
        assert ctx.inject_message("async prompt")
        return "queued"
    response = dispatch(handler, "ponytail-review", "slash.exec")
    assert response["result"]["message"] == "async prompt"
    assert ctx.inject_message("after command") is False


@pytest.mark.asyncio
async def test_timed_out_handler_cannot_inject_after_response(context, monkeypatch):
    ctx, _ = context
    release, finished = threading.Event(), threading.Event()
    accepted = []
    async def handler(arg):
        release.wait(timeout=2)
        accepted.append(ctx.inject_message("late prompt"))
        finished.set()
    monkeypatch.setattr("hermes_cli.plugins._PLUGIN_COMMAND_AWAIT_TIMEOUT_SECS", 0.01)
    try:
        response = dispatch(handler, "ponytail-review", "slash.exec")
        assert response["result"].get("type") != "send"
    finally:
        release.set()
    assert finished.wait(timeout=2)
    assert accepted == [False]


def test_parallel_command_captures_do_not_mix_sessions(context):
    ctx, _ = context
    barrier = threading.Barrier(2)
    def handler(arg):
        barrier.wait(timeout=2)
        assert ctx.inject_message(arg)
        return "queued"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(server._run_plugin_command, handler, name, name) for name in ["one", "two"]]
        assert [future.result()["message"] for future in futures] == ["one", "two"]


def test_capture_rejects_foreign_profile_and_empty_payload(context):
    from hermes_cli.plugins_dispatch import capture_plugin_command_messages
    ctx, manager = context
    with capture_plugin_command_messages("session") as messages:
        assert not ctx.inject_message("")
        assert not ctx.inject_message({"not": "text"})
        manager.scope_key = "another-profile"
        assert not ctx.inject_message("foreign")
    assert messages == []
