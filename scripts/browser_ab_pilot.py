"""Reproduce the browser A/B pilot from GERDA2.0 issue #356.

Run with ``agent-browser`` on PATH:
``uv run --extra mcp python scripts/browser_ab_pilot.py``.
The fixture server and both browser profiles are local and isolated.
"""

import asyncio
import importlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = ""
PAGES = {
    "/data.html": """<!doctype html><html><body><nav><a href='/data.html'>Data</a> <a href='/form.html'>Form</a> <a href='/dialog.html'>Dialog</a></nav><h1>Pilot Data</h1><table><tr><th>Item</th><th>Value</th></tr><tr><td>Alpha</td><td>11</td></tr><tr><td>Beta</td><td>22</td></tr><tr><td>Gamma</td><td>33</td></tr></table></body></html>""",
    "/form.html": """<!doctype html><html><body><h1>Test Form</h1><label for='name'>Name</label><input id='name'><button id='save' onclick=\"document.getElementById('result').textContent='Saved: '+document.getElementById('name').value\">Save</button><p id='result'>Not saved</p></body></html>""",
    "/dialog.html": """<!doctype html><html><body><h1>Dialog Flow</h1><button id='start' onclick=\"document.getElementById('step2').hidden=false\">Continue</button><section id='step2' hidden><button id='confirm' onclick=\"if(confirm('Proceed with pilot?')) document.getElementById('result').textContent='Dialog accepted'\">Confirm</button></section><p id='result'>Pending</p></body></html>""",
}


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        if body is None:
            self.send_error(404)
            return
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        pass


@dataclass
class Metric:
    backend: str
    scenario: str
    completed: bool
    tool_calls: int
    retries: int
    wall_seconds: float
    response_chars: int
    errors: list[str]
    desktop_interference: bool = False


def ref_for(text: str, label: str) -> str:
    match = re.search(rf'{re.escape(label)}[^\n]*ref=(e\d+)', text)
    if not match:
        raise ValueError(f"missing ref for {label!r}")
    return "@" + match.group(1)


def run_builtin() -> list[Metric]:
    from tools.browser_dialog_tool import browser_dialog
    from tools.browser_tool import (
        browser_click,
        browser_navigate,
        browser_console,
        browser_snapshot,
        browser_type,
        cleanup_browser,
    )

    metrics = []

    def run(name, body):
        task = f"pilot-builtin-{name}"
        calls = []
        errors = []
        start = time.perf_counter()
        try:
            completed = body(task, calls)
        except Exception as exc:
            completed = False
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            cleanup_browser(task)
        metrics.append(Metric(
            "built-in browser", name, completed, len(calls), 0,
            round(time.perf_counter() - start, 3), sum(len(x) for x in calls), errors,
        ))

    def call(calls, fn, *args, **kwargs):
        result = fn(*args, **kwargs)
        calls.append(str(result))
        payload = json.loads(result)
        if not payload.get("success", False):
            raise RuntimeError(payload)
        return result

    def data(task, calls):
        out = call(calls, browser_navigate, f"{BASE}/data.html", task_id=task)
        return all(value in out for value in ("Alpha", "22", "Gamma"))

    def form(task, calls):
        nav = call(calls, browser_navigate, f"{BASE}/form.html", task_id=task)
        call(calls, browser_type, ref_for(nav, "Name"), "Yakov", task_id=task)
        call(calls, browser_click, ref_for(nav, "Save"), task_id=task)
        result = call(
            calls, browser_console, expression="document.body.innerText", task_id=task
        )
        if "Saved: Yakov" not in result:
            raise RuntimeError(f"verification failed: {result}")
        return True

    def dialog(task, calls):
        nav = call(calls, browser_navigate, f"{BASE}/dialog.html", task_id=task)
        call(calls, browser_click, ref_for(nav, "Continue"), task_id=task)
        snap = call(calls, browser_snapshot, task_id=task)
        call(calls, browser_click, ref_for(snap, "Confirm"), task_id=task)
        call(calls, browser_dialog, "accept", task_id=task)
        final = call(calls, browser_snapshot, task_id=task)
        return "Dialog accepted" in final

    run("structured extraction", data)
    run("form submit", form)
    run("multi-step dialog", dialog)
    return metrics


async def run_playwright() -> list[Metric]:
    mcp = importlib.import_module("mcp")
    stdio_client = importlib.import_module("mcp.client.stdio").stdio_client
    ClientSession = mcp.ClientSession
    StdioServerParameters = mcp.StdioServerParameters
    params = StdioServerParameters(
        command="npx",
        args=[
            "--yes", "@playwright/mcp@0.0.79", "--headless", "--isolated",
            "--image-responses", "omit", "--codegen", "none",
        ],
    )
    metrics = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def run(name, body):
                calls = []
                errors = []
                start = time.perf_counter()
                try:
                    completed = await body(calls)
                except Exception as exc:
                    completed = False
                    errors.append(f"{type(exc).__name__}: {exc}")
                metrics.append(Metric(
                    "Playwright MCP", name, completed, len(calls), 0,
                    round(time.perf_counter() - start, 3),
                    sum(len(x) for x in calls), errors,
                ))

            async def call(calls, name, args):
                result = await session.call_tool(name, args)
                text = "\n".join(getattr(item, "text", "") for item in result.content)
                calls.append(text)
                if result.isError:
                    raise RuntimeError(text)
                return text

            async def snapshot(calls):
                return await call(calls, "browser_snapshot", {})

            async def data(calls):
                await call(calls, "browser_navigate", {"url": f"{BASE}/data.html"})
                snap = await snapshot(calls)
                return all(value in snap for value in ("Alpha", "22", "Gamma"))

            async def form(calls):
                await call(calls, "browser_navigate", {"url": f"{BASE}/form.html"})
                snap = await snapshot(calls)
                await call(calls, "browser_type", {
                    "target": "#name", "element": "Name", "text": "Yakov",
                })
                await call(calls, "browser_click", {
                    "target": "#save", "element": "Save",
                })
                return "Saved: Yakov" in await snapshot(calls)

            async def dialog(calls):
                await call(calls, "browser_navigate", {"url": f"{BASE}/dialog.html"})
                snap = await snapshot(calls)
                await call(calls, "browser_click", {
                    "target": "#start", "element": "Continue",
                })
                snap = await snapshot(calls)
                await call(calls, "browser_click", {
                    "target": "#confirm", "element": "Confirm",
                })
                await call(calls, "browser_handle_dialog", {"accept": True})
                return "Dialog accepted" in await snapshot(calls)

            await run("structured extraction", data)
            await run("form submit", form)
            await run("multi-step dialog", dialog)
    return metrics


os.environ.pop("BROWSER_USE_API_KEY", None)
os.environ.pop("BROWSERBASE_API_KEY", None)
server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
BASE = f"http://127.0.0.1:{server.server_port}"
try:
    metrics = run_builtin() + asyncio.run(run_playwright())
    print(json.dumps([asdict(item) for item in metrics], indent=2, ensure_ascii=False))
finally:
    server.shutdown()
    thread.join()
