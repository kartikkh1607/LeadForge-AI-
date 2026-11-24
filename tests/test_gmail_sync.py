from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from test_owner_sender import NOW, dispatch, owner_test  # noqa: F401 -- pytest fixture

from agent.gmail import GmailError
from agent.gmail_sync import classify, inbound_rows, reply_text, sync
from agent.operations import ActionError

ACCOUNT = "developer@example.com"


def gmail_message(mid, sender, subject="Re: hello", snippet="", **headers):
    return {
        "id": mid,
        "thread_id": "thread-1",
        "labels": ["INBOX"],
        "snippet": snippet,
        "internal_date": int(datetime(2026, 11, 23, tzinfo=UTC).timestamp() * 1000),
        "headers": {"from": sender, "subject": subject, **headers},
    }


def sent_fixture(db, owner_test):  # noqa: F811
    message, _settings, _row, transport, _ = owner_test
    assert dispatch(db, owner_test).state == "sent"
    lead = db.get_lead(message.lead_id)
    return message, lead, transport


def run(db, transport, inbound):
    transport.thread = Mock(
        return_value=[
            {**gmail_message("gmail-1", ACCOUNT), "labels": ["SENT"]},
            *inbound,
        ]
    )
    return sync(db, transport, ACCOUNT, clock=lambda: NOW)


def test_human_reply_marks_replied_and_is_idempotent(db, owner_test):  # noqa: F811
    _message, lead, transport = sent_fixture(db, owner_test)
    reply = gmail_message("in-1", f"Owner <{lead.email}>", snippet="Yes please, send the demo!")
    report = run(db, transport, [reply])
    assert [e["kind"] for e in report.events] == ["reply"]
    assert db.get_lead(lead.id).stage == "replied"
    assert run(db, transport, [reply]).events == []
    assert len(inbound_rows(db, lead.id)) == 1


def test_opt_out_is_permanent_suppression(db, owner_test):  # noqa: F811
    _message, lead, transport = sent_fixture(db, owner_test)
    report = run(db, transport, [gmail_message("in-2", lead.email, snippet="No thanks.")])
    assert report.count("opt_out") == 1
    assert db.get_lead(lead.id).stage == "not_interested"
    assert db.is_suppressed(lead.email, lead.domain)
    with pytest.raises(Exception, match="permanent"), db.connection:
        db.connection.execute("DELETE FROM suppression WHERE value=?", (lead.email,))


def test_quoted_history_is_not_read_as_opt_out():
    snippet = (
        "Sounds good, go ahead. On Thu, Nov 19, 2026 at 8:34 PM Kartik wrote: "
        "&gt; P.S. If this isn&#39;t useful, just reply &quot;no thanks&quot;"
    )
    assert reply_text(snippet) == "Sounds good, go ahead."
    assert classify(gmail_message("x", "a@b.com", snippet=snippet), ACCOUNT) == "reply"


@pytest.mark.parametrize(
    ("subject", "snippet", "kind"),
    [
        (
            "Delivery Status Notification (Failure)",
            "Address not found Your message wasn't delivered",
            "bounce_hard",
        ),
        (
            "Delivery Status Notification (Failure)",
            "Message blocked 550 High probability of spam",
            "spam_block",
        ),
        (
            "Delivery Status Notification (Delay)",
            "Delivery incomplete There was a temporary problem. Gmail will retry",
            "bounce_soft",
        ),
    ],
)
def test_bounce_classification(subject, snippet, kind):
    message = gmail_message(
        "b", "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", subject, snippet
    )
    assert classify(message, ACCOUNT) == kind


def test_hard_bounce_marks_message_lead_and_suppresses(db, owner_test):  # noqa: F811
    message, lead, transport = sent_fixture(db, owner_test)
    bounce = gmail_message(
        "in-3",
        "mailer-daemon@googlemail.com",
        "Delivery Status Notification (Failure)",
        "Address not found",
    )
    run(db, transport, [bounce])
    assert db.get_message(message.id).status == "bounced"
    assert db.get_lead(lead.id).email_status == "bounced"
    reason = db.connection.execute(
        "SELECT reason FROM suppression WHERE value=?", (lead.email,)
    ).fetchone()[0]
    assert reason == "bounce"


def test_soft_bounce_and_auto_reply_change_nothing(db, owner_test):  # noqa: F811
    message, lead, transport = sent_fixture(db, owner_test)
    run(
        db,
        transport,
        [
            gmail_message(
                "in-4",
                "mailer-daemon@googlemail.com",
                "Delivery Status Notification (Delay)",
                "will retry",
            ),
            gmail_message(
                "in-5",
                lead.email,
                "Out of Office",
                "Back Monday",
                **{"auto-submitted": "auto-replied"},
            ),
        ],
    )
    assert {r["kind"] for r in inbound_rows(db)} == {"bounce_soft", "auto_reply"}
    assert db.get_lead(lead.id).stage == "contacted"
    assert db.get_message(message.id).status == "sent"
    assert not db.is_suppressed(lead.email, lead.domain)


def test_account_mismatch_and_unavailable_threads(db, owner_test):  # noqa: F811
    _message, _lead, transport = sent_fixture(db, owner_test)
    transport.profile.return_value = "someone@else.com"
    with pytest.raises(ActionError, match="mismatch"):
        sync(db, transport, ACCOUNT, clock=lambda: NOW)
    transport.profile.return_value = ACCOUNT
    transport.thread = Mock(side_effect=GmailError("x"))
    report = sync(db, transport, ACCOUNT, clock=lambda: NOW)
    assert report.skipped_threads == ["thread-1"] and report.threads_checked == 0
    outcome = db.connection.execute(
        "SELECT outcome FROM gmail_sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert outcome == "partial"


def test_own_messages_are_ignored():
    assert classify(gmail_message("o", ACCOUNT), ACCOUNT) is None
