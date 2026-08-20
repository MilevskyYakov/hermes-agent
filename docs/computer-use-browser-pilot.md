# Browser A/B pilot for task-scoped computer use

Date: 2026-08-20
Tracker: [GERDA2.0 #356](https://github.com/MilevskyYakov/GERDA2.0/issues/356)

## Decision

Playwright MCP won the bounded pilot: 3/3 scenarios completed versus 1/3 for the built-in local browser path. Keep `computer_use` for approved native-app work. Use isolated Playwright MCP for these web-only scenarios after operator-approved runtime configuration.

No MCP configuration or dependency is enabled by this change. Runtime cutover remains a separate approval gate.

## Reproduction

Environment:

- macOS arm64
- Node.js 22.22.3
- `agent-browser` 0.34.0
- `@playwright/mcp` 0.0.79
- Playwright launched with `--headless --isolated --image-responses omit --codegen none`
- local fixture server on a random loopback port
- no existing browser profile, cookies, accounts, or desktop windows

Setup and run:

```bash
npm install --prefix /tmp/hermes-issue-356-pilot agent-browser@0.34.0
PATH="/tmp/hermes-issue-356-pilot/node_modules/.bin:$PATH" agent-browser install
PATH="/tmp/hermes-issue-356-pilot/node_modules/.bin:$PATH" \
  uv run --extra mcp python scripts/browser_ab_pilot.py
```

`agent-browser` 0.34.0 warns that Node.js 24 is preferred, but the tested commands completed on Node.js 22.22.3. Browser processes are isolated and cleaned after each scenario.

## Results

| Scenario | Backend | Complete | Calls | Retries | Time, s | Response chars | Desktop interference |
|---|---|---:|---:|---:|---:|---:|---:|
| Structured extraction | built-in browser | yes | 1 | 0 | 1.723 | 736 | no |
| Structured extraction | Playwright MCP | yes | 2 | 0 | 0.757 | 1,079 | no |
| Form submit + verification | built-in browser | no | 4 | 0 | 1.900 | 560 | no |
| Form submit + verification | Playwright MCP | yes | 5 | 0 | 0.607 | 800 | no |
| Multi-step dialog + verification | built-in browser | no | 5 | 0 | 1.506 | 841 | no |
| Multi-step dialog + verification | Playwright MCP | yes | 7 | 0 | 1.159 | 1,256 | no |
| **Total** | **built-in browser** | **1/3** | **10** | **0** | **5.129** | **2,137** | **no** |
| **Total** | **Playwright MCP** | **3/3** | **14** | **0** | **2.523** | **3,135** | **no** |

## Failure evidence

- Built-in form path reported successful typing and clicking, but final DOM text was `Saved:` with an empty input value. Completion criterion failed.
- Built-in dialog path reached the confirmation step, then failed with `No CDP supervisor is attached to this task`; default local browser could not handle the JavaScript dialog through `browser_dialog`.
- Playwright MCP completed all scenarios without retries or desktop interaction.

## Trade-off

Playwright used 4 more calls and 998 more response characters across the pilot. It still completed every scenario in about half the cumulative measured tool time. Completion and correct interaction outweigh the context increase for multi-step web automation.

Recommended minimal runtime configuration: isolated, headless Playwright MCP with image responses omitted and no `browser_run_code` capability. Existing-profile or account access remains separately approved per task.
