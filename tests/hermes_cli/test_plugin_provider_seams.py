"""Provider-profile seams an out-of-tree model-provider plugin relies on.

Behaviour contracts, not snapshots: a profile's own auth handler, error classification and
account-usage hook must be reachable from the core call sites, and an unregistered slug must
fall through to the built-in path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import account_usage
from agent.error_classifier import FailoverReason, classify_api_error
from hermes_cli import auth_commands
from providers import register_provider, _REGISTRY, _ALIASES
from providers.base import ProviderProfile

SLUG = "compat-seam-fixture"


@pytest.fixture
def profile():
    """A minimal plugin-shaped profile registered for the test, removed afterwards."""
    prof = ProviderProfile(name=SLUG, display_name="Compat Fixture", base_url=f"process://{SLUG}",
                           auth_type="external_process", supports_health_check=False)
    register_provider(prof)
    try:
        yield prof
    finally:
        _REGISTRY.pop(SLUG, None)
        _ALIASES.pop(SLUG, None)


def test_auth_handler_owns_the_action(profile):
    calls = []
    profile.auth_handler = lambda action, args: calls.append(action) or True

    assert auth_commands._dispatch_provider_auth("status", SimpleNamespace(), SLUG) is True
    assert calls == ["status"]


def test_auth_handler_falls_through_when_absent(profile):
    assert auth_commands._dispatch_provider_auth("add", SimpleNamespace(), SLUG) is False
    assert auth_commands._dispatch_provider_auth("add", SimpleNamespace(), "not-a-provider") is False


def test_profile_classifies_its_own_error(profile):
    profile.classify_api_error = lambda error, **kw: {
        "reason": "billing", "should_fallback": True}

    verdict = classify_api_error(RuntimeError("credit balance exhausted"), provider=SLUG, model="m")

    assert verdict.reason == FailoverReason.billing
    # A hook asking for fallback must cascade like a built-in terminal verdict.
    assert verdict.retryable is False
    assert verdict.should_fallback is True


def test_profile_error_hook_cannot_hijack_other_providers(profile):
    profile.classify_api_error = lambda error, **kw: {"reason": "billing"}

    verdict = classify_api_error(RuntimeError("boom"), provider="openrouter", model="m")

    assert verdict.reason != FailoverReason.billing


def test_account_usage_hook_is_consulted(profile):
    marker = account_usage.AccountUsageSnapshot(
        provider=SLUG, source="fixture", fetched_at=account_usage._utc_now())
    profile.fetch_account_usage = lambda **kw: marker

    assert account_usage.fetch_account_usage(SLUG) is marker
    assert account_usage.fetch_account_usage("not-a-provider") is None


def test_profile_without_hook_returns_none(profile):
    assert account_usage.fetch_account_usage(SLUG) is None
