"""Addressed Telegram delivery cannot retry into a different topic or report partial text as sent."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.parametrize("failure", [BadRequest("Message thread not found"), NetworkError("connection lost after send")])
def test_strict_question_never_retries_or_falls_back(failure):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="synthetic"))
    adapter._bot = SimpleNamespace(send_message=AsyncMock(side_effect=failure))
    metadata = {"_question_relay": True, "notify": True, "thread_id": "7"}
    result = asyncio.run(adapter.send("-100123", "Question?", metadata=metadata))
    assert not result.success
    # Failure may be ambiguous, but it is never repaired by changing destination.
    assert adapter._bot.send_message.await_count == 1
    call = adapter._bot.send_message.await_args.kwargs
    assert call["message_thread_id"] == 7
    assert call["parse_mode"] is None


def test_strict_edit_never_truncates_or_changes_reply_target():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="synthetic"))
    adapter._bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
    metadata = {"_question_relay": True}
    text = "_*[]" * 900
    assert asyncio.run(adapter.edit_message("101", "12", text, metadata=metadata)).success
    assert adapter._bot.edit_message_text.await_args.kwargs["text"] == text
    assert adapter._bot.edit_message_text.await_args.kwargs.get("parse_mode") is None
    assert not asyncio.run(adapter.edit_message("101", "12", "😀" * 2100, metadata=metadata)).success
    assert adapter._bot.edit_message_text.await_count == 1
    adapter._bot.edit_message_text.side_effect = BadRequest("Message is too long")
    assert not asyncio.run(adapter.edit_message("101", "12", text, metadata=metadata)).success
    assert adapter._bot.edit_message_text.await_count == 2
    adapter._bot.send_message.assert_not_awaited()


def test_strict_question_preserves_plain_text_and_refuses_oversize():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="synthetic"))
    adapter._bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=12)))
    metadata = {"_question_relay": True, "notify": True}
    text = "[Hermes question q] *literal* __names__ 😀"
    result = asyncio.run(adapter.send("101", text, metadata=metadata))
    assert result.success and result.message_id == "12"
    assert adapter._bot.send_message.await_args.kwargs["text"] == text
    result = asyncio.run(adapter.send("101", "😀" * 2100, metadata=metadata))
    assert not result.success
    assert adapter._bot.send_message.await_count == 1
