import json

import httpx
import pytest
from groq import Groq

from agent.models import SourceEvidence, Suppression
from agent.personalize import (
    GroqPersonalizer,
    PersonalizationError,
    build_message,
    personalize_lead,
    render_template,
    revise_draft,
    validate_managed_message,
)
from agent.safety import check_send_eligibility


def generator(settings, payload, *, status=200, finish="stop", inspect=None):
    def handler(request):
        if inspect:
            inspect(json.loads(request.content))
        return httpx.Response(
            status,
            json={
                "id": "test",
                "created": 0,
                "model": "test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish,
                        "message": {"role": "assistant", "content": payload},
                    }
                ],
            },
        )

    client = Groq(
        api_key="fake",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return GroqPersonalizer(settings, client)


def test_mock_sdk_generates_only_draft(db, phase3):
    lead, _, slots, settings = phase3

    def inspect(request):
        assert request["response_format"] == {"type": "json_object"}
        assert request["messages"][0]["role"] == "system"
        assert "UNTRUSTED" in request["messages"][0]["content"]
        assert "tools" not in request
        assert "fake-key" not in request["messages"][1]["content"]

    draft = personalize_lead(
        db, lead.id, settings, generator(settings, slots.model_dump_json(), inspect=inspect)
    )
    assert draft.status == "draft" and draft.requires_manual_review
    assert draft.approved_at is None and draft.gmail_message_id is None
    assert len(draft.body.split()) < 170 and "Hi Ada," in draft.body
    assert draft.version == 1 and len(draft.content_hash) == 64
    assert len(db.message_versions(draft.id)) == 1
    assert not check_send_eligibility(db, draft.id, settings).allowed
    validate_managed_message(db, draft)


@pytest.mark.parametrize("change", ["hook", "name", "question", "wrong_url", "unsupported_claim"])
def test_fabricated_outputs_rejected_without_saving(db, phase3, change):
    lead, _, slots, settings = phase3
    data = slots.model_dump(mode="json")
    if change == "hook":
        data["hook_line"] = "You have thousands of happy students."
        data["evidence"][0]["claim"] = data["hook_line"]
    elif change == "name":
        data["first_name"] = "Bob"
        data["evidence"][-1] = {"claim": "Bob", "quote": "Bob", "url": "https://willow-art.com/"}
    elif change == "question":
        data["q2"] = "Do you offer a money-back guarantee?"
    elif change == "wrong_url":
        data["evidence"][0]["url"] = "https://other.com/"
    else:
        data["evidence"].append(
            {"claim": "Invented", "quote": "Start today.", "url": "https://willow-art.com/"}
        )
    with pytest.raises(ValueError):
        personalize_lead(db, lead.id, settings, generator(settings, json.dumps(data)))
    assert db.connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert db.get_lead(lead.id).notes == "evidence failed"


@pytest.mark.parametrize(
    "raw", ["not json", "[]", "{}", "```json\n{}\n```", '{"q1":"one","q1":"two"}']
)
def test_malformed_json(db, phase3, raw):
    lead, _, _, settings = phase3
    with pytest.raises(PersonalizationError):
        personalize_lead(db, lead.id, settings, generator(settings, raw))
    assert not db.connection.execute("SELECT 1 FROM messages").fetchone()


@pytest.mark.parametrize("status", [401, 429, 500])
def test_api_errors_sanitized_and_no_fallback(db, phase3, status):
    lead, _, _, settings = phase3
    with pytest.raises(PersonalizationError, match="Groq request failed") as exc:
        personalize_lead(db, lead.id, settings, generator(settings, "secret", status=status))
    assert "fake" not in str(exc.value) and "secret" not in str(exc.value)
    assert not db.connection.execute("SELECT 1 FROM messages").fetchone()


def test_truncated_output_is_not_accepted(db, phase3):
    lead, _, slots, settings = phase3
    with pytest.raises(PersonalizationError, match="Incomplete"):
        personalize_lead(
            db, lead.id, settings, generator(settings, slots.model_dump_json(), finish="length")
        )


def test_duplicate_generation_blocked_before_api(db, phase3):
    lead, _, slots, settings = phase3
    db.create_draft(build_message(db, lead.id, slots, settings))
    with pytest.raises(PersonalizationError, match="already exists"):
        personalize_lead(db, lead.id, settings, None)


def test_edits_version_and_invalidate_approval_and_unsupported_edits_cannot_reapprove(db, phase3):
    lead, _, slots, settings = phase3
    draft = db.create_draft(build_message(db, lead.id, slots, settings))
    approved = db.approve_message(
        draft.id, truthful_identity_confirmed=True, opt_out_confirmed=True
    )
    edited = db.edit_message(approved.id, approved.subject, approved.body + "\nThanks.")
    assert edited.version == 2 and edited.content_hash != approved.content_hash
    assert edited.status == "draft" and edited.approved_at is None
    with pytest.raises(PersonalizationError, match="Edited content"):
        db.approve_message(edited.id, truthful_identity_confirmed=True, opt_out_confirmed=True)
    restored = revise_draft(db, edited.id, slots, settings)
    assert restored.version == 3 and restored.content_hash == approved.content_hash
    assert len(db.message_versions(draft.id)) == 3
    db.approve_message(draft.id, truthful_identity_confirmed=True, opt_out_confirmed=True)


def test_rejected_and_cancelled_are_distinct_and_final(db, phase3):
    lead, _, slots, settings = phase3
    draft = db.create_draft(build_message(db, lead.id, slots, settings))
    rejected = db.reject_message(draft.id, "Not appropriate")
    assert rejected.status == "rejected" and rejected.error == "Not appropriate"
    with pytest.raises(ValueError):
        db.approve_message(draft.id, truthful_identity_confirmed=True, opt_out_confirmed=True)
    with pytest.raises(ValueError):
        revise_draft(db, draft.id, slots, settings)
    db.suppress(Suppression(kind="domain", value=lead.domain, reason="opt_out"))
    assert db.get_message(draft.id).status == "rejected"


def test_suppression_rechecked_before_approval(db, phase3):
    lead, _, slots, settings = phase3
    draft = db.create_draft(build_message(db, lead.id, slots, settings))
    db.suppress(Suppression(kind="email", value=lead.email, reason="opt_out"))
    assert db.get_message(draft.id).status == "cancelled"
    with pytest.raises(PersonalizationError, match="Suppressed"):
        personalize_lead(db, lead.id, settings)


def test_source_change_invalidates_qualification_compliance_and_draft(db, phase3):
    lead, page, slots, settings = phase3
    draft = db.create_draft(build_message(db, lead.id, slots, settings))
    db.approve_message(draft.id, truthful_identity_confirmed=True, opt_out_confirmed=True)
    db.save_source(SourceEvidence(lead_id=lead.id, url=page.url, text=page.text + " Updated."))
    assert db.get_message(draft.id).status == "draft"
    with pytest.raises(PersonalizationError, match="Stale"):
        validate_managed_message(db, db.get_message(draft.id))


def test_first_name_falls_back_when_absent(db, phase3):
    lead, _, slots, settings = phase3
    slots.first_name = None
    slots.evidence = slots.evidence[:-1]
    assert build_message(db, lead.id, slots, settings).body.startswith("Hi there,")


@pytest.mark.parametrize("address", ["", "[address]", "{postal_address}", "https://extra-link.com"])
def test_invalid_postal_address_cannot_generate(db, phase3, address):
    lead, _, slots, settings = phase3
    with pytest.raises(ValueError):
        build_message(db, lead.id, slots, settings.model_copy(update={"postal_address": address}))


def test_template_variants_and_word_budget(phase3):
    _, _, slots, settings = phase3
    initial = render_template(slots, "Willow Art School", settings.postal_address, "initial")[1]
    upgrade = render_template(slots, "Willow Art School", settings.postal_address, "upgrade")[1]
    assert "For an assistant upgrade" in upgrade and initial != upgrade
    for body in (initial, upgrade):
        assert "can answer questions like" in body and "instantly" not in body
        assert "Kartik Khandelwal\nAI developer" in body
        assert 'just reply "no thanks"' in body
        assert body.count("https://") == 1
    with pytest.raises(ValueError, match="170"):
        render_template(
            slots.model_copy(update={"hook_line": "word " * 170}),
            "Willow Art School",
            settings.postal_address,
            "initial",
        )


def test_confidence_cannot_approve_or_bypass_evidence(db, phase3):
    lead, _, slots, settings = phase3
    slots.confidence = 1.0
    slots.requires_manual_review = False
    message = build_message(db, lead.id, slots, settings)
    assert message.requires_manual_review and message.status == "draft"
    slots.hook_line = "Invented high confidence fact"
    with pytest.raises(PersonalizationError):
        build_message(db, lead.id, slots, settings)
