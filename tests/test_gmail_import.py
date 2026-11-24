from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from test_owner_sender import owner_test  # noqa: F401 -- pytest fixture

from agent.gmail_import import import_sent, sent_query
from agent.gmail_sync import sync
from agent.models import Lead
from agent.operations import ActionError

ACCOUNT = "developer@example.com"
NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)


def sent(mid, to, subject="A quick idea for Example", thread=None, **extra):
    return {
        "id": mid,
        "thread_id": thread or "t-" + mid,
        "labels": ["SENT"],
        "snippet": "Hi there, I came across Example and your online course.",
        "internal_date": int(datetime(2026, 11, 24, 9, tzinfo=UTC).timestamp() * 1000),
        "headers": {"to": to, "subject": subject, "message-id": f"<{mid}@mail.gmail.com>", **extra},
    }


def transport_with(messages):
    transport = Mock()
    transport.profile.return_value = ACCOUNT
    transport.sent_messages.return_value = messages
    return transport


def run(db, messages):
    return import_sent(db, transport_with(messages), ACCOUNT, clock=lambda: NOW)


def test_import_marks_lead_contacted_and_is_idempotent(db, lead):
    report = run(db, [sent("g1", f"Owner <{lead.email}>")])
    assert [r["lead_id"] for r in report.imported] == [lead.id]
    assert db.get_lead(lead.id).stage == "contacted"
    row = db.connection.execute("SELECT * FROM messages WHERE lead_id=?", (lead.id,)).fetchone()
    assert row["status"] == "sent" and row["gmail_thread_id"] == "t-g1"
    assert row["sent_at"].startswith("2026-11-24T09")
    again = run(db, [sent("g1", lead.email)])
    assert again.imported == [] and again.already_tracked == [lead.email]


def test_unmatched_and_multi_recipient_are_reported_not_imported(db, lead):
    report = run(
        db,
        [
            sent("g1", "stranger@nowhere.org"),
            sent("g2", f"{lead.email}, other@example.org"),
        ],
    )
    assert report.unmatched == ["stranger@nowhere.org"]
    assert report.skipped == ["g2"]
    assert db.get_lead(lead.id).stage == "qualified"


def test_matches_by_domain_when_email_differs(db):
    lead = db.add_lead(Lead(domain="quiltskipper.com", email=None, stage="qualified"))
    report = run(db, [sent("g1", "jenny@quiltskipper.com")])
    assert [r["lead_id"] for r in report.imported] == [lead.id]


def test_existing_queued_draft_is_replaced_and_can_never_send(db, owner_test):  # noqa: F811
    message, _settings, _row, _transport, _ = owner_test
    lead = db.get_lead(message.lead_id)
    report = run(db, [sent("g9", lead.email)])
    assert len(report.imported) == 1
    stored = db.get_message(message.id)
    assert stored.status == "sent"
    state = db.connection.execute(
        "SELECT state FROM outbound_queue WHERE message_id=?", (message.id,)
    ).fetchone()[0]
    assert state == "rejected"


def test_imported_thread_is_picked_up_by_sync(db, lead):
    run(db, [sent("g1", lead.email, thread="thread-7")])
    transport = transport_with([])
    transport.thread = Mock(
        return_value=[
            sent("g1", lead.email, thread="thread-7"),
            {
                "id": "in-1",
                "thread_id": "thread-7",
                "labels": ["INBOX"],
                "snippet": "Sounds interesting, tell me more",
                "internal_date": int(NOW.timestamp() * 1000),
                "headers": {"from": lead.email, "subject": "Re: A quick idea"},
            },
        ]
    )
    report = sync(db, transport, ACCOUNT, clock=lambda: NOW)
    assert report.count("reply") == 1
    assert db.get_lead(lead.id).stage == "replied"


def test_account_mismatch_aborts(db, lead):
    transport = transport_with([sent("g1", lead.email)])
    transport.profile.return_value = "someone@else.com"
    with pytest.raises(ActionError):
        import_sent(db, transport, ACCOUNT)
    assert db.get_lead(lead.id).stage == "qualified"


def test_sent_query():
    assert sent_query() == 'in:sent newer_than:180d subject:"quick idea"'
    assert sent_query("", 30) == "in:sent newer_than:30d"
