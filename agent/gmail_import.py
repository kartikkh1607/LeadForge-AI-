"""Import emails sent outside LeadForge (e.g. straight from Gmail) into the tracker.

Reads your Gmail Sent folder (read-only), matches each recipient to a lead, and
records the email as that lead's sent initial message. After an import:

- the lead's stage becomes 'contacted', so discovery/qualification won't treat it
  as a fresh prospect and the send flow won't email it again;
- `sync` reads that Gmail thread for replies, bounces and opt-outs;
- follow-up eligibility counts from the real send date.

Never sends, labels or deletes mail. Re-running is safe: already-tracked
messages are skipped.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import getaddresses

from agent.gmail import CredentialStore, GmailError, GmailTransport, load_credentials
from agent.models import utc_now
from agent.operations import ActionError, mutation_lock

DEFAULT_SUBJECT = "quick idea"
IMPORT_NOTE = "imported_from_gmail"


@dataclass
class ImportReport:
    checked: int = 0
    imported: list[dict] = field(default_factory=list)
    already_tracked: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (
            f"Checked {self.checked} sent emails: imported {len(self.imported)}, "
            f"already tracked {len(self.already_tracked)}, "
            f"not in LeadForge {len(self.unmatched)}"
        )
        if self.skipped:
            text += f", skipped {len(self.skipped)} (multiple recipients)"
        return text + ". Read-only; nothing sent."


def sent_query(subject: str = DEFAULT_SUBJECT, days: int = 180) -> str:
    query = f"in:sent newer_than:{int(days)}d"
    subject = subject.strip().replace('"', "")
    return query + (f' subject:"{subject}"' if subject else "")


def _recipient(message):
    headers = message["headers"]
    recipients = [a for _, a in getaddresses([headers.get("to", ""), headers.get("cc", "")]) if a]
    return recipients[0].strip().casefold() if len(recipients) == 1 else None


def _find_lead(db, email):
    row = db.connection.execute(
        "SELECT id FROM leads WHERE email=? COLLATE NOCASE", (email,)
    ).fetchone()
    if row:
        return row[0]
    domain = email.rsplit("@", 1)[-1]
    rows = db.connection.execute(
        "SELECT id FROM leads WHERE domain=? COLLATE NOCASE OR domain=?",
        (domain, domain.removeprefix("www.")),
    ).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _record(db, lead_id, message, now):
    """Record one sent Gmail message; returns False if it was already tracked."""
    if db.connection.execute(
        "SELECT 1 FROM messages WHERE gmail_message_id=?", (message["id"],)
    ).fetchone():
        return False
    existing = db.connection.execute(
        "SELECT id,status FROM messages WHERE lead_id=? AND kind='initial'", (lead_id,)
    ).fetchone()
    if existing and existing["status"] in {"sent", "bounced"}:
        return False
    sent_at = datetime.fromtimestamp(message["internal_date"] / 1000, UTC).isoformat()
    subject = message["headers"].get("subject", "").strip() or "(no subject)"
    body = message["snippet"].strip() or "(sent from Gmail)"
    rfc_id = message["headers"].get("message-id", "").strip() or f"<gmail-{message['id']}>"
    if existing:
        # A LeadForge draft never went out; the Gmail email did. Make the record match reality
        # and make sure the queue can never send the old draft as a duplicate.
        db.connection.execute(
            "UPDATE outbound_queue SET state='rejected',outcome='sent_outside_leadforge' "
            "WHERE message_id=? AND state IN ('pending','uncertain')",
            (existing["id"],),
        )
        db.connection.execute(
            "UPDATE messages SET subject=?,body=? WHERE id=?", (subject, body, existing["id"])
        )
        db.connection.execute(
            "UPDATE messages SET status='sent',approved_at=?,sent_at=?,requires_manual_review=0,"
            "rfc822_message_id=?,gmail_message_id=?,gmail_thread_id=?,error=? WHERE id=?",
            (
                sent_at,
                sent_at,
                rfc_id,
                message["id"],
                message["thread_id"],
                IMPORT_NOTE,
                existing["id"],
            ),
        )
    else:
        db.connection.execute(
            "INSERT INTO messages(lead_id,kind,subject,body,status,rfc822_message_id,"
            "gmail_message_id,gmail_thread_id,approved_at,sent_at,error,created_at,"
            "requires_manual_review) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                lead_id,
                "initial",
                subject,
                body,
                "sent",
                rfc_id,
                message["id"],
                message["thread_id"],
                sent_at,
                sent_at,
                IMPORT_NOTE,
                now.isoformat(),
            ),
        )
    db.connection.execute(
        "UPDATE leads SET stage='contacted' WHERE id=? AND stage IN ('new','qualified','skipped')",
        (lead_id,),
    )
    return True


def import_sent(db, transport, account, *, subject=DEFAULT_SUBJECT, days=180, clock=utc_now):
    report = ImportReport()
    with mutation_lock(db):
        if transport.profile() != account.casefold():
            raise ActionError("Gmail account mismatch")
        messages = transport.sent_messages(sent_query(subject, days))
        for message in sorted(messages, key=lambda m: m["internal_date"]):
            if "SENT" not in message["labels"]:
                continue
            report.checked += 1
            email = _recipient(message)
            if email is None:
                report.skipped.append(message["id"])
                continue
            lead_id = _find_lead(db, email)
            if lead_id is None:
                report.unmatched.append(email)
                continue
            with db.connection:
                db.connection.execute("BEGIN IMMEDIATE")
                if _record(db, lead_id, message, clock()):
                    report.imported.append({"lead_id": lead_id, "email": email})
                else:
                    report.already_tracked.append(email)
    return report


def open_transport(settings):
    """Build a Gmail transport from the stored OAuth token; friendly errors for the UI."""
    if not settings.gmail_account:
        raise ActionError("Set GMAIL_ACCOUNT in Settings or .env first")
    try:
        credentials = load_credentials(settings.gmail_account, CredentialStore())
    except Exception:  # noqa: BLE001 -- never surface token details
        raise ActionError(
            "Gmail not connected. Run `python -m agent.send_worker connect` once."
        ) from None
    from agent.sender import owners

    return GmailTransport(
        credentials.token, owner_addresses=owners(settings), account=settings.gmail_account
    )


def run_with_gmail(settings, callback):
    transport = open_transport(settings)
    try:
        return callback(transport)
    except GmailError as exc:
        raise ActionError(str(exc)) from None
    finally:
        transport.close()
