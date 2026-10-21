"""Durable clarify decisions in the existing profile SessionDB; no execution or API calls.

The original process alone consumes an answer. An uncertain delivery or dead owner is
never retried as an agent turn: exactly-once arbitrary external effects cannot be inferred
from a saved answer. Recovery must reconcile the original transcript first.
"""
import json
import os
import re
import socket
import time


def route_for(config, session_id):
    relay = config.get("question_relay", {})
    if not isinstance(relay, dict):
        raise ValueError("question_relay must be a mapping")
    routes = relay.get("sessions", {})
    if not isinstance(routes, dict):
        raise ValueError("question_relay.sessions must be a mapping")
    route = routes.get(session_id)
    if route is None:
        return None
    if not isinstance(route, dict) or not isinstance(route.get("task"), str) or not route["task"].strip():
        raise ValueError("question relay requires an explicit task")
    # Telegram numeric identities, never usernames, wildcard recipients or a home-channel fallback.
    for key in ("chat_id", "user_id"):
        if not re.fullmatch(r"-?[1-9][0-9]*" if key == "chat_id" else r"[1-9][0-9]*", str(route.get(key, ""))):
            raise ValueError(f"question relay requires a numeric {key}")
    thread = str(route.get("thread_id") or "")
    if thread and not re.fullmatch(r"[1-9][0-9]*", thread):
        raise ValueError("invalid question relay thread_id")
    return {"task": route["task"], "chat_id": str(route["chat_id"]), "user_id": str(route["user_id"]), "thread_id": thread}


def _decode(row):
    if row is None:
        return None
    result = dict(row)
    for key in ("questions", "answers"):
        result[key] = json.loads(result[key])
    return result


class RelayStore:
    """One atomic winner per question, shared by the UI and messaging processes."""

    def __init__(self, db):
        self.db = db

    def get(self, request_id):
        return _decode(self.db._read_one("SELECT * FROM question_relay WHERE request_id = ?", (request_id,)))

    def for_session(self, session_id):
        return [_decode(r) for r in self.db._read_all(
            "SELECT * FROM question_relay WHERE session_id = ? ORDER BY updated_at", (session_id,))]

    def create(self, request_id, session_id, owner, route, questions):
        route = route_for({"question_relay": {"sessions": {session_id: route}}}, session_id)
        if route is None:
            raise ValueError("explicit question relay route required")
        if not request_id or not session_id or not owner or not questions:
            raise ValueError("request, session, owner and questions required")
        ids = [q["qid"] for q in questions]
        if len(set(ids)) != len(ids):
            raise ValueError("question IDs must be unique")
        identity = json.dumps(process_identity())
        def write(conn):
            if conn.execute("SELECT 1 FROM question_relay WHERE session_id = ? AND "
                            "(status = 'stopped' OR (owner != ? AND status NOT IN ('complete', 'expired', 'reviewed')))",
                            (session_id, owner)).fetchone():
                raise ValueError("question session already has an owner or requires recovery")
            conn.execute(
                "INSERT INTO question_relay(request_id, session_id, owner, task, chat_id, user_id, thread_id, "
                "questions, updated_at, owner_seen, owner_process) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, session_id, owner, route["task"], route["chat_id"], route["user_id"], route["thread_id"],
                 json.dumps(questions, ensure_ascii=False), time.time(), time.time(), identity))
        self.db._execute_write(write)

    def answer(self, request_id, question_id, answer):
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 16000:
            return "invalid"
        def write(conn):
            row = _decode(conn.execute("SELECT * FROM question_relay WHERE request_id = ?", (request_id,)).fetchone())
            if not row:
                return "unknown"
            if row["status"] in {"stopped", "expired", "recovery"}:
                return row["status"]
            questions = {q["qid"]: q for q in row["questions"]}
            if question_id not in questions:
                return "unknown_question"
            if question_id in row["answers"]:
                return "duplicate" if row["answers"][question_id] == answer else "conflict"
            if row["status"] != "waiting":
                return "expired"
            answers = row["answers"] | {question_id: answer}
            status = "answered" if len(answers) == len(questions) else "waiting"
            conn.execute("UPDATE question_relay SET answers = ?, status = ?, revision = revision + 1, updated_at = ? "
                         "WHERE request_id = ?", (json.dumps(answers, ensure_ascii=False), status, time.time(), request_id))
            return "accepted"
        return self.db._execute_write(write)

    def find_reply(self, chat_id, thread_id, message_id):
        return _decode(self.db._read_one(
            "SELECT * FROM question_relay WHERE chat_id = ? AND thread_id = ? AND message_id = ?",
            (str(chat_id), str(thread_id or ""), str(message_id))))

    def reply(self, chat_id, thread_id, user_id, message_id, text):
        row = self.find_reply(chat_id, thread_id, message_id)
        if not row:
            return "unknown"
        if str(user_id) != row["user_id"]:
            return "forbidden"
        if text.strip().lower() in {"/stop", "/revoke"}:
            self.stop_session(row["session_id"])
            return "stopped"
        questions = row["questions"]
        qid = questions[0]["qid"]
        if len(questions) > 1:
            qid, separator, text = text.partition(":")
            if not separator:
                return "question_id_required"
            qid, text = qid.strip(), text.strip()
        return self.answer(row["request_id"], qid, text)

    def consume(self, request_id, owner):
        def write(conn):
            row = conn.execute("UPDATE question_relay SET status = 'consumed', revision = revision + 1, updated_at = ? "
                               "WHERE request_id = ? AND owner = ? AND status = 'answered' RETURNING answers",
                               (time.time(), request_id, owner)).fetchone()
            return json.loads(row[0]) if row else None
        return self.db._execute_write(write)

    def stop_session(self, session_id):
        self.db._write_sql("UPDATE question_relay SET status = 'stopped', revision = revision + 1, updated_at = ? "
                           "WHERE session_id = ? AND status NOT IN ('stopped', 'complete', 'expired')", (time.time(), session_id))

    def expire(self, request_id):
        self.db._write_sql("UPDATE question_relay SET status = 'expired', revision = revision + 1, updated_at = ? "
                           "WHERE request_id = ? AND status = 'waiting'", (time.time(), request_id))

    def heartbeat(self, request_id, owner):
        self.db._write_sql("UPDATE question_relay SET owner_seen = ? WHERE request_id = ? AND owner = ?",
                           (time.time(), request_id, owner))

    def finish(self, session_id, result):
        self.db._write_sql("UPDATE question_relay SET result = ?, status = 'complete', revision = revision + 1, updated_at = ? "
                           "WHERE session_id = ? AND status = 'consumed'", (result, time.time(), session_id))

    def pending_delivery(self):
        return [_decode(r) for r in self.db._read_all(
            "SELECT * FROM question_relay WHERE delivery = 'queued' OR "
            "(delivery = 'sent' AND delivered_revision < revision) ORDER BY updated_at LIMIT 50")]

    def cancel_delivery(self, request_id):
        # Disable delivery without deleting the address/message needed to reject a late reply.
        self.db._write_sql("UPDATE question_relay SET delivery = 'disabled' WHERE request_id = ?", (request_id,))

    def claim_delivery(self, request_id):
        return self.db._write_rowcount("UPDATE question_relay SET delivery = 'sending' "
                                      "WHERE request_id = ? AND delivery = 'queued'", (request_id,)) == 1

    def delivery(self, request_id, state, message_id=None, revision=-1):
        if state not in {"sent", "unknown", "failed"}:
            raise ValueError("invalid delivery state")
        self.db._write_sql("UPDATE question_relay SET delivery = ?, message_id = ?, delivered_revision = ? "
                           "WHERE request_id = ?", (state, message_id, revision, request_id))

    def mark_displayed(self, request_id, revision):
        self.db._write_sql("UPDATE question_relay SET delivered_revision = MAX(delivered_revision, ?) WHERE request_id = ?",
                           (revision, request_id))

    def recover(self, session_id, request_id, evidence):
        """Record reviewed recovery only after the original owner is provably dead.

        Never starts a turn, replays tools, resends a message or grants permissions.
        The next ordinary resume reads the saved decisions with its existing transcript.
        """
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("external-effect review evidence required")
        row = self.get(request_id)
        if row is None or row["session_id"] != session_id:
            raise ValueError("question does not belong to this session")
        if row["status"] == "reviewed":
            return False
        if row["status"] in {"complete", "expired"}:
            raise ValueError("question needs no recovery")
        import psutil
        identity = json.loads(row["owner_process"])
        if identity.get("host") != socket.gethostname():
            raise ValueError("owner host cannot be verified locally")
        try:
            if psutil.Process(identity["pid"]).create_time() == identity["started"]:
                raise ValueError("original owner is still alive; stop it before recovery")
        except psutil.NoSuchProcess:
            pass
        def write(conn):
            if conn.execute("SELECT 1 FROM session_turn_leases WHERE conversation_id = ? AND expires_at > ?",
                            (session_id, time.time())).fetchone():
                raise ValueError("a session turn lease is still active")
            return conn.execute("UPDATE question_relay SET status = 'reviewed', recovery_note = ?, "
                                "revision = revision + 1, updated_at = ? WHERE request_id = ? AND owner = ? "
                                "AND status = ?", (evidence, time.time(), request_id, row["owner"], row["status"])).rowcount == 1
        return self.db._execute_write(write)


    def resume_note(self, session_id, owner, route=None):
        if not self.for_session(session_id):
            return ""  # Ordinary sessions do not acquire a relay write lock.
        # Claim reviewed decisions atomically before model/tools, so a second resume
        # cannot execute them and the new owner can publish completion on the same card.
        def write(conn):
            rows = [_decode(r) for r in conn.execute(
                "SELECT * FROM question_relay WHERE session_id = ?", (session_id,))]
            blocked = [r for r in rows if r["status"] == "stopped" or
                       (r["owner"] != owner and r["status"] not in {"complete", "expired", "reviewed"})]
            if blocked:
                ids = ", ".join(r["request_id"] for r in blocked)
                raise RuntimeError(f"Question relay recovery required ({ids}). Inspect `hermes sessions questions` before resuming; no actions started.")
            reviewed = [r for r in rows if r["status"] == "reviewed"]
            if reviewed:
                if not owner or any(route != {k: r[k] for k in ("task", "chat_id", "user_id", "thread_id")} for r in reviewed):
                    raise RuntimeError("Question relay recovery requires an active unchanged route; no actions started.")
                conn.execute("UPDATE question_relay SET status = 'consumed', owner = ?, owner_process = ?, "
                             "owner_seen = ?, updated_at = ?, revision = revision + 1 "
                             "WHERE session_id = ? AND status = 'reviewed'",
                             (owner, json.dumps(process_identity()), time.time(), time.time(), session_id))
            return [{"questions": r["questions"], "decisions": r["answers"], "effect_review": r["recovery_note"]}
                    for r in reviewed]
        reviewed = self.db._execute_write(write)
        if not reviewed:
            return ""
        return ("Recovered question decisions, reviewed by the operator. Reuse the existing transcript and confirmed "
                "external results; do not repeat completed actions. This is not new execution permission.\n" +
                json.dumps(reviewed, ensure_ascii=False))


def process_identity():
    import psutil
    return {"pid": os.getpid(), "started": psutil.Process().create_time(), "host": socket.gethostname()}


def sessions_questions(db, args):
    """Local status and explicitly reviewed recovery for `hermes sessions questions`."""
    store = RelayStore(db)
    if args.recover:
        try:
            changed = store.recover(args.session_id, args.recover, args.effects_reviewed)
        except (ValueError, OSError) as exc:
            print(f"Recovery refused: {exc}")
            return 1
        print("Recovery review recorded; no execution started." if changed else "Already reviewed; unchanged.")
    rows = store.for_session(args.session_id)
    # Recipient IDs and host paths are omitted. The decisions themselves may be private.
    print(json.dumps([{k: row[k] for k in ("request_id", "task", "status", "delivery", "answers", "result",
                                          "revision", "delivered_revision", "recovery_note")} for row in rows],
                     ensure_ascii=False, indent=2))
    return 0
