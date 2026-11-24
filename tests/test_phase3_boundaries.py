from pathlib import Path

import pytest

from agent.compliance import review_compliance
from agent.db import Database
from agent.models import SourceEvidence
from agent.personalize import PersonalizationError, build_message
from agent.qualify import qualify_lead
from agent.safety import check_send_eligibility


def test_legacy_draft_cannot_bypass_grounding(db, draft):
    db.edit_message(draft.id, draft.subject, draft.body + " You have 500 clients.")
    with pytest.raises(PersonalizationError, match="envelope required"):
        db.approve_message(draft.id, truthful_identity_confirmed=True, opt_out_confirmed=True)
    assert db.get_message(draft.id).status == "draft"


def test_legacy_approval_never_passes_preflight(db, approved):
    from test_safety import live_settings

    decision = check_send_eligibility(db, approved.id, live_settings())
    assert {"invalid_draft", "compliance_ineligible"} <= set(decision.blockers)


def test_legacy_compliance_cannot_be_reused_without_sources(db, phase3):
    lead, _, _, _ = phase3
    record = db.get_compliance(lead.id).model_copy(
        update={"source_hash": "", "supporting_evidence": []}
    )
    assert not db.save_compliance(record).eligible


@pytest.mark.parametrize("country", [" UK ", " ca ", " AU "])
def test_whitespace_cannot_bypass_country_source_rules(db, phase3, country):
    lead, _, _, _ = phase3
    record = db.get_compliance(lead.id).model_copy(
        update={"country": country, "role_relevant": True}
    )
    with pytest.raises(ValueError):
        review_compliance(db, record)


def test_unmatched_source_cannot_earn_activity_points(db, phase3):
    lead, page, _, _ = phase3
    db.save_source(SourceEvidence(lead_id=lead.id, url=page.url, text="Updated 2026."))
    assert qualify_lead(db, lead.id).score == 0


def test_missing_information_is_not_delegated_to_model(db, phase3):
    lead, _, slots, settings = phase3
    slots.first_name = None
    slots.evidence = slots.evidence[:-1]
    slots.missing_information = []
    message = build_message(db, lead.id, slots, settings)
    assert len(message.missing_information) == 2
    assert message.requires_manual_review


def test_managed_initial_cannot_be_relabelled_followup(db, phase3):
    lead, _, slots, settings = phase3
    message = build_message(db, lead.id, slots, settings)
    message.kind = "followup"
    with pytest.raises(PersonalizationError, match="Follow-up"):
        db.create_draft(message)


def test_upgrade_revokes_legacy_approval_preserving_content(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    for number in ("001_foundation.sql", "002_discovery.sql", "003_personalization.sql"):
        (old / number).write_bytes((Path("agent/migrations") / number).read_bytes())
    with Database(tmp_path / "legacy.sqlite3") as db:
        db.migrate(old)
        with db.connection:
            db.connection.execute(
                "INSERT INTO leads(domain,created_at) VALUES ('example.com','2026-11-20')"
            )
            db.connection.execute(
                "INSERT INTO messages(lead_id,kind,subject,body,status,approved_at,"
                "requires_manual_review,created_at) VALUES "
                "(1,'initial','Keep subject','Keep body','approved','2026-11-20',0,'2026-11-20')"
            )
        db.migrate()

        message = db.get_message(1)
        assert message.body == "Keep body" and message.subject == "Keep subject"
        assert message.status == "draft" and message.approved_at is None
        assert len(db.message_versions(1)) == 1
        assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.migrate()


def test_offline_phase3_smoke(capsys):
    from scripts.personalize_smoke import main

    main()
    output = capsys.readouterr().out
    assert output.count('"status": "draft"') == 3
    assert '"fixture_variant": "upgrade"' in output
