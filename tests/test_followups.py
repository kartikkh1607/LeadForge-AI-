from datetime import timedelta
from unittest.mock import Mock

import pytest
from test_gmail_sync import ACCOUNT, gmail_message, run, sent_fixture
from test_owner_sender import NOW, owner_test  # noqa: F401 -- pytest fixture

from agent.conversions import ConversionError, latest_stage, record_progress, summary
from agent.followups import FollowupError, draft_followup, due_followups, followup_blockers
from agent.gmail import Outcome
from agent.gmail_sync import sync
from agent.sender import confirmation, enqueue, send_one

LATER = NOW + timedelta(days=6)


def synced(db, transport, inbound=(), when=LATER):
    transport.thread = Mock(
        return_value=[{**gmail_message("gmail-1", ACCOUNT), "labels": ["SENT"]}, *inbound]
    )
    return sync(db, transport, ACCOUNT, clock=lambda: when)


def test_full_followup_flow_is_threaded_and_single(db, owner_test, monkeypatch):  # noqa: F811
    message, lead, transport = sent_fixture(db, owner_test)
    settings = owner_test[1]
    assert followup_blockers(db, lead.id, settings, NOW + timedelta(days=1)) == [
        "followup_wait_not_elapsed",
        "gmail_sync_required",
    ]
    synced(db, transport)
    assert due_followups(db, settings, LATER) == [{"lead_id": lead.id, "due": True, "blockers": []}]
    followup = draft_followup(db, lead.id, settings, LATER)
    assert followup.kind == "followup" and followup.status == "draft"
    assert followup.subject == "Re: " + message.subject
    assert "Just following up" in followup.body and settings.postal_address in followup.body
    assert 'reply "no thanks"' in followup.body
    # Only one follow-up per lead, ever.
    assert due_followups(db, settings, LATER) == []
    with pytest.raises(Exception):  # noqa: B017 -- UNIQUE(lead_id, kind)
        draft_followup(db, lead.id, settings, LATER)

    db.approve_message(followup.id, truthful_identity_confirmed=True, opt_out_confirmed=True)
    # Queue and send preflights re-check wait time and recent sync against the clock.
    monkeypatch.setattr("agent.followups.utc_now", lambda: LATER)
    row = enqueue(db, followup.id, settings)
    transport.send = Mock(return_value=Outcome("sent", "accepted", "gmail-2", "thread-1"))
    result = send_one(db, followup.id, settings, transport, confirmation(row), clock=lambda: LATER)
    assert result.state == "sent"
    raw = transport.send.call_args.args[0]
    assert transport.send.call_args.kwargs == {"thread_id": "thread-1"}
    import base64

    decoded = base64.urlsafe_b64decode(raw).decode()
    initial = db.get_message(message.id)
    assert f"In-Reply-To: {initial.rfc822_message_id}" in decoded


@pytest.mark.parametrize(
    ("snippet", "blocker"),
    [("Yes, interested!", "lead_not_awaiting_reply"), ("No thanks", "suppressed")],
)
def test_reply_or_opt_out_blocks_followup(db, owner_test, snippet, blocker):  # noqa: F811
    _message, lead, transport = sent_fixture(db, owner_test)
    synced(db, transport, [gmail_message("in-1", lead.email, snippet=snippet)])
    blockers = followup_blockers(db, lead.id, owner_test[1], LATER)
    assert blocker in blockers and "inbound_activity_on_thread" in blockers
    with pytest.raises(FollowupError):
        draft_followup(db, lead.id, owner_test[1], LATER)


def test_reply_after_drafting_cancels_pending_followup(db, owner_test):  # noqa: F811
    _message, lead, transport = sent_fixture(db, owner_test)
    synced(db, transport)
    followup = draft_followup(db, lead.id, owner_test[1], LATER)
    synced(db, transport, [gmail_message("in-9", lead.email, snippet="Sure, show me")])
    assert db.get_message(followup.id).status == "cancelled"


def test_bounce_blocks_followup(db, owner_test):  # noqa: F811
    _message, lead, transport = sent_fixture(db, owner_test)
    bounce = gmail_message(
        "in-2",
        "mailer-daemon@googlemail.com",
        "Delivery Status Notification (Failure)",
        "Address not found",
    )
    synced(db, transport, [bounce])
    assert "email_bounced" in followup_blockers(db, lead.id, owner_test[1], LATER)


def test_edited_followup_fails_revalidation(db, owner_test):  # noqa: F811
    _message, lead, transport = sent_fixture(db, owner_test)
    synced(db, transport)
    followup = draft_followup(db, lead.id, owner_test[1], LATER)
    db.edit_message(followup.id, followup.subject, followup.body + "\nBuy now!")
    with pytest.raises(ValueError):
        db.approve_message(followup.id, truthful_identity_confirmed=True, opt_out_confirmed=True)


def test_conversion_tracking_requires_reply_and_feeds_summary(db, owner_test):  # noqa: F811
    _message, lead, transport = sent_fixture(db, owner_test)
    with pytest.raises(ConversionError, match="replied"):
        record_progress(db, lead.id, "interested")
    run(db, transport, [gmail_message("in-3", lead.email, snippet="Love it, build the demo")])
    record_progress(db, lead.id, "interested", now=NOW)
    record_progress(db, lead.id, "demo_sent", "Sent Loom walkthrough", now=NOW)
    with pytest.raises(ConversionError, match="won"):
        record_progress(db, lead.id, "proposal_sent", value_usd=100)
    assert latest_stage(db, lead.id) == "demo_sent"
    record_progress(db, lead.id, "won", value_usd=150, now=NOW + timedelta(days=1))
    assert db.get_lead(lead.id).stage == "won"
    stats = summary(db)
    assert stats["contacted"] == stats["delivered"] == stats["replied"] == 1
    assert stats["reply_rate"] == 1.0 and stats["won"] == 1 and stats["revenue_usd"] == 150.0


def test_pipeline_view_and_cli_summary(db, owner_test, capsys, monkeypatch, tmp_path):  # noqa: F811
    from agent.dashboard import pipeline_view

    _message, _lead, _transport = sent_fixture(db, owner_test)
    view = pipeline_view(db, owner_test[1])
    assert view["summary"]["contacted"] == 1 and view["inbound"] == []
    assert view["followups"][0]["blockers"]

    from agent import pipeline_cli

    path = tmp_path / "cli.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    pipeline_cli.main(["summary"])
    assert '"contacted": 0' in capsys.readouterr().out
