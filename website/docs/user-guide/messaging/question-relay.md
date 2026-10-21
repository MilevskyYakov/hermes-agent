---
title: "Addressed project questions"
description: "Reply to a CLI or TUI question from Telegram without moving the executor"
---

# Addressed project questions

This optional route connects a **waiting `clarify` question** in a classic CLI/Orca terminal or TUI/Desktop session to Telegram. It does not turn `hermes send` into a remote terminal, route ordinary chat to the last active project, or transfer the task with `/handoff`.

The existing project process remains the sole executor. Telegram transports decisions through the same profile's `state.db`; it never starts another agent. Model, provider, tool approvals, task scope and existing gateway authorization are unchanged. This is not a route for passwords, sudo prompts or dangerous-command approval cards.

## Enable only for a named session

Prerequisites:

- The approved build must be used by **both** the project process and messaging gateway. An edited checkout is not an installed build.
- Both processes must use the same profile home and state database, on the same awake computer.
- Telegram must already be configured and the human sender authorized through normal gateway policy.
- Get the exact persisted session ID from the project session information/history. A displayed task title or a session ID written in message text is not a binding. Do not use a shortened ID.
- Agree on the exact private test chat or existing topic before live testing. A named worktree does not isolate Telegram data. Do not create or change credentials as part of this setup.

With explicit configuration permission, add this section to that profile's existing `config.yaml`, preserving other settings:

```yaml
question_relay:
  sessions:
    EXACT_STORED_SESSION_ID:
      task: "Approved project task"
      chat_id: "REPLACE_WITH_NUMERIC_CHAT_ID"
      user_id: "REPLACE_WITH_NUMERIC_HUMAN_ID"
      # thread_id: "REPLACE_WITH_NUMERIC_TOPIC_ID"
```

The placeholders deliberately fail validation. Use numeric identities, not usernames. Omit `thread_id` only for the agreed unthreaded destination. There is no default/home-channel fallback. Configuration is read again while waiting and before accepting an answer. Other sessions are not opted in.

The current binding is exact, not a task-discovery service. Starting/resetting a session or rotating to a new stored CLI session ID requires checking the new identity before authorizing its route. Do not copy a binding to a different task implicitly.

## Answer and stop

1. Start the already authorized work in its project. The agent must actually call `clarify`; an ordinary prose question or `hermes send` does not establish a waiter.
2. Telegram receives a question card containing the task label, request ID, question IDs, choices and state. Use Telegram **Reply to that card**.
3. For one question, reply with the answer. For a batch, send `qid: answer`, using the displayed question ID. For multi-select, send a JSON array of exact choice labels. Free text remains available.
4. Alternatively, answer through the original terminal/TUI control. The first committed answer per question wins. A conflicting answer is rejected rather than silently replacing it.
5. The original blocked callback returns the decision once. No copied prompt, additional “go”, handoff, model call or second executor is needed to deliver it.
6. The same card is edited with the saved decision, consumption state and final result. The project displays the saved decision and its ordinary final response. Technical logs remain local.

Reply `/stop` or `/revoke` **to the specific card** to stop that task. Normal project interrupt/cancel also stops its relay. Removing/changing the configured binding revokes it. Revocation does not authorize sending old task data to a new recipient.

Stop prevents continuation through the waiting question and requests the native hard interrupt for a running owner. An already-started external action may finish; do not interpret “stop requested” as proof of rollback. Stopped tasks remain fenced until reviewed recovery. A bare `/stop` outside the card retains its ordinary Telegram meaning.

## Read state without guessing

```sh
hermes sessions questions EXACT_STORED_SESSION_ID
```

Run this with the approved build and original profile. Output is local and may contain private decisions; do not publish it unredacted.

- `waiting`: no complete decision set yet.
- `answered`: decisions are saved, but the original owner has not consumed them.
- `consumed`: the original callback received them, or a reviewed recovery was claimed by its new owner; task completion is not yet confirmed.
- `complete`: the original project reported normal completion and saved its result.
- `stopped` / `expired`: no new answer will restart this question.
- `reviewed`: recovery was reviewed; the next normal project resume receives the saved decisions as context, not new permission.

Delivery is separate: `queued` means the gateway has not acknowledged a send; `sent` means an API receipt with a message ID was saved, not that a human read the message. `sending` after a crash and `unknown` are ambiguous. `failed` means rendering/delivery could not be completed. `disabled` means the route was revoked. A Telegram edit is current only when `delivered_revision` reaches `revision`; an old card is not proof of the latest decision or completion.

The current card limit is **4,000 UTF-16 units for the complete question/decision/result text**. Oversized content is refused rather than truncated or sent to another destination. Keep question batches and final summaries within that limit; detailed logs belong in the project. An oversize result remains saved locally and its newer revision is not marked delivered. This limit and the actual controls require user acceptance in the live pilot.

## Delivery failures and gateway restarts

- A stopped/offline gateway leaves a question queued. The original terminal still works. Starting the already approved gateway can deliver the queued state; it does not restart task execution.
- A send timeout may mean Telegram received a message. The relay never blindly resends an ambiguous send. The strict adapter path does not retry into another topic or strip a failed thread binding.
- `sending`/`unknown` without a saved message ID is not a valid reply route. Inspect the agreed chat independently; do not patch the database or infer delivery from matching text. Answer the still-live original terminal instead, or stop and reconcile the task.
- Failed edits retain the previous acknowledged revision and retry that same message, not a fresh execution. No new task is created by duplicate, expired, unknown or unauthorized replies.
- A gateway restart with an already saved message ID retains reply routing. It does not transfer ownership from the project process.

## Owner loss and reviewed recovery

If the project process disappears, decisions already committed to SQLite survive. A new owner is fenced **before** its next model/tool call; saved answers alone do not prove whether earlier external actions completed. The relay does not automatically replay a turn.

1. Inspect local question state and the original session transcript. Reconcile any external effects against their actual targets. Record what completed, what did not, and what remains unknown. Unknown effects block safe recovery.
2. Ensure the old task writers have exited. Recovery checks the original host, PID and process start time, and refuses an active session-turn lease. It never kills another process for you.
3. With explicit recovery permission, record the review for each unresolved request:

   ```sh
   hermes sessions questions EXACT_STORED_SESSION_ID \
     --recover EXACT_REQUEST_ID \
     --effects-reviewed "Verified existing effects and their identifiers; reuse completed results"
   ```

4. Resume that original project session through its normal interface, preserving the accepted task and grants. The original exact route must still be authorized; a removed or changed route blocks recovery. The recorded decisions and effect review are claimed atomically by one new owner and supplied as context. Completion updates the existing Telegram card. Do not start an unrelated new executor or repeat completed external actions.

The recovery command itself does not execute tools, resend Telegram messages, change models or extend permissions. Repeating it for an already reviewed request is a no-op. An addressed stop/revoke also cancels a reviewed but not yet resumed request; it requires a new explicit review before recovery. A process restart after an arbitrary external effect cannot be made exactly-once by a question queue; reconciliation is an explicit safety gate, not a hidden success claim.

## Local checks and live acceptance are separate

Local tests use temporary databases, real CLI/TUI callback and gateway dispatch code, fake Telegram transport/model calls, and abruptly terminated child processes for recovery and delivery uncertainty. They check competing recovery owners, revoked routes, preservation of a completed synthetic effect, and publication to the same card. They do not establish visual or production acceptance or guarantee arbitrary model-driven external actions are exactly-once.

```sh
scripts/run_tests.sh -j 4 \
  tests/test_question_relay.py \
  tests/test_question_relay_recovery.py \
  tests/cli/test_question_relay.py \
  tests/tui_gateway/test_question_relay.py \
  tests/gateway/test_question_relay_delivery.py \
  tests/gateway/test_question_relay_restart.py
```

Broader regression coverage includes `tests/cli/`, `tests/tui_gateway/`, `tests/gateway/`, `tests/test_hermes_state.py`, and the two clarify tool suites. Record the exact source version, dirty-source fingerprint, commands and results; a local pass is not a live Telegram test.

Frontend checks require already installed workspace dependencies. Set `NODE_ENV=test` explicitly: an inherited production environment disables React's test API. Build the local Ink package before running TUI tests. The remote-answer regression must fail without `clarify.resolved`, then pass with one accepted answer and no synthesized timeout. Replayed events and late local RPC responses must not duplicate the decision or dismiss a newer question.

```sh
npm run build:ink --workspace ui-tui
NODE_ENV=test npm test --workspace ui-tui
npm run typecheck --workspace ui-tui
NODE_ENV=test npm run test:ui --workspace apps/desktop
npm run typecheck --workspace apps/desktop
```

Missing dependencies are setup blockers, not permission to install packages or substitute mocks for the required suite. Telegram decision cards are sent and edited as bounded plain text; an oversized or rejected edit remains unconfirmed, never a silently shortened success.

A separately approved pilot must exercise the **actual Orca launch path**, with synthetic tasks and the accepted model/provider:

- Telegram reply produces `DONE BLUE` in the original task once, and the Telegram card shows the same result.
- A terminal answer settles the Telegram card; no additional “go” or manual injection occurs.
- Two waiting tasks retain their own answers; targeted stop and late/duplicate/conflicting replies preserve honest state.
- Ordinary Telegram/project work remains intact, and the user confirms the controls are convenient.

Exercise wrong sender/chat/topic, configuration revocation, network failures, gateway loss and owner recovery in the isolated suite above. Do not inject failures into a working gateway or unrelated sessions. Combine that evidence with the real positive Orca/Telegram pilot; label transport simulation separately rather than calling it a live Telegram failure test.

Do not stop a working gateway, send test messages to existing conversations, change profiles or install this build without explicit permission for those exact actions.

## Reversible rollout and rollback

Prepare a versioned artifact from the tested source, record the current project/gateway launch targets, and back up the relevant state database using a SQLite-consistent backup. Do not copy a live WAL database as a single file. Configuration and rollout require separate approval; this document does not authorize either.

Pilot only the agreed synthetic sessions. Keep the previous runtime available. Activate both sides on the same approved version; record their actual versions and verify the exact recipients and results independently. Expand only after the live acceptance above.

To disable safely, stop task-owned work, reconcile already-started effects, and remove only the approved session bindings. Preserve the `question_relay` table and transcript. The additive table is inert to older code; do not delete it or restore an old whole database merely to roll back code. Do not resume an unresolved task on an older runtime that lacks the recovery fence. Switching launch targets, restarting processes and restoring data retain their own approval gates.

Source task: [GERDA2.0 #588](https://github.com/MilevskyYakov/GERDA2.0/issues/588). This bounded route does not complete its parent program or authorize issue closeout.
