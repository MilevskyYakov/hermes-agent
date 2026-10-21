"""Classic CLI/Orca integration using its existing modal and agent-monitor loops."""
import time
import uuid

from hermes_question_relay import RelayStore, route_for, load_relay_config


def _config():
    return load_relay_config()


def resume_note(cli):
    db = getattr(cli, "_session_db", None)
    if db is None:
        config = _config()
        relay = config.get("question_relay")
        auto = relay.get("auto") if isinstance(relay, dict) else None
        if (route_for(config, getattr(cli, "session_id", None)) is not None
                or isinstance(auto, dict) and auto.get("enabled") is True):
            raise RuntimeError("Question relay requires the original session database")
        return ""
    if not getattr(cli, "_question_relay_owner", None):
        cli._question_relay_owner = uuid.uuid4().hex
    route = route_for(_config(), cli.session_id, db)
    note = RelayStore(db).resume_note(cli.session_id, cli._question_relay_owner, route)
    if note:
        cli._question_relay_binding = (cli.session_id, route)
    return note


def block(cli, question, choices, multi_select=False, questions=None):
    config = _config()
    route = route_for(config, getattr(cli, "session_id", None), getattr(cli, "_session_db", None))
    if route is None:
        if config.get("question_relay"):
            from cli import _cprint
            _cprint("Question relay: no authorized route for this session; using local clarification.")
        return False, None
    if getattr(cli, "_session_db", None) is None:
        raise RuntimeError("Question relay requires the original session database")
    store = RelayStore(cli._session_db)
    if not getattr(cli, "_question_relay_owner", None):
        cli._question_relay_owner = uuid.uuid4().hex
    owner = cli._question_relay_owner
    store.resume_note(cli.session_id, owner, route)
    rid = uuid.uuid4().hex
    entries = questions or [{"qid": "q1", "question": question, "choices": choices or [], "multi_select": multi_select}]
    store.create(rid, cli.session_id, owner, route, entries)
    cli._question_relay_binding = (cli.session_id, route)
    cli._question_relay_waiting = (rid, entries)
    try:
        # Reuse the batch modal even for one question: one submission hook covers
        # choice, multi-select, free text and all keyboard bindings.
        result = cli._clarify_callback_batch(entries)
        if isinstance(result, dict) and result.get("timed_out"):
            store.expire(rid)
        elif not isinstance(result, dict):
            store.stop_session(cli.session_id)
        answer = store.consume(rid, owner)
        if answer is not None:
            return True, {"answers": answer} if questions else answer["q1"]
        row = store.get(rid)
        if row["status"] == "stopped":
            _interrupt(cli)
            return True, ""
        return True, {"answers": row["answers"], "timed_out": True} if questions else ""
    except Exception:
        store.stop_session(cli.session_id)
        _interrupt(cli)
        raise
    finally:
        cli._question_relay_waiting = None
        cli._clarify_teardown()


def lock_answer(cli, state, answer):
    waiting = getattr(cli, "_question_relay_waiting", None)
    if waiting is None:
        return answer
    from cli import _cprint
    rid, _ = waiting
    qid = state["questions"][state["active"]]["qid"]
    store = RelayStore(cli._session_db)
    status = store.answer(rid, qid, answer)
    row = store.get(rid)
    if status not in {"accepted", "duplicate"}:
        _cprint(f"Question relay: {status}; saved decision unchanged.")
    # The native renderer must show the durable winner, never the losing local proposal.
    return row["answers"].get(qid)


def poll(cli, response_queue):
    waiting = getattr(cli, "_question_relay_waiting", None)
    if waiting is None:
        return
    from cli import _cprint
    rid, entries = waiting
    store = RelayStore(cli._session_db)
    session_id, route = cli._question_relay_binding
    if route_for(_config(), session_id, cli._session_db) != route:
        store.stop_session(session_id)
    row = store.get(rid)
    snapshot = (row["status"], row["delivery"], row["revision"], row["delivered_revision"])
    if snapshot != getattr(cli, "_question_relay_display", None):
        cli._question_relay_display = snapshot
        _cprint(f"Question relay: {row['delivery']}; {row['status']}; Telegram revision "
                f"{row['delivered_revision']}/{row['revision']}")
    state = getattr(cli, "_clarify_state", None)
    if state is not None:
        for entry in entries:
            qid = entry["qid"]
            if qid in row["answers"] and state["answers"].get(qid) != row["answers"][qid]:
                state["answers"][qid] = row["answers"][qid]
                cli._persist_prompt_summary("?", "Clarify", entry["question"], row["answers"][qid])
        unanswered = [i for i, q in enumerate(entries) if q["qid"] not in row["answers"]]
        if unanswered and entries[state["active"]]["qid"] in row["answers"]:
            cli._clarify_batch_set_active(state, unanswered[0])
    if row["status"] == "stopped":
        _interrupt(cli)
        response_queue.put("")
    elif row["status"] == "answered":
        response_queue.put(row["answers"])
    store.heartbeat(rid, cli._question_relay_owner)


def _interrupt(cli):
    from agent.interrupt_compat import request_hard_interrupt
    request_hard_interrupt(cli.agent)


def tick(cli):
    binding = getattr(cli, "_question_relay_binding", None)
    if binding is None:
        return
    now = time.monotonic()
    if now - getattr(cli, "_question_relay_last_poll", 0) < 0.5:
        return
    cli._question_relay_last_poll = now
    session_id, route = binding
    try:
        store = RelayStore(cli._session_db)
        if route_for(_config(), session_id, cli._session_db) != route:
            store.stop_session(session_id)
        if any(row["status"] == "stopped" for row in store.for_session(session_id)):
            _interrupt(cli)
    except Exception:
        from cli import _cprint
        _interrupt(cli)
        _cprint("Question relay state unavailable; stop requested, no continuation confirmed.")


def stop(cli):
    binding = getattr(cli, "_question_relay_binding", None)
    if binding is not None:
        RelayStore(cli._session_db).stop_session(binding[0])


def finish(cli, result):
    binding = getattr(cli, "_question_relay_binding", None)
    if binding is None:
        return
    try:
        store = RelayStore(cli._session_db)
        if result and result.get("completed") and not result.get("interrupted") and not result.get("failed"):
            store.finish(binding[0], result.get("final_response", ""))
        else:
            store.stop_session(binding[0])
    except Exception:
        from cli import _cprint
        _cprint("Question relay result was not persisted; Telegram completion is unverified.")
    cli._question_relay_binding = None
