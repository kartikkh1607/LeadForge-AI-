"""Owner-only, one-shot queue. No scheduler, prospect delivery or follow-up sends."""

import json
from datetime import UTC, datetime, timedelta
from email.utils import make_msgid
from random import SystemRandom
from zoneinfo import ZoneInfo

from agent.config import SEND_TIMEZONE
from agent.gmail import Outcome, mime_message
from agent.models import utc_now
from agent.operations import ActionError, mutation_lock
from agent.safety import check_send_eligibility
from agent.settings_store import effective_settings
from agent.verify import normalize_email


def owners(settings):
    return {
        normalize_email(s.strip()).casefold()
        for s in settings.owner_test_addresses.split(",")
        if s.strip()
    }


def owner_blockers(db, message_id, settings, *, claimed=False):
    message = db.get_message(message_id)
    lead = db.get_lead(message.lead_id)
    blockers = list(check_send_eligibility(db, message_id, settings, claimed=claimed).blockers)
    if not lead.email or lead.email.casefold() not in owners(settings):
        blockers.append("owner_allowlist_required")
    if not settings.gmail_account:
        blockers.append("gmail_account_required")
    else:
        normalize_email(settings.gmail_account)
    return blockers


def queue_rows(db):
    return [
        dict(r) for r in db.connection.execute("SELECT * FROM outbound_queue ORDER BY message_id")
    ]


def prepare_owner_test(db, message_id, base):
    """Label an existing grounded owner draft; revoke approval and never enqueue/send."""
    from agent.evidence import validate_draft
    from agent.personalize import validate_managed_message

    with mutation_lock(db), db.connection:
        db.connection.execute("BEGIN IMMEDIATE")
        settings = effective_settings(db, base)
        message = db.get_message(message_id)
        lead = db.get_lead(message.lead_id)
        if not lead.email or lead.email.casefold() not in owners(settings):
            raise ActionError("owner_allowlist_required")
        if message.status not in {"draft", "approved"}:
            raise ActionError("Only pending grounded drafts can become owner tests")
        validate_managed_message(db, message)
        if message.personalization.get("owner_test") is True:
            return message
        subject = "Owner-only test: " + message.subject
        body = "This is an owner-only delivery test.\n\n" + message.body
        validate_draft(subject, body)
        envelope = {**message.personalization, "owner_test": True}
        db.connection.execute(
            "UPDATE messages SET subject=?,body=?,personalization_json=?,status='draft',"
            "approved_at=NULL,requires_manual_review=1,truthful_identity_confirmed=0,opt_out_confirmed=0 WHERE id=?",
            (subject, body, json.dumps(envelope), message_id),
        )
    return db.get_message(message_id)


def enqueue(db, message_id, base):
    """Explicit queueing is safe while paused/dry-run; no permission to send is stored."""
    with mutation_lock(db), db.connection:
        db.connection.execute("BEGIN IMMEDIATE")
        settings = effective_settings(db, base)
        blockers = set(owner_blockers(db, message_id, settings)) - {"paused", "dry_run"}
        if blockers:
            raise ActionError(", ".join(sorted(blockers)))
        message = db.get_message(message_id)
        lead = db.get_lead(message.lead_id)
        db.connection.execute(
            "INSERT INTO outbound_queue(message_id,version,content_hash,approved_at,recipient,"
            "account,rfc_message_id,created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
            "version=excluded.version,content_hash=excluded.content_hash,approved_at=excluded.approved_at,"
            "recipient=excluded.recipient,account=excluded.account,rfc_message_id=excluded.rfc_message_id "
            "WHERE outbound_queue.state='pending' AND (outbound_queue.version!=excluded.version "
            "OR outbound_queue.content_hash!=excluded.content_hash OR outbound_queue.approved_at!=excluded.approved_at "
            "OR outbound_queue.recipient!=excluded.recipient OR outbound_queue.account!=excluded.account)",
            (
                message.id,
                message.version,
                message.content_hash,
                message.approved_at.isoformat(),
                lead.email,
                settings.gmail_account.casefold(),
                make_msgid(domain="leadforge.local"),
                utc_now().isoformat(),
            ),
        )
    return next(r for r in queue_rows(db) if r["message_id"] == message_id)


def confirmation(row):
    return f"SEND OWNER TEST {row['message_id']} {row['recipient']} {row['content_hash']} {row['rfc_message_id']}"


def day_key(now):
    if now.tzinfo is None:
        raise ValueError("Timezone-aware clock required")
    return now.astimezone(ZoneInfo(SEND_TIMEZONE)).date().isoformat()


def quota_used(db, now, *, exclude_message_id=None):
    day = day_key(now)
    # UNION counts each message once, including pre-existing initial/follow-up sends.
    used = {
        r["id"]
        for r in db.connection.execute("SELECT id,sent_at FROM messages WHERE sent_at IS NOT NULL")
        if day_key(datetime.fromisoformat(r["sent_at"])) == day
    }
    for row in queue_rows(db):
        if row["quota_day"] == day or row["state"] in {"sending", "uncertain"}:
            used.add(row["message_id"])
    used.discard(exclude_message_id)
    return len(used)


def _snapshot_matches(db, row, settings):
    message = db.get_message(row["message_id"])
    lead = db.get_lead(message.lead_id)
    return (
        message.version == row["version"]
        and message.content_hash == row["content_hash"]
        and message.approved_at
        and message.approved_at.isoformat() == row["approved_at"]
        and lead.email == row["recipient"]
        and settings.gmail_account.casefold() == row["account"]
    )


def _record(db, row, result, now):
    sent_at = result.sent_at or now.isoformat() if result.state == "sent" else None
    db.connection.execute(
        "INSERT INTO outbound_events(message_id,state,outcome,recorded_at) VALUES (?,?,?,?)",
        (row["message_id"], result.state, result.code, now.isoformat()),
    )
    db.connection.execute(
        "UPDATE outbound_queue SET state=?,outcome=?,gmail_message_id=?,gmail_thread_id=?,sent_at=? WHERE message_id=?",
        (
            result.state,
            result.code,
            result.message_id,
            result.thread_id,
            sent_at,
            row["message_id"],
        ),
    )
    if result.state == "sent":
        db.connection.execute(
            "UPDATE messages SET status='sent',gmail_message_id=?,gmail_thread_id=?,sent_at=?,error=NULL WHERE id=?",
            (result.message_id, result.thread_id, sent_at, row["message_id"]),
        )
        db.connection.execute(
            "UPDATE leads SET stage='contacted' WHERE id=(SELECT lead_id FROM messages WHERE id=?) AND stage='qualified'",
            (row["message_id"],),
        )
    elif result.state == "rejected":
        db.connection.execute(
            "UPDATE messages SET status='failed',error=? WHERE id=?",
            (result.code, row["message_id"]),
        )
    else:
        db.connection.execute(
            "UPDATE messages SET error=? WHERE id=?", (result.code, row["message_id"])
        )


def reconcile(db, transport, account, clock=utc_now):
    """Read-only Gmail calls, including startup recovery; zero matches NEVER requeues."""
    with mutation_lock(db):
        if transport.profile() != account.casefold():
            raise ActionError("Gmail account mismatch")
        for row in queue_rows(db):
            if row["state"] in {"sending", "uncertain"} and row["account"] == account.casefold():
                result = transport.reconcile(row["rfc_message_id"])
                with db.connection:
                    _record(db, row, result, clock())


def send_one(db, message_id, base, transport, explicit_confirmation, clock=utc_now):
    """Single-worker OS lock + atomic SQLite claim; confirmation consumed by the claim.

    Keep a write transaction through final preflight and bounded API request so other
    writers cannot change eligibility between that check and dispatch. A crash rolls
    back only the outcome, leaving the committed claim for reconciliation.
    """
    with mutation_lock(db):
        settings = effective_settings(db, base)
        row = next((r for r in queue_rows(db) if r["message_id"] == message_id), None)
        if not row or row["state"] != "pending":
            raise ActionError("No pending owner test; uncertain sends require reconciliation")
        if explicit_confirmation != confirmation(row):
            raise ActionError("Separate exact owner-test confirmation required")
        blockers = owner_blockers(db, message_id, settings)
        if blockers:
            raise ActionError(", ".join(blockers))
        if transport.profile() != settings.gmail_account.casefold():
            raise ActionError("Gmail account mismatch")
        now = clock().astimezone(UTC)
        with db.connection:
            db.connection.execute("BEGIN IMMEDIATE")
            settings = effective_settings(db, base)
            blockers = owner_blockers(db, message_id, settings)
            if blockers or not _snapshot_matches(db, row, settings):
                raise ActionError(", ".join(blockers) or "Queue approval snapshot changed")
            if quota_used(db, now) >= settings.daily_limit:
                raise ActionError("daily_quota")
            if any(
                r["next_send_at"] and datetime.fromisoformat(r["next_send_at"]) > now
                for r in queue_rows(db)
            ):
                raise ActionError("send_delay")
            changed = db.connection.execute(
                "UPDATE outbound_queue SET state='sending',attempted_at=?,quota_day=?,next_send_at=?,outcome='claimed' "
                "WHERE message_id=? AND state='pending'",
                (
                    now.isoformat(),
                    day_key(now),
                    (now + timedelta(seconds=SystemRandom().randint(60, 180))).isoformat(),
                    message_id,
                ),
            ).rowcount
            if changed != 1:
                raise ActionError("Queue already claimed")
            db.connection.execute(
                "UPDATE messages SET status='sending',rfc822_message_id=? WHERE id=?",
                (row["rfc_message_id"], message_id),
            )
        with db.connection:
            db.connection.execute("BEGIN IMMEDIATE")
            settings = effective_settings(db, base)
            blockers = owner_blockers(db, message_id, settings, claimed=True)
            dispatch_time = clock().astimezone(UTC)
            if quota_used(db, dispatch_time, exclude_message_id=message_id) >= settings.daily_limit:
                blockers.append("daily_quota")
            if blockers or not _snapshot_matches(db, row, settings):
                _record(db, row, Outcome("rejected", "pre_dispatch_blocked"), clock())
                return Outcome("rejected", "pre_dispatch_blocked")
            message = db.get_message(message_id)
            raw = mime_message(row["account"], row["recipient"], message, row["rfc_message_id"])
            db.connection.execute(
                "UPDATE outbound_queue SET quota_day=? WHERE message_id=?",
                (day_key(dispatch_time), message_id),
            )
            db.connection.execute("UPDATE messages SET status='sending' WHERE id=?", (message_id,))
            try:
                result = transport.send(raw)
            except Exception:  # noqa: BLE001 -- sanitize provider errors; uncertain delivery fails closed
                result = Outcome("uncertain", "transport_ambiguous")
            _record(db, row, result, clock())
            db.connection.execute(
                "UPDATE outbound_queue SET next_send_at=? WHERE message_id=?",
                (
                    (clock() + timedelta(seconds=SystemRandom().randint(60, 180))).isoformat(),
                    message_id,
                ),
            )
        return result
