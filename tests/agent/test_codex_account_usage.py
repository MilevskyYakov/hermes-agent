"""Account-level Codex telemetry and session-sticky balancing."""

from __future__ import annotations

from agent.codex_account_usage import (
    codex_credits,
    week_start_timestamp,
    weekly_account_usage,
)
from agent.credential_pool import CredentialPool, PooledCredential
from hermes_state import SessionDB


def _entry(entry_id: str, priority: int) -> PooledCredential:
    return PooledCredential(
        provider="openai-codex",
        id=entry_id,
        label="redacted",
        auth_type="api_key",
        priority=priority,
        source="manual",
        access_token=f"test-token-{entry_id}",
    )


def test_rate_card_and_weekly_attribution(tmp_path):
    assert codex_credits("gpt-5.6-sol", 1_000_000, 1_000_000, 1_000_000) == 887.5
    assert week_start_timestamp(1786320000) == 1786320000  # 2026-08-10 00:00 UTC
    assert week_start_timestamp(1786924799) == 1786320000  # following Sunday

    db = SessionDB(tmp_path / "state.db")
    for alias in ("A", "B"):
        session_id = f"session-{alias}"
        db.ensure_session(session_id, model="gpt-5.6-sol")
        db.update_token_counts(
            session_id,
            model="gpt-5.6-sol",
            billing_provider="openai-codex",
            billing_mode="subscription_included",
            account_alias=alias,
            input_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            output_tokens=1_000_000,
            api_call_count=1,
        )

    assert db._conn is not None
    report = weekly_account_usage(db._conn)
    assert report.accounts == {"A": 887.5, "B": 887.5}
    assert report.total_credits == report.telemetry_credits == 1775.0
    rows = db._conn.execute(
        "SELECT DISTINCT account_alias FROM codex_account_usage ORDER BY account_alias"
    ).fetchall()
    assert [row[0] for row in rows] == ["A", "B"]
    telemetry_rows = db._conn.execute("SELECT * FROM codex_account_usage").fetchall()
    telemetry_dump = " ".join(
        str(value) for row in telemetry_rows for value in row
    ).lower()
    assert "@" not in telemetry_dump
    assert "jwt" not in telemetry_dump
    assert "token" not in telemetry_dump
    assert "account_id" not in telemetry_dump
    db.close()


def test_less_loaded_account_is_sticky_until_rate_limit(monkeypatch):
    monkeypatch.setattr(
        "agent.codex_account_usage.load_weekly_account_credits",
        lambda: {"A": 10.0, "B": 2.0},
    )
    pool = CredentialPool("openai-codex", [_entry("a", 0), _entry("b", 1)])
    monkeypatch.setattr(pool, "_persist", lambda **_: None)

    selected = pool.select()
    assert selected is not None and selected.id == "b"
    assert pool.account_alias_for_entry_id("b") == "B"
    selected = pool.select()
    assert selected is not None and selected.id == "b"

    rotated = pool.mark_exhausted_and_rotate(status_code=429, credential_id="b")
    assert rotated is not None
    assert rotated.id == "a"
    assert pool.account_alias_for_entry_id("a") == "A"


def test_tie_break_and_duplicate_pool_fail_closed(monkeypatch):
    monkeypatch.setattr(
        "agent.codex_account_usage.load_weekly_account_credits",
        lambda: {"A": 0.0, "B": 0.0},
    )
    two = CredentialPool("openai-codex", [_entry("a", 0), _entry("b", 1)])
    selected = two.select()
    assert selected is not None and selected.id == "a"

    three = CredentialPool(
        "openai-codex",
        [_entry("a", 0), _entry("duplicate", 1), _entry("b", 2)],
    )
    assert three.account_alias_for_entry_id("a") is None
    selected = three.select()
    assert selected is not None and selected.id == "a"
