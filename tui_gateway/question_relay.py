"""Opt-in TUI/Desktop question relay. The waiting agent retains execution ownership."""
import json
import threading
import time
import uuid

from hermes_question_relay import RelayStore, route_for, load_relay_config

_active = {}
_lock = threading.RLock()


def config_for(session):
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(session.get("profile_home") or get_hermes_home())
    try:
        return load_relay_config()
    finally:
        reset_hermes_home_override(token)


def block(sid, payload, timeout, batch_qids=None):
    from tui_gateway import server
    session = server._sessions.get(sid)
    if session is None:
        return False, ""
    key = session.get("session_key")
    with server._session_db(session) as db:
        config = config_for(session)
        route = route_for(config, key, db)
        if route is None:
            if config.get("question_relay"):
                server._emit("status.update", sid, {"kind": "info", "text":
                    "Question relay: no authorized route for this session; using local clarification."})
            return False, ""
        if db is None:
            raise RuntimeError("Question relay requires the original session database")
        store = RelayStore(db)
        owner = session.setdefault("_question_relay_owner", uuid.uuid4().hex)
        store.resume_note(key, owner, route)
        rid = uuid.uuid4().hex
        questions = payload.get("questions") or [{"qid": "q1", **payload}]
        store.create(rid, key, owner, route, questions)
        event = threading.Event()
        with _lock:
            _active[rid] = (sid, store, questions)
        with server._prompt_lock:
            server._pending[rid] = (sid, event)
            server._pending_prompt_payloads[rid] = ("clarify.request", {**payload, "request_id": rid})
        last_display = None
        last_answers = {}
        resolved = False
        last_heartbeat = 0
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            server._emit("clarify.request", sid, {**payload, "request_id": rid})
            while True:
                if event.is_set() or session.get("_turn_cancel_requested"):
                    store.stop_session(key)
                if route_for(config_for(session), key, db) != route:
                    store.stop_session(key)
                row = store.get(rid)
                if row is None:
                    raise RuntimeError("Question relay state disappeared")
                display = (row["delivery"], row["status"], row["revision"], row["delivered_revision"])
                if row["answers"] != last_answers and row["status"] == "waiting":
                    last_answers = row["answers"]
                    replay = {**payload, "request_id": rid, "answers": last_answers}
                    with server._prompt_lock:
                        server._pending_prompt_payloads[rid] = ("clarify.request", replay)
                    server._emit("clarify.request", sid, replay)
                if display != last_display:
                    last_display = display
                    server._emit("status.update", sid, {"kind": "info", "text":
                        f"Question relay: {row['delivery']}; {row['status']}; decisions: "
                        f"{json.dumps(row['answers'], ensure_ascii=False)}; Telegram revision "
                        f"{row['delivered_revision']}/{row['revision']}"})
                if row["status"] == "stopped":
                    server._interrupt_session_turn(sid, session)
                    return True, ""
                answer = store.consume(rid, owner)
                if answer is not None:
                    session["_question_relay_used"] = True
                    resolved = True
                    server._emit("clarify.resolved", sid, {"request_id": rid, "answers": answer})
                    return True, json.dumps({"answers": answer}, ensure_ascii=False) if batch_qids else answer["q1"]
                if deadline is not None and time.monotonic() >= deadline:
                    store.expire(rid)
                    row = store.get(rid)
                    if row["status"] != "expired":
                        # A last answer or stop may have won the transaction before expiry.
                        continue
                    # No answer is not approval. Keep the native timeout shape without inventing a decision.
                    return True, json.dumps({"answers": row["answers"], "timed_out": True}) if batch_qids else ""
                if time.monotonic() - last_heartbeat >= 5:
                    store.heartbeat(rid, owner)
                    last_heartbeat = time.monotonic()
                event.wait(0.2)
        except Exception:
            store.stop_session(key)
            raise
        finally:
            with _lock:
                _active.pop(rid, None)
            with server._prompt_lock:
                server._pending.pop(rid, None)
                server._pending_prompt_payloads.pop(rid, None)
                server._answers.pop(rid, None)
            if not resolved:
                server._emit("clarify.expire", sid, {"request_id": rid})


def respond(params):
    """None = ordinary UI question; a relay answer uses the same DB arbitration as Telegram."""
    from tui_gateway import server
    with _lock:
        entry = _active.get(params.get("request_id"))
        if entry is None:
            # Native clients use an opaque request id and may omit session_id.
            # Reject late relay clicks as errors, never false UI confirmations.
            for session in list(server._sessions.values()):
                with server._session_db(session) as db:
                    if db is not None and RelayStore(db).get(params.get("request_id")):
                        return {"status": "expired"}
            return None
        sid, store, questions = entry
        if params.get("session_id") is not None and params["session_id"] != sid:
            return {"status": "forbidden"}
        rid = params["request_id"]
        answer = params.get("answer", "")
        if answer == "":
            store.stop_session(store.get(rid)["session_id"])
            return {"status": "stopped"}
        qid = params.get("question_id") or (questions[0]["qid"] if len(questions) == 1 else "")
        status = store.answer(rid, qid, answer)
        row = store.get(rid)
        return {"status": "ok" if status in {"accepted", "duplicate"} else status,
                "answers": row["answers"],
                "remaining": [q["qid"] for q in questions if q["qid"] not in row["answers"]]}


def tick(sid, session):
    """Observe remote stop after a decision as well as while the question is waiting."""
    if not session.get("_question_relay_used"):
        return
    from tui_gateway import server
    with server._session_db(session) as db:
        if db is None:
            raise RuntimeError("Question relay state unavailable")
        store = RelayStore(db)
        rows = store.for_session(session["session_key"])
        route = route_for(config_for(session), session["session_key"], db)
        active = [r for r in rows if r["owner"] == session.get("_question_relay_owner") and r["status"] != "complete"]
        if any(r["status"] == "stopped" or route != {k: r[k] for k in ("task", "chat_id", "user_id", "thread_id")} for r in active):
            store.stop_session(session["session_key"])
            server._interrupt_session_turn(sid, session)
            session["_question_relay_used"] = False


def stop(session):
    if not session.get("_question_relay_owner"):
        return
    from tui_gateway import server
    with server._session_db(session) as db:
        if db is not None:
            RelayStore(db).stop_session(session["session_key"])


def finish(session, text, status="complete"):
    if not session.get("_question_relay_used"):
        return
    from tui_gateway import server
    with server._session_db(session) as db:
        if db is None:
            raise RuntimeError("Question relay result was not persisted")
        store = RelayStore(db)
        if status != "complete":
            store.stop_session(session["session_key"])
        else:
            store.finish(session["session_key"], text)


def resume_note(session):
    """Fence a new owner before any model/tool call, not at its next clarify question."""
    from tui_gateway import server
    with server._session_db(session) as db:
        if db is None:
            config = config_for(session)
            relay = config.get("question_relay")
            auto = relay.get("auto") if isinstance(relay, dict) else None
            if (route_for(config, session.get("session_key")) is not None
                    or isinstance(auto, dict) and auto.get("enabled") is True):
                raise RuntimeError("Question relay state unavailable; cannot safely resume")
            return ""
        owner = session.setdefault("_question_relay_owner", uuid.uuid4().hex)
        route = route_for(config_for(session), session.get("session_key"), db)
        note = RelayStore(db).resume_note(session.get("session_key"), owner, route)
        if note:
            session["_question_relay_used"] = True
        return note
