from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from agent.config import Settings
from agent.db import Database
from agent.gmail import Outcome
from agent.models import SourceEvidence, Suppression
from agent.operations import ActionError, mutation_lock
from agent.personalize import build_message
from agent.sender import confirmation, day_key, enqueue, queue_rows, quota_used, reconcile, send_one
from agent.settings_store import preferences_for, save_preferences

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


@pytest.fixture
def owner_test(db, phase3):
    lead, page, slots, base = phase3
    settings = Settings(
        **{
            **base.model_dump(),
            "paused": False,
            "dry_run": False,
            "postal_address_confirmed": True,
            "gmail_account": "developer@example.com",
            "owner_test_addresses": lead.email,
        }
    )
    message = db.create_draft(build_message(db, lead.id, slots, settings))
    db.approve_message(message.id, truthful_identity_confirmed=True, opt_out_confirmed=True)
    row = enqueue(db, message.id, settings)
    transport = Mock()
    transport.profile.return_value = settings.gmail_account
    transport.send.return_value = Outcome("sent", "accepted", "gmail-1", "thread-1")
    transport.reconcile.return_value = Outcome("uncertain", "reconciliation_no_unique_match")
    return message, settings, row, transport, page


def dispatch(db, fixture, **kwargs):
    message, settings, row, transport, _ = fixture
    return send_one(
        db,
        message.id,
        kwargs.pop("settings", settings),
        transport,
        kwargs.pop("consent", confirmation(row)),
        clock=lambda: NOW,
        **kwargs,
    )


def test_success_and_duplicate_claim(db, owner_test):
    message, settings, row, transport, _ = owner_test
    assert enqueue(db, message.id, settings) == row
    assert dispatch(db, owner_test).state == "sent"
    saved = db.get_message(message.id)
    assert saved.rfc822_message_id == row["rfc_message_id"]
    assert saved.gmail_message_id == "gmail-1" and saved.gmail_thread_id == "thread-1"
    assert saved.sent_at == NOW and db.get_lead(saved.lead_id).stage == "contacted"
    assert queue_rows(db)[0]["outcome"] == "accepted"
    with pytest.raises(ActionError):
        dispatch(db, owner_test)
    transport.send.assert_called_once()


@pytest.mark.parametrize(
    "changes,blocker",
    [
        ({"dry_run": True}, "dry_run"),
        ({"paused": True}, "paused"),
        ({"postal_address": ""}, "postal_address_required"),
        ({"postal_address_confirmed": False}, "postal_address_required"),
        ({"postal_address": "changed"}, "postal_address_missing"),
        ({"owner_test_addresses": ""}, "owner_allowlist"),
        ({"owner_test_addresses": "someone@example.com"}, "owner_allowlist"),
    ],
)
def test_settings_enforced(db, owner_test, changes, blocker):
    with pytest.raises(ActionError, match=blocker):
        dispatch(db, owner_test, settings=owner_test[1].model_copy(update=changes))
    owner_test[3].send.assert_not_called()
    assert queue_rows(db)[0]["state"] == "pending"


def test_confirmation_is_separate_and_exact(db, owner_test):
    with pytest.raises(ActionError, match="confirmation"):
        dispatch(db, owner_test, consent="yes")
    owner_test[3].send.assert_not_called()


@pytest.mark.parametrize(
    "change",
    ["edit", "source", "compliance", "suppression", "hash", "version", "recipient", "approval"],
)
def test_changed_eligibility(db, owner_test, change):
    message, _settings, row, transport, page = owner_test
    if change == "edit":
        db.edit_message(message.id, message.subject, message.body + " Thanks.")
    elif change == "source":
        db.save_source(SourceEvidence(lead_id=message.lead_id, url=page.url, text="Changed"))
    elif change == "compliance":
        with db.connection:
            db.connection.execute("UPDATE compliance SET eligible=0")
    elif change == "suppression":
        db.suppress(Suppression(kind="email", value=row["recipient"], reason="manual"))
    elif change == "recipient":
        with db.connection:
            db.connection.execute("UPDATE leads SET email='other@example.com'")
    else:
        sql = {
            "hash": "UPDATE messages SET content_hash='bad'",
            "version": "UPDATE messages SET version=version+1",
            "approval": "UPDATE messages SET approved_at='2026-11-20T00:00:00+00:00'",
        }[change]
        with db.connection:
            db.connection.execute(sql)
    with pytest.raises(ActionError):
        dispatch(db, owner_test)
    transport.send.assert_not_called()


def test_persisted_pause_wins(db, owner_test):
    values = preferences_for(owner_test[1]).model_dump()
    values["paused"] = True
    save_preferences(db, owner_test[1], values)
    with pytest.raises(ActionError, match="paused"):
        dispatch(db, owner_test)


def test_lock_and_concurrent_access(db, owner_test):
    path = db.connection.execute("PRAGMA database_list").fetchone()[2]

    def attempt():
        with Database(path) as other:
            try:
                dispatch(other, owner_test)
                return "sent"
            except ActionError:
                return "blocked"

    with mutation_lock(db), ThreadPoolExecutor(2) as pool:
        assert list(pool.map(lambda _: attempt(), range(2))) == ["blocked", "blocked"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["blocked", "sent"]
    owner_test[3].send.assert_called_once()


def test_duplicate_enqueue_concurrently(db, owner_test):
    path = db.connection.execute("PRAGMA database_list").fetchone()[2]

    def attempt(_):
        with Database(path) as other:
            try:
                return enqueue(other, owner_test[0].id, owner_test[1])["rfc_message_id"]
            except ActionError:
                return None

    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(attempt, range(2)))
    assert set(ids) <= {None, owner_test[2]["rfc_message_id"]}
    assert len(queue_rows(db)) == 1


@pytest.mark.parametrize("state", ["uncertain", "rejected"])
def test_failure_never_retries(db, owner_test, state):
    owner_test[3].send.return_value = Outcome(state, "mock_failure")
    dispatch(db, owner_test)
    with pytest.raises(ActionError):
        dispatch(db, owner_test)
    owner_test[3].send.assert_called_once()
    assert quota_used(db, NOW) == 1


def test_timeout_and_reconciliation(db, owner_test):
    message, settings, _row, transport, _ = owner_test
    transport.send.side_effect = TimeoutError("secret")
    assert dispatch(db, owner_test).state == "uncertain"
    reconcile(db, transport, settings.gmail_account)
    assert queue_rows(db)[0]["state"] == "uncertain"
    assert quota_used(db, NOW + timedelta(days=1)) == 1
    transport.reconcile.return_value = Outcome(
        "sent", "reconciled", "found", "thread", NOW.isoformat()
    )
    reconcile(db, transport, settings.gmail_account)
    assert db.get_message(message.id).gmail_message_id == "found"
    assert quota_used(db, NOW) == 1
    transport.send.assert_called_once()


def test_restart_recovery(db, owner_test):
    message, settings, _row, transport, _ = owner_test
    transport.send.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        dispatch(db, owner_test)
    assert queue_rows(db)[0]["state"] == "sending"
    assert db.get_message(message.id).status == "sending"
    path = db.connection.execute("PRAGMA database_list").fetchone()[2]
    transport.reconcile.return_value = Outcome(
        "sent", "reconciled", "found", "thread", NOW.isoformat()
    )
    with Database(path) as restarted:
        restarted.migrate()
        reconcile(restarted, transport, settings.gmail_account)
        assert restarted.get_message(message.id).status == "sent"
        with pytest.raises(ActionError):
            dispatch(restarted, owner_test)
    transport.send.assert_called_once()


def test_quota_boundaries_and_followups(db, owner_test):
    message = owner_test[0]
    before = datetime(2026, 11, 22, 18, 29, 59, tzinfo=UTC)
    after = before + timedelta(seconds=1)
    assert day_key(before) == "2026-11-22" and day_key(after) == "2026-11-23"
    # Historical follow-up uses the same quota pool, without enabling follow-up delivery.
    with db.connection:
        db.connection.execute(
            "INSERT INTO messages(lead_id,kind,subject,body,status,approved_at,rfc822_message_id,"
            "gmail_message_id,sent_at,created_at) VALUES (?,'followup','historical','body','sent',?,?,?,?,?)",
            (
                message.lead_id,
                before.isoformat(),
                "<historical@test>",
                "old",
                before.isoformat(),
                before.isoformat(),
            ),
        )
    assert quota_used(db, before) == 1 and quota_used(db, after) == 0
    with pytest.raises(ActionError, match="daily_quota"):
        dispatch(db, owner_test, settings=owner_test[1].model_copy(update={"daily_limit": 1}))
    owner_test[3].send.assert_not_called()


def test_followup_blocked_and_defaults(db, owner_test):
    assert Settings().dry_run and Settings().paused
    with db.connection:
        db.connection.execute("UPDATE messages SET kind='followup'")
    with pytest.raises(ActionError, match="followup_not_implemented"):
        dispatch(db, owner_test)


def test_migration_integrity(db, owner_test):
    db.migrate()
    assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()


def test_os_lock_across_processes(db):
    import subprocess
    import sys

    path = db.connection.execute("PRAGMA database_list").fetchone()[2]
    code = (
        "import sys\nfrom agent.db import Database\n"
        "from agent.operations import mutation_lock, ActionError\n"
        "with Database(sys.argv[1]) as db:\n"
        " try:\n  with mutation_lock(db): pass\n"
        " except ActionError: sys.exit(7)\n"
    )
    with mutation_lock(db):
        result = subprocess.run(
            [sys.executable, "-c", code, path], capture_output=True, timeout=20, check=False
        )
    assert result.returncode == 7, result.stderr
    result = subprocess.run(
        [sys.executable, "-c", code, path], capture_output=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr


def test_change_during_profile_check_is_caught(db, owner_test):
    def profile():
        with db.connection:
            db.connection.execute("UPDATE compliance SET eligible=0")
        return owner_test[1].gmail_account

    owner_test[3].profile.side_effect = profile
    with pytest.raises(ActionError, match="compliance_ineligible"):
        dispatch(db, owner_test)
    owner_test[3].send.assert_not_called()


def test_success_then_local_record_failure_recovers(db, owner_test, monkeypatch):
    from agent import sender

    original = sender._record
    monkeypatch.setattr(sender, "_record", Mock(side_effect=RuntimeError("disk failure")))
    with pytest.raises(RuntimeError):
        dispatch(db, owner_test)
    assert queue_rows(db)[0]["state"] == "sending"
    monkeypatch.setattr(sender, "_record", original)
    owner_test[3].reconcile.return_value = Outcome(
        "sent", "reconciled", "gmail-1", "thread-1", NOW.isoformat()
    )
    reconcile(db, owner_test[3], owner_test[1].gmail_account)
    assert queue_rows(db)[0]["state"] == "sent"
    owner_test[3].send.assert_called_once()


def test_reapproval_requires_explicit_queue_refresh(db, owner_test):
    message, settings, row, _, _ = owner_test
    db.edit_message(message.id, message.subject, message.body)
    db.approve_message(message.id, truthful_identity_confirmed=True, opt_out_confirmed=True)
    with pytest.raises(ActionError, match="snapshot"):
        dispatch(db, owner_test)
    fresh = enqueue(db, message.id, settings)
    assert fresh["rfc_message_id"] != row["rfc_message_id"]
    with pytest.raises(ActionError, match="confirmation"):
        dispatch(db, owner_test)


def test_dashboard_diagnostics_are_read_only(db, owner_test):
    from agent.dashboard import Dashboard

    before = db.connection.total_changes
    view = Dashboard(db, owner_test[1]).send_preview()
    assert view["queue"][0]["state"] == "pending"
    assert not view["messages"][0]["sendable"]
    assert view["owner_diagnostics"][0]["blockers"] == ""
    assert db.connection.total_changes == before


def test_cli_defaults_and_noninteractive_live_refused(db, owner_test, monkeypatch, capsys):
    from pathlib import Path

    import agent.send_worker as cli

    settings = owner_test[1].model_copy(
        update={"database_path": Path(db.connection.execute("PRAGMA database_list").fetchone()[2])}
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    vault = Mock(side_effect=AssertionError("No credentials should be accessed"))
    monkeypatch.setattr(cli, "CredentialStore", vault)
    monkeypatch.setattr("sys.argv", ["worker"])
    cli.main()
    assert "quota=" in capsys.readouterr().out
    monkeypatch.setattr(
        "sys.argv", ["worker", "send-owner-test", "--message-id", str(owner_test[0].id)]
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit):
        cli.main()
    vault.assert_not_called()


def test_persistent_delay_blocks_restart(db, owner_test):
    with db.connection:
        db.connection.execute(
            "UPDATE outbound_queue SET next_send_at=?", ((NOW + timedelta(seconds=90)).isoformat(),)
        )
    with pytest.raises(ActionError, match="send_delay"):
        dispatch(db, owner_test)
    owner_test[3].send.assert_not_called()


def test_midnight_quota_rechecked_after_claim(db, owner_test):
    message, settings, row, transport, _ = owner_test
    before = datetime(2026, 11, 22, 18, 29, 59, tzinfo=UTC)
    after = before + timedelta(seconds=1)
    with db.connection:
        db.connection.execute(
            "INSERT INTO messages(lead_id,kind,subject,body,status,approved_at,rfc822_message_id,"
            "gmail_message_id,sent_at,created_at) VALUES (?,'followup','history','body','sent',?,?,?,?,?)",
            (
                message.lead_id,
                after.isoformat(),
                "<history@test>",
                "history",
                after.isoformat(),
                after.isoformat(),
            ),
        )
    times = iter([before, after, after])
    result = send_one(
        db,
        message.id,
        settings.model_copy(update={"daily_limit": 1}),
        transport,
        confirmation(row),
        clock=lambda: next(times),
    )
    assert result.state == "rejected"
    transport.send.assert_not_called()


def test_authenticated_identity_mismatch(db, owner_test):
    owner_test[3].profile.return_value = "someone-else@example.com"
    with pytest.raises(ActionError, match="account mismatch"):
        dispatch(db, owner_test)
    owner_test[3].send.assert_not_called()


def test_delay_starts_after_slow_send_completes(db, owner_test):
    message, settings, row, transport, _ = owner_test
    now = [NOW]

    def slow_send(raw):
        now[0] += timedelta(seconds=200)
        return Outcome("sent", "accepted", "slow", "thread")

    transport.send.side_effect = slow_send
    send_one(db, message.id, settings, transport, confirmation(row), clock=lambda: now[0])
    next_time = datetime.fromisoformat(queue_rows(db)[0]["next_send_at"])
    assert 60 <= (next_time - now[0]).total_seconds() <= 180


def test_readiness_flags_stale_pending_approval(db, owner_test):
    from agent.readiness import readiness

    message, settings, _, _, _ = owner_test
    assert readiness(db, settings)["checks"]["queue_pending_blocked"] == 0
    db.edit_message(message.id, message.subject, message.body)
    assert readiness(db, settings)["checks"]["queue_pending_blocked"] == 1


def test_owner_test_label_requires_fresh_approval(db, owner_test):
    from agent.personalize import validate_managed_message
    from agent.sender import prepare_owner_test

    message, settings, row, transport, _ = owner_test
    marked = prepare_owner_test(db, message.id, settings)
    assert marked.subject.startswith("Owner-only test: ")
    assert marked.body.startswith("This is an owner-only delivery test.")
    assert marked.status == "draft" and not marked.approved_at
    assert marked.version == message.version + 1
    assert marked.evidence == message.evidence
    validate_managed_message(db, marked)
    assert prepare_owner_test(db, message.id, settings) == marked
    with pytest.raises(ActionError, match="approval"):
        dispatch(db, owner_test)
    db.approve_message(message.id, truthful_identity_confirmed=True, opt_out_confirmed=True)
    fresh = enqueue(db, message.id, settings)
    assert fresh["rfc_message_id"] != row["rfc_message_id"]
    result = send_one(db, message.id, settings, transport, confirmation(fresh), clock=lambda: NOW)
    assert result.state == "sent"  # Mock transport only.


def test_owner_label_does_not_bypass_allowlist_or_evidence(db, owner_test):
    from agent.sender import prepare_owner_test

    message, settings, _, _, _ = owner_test
    with pytest.raises(ActionError, match="allowlist"):
        prepare_owner_test(db, message.id, settings.model_copy(update={"owner_test_addresses": ""}))
    db.edit_message(message.id, message.subject, message.body + " Invented claim.")
    with pytest.raises(ValueError):
        prepare_owner_test(db, message.id, settings)


@pytest.mark.parametrize("oauth_failure", [False, True])
def test_cli_restores_pause_and_dry_run(db, owner_test, monkeypatch, oauth_failure):
    from pathlib import Path

    from agent import send_worker as cli
    from agent.gmail import GmailError
    from agent.settings_store import effective_settings

    message, settings, row, transport, _ = owner_test
    settings = settings.model_copy(
        update={"database_path": Path(db.connection.execute("PRAGMA database_list").fetchone()[2])}
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["worker", "send-owner-test", "--message-id", str(message.id)])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: confirmation(row))
    monkeypatch.setattr(cli, "CredentialStore", Mock())
    monkeypatch.setattr(cli, "GmailTransport", Mock(return_value=transport))
    if oauth_failure:
        monkeypatch.setattr(
            cli, "load_credentials", Mock(side_effect=GmailError("Reconnect required"))
        )
        with pytest.raises(SystemExit):
            cli.main()
        transport.send.assert_not_called()
    else:
        monkeypatch.setattr(cli, "load_credentials", Mock(return_value=Mock(token="fake")))
        cli.main()
        transport.send.assert_called_once()
    restored = effective_settings(db, settings)
    assert restored.paused and restored.dry_run
