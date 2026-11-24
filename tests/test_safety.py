import pytest

from agent.compliance import evaluate_compliance
from agent.config import Settings
from agent.models import Compliance, Suppression, utc_now
from agent.safety import check_send_eligibility
from agent.verify import VerificationResult


def live_settings(**kwargs):
    return Settings(
        **dict(
            {
                "dry_run": False,
                "paused": False,
                "postal_address": "123 Test Street",
                "postal_address_confirmed": True,
            },
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    ("country", "subscriber", "published", "refusal", "relevant", "expected"),
    [
        ("US", "company", False, None, False, True),
        ("US", "sole_trader_or_individual", False, None, False, True),
        ("US", "unknown", False, None, False, False),
        ("GB", "company", False, None, False, True),
        ("UK", "sole_trader_or_individual", False, None, False, False),
        ("DE", "company", True, False, True, False),
        (None, "company", True, False, True, False),
        ("IN", "company", True, False, True, False),
        ("CA", "company", True, False, True, True),
        ("AU", "company", True, False, True, True),
        ("CA", "company", False, False, True, False),
        ("AU", "company", True, True, True, False),
        ("CA", "company", True, None, True, False),
        ("AU", "company", True, False, False, False),
    ],
)
def test_compliance_matrix(country, subscriber, published, refusal, relevant, expected):
    record = Compliance(
        lead_id=1,
        country=country,
        subscriber_type=subscriber,
        contact_source_url="https://example.com/contact",
        reviewed_at=utc_now(),
        reviewed_by="test",
        country_evidence="site address",
        subscriber_evidence="site company",
        conspicuously_published=published,
        no_unsolicited_statement=refusal,
        role_relevant=relevant,
    )
    assert evaluate_compliance(record)[0] == expected


def test_cannot_self_declare_eligible(db, lead):
    record = db.save_compliance(Compliance(lead_id=lead.id, eligible=True, country="US"))
    assert not record.eligible


def test_valid_preflight_is_read_only(db, phase3):
    from agent.personalize import build_message

    lead, _, slots, settings = phase3
    draft = db.create_draft(build_message(db, lead.id, slots, settings))
    approved = db.approve_message(
        draft.id, truthful_identity_confirmed=True, opt_out_confirmed=True
    )
    assert check_send_eligibility(
        db, approved.id, live_settings(postal_address=settings.postal_address)
    ).allowed
    assert db.get_message(approved.id) == approved


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        ({"dry_run": True}, "dry_run"),
        ({"paused": True}, "paused"),
        ({"postal_address": ""}, "postal_address_required"),
        ({"postal_address_confirmed": False}, "postal_address_required"),
        ({"postal_address": "Different address"}, "postal_address_missing_from_message"),
    ],
)
def test_settings_gate(db, approved, overrides, blocker):
    decision = check_send_eligibility(db, approved.id, live_settings(**overrides))
    assert not decision.allowed and blocker in decision.blockers


def test_default_blocks(db, draft):
    decision = check_send_eligibility(db, draft.id, Settings())
    assert {
        "dry_run",
        "approval_required",
        "postal_address_required",
        "compliance_ineligible",
    } <= set(decision.blockers)


def test_suppression_rechecked_after_approval(db, approved, lead):
    db.suppress(Suppression(kind="domain", value=lead.domain, reason="manual"))
    assert "suppressed" in check_send_eligibility(db, approved.id, live_settings()).blockers


def test_dns_retry_blocks_previously_good_address(db, approved, lead):
    db.save_verification(lead.id, VerificationResult(None, "temporary DNS error", True))
    assert "dns_retry_pending" in check_send_eligibility(db, approved.id, live_settings()).blockers


def test_approval_requires_explicit_human_attestations(db, draft):
    with pytest.raises(ValueError):
        db.approve_message(draft.id, truthful_identity_confirmed=False, opt_out_confirmed=True)
