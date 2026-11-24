import socket
from pathlib import Path

import pytest

from agent.db import Database
from agent.models import Compliance, Evidence, Lead, Message, SourceEvidence, utc_now


@pytest.fixture
def phase3(db):
    from agent.compliance import review_compliance
    from agent.config import Settings
    from agent.personalize import Slots
    from agent.qualify import qualify_lead
    from agent.scrape import extract_page
    from agent.verify import VerificationResult

    url = "https://willow-art.com/"
    page = extract_page(Path("tests/fixtures/qualified_creator.html").read_text(), url)
    lead = db.add_lead(Lead(domain="willow-art.com", niche="watercolor", website=url))
    db.save_crawl_page(lead.id, url, page=page)
    db.apply_scraped_facts(lead.id, [page])
    db.save_verification(lead.id, VerificationResult("domain_mail_capable", "mock MX"))
    qualify_lead(db, lead.id)
    review_compliance(
        db,
        Compliance(
            lead_id=lead.id,
            country="US",
            subscriber_type="company",
            contact_source_url=url,
            reviewed_at=utc_now(),
            reviewed_by="Fixture reviewer",
            country_evidence="United States",
            subscriber_evidence="Willow Art School LLC",
            supporting_evidence=[
                Evidence(claim=q, quote=q, url=url)
                for q in ["United States", "Willow Art School LLC"]
            ],
        ),
    )
    claims = ["Start today.", "When can I start?", "How long is access?", "Ada"]
    slots = Slots(
        first_name="Ada",
        hook_line=claims[0],
        q1=claims[1],
        q2=claims[2],
        evidence=[Evidence(claim=q, quote=q, url=url) for q in claims],
    )
    settings = Settings(postal_address="123 Example Street, Test City", groq_api_key="fake-key")
    return db.get_lead(lead.id), page, slots, settings


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket.socket, "sendto", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "test.sqlite3") as database:
        database.migrate()
        yield database


@pytest.fixture
def lead(db):
    return db.add_lead(
        Lead(
            domain="EXAMPLE.com",
            email="owner@example.com",
            stage="qualified",
            score=75,
            email_status="domain_mail_capable",
        )
    )


@pytest.fixture
def draft(db, lead):
    url = "https://example.com/faq"
    hook = "Your course includes lifetime access."
    questions = ["How do I log in?", "When does enrollment open?"]
    text = "\n".join([hook, *questions])
    db.save_source(SourceEvidence(lead_id=lead.id, url=url, text=text))
    return db.create_draft(
        Message(
            lead_id=lead.id,
            subject="A quick idea for your course",
            body=f"Hi there, {text}\nKartik Khandelwal\n123 Test Street\nReply no thanks to opt out.",
            evidence=[Evidence(claim=s, quote=s, url=url) for s in [hook, *questions]],
            hook_line=hook,
            hook_evidence=hook,
            hook_source_url=url,
            example_questions=questions,
        )
    )


@pytest.fixture
def approved(db, draft, lead):
    db.save_compliance(
        Compliance(
            lead_id=lead.id,
            country="US",
            subscriber_type="company",
            contact_source_url="https://example.com/contact",
            reviewed_at=utc_now(),
            reviewed_by="test reviewer",
            country_evidence="US address on site",
            subscriber_evidence="Company on site",
        )
    )
    # Simulate an approval persisted by Phase 1. Phase 3 may read/revoke it,
    # but must never approve or authorize sending this legacy draft again.
    with db.connection:
        db.connection.execute(
            "UPDATE messages SET status='approved',approved_at=?,requires_manual_review=0,"
            "truthful_identity_confirmed=1,opt_out_confirmed=1 WHERE id=?",
            (utc_now().isoformat(), draft.id),
        )
    return db.get_message(draft.id)
