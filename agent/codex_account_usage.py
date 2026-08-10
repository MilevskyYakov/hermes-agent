"""Local, pseudonymous Codex account usage accounting."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from hermes_constants import get_hermes_home

_ACCOUNT_ALIASES = ("A", "B")

# OpenAI Codex token rate card, credits per 1M tokens (2026-08-10).
# Fast mode and preview models without published token rates stay unattributed.
_RATE_CARD = (
    ("gpt-5.6-luna", (5.0, 0.5, 30.0)),
    ("gpt-5.6-terra", (50.0, 5.0, 300.0)),
    ("gpt-5.6-sol", (125.0, 12.5, 750.0)),
    ("gpt-5.5-cyber", (312.5, 31.25, 1875.0)),
    ("gpt-5.5", (125.0, 12.5, 750.0)),
    ("gpt-5.4-mini", (18.75, 1.875, 113.0)),
    ("gpt-5.4", (62.5, 6.25, 375.0)),
    ("gpt-5.3-codex", (43.75, 4.375, 350.0)),
    ("gpt-5.2", (43.75, 4.375, 350.0)),
)


@dataclass(frozen=True)
class WeeklyAccountUsage:
    week_start: float
    accounts: dict[str, float]
    total_credits: float
    telemetry_credits: float

    @property
    def discrepancy_credits(self) -> float:
        return self.telemetry_credits - self.total_credits


def week_start_timestamp(now: float | None = None) -> float:
    current = datetime.fromtimestamp(now, tz=timezone.utc) if now is not None else datetime.now(timezone.utc)
    monday = (current - timedelta(days=current.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday.timestamp()


def _normalized_model(model: str) -> str:
    return str(model or "").strip().lower().replace("_", "-").replace(" ", "-")


def codex_credits(
    model: str,
    input_tokens: int = 0,
    cache_read_tokens: int = 0,
    output_tokens: int = 0,
) -> float | None:
    normalized = _normalized_model(model)
    for name, rates in _RATE_CARD:
        if name in normalized:
            return (
                max(int(input_tokens or 0), 0) * rates[0]
                + max(int(cache_read_tokens or 0), 0) * rates[1]
                + max(int(output_tokens or 0), 0) * rates[2]
            ) / 1_000_000
    return None


def _sum_rows(rows: Iterable[sqlite3.Row]) -> float:
    total = 0.0
    for row in rows:
        credits = codex_credits(
            row["model"],
            row["input_tokens"],
            row["cache_read_tokens"],
            row["output_tokens"],
        )
        if credits is not None:
            total += credits
    return total


def weekly_account_usage(conn: sqlite3.Connection, now: float | None = None) -> WeeklyAccountUsage:
    week_start = week_start_timestamp(now)
    account_rows = conn.execute(
        """SELECT account_alias, model,
                  SUM(input_tokens) AS input_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(output_tokens) AS output_tokens
           FROM codex_account_usage
           WHERE week_start = ?
           GROUP BY account_alias, model""",
        (week_start,),
    ).fetchall()
    accounts = {alias: 0.0 for alias in _ACCOUNT_ALIASES}
    for row in account_rows:
        alias = row["account_alias"]
        credits = _sum_rows((row,))
        if alias in accounts:
            accounts[alias] += credits

    telemetry_rows = conn.execute(
        """SELECT model,
                  SUM(input_tokens) AS input_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(output_tokens) AS output_tokens
           FROM session_model_usage
           WHERE billing_provider = 'openai-codex' AND last_seen >= ?
           GROUP BY model""",
        (week_start,),
    ).fetchall()
    total = sum(accounts.values())
    return WeeklyAccountUsage(
        week_start=week_start,
        accounts=accounts,
        total_credits=total,
        telemetry_credits=_sum_rows(telemetry_rows),
    )


def load_weekly_account_credits(db_path: Path | None = None) -> dict[str, float]:
    path = db_path or (get_hermes_home() / "state.db")
    if not path.exists():
        return {alias: 0.0 for alias in _ACCOUNT_ALIASES}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            return weekly_account_usage(conn).accounts
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {alias: 0.0 for alias in _ACCOUNT_ALIASES}


def format_weekly_account_usage(report: WeeklyAccountUsage) -> str:
    week = datetime.fromtimestamp(report.week_start, tz=timezone.utc).date().isoformat()
    discrepancy = report.discrepancy_credits
    return "\n".join(
        (
            f"Codex account usage — week {week} UTC",
            f"  A: {report.accounts['A']:.2f} credits",
            f"  B: {report.accounts['B']:.2f} credits",
            f"  Total A/B: {report.total_credits:.2f} credits",
            f"  Total telemetry: {report.telemetry_credits:.2f} credits",
            f"  Unattributed/rounding: {discrepancy:.2f} credits",
            "  Local rate-card credits; not subscription-limit percentage.",
        )
    )
