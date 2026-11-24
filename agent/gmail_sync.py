"""Phase 6: read-only Gmail sync for replies, bounces and opt-outs.

Only threads that LeadForge itself sent are read. Each inbound Gmail message is
recorded once (unique gmail_message_id), so re-running a sync is idempotent.
Effects are conservative and never send anything:

- hard bounce   -> message 'bounced', lead email_status 'bounced', email suppressed
- spam block    -> same as hard bounce, suppression reason 'spam_block'
- soft bounce   -> recorded only (Gmail is still retrying)
- auto-reply    -> recorded only (out-of-office is not engagement)
- opt-out       -> permanent email suppression, lead 'not_interested'
- human reply   -> lead 'replied'; the existing trigger cancels pending follow-ups
"""

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr

from agent.gmail import GmailError
from agent.models import utc_now
from agent.operations import ActionError, mutation_lock

BOUNCE_SENDERS = re.compile(r"^(mailer-daemon|postmaster|mail-daemon)@", re.IGNORECASE)
SOFT_BOUNCE = re.compile(
    r"\(delay\)|delivery incomplete|temporary problem|will retry|delayed", re.IGNORECASE
)
SPAM_BLOCK = re.compile(r"\b5\.7\.\d\b|spam|blocked|blacklist|blocklist|policy", re.IGNORECASE)
AUTO_SUBJECT = re.compile(
    r"^(out of (the )?office|automatic reply|auto(matic)?[- ]?reply|autoreply|away from)",
    re.IGNORECASE,
)
OPT_OUT = re.compile(
    r"\b(unsubscribe|remove me|take me off|opt(?:ed)?[- ]out|"
    r"stop (?:emailing|contacting|sending)|do not (?:email|contact)|don'?t (?:email|contact)|"
    r"not interested|no thanks|no,? thank you)\b",
    re.IGNORECASE,
)
# Quoted history must never be read as the prospect's words (our own P.S. says "no thanks").
QUOTE_START = re.compile(
    r"\bOn .{0,120}?wrote:|^>|-{2,}\s*Original Message", re.IGNORECASE | re.MULTILINE
)


@dataclass
class SyncReport:
    threads_checked: int = 0
    events: list[dict] = field(default_factory=list)
    skipped_threads: list[str] = field(default_factory=list)

    def count(self, kind):
        return sum(1 for e in self.events if e["kind"] == kind)


def reply_text(snippet: str) -> str:
    text = html.unescape(snippet)
    match = QUOTE_START.search(text)
    return (text[: match.start()] if match else text).strip()


def classify(message: dict, account: str) -> str | None:
    headers = message["headers"]
    sender = parseaddr(headers.get("from", ""))[1].casefold()
    subject = headers.get("subject", "")
    labels = set(message["labels"])
    if sender == account.casefold() or "SENT" in labels or "DRAFT" in labels:
        return None
    content_type = headers.get("content-type", "").casefold()
    is_bounce = bool(BOUNCE_SENDERS.match(sender)) or (
        "multipart/report" in content_type and "delivery-status" in content_type
    )
    if is_bounce:
        detail = subject + " " + message["snippet"]
        if SOFT_BOUNCE.search(detail) and not re.search(r"\(failure\)", subject, re.IGNORECASE):
            return "bounce_soft"
        return "spam_block" if SPAM_BLOCK.search(detail) else "bounce_hard"
    auto = headers.get("auto-submitted", "").casefold()
    if (
        (auto and auto != "no")
        or headers.get("x-autoreply")
        or headers.get("x-autorespond")
        or AUTO_SUBJECT.search(subject)
    ):
        return "auto_reply"
    if OPT_OUT.search(reply_text(message["snippet"])):
        return "opt_out"
    return "reply"


def sent_threads(db):
    return [
        dict(r)
        for r in db.connection.execute(
            "SELECT m.id AS message_id, m.lead_id, m.gmail_thread_id, m.gmail_message_id, "
            "l.email, l.stage FROM messages m JOIN leads l ON l.id=m.lead_id "
            "WHERE m.status IN ('sent','bounced') AND m.gmail_thread_id IS NOT NULL "
            "ORDER BY m.id"
        )
    ]


def _apply(db, row, kind, message, now):
    lead_id, message_id = row["lead_id"], row["message_id"]
    received = datetime.fromtimestamp(message["internal_date"] / 1000, UTC).isoformat()
    inserted = db.connection.execute(
        "INSERT OR IGNORE INTO inbound_events(lead_id,message_id,gmail_message_id,gmail_thread_id,"
        "kind,sender,subject,snippet,received_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            lead_id,
            message_id,
            message["id"],
            message["thread_id"],
            kind,
            parseaddr(message["headers"].get("from", ""))[1],
            message["headers"].get("subject", "")[:300],
            message["snippet"][:500],
            received,
            now.isoformat(),
        ),
    ).rowcount
    if not inserted:
        return False
    email = row["email"]
    if kind in {"bounce_hard", "spam_block"}:
        db.connection.execute(
            "UPDATE messages SET status='bounced',error=? WHERE id=? AND status='sent'",
            (kind, message_id),
        )
        db.connection.execute("UPDATE leads SET email_status='bounced' WHERE id=?", (lead_id,))
        if email:
            db.connection.execute(
                "INSERT OR IGNORE INTO suppression(kind,value,reason,created_at) "
                "VALUES ('email',?,?,?)",
                (email, "bounce" if kind == "bounce_hard" else "spam_block", now.isoformat()),
            )
    elif kind == "opt_out":
        if email:
            db.connection.execute(
                "INSERT OR IGNORE INTO suppression(kind,value,reason,created_at) "
                "VALUES ('email',?,'opt_out',?)",
                (email, now.isoformat()),
            )
        db.connection.execute(
            "UPDATE leads SET stage='not_interested' WHERE id=? AND stage IN "
            "('contacted','replied')",
            (lead_id,),
        )
    elif kind == "reply":
        db.connection.execute(
            "UPDATE leads SET stage='replied' WHERE id=? AND stage='contacted'", (lead_id,)
        )
    return True


def sync(db, transport, account, clock=utc_now) -> SyncReport:
    """Read sent threads and record inbound events. Never sends, labels or deletes mail."""
    report = SyncReport()
    with mutation_lock(db):
        if transport.profile() != account.casefold():
            raise ActionError("Gmail account mismatch")
        started = clock().isoformat()
        with db.connection:
            run_id = db.connection.execute(
                "INSERT INTO gmail_sync_runs(account,started_at) VALUES (?,?)",
                (account.casefold(), started),
            ).lastrowid
        for row in sent_threads(db):
            try:
                messages = transport.thread(row["gmail_thread_id"])
            except GmailError:
                report.skipped_threads.append(row["gmail_thread_id"])
                continue
            report.threads_checked += 1
            for message in sorted(messages, key=lambda m: m["internal_date"]):
                if message["id"] == row["gmail_message_id"]:
                    continue
                kind = classify(message, account)
                if kind is None:
                    continue
                with db.connection:
                    db.connection.execute("BEGIN IMMEDIATE")
                    if _apply(db, row, kind, message, clock()):
                        report.events.append(
                            {
                                "lead_id": row["lead_id"],
                                "message_id": row["message_id"],
                                "kind": kind,
                                "gmail_message_id": message["id"],
                            }
                        )
        with db.connection:
            db.connection.execute(
                "UPDATE gmail_sync_runs SET finished_at=?,threads_checked=?,events_recorded=?,"
                "outcome=? WHERE id=?",
                (
                    clock().isoformat(),
                    report.threads_checked,
                    len(report.events),
                    "partial" if report.skipped_threads else "ok",
                    run_id,
                ),
            )
    return report


def inbound_rows(db, lead_id=None):
    query = "SELECT * FROM inbound_events"
    params = ()
    if lead_id is not None:
        query += " WHERE lead_id=?"
        params = (lead_id,)
    return [dict(r) for r in db.connection.execute(query + " ORDER BY received_at DESC", params)]
