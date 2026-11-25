import sqlite3
from pathlib import Path

import pytest

from agent.db import Database
from agent.models import Lead, SourceEvidence, Suppression
from agent.verify import VerificationResult


def test_migrations_idempotent_and_persistent(tmp_path):
    path = tmp_path / "nested" / "db.sqlite3"
    with Database(path) as db:
        db.migrate()
        lead = db.add_lead(Lead(domain="https://www.example.com/"))
        db.migrate()
        assert db.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 10
        assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with Database(path) as db:
        db.migrate()
        assert db.get_lead(lead.id).domain == "example.com"


def test_failed_migration_rolls_back(db, tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    for original in Path("agent/migrations").glob("*.sql"):
        (directory / original.name).write_text(original.read_text(), encoding="utf-8")
    (directory / "007_bad.sql").write_text("CREATE TABLE transient(id INTEGER);\nINVALID SQL;\n")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(directory)
    assert not db.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='transient'"
    ).fetchone()
    assert db.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 10


def test_changed_migration_rejected(db, tmp_path):
    for original in Path("agent/migrations").glob("*.sql"):
        (tmp_path / original.name).write_text(original.read_text(), encoding="utf-8")
    (tmp_path / "001_foundation.sql").write_text("SELECT 1;\n")
    with pytest.raises(ValueError, match="changed"):
        db.migrate(tmp_path)


def test_phase4_upgrade_preserves_records_and_defaults(tmp_path):
    old = tmp_path / "phase4"
    old.mkdir()
    for original in sorted(Path("agent/migrations").glob("*.sql"))[:5]:
        (old / original.name).write_bytes(original.read_bytes())
    with Database(tmp_path / "upgrade.sqlite3") as db:
        db.migrate(old)
        lead = db.add_lead(Lead(domain="example.com"))
        before = db.get_lead(lead.id)
        db.migrate()
        assert db.get_lead(lead.id) == before
        assert not db.connection.execute("SELECT * FROM outbound_queue").fetchall()
        assert not db.connection.execute("SELECT * FROM gmail_connection").fetchall()
        assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()
        db.migrate()


def test_lead_uniqueness(db, lead):
    with pytest.raises(sqlite3.IntegrityError):
        db.add_lead(Lead(domain="https://www.EXAMPLE.com/"))
    with pytest.raises(sqlite3.IntegrityError):
        db.add_lead(Lead(domain="other.com", email="OWNER@example.com"))
    # Unknown email is NULL, so multiple not-yet-scraped businesses are allowed.
    db.add_lead(Lead(domain="other.com"))
    db.add_lead(Lead(domain="another.com"))


def test_foreign_keys_and_checks(db, lead):
    with pytest.raises(sqlite3.IntegrityError):
        db.save_source(SourceEvidence(lead_id=999, url="https://example.com", text="x"))
    for column, value in [("stage", "drafted"), ("score", -1), ("email_status", "verified")]:
        with pytest.raises(sqlite3.IntegrityError), db.connection:
            db.connection.execute(f"UPDATE leads SET {column}=? WHERE id=?", (value, lead.id))


def test_message_roundtrip_and_unique_kind(db, draft):
    assert db.get_message(draft.id) == draft
    with pytest.raises(sqlite3.IntegrityError):
        db.create_draft(draft.model_copy(update={"id": None}))


def test_evidence_failure_not_saved(db, draft):
    db.connection.execute("DELETE FROM messages")
    db.connection.commit()
    bad = draft.model_copy(update={"hook_evidence": "invented"})
    with pytest.raises(ValueError, match="Evidence failed"):
        db.create_draft(bad)
    assert db.get_lead(draft.lead_id).notes == "evidence failed"
    assert db.connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_edit_revokes_approval(db, approved):
    edited = db.edit_message(approved.id, approved.subject, approved.body + " Thanks.")
    assert edited.status == "draft" and edited.approved_at is None
    assert edited.requires_manual_review and not edited.opt_out_confirmed


def test_source_change_revokes_approval(db, approved):
    db.save_source(
        SourceEvidence(lead_id=approved.lead_id, url=approved.hook_source_url, text="Updated site")
    )
    assert db.get_message(approved.id).status == "draft"


def test_recipient_change_revokes_approval_and_compliance(db, approved):
    with db.connection:
        db.connection.execute(
            "UPDATE leads SET email='new@example.com' WHERE id=?", (approved.lead_id,)
        )
    assert db.get_message(approved.id).status == "draft"
    assert not db.get_compliance(approved.lead_id).eligible


def test_suppression_cancels_and_blocks_drafts(db, approved, lead):
    db.suppress(Suppression(kind="email", value="OWNER@EXAMPLE.COM", reason="opt_out"))
    assert db.is_suppressed("other@example.com", "example.com")
    assert db.is_suppressed("another@sub.example.com", "sub.example.com")
    assert not db.is_suppressed("other@notexample.com", "notexample.com")
    assert db.get_message(approved.id).status == "cancelled"
    with pytest.raises(ValueError, match="Suppressed"):
        db.create_draft(approved.model_copy(update={"status": "draft", "approved_at": None}))
    # Repeated opt-outs are idempotent; they can never be downgraded or removed.
    db.suppress(Suppression(kind="email", value=lead.email, reason="manual"))
    for sql in ["DELETE FROM suppression", "UPDATE suppression SET reason='manual'"]:
        with pytest.raises(sqlite3.IntegrityError), db.connection:
            db.connection.execute(sql)


def test_email_optout_suppresses_business_domain_too(db):
    db.add_lead(Lead(domain="business.com", email="owner@other.com"))
    db.suppress(Suppression(kind="email", value="owner@other.com", reason="opt_out"))
    assert db.is_suppressed("new@business.com", "business.com")


def test_timeout_preserves_status_and_bounce_never_cleared(db, lead):
    db.save_verification(lead.id, VerificationResult(None, "timeout", True))
    assert db.get_lead(lead.id).email_status == "domain_mail_capable"
    db.save_verification(lead.id, VerificationResult("bounced", "permanent bounce"))
    db.save_verification(lead.id, VerificationResult("domain_mail_capable", "MX"))
    assert db.get_lead(lead.id).email_status == "bounced"


def test_reply_cancels_pending_followup(db, draft):
    followup = db.create_draft(draft.model_copy(update={"id": None, "kind": "followup"}))
    db.update_stage(draft.lead_id, "replied")
    assert db.get_message(followup.id).status == "cancelled"
