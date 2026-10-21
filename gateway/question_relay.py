"""Telegram transport for durable questions. This module never starts an agent."""
import asyncio
import logging

from gateway.config import Platform
from gateway.session import SessionSource
from hermes_question_relay import RelayStore, route_for

logger = logging.getLogger(__name__)
PREFIX = "[Hermes question "


def render(row):
    parts = [f"{PREFIX}{row['request_id']}] {row['task']}", f"State: {row['status']}"]
    for q in row["questions"]:
        parts.append(f"{q['qid']}: {q['question']}")
        parts.extend(f"  {choice}" for choice in q.get("choices") or [])
        if q["qid"] in row["answers"]:
            parts.append(f"Decision: {row['answers'][q['qid']]}")
    parts.append("Reply to this message. For multiple questions: qid: answer. /stop or /revoke stops this task.")
    if row["status"] == "consumed":
        parts.append("Original owner received the decision; completion is not yet confirmed.")
    if row["status"] == "stopped":
        parts.append("Stop requested. An action already in progress may still finish.")
    if row["result"]:
        parts.append("Result:\n" + row["result"])
    text = "\n".join(parts)
    if len(text.encode("utf-16-le")) // 2 > 4000:
        raise ValueError("Question relay message exceeds Telegram limit; full content remains in the project")
    return text


async def deliver_pending(store, adapter, config=None):
    """A send with an unknown outcome is never retried; edits are idempotent."""
    for row in await asyncio.to_thread(store.pending_delivery):
        request_id = row["request_id"]
        if config is not None and route_for(config, row["session_id"]) != {k: row[k] for k in ("task", "chat_id", "user_id", "thread_id")}:
            await asyncio.to_thread(store.stop_session, row["session_id"])
            await asyncio.to_thread(store.cancel_delivery, request_id)
            continue
        metadata = {"_question_relay": True, "notify": True}
        if row["thread_id"]:
            metadata["thread_id"] = row["thread_id"]
        if row["delivery"] == "queued":
            if not await asyncio.to_thread(store.claim_delivery, request_id):
                continue
            try:
                content = render(row)
            except ValueError:
                await asyncio.to_thread(store.delivery, request_id, "failed")
                continue
            try:
                sent = await adapter.send(chat_id=row["chat_id"], content=content, metadata=metadata)
            except Exception:
                await asyncio.to_thread(store.delivery, request_id, "unknown")
                logger.warning("Question relay delivery outcome unknown; no automatic resend")
                continue
            if sent.success and sent.message_id:
                await asyncio.to_thread(store.delivery, request_id, "sent", str(sent.message_id), row["revision"])
            else:
                await asyncio.to_thread(store.delivery, request_id, "unknown")
        else:
            try:
                edited = await adapter.edit_message(chat_id=row["chat_id"], message_id=row["message_id"],
                                                    content=render(row), metadata=metadata)
                if edited.success:
                    await asyncio.to_thread(store.mark_displayed, request_id, row["revision"])
            except Exception:
                logger.warning("Question relay state not confirmed in Telegram; retained for reconciliation")


def _store():
    from hermes_state_registry import acquire
    return RelayStore(acquire())


def _known_reply(event):
    source = event.source
    if source.platform != Platform.TELEGRAM or not event.reply_to_message_id:
        return False
    if (event.reply_to_text or "").startswith(PREFIX):
        return True  # Only selects the refusal/resolution path; never grants authority.
    store = _store()
    try:
        return store.find_reply(source.chat_id, source.thread_id, event.reply_to_message_id) is not None
    finally:
        store.db.close()


async def is_relay_reply(event):
    return await asyncio.to_thread(_known_reply, event)


async def handle_reply(event):
    """Called after normal gateway auth and before commands/session creation."""
    if not await is_relay_reply(event):
        return None
    if event.internal or not event.allow_gateway_control:
        return "Question reply rejected: user control is required."
    def resolve():
        from hermes_cli.config import load_config_readonly
        store = _store()
        try:
            source = event.source
            row = store.find_reply(source.chat_id, source.thread_id, event.reply_to_message_id)
            if not row or str(source.user_id) != row["user_id"]:
                return "unknown or unauthorized question"
            current = route_for(load_config_readonly(), row["session_id"])
            expected = {k: row[k] for k in ("task", "chat_id", "user_id", "thread_id")}
            if current != expected:
                store.stop_session(row["session_id"])
                return "route revoked; stop requested"
            return store.reply(source.chat_id, source.thread_id, source.user_id, event.reply_to_message_id, event.text or "")
        finally:
            store.db.close()
    status = await asyncio.to_thread(resolve)
    # A durable acceptance is not a claim that the original process is still alive.
    return f"Question reply: {status}. Execution/completion is confirmed only by the original project's state."


async def watch(runner):
    """Runs inside the existing gateway; no daemon, listener or second executor."""
    from contextlib import asynccontextmanager
    from gateway.run import _handoff_watch_scopes, _async_profile_runtime_scope

    @asynccontextmanager
    async def scope(home):
        if home is None:
            yield
        else:
            async with _async_profile_runtime_scope(home):
                yield
    while runner._running:
        for profile_name, profile_home in _handoff_watch_scopes(runner):
            async with scope(profile_home):
                from hermes_cli.config import load_config_readonly
                config = await asyncio.to_thread(load_config_readonly)
                if not (config.get("question_relay") or {}).get("sessions"):
                    continue
                store = await asyncio.to_thread(_store)
                try:
                    source = SessionSource(platform=Platform.TELEGRAM, chat_id="", profile=profile_name)
                    adapter = runner._adapter_for_source(source)
                    if adapter is not None:
                        await deliver_pending(store, adapter, config)
                finally:
                    await asyncio.to_thread(store.db.close)
        await asyncio.sleep(1)
