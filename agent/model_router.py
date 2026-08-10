"""Conservative first-task model router.

The router is intentionally stateless: hosts decide whether a turn is the
first task in a session and whether to surface a proposal. Shadow mode always
executes on the configured Sol baseline while recording Luna's proposed route.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_ALLOWED_INTENTS = {
    "mechanical_bounded",
    "normal_dev",
    "architecture_debug",
    "complex_large",
    "ultra_research",
    "ambiguous",
}
_ALLOWED_TOOLSETS = {"dev", "research", "system", "visual", "hub", "full"}
_TIER_LABELS = {
    "luna_max": "Luna Max",
    "sol_medium": "Sol Medium",
    "sol_max": "Sol Max",
    "sol_ultra": "Sol Ultra",
}

_SYSTEM_PROMPT = """Classify one new task for a conservative model router.
Return exactly one JSON object with these keys:
- intent: mechanical_bounded | normal_dev | architecture_debug | complex_large | ultra_research | ambiguous
- confidence: number from 0 to 1
- reason: short user-visible reason, maximum 80 characters
- toolset: dev | research | system | visual | hub | full

Rules:
- mechanical_bounded only for short, explicit, low-ambiguity work with a clear done state.
- normal_dev for ordinary implementation, review, debugging, or multi-file work.
- architecture_debug for architecture, broad debugging, migrations, or risky changes.
- complex_large only when Sol Max is materially needed.
- ultra_research only for exceptional deep research where Sol Ultra is materially needed.
- ambiguous whenever uncertain. Never infer missing scope.
Do not include the task text in reason. Do not add markdown."""


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    text = raw.strip()
    first, last = text.find("{"), text.rfind("}")
    if first < 0 or last < first:
        return None
    try:
        value = json.loads(text[first : last + 1])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _bounded_reason(value: Any, fallback: str) -> str:
    reason = " ".join(str(value or "").split())
    if not reason:
        return fallback
    return reason[:80]


def _policy(verdict: Optional[Dict[str, Any]], threshold: float) -> Dict[str, Any]:
    if not verdict:
        return {
            "intent": "ambiguous",
            "confidence": 0.0,
            "reason": "router unavailable; conservative fallback",
            "reason_code": "router_unavailable",
            "toolset": "full",
            "proposed_tier": "sol_medium",
        }

    intent = str(verdict.get("intent") or "").strip().lower()
    toolset = str(verdict.get("toolset") or "").strip().lower()
    raw_confidence = verdict.get("confidence")
    try:
        confidence = float(raw_confidence) if raw_confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    if intent not in _ALLOWED_INTENTS or confidence < threshold:
        return {
            "intent": "ambiguous",
            "confidence": confidence,
            "reason": "low-confidence route; conservative fallback",
            "reason_code": "low_confidence",
            "toolset": toolset if toolset in _ALLOWED_TOOLSETS else "full",
            "proposed_tier": "sol_medium",
        }

    tier_by_intent = {
        "mechanical_bounded": "luna_max",
        "normal_dev": "sol_medium",
        "architecture_debug": "sol_medium",
        "complex_large": "sol_max",
        "ultra_research": "sol_ultra",
        "ambiguous": "sol_medium",
    }
    proposed_tier = tier_by_intent[intent]
    return {
        "intent": intent,
        "confidence": confidence,
        "reason": _bounded_reason(verdict.get("reason"), intent.replace("_", " ")),
        "reason_code": intent,
        "toolset": toolset if toolset in _ALLOWED_TOOLSETS else "full",
        "proposed_tier": proposed_tier,
    }


def route_first_task(
    user_message: Any,
    *,
    config: Dict[str, Any],
    main_model: str,
    main_runtime: Dict[str, Any],
    session_id: str = "",
    call: Optional[Callable[..., Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return one validated first-task route, or ``None`` when disabled.

    No prompt or free-form reason is logged. Telemetry uses only session id,
    route mode, tier, confidence, toolset, and a bounded reason code.
    """
    cfg = config.get("model_router") or {}
    if not cfg.get("enabled"):
        return None
    mode = str(cfg.get("mode") or "shadow").strip().lower()
    if mode not in {"shadow", "live"}:
        return None

    if isinstance(user_message, str):
        task_text = user_message.strip()
    elif isinstance(user_message, list):
        task_text = "\n".join(
            str(part.get("text") or "")
            for part in user_message
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    else:
        task_text = ""

    verdict = None
    if task_text:
        try:
            if call is None:
                from agent.auxiliary_client import call_llm

                call = call_llm
            response = call(
                task="model_router",
                main_runtime=main_runtime,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": task_text[:12000]},
                ],
                max_tokens=300,
            )
            raw = response.choices[0].message.content or ""
            verdict = _extract_json(raw)
        except Exception as exc:
            logger.warning("Model router auxiliary call failed: %s", type(exc).__name__)

    try:
        threshold = float(cfg.get("confidence_threshold", 0.8))
    except (TypeError, ValueError):
        threshold = 0.8
    try:
        luna_call_limit = max(1, int(cfg.get("luna_call_limit", 4) or 4))
    except (TypeError, ValueError):
        luna_call_limit = 4
    decision = _policy(verdict, min(1.0, max(0.0, threshold)))

    proposed_tier = decision["proposed_tier"]
    proposal_required = mode == "live" and proposed_tier in {"sol_max", "sol_ultra"}
    effective_tier = proposed_tier if mode == "live" and not proposal_required else "sol_medium"
    if mode == "shadow":
        effective_tier = "sol_medium"

    sol_model = str(cfg.get("sol_model") or "gpt-5.6-sol")
    luna_model = str(cfg.get("luna_model") or "gpt-5.6-luna")
    effective_model = luna_model if effective_tier == "luna_max" else sol_model
    effort = {
        "luna_max": "max",
        "sol_medium": "medium",
        "sol_max": "max",
        "sol_ultra": "ultra",
    }[effective_tier]

    visible = f"Model: {_TIER_LABELS[effective_tier]} — {decision['reason']}"
    if mode == "shadow" and proposed_tier != effective_tier:
        visible += f" (shadow: {_TIER_LABELS[proposed_tier]})"
    if proposal_required:
        visible = (
            f"Model proposal: {_TIER_LABELS[proposed_tier]} — {decision['reason']}"
        )

    result = {
        **decision,
        "mode": mode,
        "effective_tier": effective_tier,
        "effective_model": effective_model,
        "sol_model": sol_model,
        "luna_call_limit": luna_call_limit,
        "reasoning_config": {"effort": effort},
        "proposal_required": proposal_required,
        "escalation_policy": "fresh_linked_session" if effective_tier == "luna_max" else "none",
        "visible_line": visible,
    }
    logger.info(
        "model_route session=%s mode=%s proposed=%s effective=%s confidence=%.2f toolset=%s reason_code=%s",
        session_id or "-",
        mode,
        proposed_tier,
        effective_tier,
        decision["confidence"],
        decision["toolset"],
        decision["reason_code"],
    )
    return result


def apply_route(base: Dict[str, Any], route: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Overlay an enabled route while preserving provider/account infrastructure."""
    result = dict(base)
    if route is None:
        return result
    result["model_route"] = route
    result["model"] = route["effective_model"]
    result["reasoning_config"] = route["reasoning_config"]
    return result


def proposal_response(route: Dict[str, Any]) -> str:
    """User-visible gate for expensive tiers; hosts must not initialize an agent."""
    return (
        f"{route['visible_line']}\n"
        "Confirm the higher tier explicitly, then resend the task; no execution started."
    )


def request_luna_escalation_if_due(agent: Any) -> bool:
    """Bound Luna execution even when hidden complexity needs no new toolset."""
    if getattr(agent, "_model_route_tier", "") != "luna_max":
        return False
    route = getattr(agent, "_model_route", {}) or {}
    limit = max(1, int(route.get("luna_call_limit", 4) or 4))
    calls = max(0, int(getattr(agent, "session_api_calls", 0) or 0))
    if calls < limit:
        return False
    agent._model_escalation_requested = True
    agent._model_escalation_reason = "Luna call limit reached"
    return True


def attach_route(agent: Any, route: Dict[str, Any]) -> None:
    """Attach runtime state and persist prompt-free route telemetry."""
    agent._model_route = route
    agent._model_route_tier = route["effective_tier"]
    telemetry = {
        key: route.get(key)
        for key in (
            "mode",
            "intent",
            "confidence",
            "reason_code",
            "toolset",
            "proposed_tier",
            "effective_tier",
            "escalation_policy",
        )
    }
    metadata = dict(getattr(agent, "_session_init_model_config", {}) or {})
    metadata["_model_route"] = telemetry
    agent._session_init_model_config = metadata
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", "")
    if session_db is not None and session_id:
        try:
            session_db.update_session_meta(
                session_id,
                json.dumps(metadata, ensure_ascii=False),
                model=getattr(agent, "model", None),
            )
        except Exception:
            logger.warning("Model route telemetry persistence failed", exc_info=True)
