import pytest

from agent.compliance import compliance_is_current, review_compliance
from agent.models import Evidence, Suppression


@pytest.mark.parametrize(
    "country,subscriber",
    [(None, "company"), ("US", "unknown"), ("DE", "company"), ("CA", "company"), ("AU", "company")],
)
def test_unknown_or_unproven_conditions_remain_ineligible(db, phase3, country, subscriber):
    lead, _, _, _ = phase3
    previous = db.get_compliance(lead.id)
    record = previous.model_copy(update={"country": country, "subscriber_type": subscriber})
    reviewed = review_compliance(db, record)
    assert not reviewed.eligible and not compliance_is_current(db, lead.id)
    assert db.connection.execute("SELECT count(*) FROM compliance_reviews").fetchone()[0] == 2


def test_unverified_quote_cannot_replace_human_review(db, phase3):
    lead, _, _, _ = phase3
    previous = db.get_compliance(lead.id)
    record = previous.model_copy(update={"country_evidence": "Fabricated address"})
    with pytest.raises(ValueError):
        review_compliance(db, record)
    assert db.get_compliance(lead.id) == previous


def test_uk_appearance_does_not_prove_registration(db, phase3):
    lead, _, _, _ = phase3
    record = db.get_compliance(lead.id).model_copy(update={"country": "UK"})
    with pytest.raises(ValueError, match="Ltd"):
        review_compliance(db, record)


def test_suppression_survives_explicit_review(db, phase3):
    lead, _, _, _ = phase3
    db.suppress(Suppression(kind="domain", value=lead.domain, reason="manual"))
    reviewed = review_compliance(db, db.get_compliance(lead.id))
    assert not reviewed.eligible and reviewed.reason == "Suppressed recipient"
    assert db.is_suppressed(lead.email, lead.domain)


def test_direct_save_cannot_forge_source_review(db, phase3):
    lead, _, _, _ = phase3
    record = db.get_compliance(lead.id).model_copy(
        update={
            "supporting_evidence": [
                Evidence(claim="Country", quote="Not on site", url="https://willow-art.com/")
            ]
        }
    )
    with pytest.raises(ValueError, match="quotes"):
        db.save_compliance(record)


@pytest.mark.parametrize("country", ["CA", "AU"])
def test_published_email_requires_role_evidence_and_explicit_review(db, phase3, country):
    lead, page, _, _ = phase3
    record = db.get_compliance(lead.id).model_copy(
        update={
            "country": country,
            "conspicuously_published": True,
            "no_unsolicited_statement": False,
            "role_relevant": True,
        }
    )
    with pytest.raises(ValueError, match="Role relevance"):
        review_compliance(db, record)
    record.role_evidence = "An online watercolor course with six lessons."
    record.supporting_evidence.append(
        Evidence(claim="Business role", quote=record.role_evidence, url=page.url)
    )
    assert review_compliance(db, record).eligible


def test_no_automatic_country_classification_or_review(db, phase3):
    lead, _, _, _ = phase3
    old = db.get_compliance(lead.id)
    from agent.qualify import qualify_lead

    qualify_lead(db, lead.id)
    assert db.get_compliance(lead.id) == old
