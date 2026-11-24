import json

import pytest
from test_personalize import generator

from agent.config import Settings
from agent.dashboard import Dashboard, selected_slots
from agent.db import Database
from agent.models import SourceEvidence
from agent.operations import ActionError
from agent.personalize import build_message
from agent.settings_store import effective_settings, preferences_for, save_preferences


def test_evidence_inspection_is_read_only(db, phase3):
    lead, page, slots, settings = phase3
    service = Dashboard(db, settings)
    before = db.connection.total_changes
    view = service.inspect(lead.id)
    assert view["sources"][page.url] == page.text and view["eligible"]
    assert view["qualification"].score == 100
    context = view["slot_choices"]
    selected = selected_slots(context, 0, 0, 1, 1)
    assert selected["q1"] == slots.q1 and selected["first_name"] == "Ada"
    assert db.connection.total_changes == before


def test_generation_submission_replay_and_approval_replay(db, phase3):
    lead, _, slots, settings = phase3
    service = Dashboard(db, settings)
    token = service.review_token(lead.id)
    message_id = service.generate(
        lead.id, token, "generate", generator(settings, slots.model_dump_json())
    )
    assert service.generate(lead.id, token, "generate") == message_id
    assert len(service.messages()) == 1
    token = service.review_token(lead.id)
    service.approve(message_id, token, "approve", identity_confirmed=True, opt_out_confirmed=True)
    original = db.get_message(message_id)
    service.approve(message_id, token, "approve", identity_confirmed=True, opt_out_confirmed=True)
    assert db.get_message(message_id) == original
    service.edit(
        message_id,
        service.review_token(lead.id),
        "edit",
        subject=original.subject,
        body=original.body + " Thanks.",
    )
    service.approve(message_id, token, "approve", identity_confirmed=True, opt_out_confirmed=True)
    assert db.get_message(message_id).status == "draft"  # Old submission cannot reapprove an edit.
    with pytest.raises(ActionError):
        service.approve(
            message_id,
            service.review_token(lead.id),
            "new-approve",
            identity_confirmed=True,
            opt_out_confirmed=True,
        )


def test_stale_form_cannot_overwrite_edit(db, phase3):
    lead, _, slots, settings = phase3
    message = db.create_draft(build_message(db, lead.id, slots, settings))
    service = Dashboard(db, settings)
    token = service.review_token(lead.id)
    service.edit(
        message.id, token, "first", subject=message.subject, body=message.body + " Thanks."
    )
    with pytest.raises(ActionError, match="changed"):
        service.edit(message.id, token, "stale", subject=message.subject, body=message.body)
    assert db.get_message(message.id).version == 2


def test_grounded_edit_invalidates_approval_then_can_reapprove(db, phase3):
    lead, _, slots, settings = phase3
    message = db.create_draft(build_message(db, lead.id, slots, settings))
    service = Dashboard(db, settings)
    service.approve(
        message.id,
        service.review_token(lead.id),
        "a",
        identity_confirmed=True,
        opt_out_confirmed=True,
    )
    context = service.inspect(lead.id)["slot_choices"]
    service.edit(
        message.id, service.review_token(lead.id), "e", slots=selected_slots(context, 1, 0, 1, 0)
    )
    assert db.get_message(message.id).status == "draft"
    assert service.validation(message.id) == "Valid grounded draft"
    service.approve(
        message.id,
        service.review_token(lead.id),
        "a2",
        identity_confirmed=True,
        opt_out_confirmed=True,
    )
    assert db.get_message(message.id).status == "approved"


def test_compliance_change_blocks_approval_and_preserves_review(db, phase3):
    lead, _, slots, settings = phase3
    message = db.create_draft(build_message(db, lead.id, slots, settings))
    service = Dashboard(db, settings)
    values = db.get_compliance(lead.id).model_dump(mode="json", exclude={"lead_id", "reviewed_at"})
    values["subscriber_type"] = "unknown"
    token = service.review_token(lead.id)
    service.compliance_review(lead.id, token, "review", values)
    service.compliance_review(lead.id, token, "review", values)
    assert not service.inspect(lead.id)["eligible"]
    assert db.connection.execute("SELECT count(*) FROM compliance_reviews").fetchone()[0] == 2
    with pytest.raises(ActionError):
        service.approve(
            message.id,
            service.review_token(lead.id),
            "a",
            identity_confirmed=True,
            opt_out_confirmed=True,
        )


def test_suppression_cancels_and_preview_never_sendable(db, phase3):
    lead, _, slots, settings = phase3
    message = db.create_draft(build_message(db, lead.id, slots, settings))
    service = Dashboard(
        db,
        settings.model_copy(
            update={"dry_run": False, "paused": False, "postal_address_confirmed": True}
        ),
    )
    service.approve(
        message.id,
        service.review_token(lead.id),
        "a",
        identity_confirmed=True,
        opt_out_confirmed=True,
    )
    before = db.connection.total_changes
    preview = service.send_preview()
    assert db.connection.total_changes == before
    assert not preview["messages"][0]["sendable"]
    assert preview["messages"][0]["blockers"] == "sending_not_implemented"
    service.suppress(lead.id, service.review_token(lead.id), "s", "email", "opt_out")
    assert db.get_message(message.id).status == "cancelled"
    assert service.send_preview()["messages"][0]["suppressed"]


def test_settings_whitelist_persistence_and_address_confirmation(db, phase3):
    lead, _, slots, settings = phase3
    message = db.create_draft(build_message(db, lead.id, slots, settings))
    db.approve_message(message.id, truthful_identity_confirmed=True, opt_out_confirmed=True)
    values = preferences_for(settings).model_dump()
    with pytest.raises(ValueError):
        save_preferences(db, settings, {**values, "groq_api_key": "must-not-persist"})
    values.update(
        postal_address="456 Different Street", postal_address_confirmed=True, paused=False
    )
    updated = save_preferences(db, settings, values)
    assert not updated.postal_address_confirmed
    assert db.get_message(message.id).status == "draft"
    updated = save_preferences(db, settings, values)
    assert updated.postal_address_confirmed and not updated.paused
    path = db.connection.execute("PRAGMA database_list").fetchone()[2]
    with Database(path) as other:
        assert effective_settings(other, settings) == updated
    raw = db.connection.execute("SELECT values_json FROM dashboard_settings").fetchone()[0]
    assert "fake-key" not in raw and "api_key" not in raw
    assert Settings().dry_run and Settings().paused
    assert json.loads(raw)["dry_run"]


def test_rejection_and_stale_evidence(db, phase3):
    lead, page, slots, settings = phase3
    message = db.create_draft(build_message(db, lead.id, slots, settings))
    service = Dashboard(db, settings)
    old_token = service.review_token(lead.id)
    db.save_source(SourceEvidence(lead_id=lead.id, url=page.url, text=page.text + " Changed."))
    with pytest.raises(ActionError, match="changed"):
        service.approve(message.id, old_token, "a", identity_confirmed=True, opt_out_confirmed=True)
    service.reject(message.id, service.review_token(lead.id), "reject", "Not suitable")
    assert db.get_message(message.id).status == "rejected"


def test_tracker_filter_and_sort(db, phase3):
    from agent.models import Lead

    _, _, _, settings = phase3
    db.add_lead(Lead(domain="other.com", score=10, stage="skipped"))
    service = Dashboard(db, settings)
    assert len(service.leads(minimum_score=60)) == 1
    assert service.leads(stage="skipped")[0]["domain"] == "other.com"
    assert service.leads(sort="score", descending=False)[0]["score"] == 10


def test_duplicate_settings_cannot_confirm_new_address(db):
    from agent.operations import fingerprint

    base = Settings()
    values = preferences_for(base).model_dump()
    token = fingerprint(values)
    values.update(postal_address="456 Test Street", postal_address_confirmed=True)
    assert not save_preferences(
        db, base, values, submission_key="same", expected=token
    ).postal_address_confirmed
    assert not save_preferences(
        db, base, values, submission_key="same", expected=token
    ).postal_address_confirmed
    with pytest.raises(ActionError, match="Settings changed"):
        save_preferences(db, base, values, submission_key="stale", expected=token)


def test_dashboard_offline_smoke(db):
    from scripts.dashboard_smoke import seed_demo

    result = seed_demo(db)
    assert result["score"] == 100 and result["status"] == "draft"
    assert result["network_requests"] == 0
