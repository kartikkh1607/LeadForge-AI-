from datetime import date
from pathlib import Path

import pytest

from agent.models import Lead
from agent.qualify import evaluate_qualification, evidence_states
from agent.scrape import extract_page


def page(name, url="https://artist.com/course"):
    return extract_page(Path(f"tests/fixtures/{name}.html").read_text(encoding="utf-8"), url)


def qualification(p):
    return evaluate_qualification(
        Lead(id=1, domain="artist.com", niche="watercolor"),
        [p],
        {p.url: p.text},
        today=date(2026, 11, 22),
    )


@pytest.mark.parametrize("name", ["offer_split", "offer_cards", "offer_hosted", "offer_schema"])
def test_direct_course_offers_in_distinct_layouts(name):
    p = page(name)
    q = qualification(p)
    assert q.criteria["relevant_course"].points == 25
    assert q.relevant_course_business and not q.qualified
    for offer in p.courses:
        assert offer.value in p.text
        assert all(span in p.text for span in offer.supporting_text)
        assert offer.source_url == p.url and offer.method


def test_card_links_do_not_merge_different_products():
    p = page("offer_cards")
    watercolor = next(o for o in p.courses if o.value == "Watercolor Roses Course")
    oil = next(o for o in p.courses if o.value == "Oil Painting Course")
    assert watercolor.landing_url == "https://artist.com/courses/watercolor"
    assert watercolor.enrollment_url == "https://artist.com/enroll-roses"
    assert "Oil Painting" not in " ".join(watercolor.supporting_text)
    assert oil.evidence_classification != "online_offer"


@pytest.mark.parametrize("name", ["offer_javascript", "offer_misleading"])
def test_no_invisible_or_neighboring_product_evidence(name):
    p = page(name)
    assert qualification(p).criteria["relevant_course"].points == 0


def test_schema_only_price_cannot_confirm_visible_course():
    p = extract_page(
        '<script type="application/ld+json">{"@type":"Course","name":"Watercolor Course","offers":{"price":"50"}}</script><h1>Watercolor Course</h1><p>Online watercolor lessons.</p>',
        "https://artist.com/course",
    )
    assert qualification(p).relevant_course_business
    assert qualification(p).criteria["relevant_course"].points == 0


def test_faq_preserves_paragraphs_lists_and_nested_accordion_boundaries():
    p = page("faq_nested")
    assert [(f.question, f.answer) for f in p.faqs] == [
        (
            "What does the course include?",
            "You receive: Five video lessons. Practice sheets. Access lasts one year.",
        ),
        (
            "How do I get help?",
            "Email the tutor. Include your lesson number. Replies arrive on weekdays.",
        ),
        ("Are downloads included?", "No."),
    ]
    assert all(f.source_url == p.url and f.answer in p.text for f in p.faqs)


def test_evidence_states_do_not_imply_compliance(db, phase3):
    lead, _, _, _ = phase3
    with db.connection:
        db.connection.execute("DELETE FROM compliance WHERE lead_id=?", (lead.id,))
    states = evidence_states(db, lead.id)
    assert states["relevant_course_business"] and states["confirmed_course_offering"]
    assert states["contactable_business"] and states["evidence_ready_personalization"]
    assert not states["compliance_approved_outreach"]


def test_source_tampering_invalidates_assembled_offer():
    p = page("offer_split")
    p.courses[0].supporting_text.append("Invented course content")
    assert qualification(p).criteria["relevant_course"].points == 0


def test_existing_audit_never_searches_or_copies_approvals(db, tmp_path):
    from test_safe_http import fetcher_for, response

    from agent.config import Settings
    from agent.models import Suppression
    from scripts.audit_existing import audit

    db.add_lead(Lead(domain="artist.com", website="https://artist.com/course", niche="watercolor"))
    db.add_lead(Lead(domain="blocked.com", website="https://blocked.com/", niche="watercolor"))
    db.suppress(Suppression(kind="domain", value="blocked.com", reason="opt_out"))

    def factory(**kwargs):
        def handler(request):
            assert request.headers["host"] == "artist.com"
            if request.url.path == "/robots.txt":
                return response(body="User-agent: *\nAllow: /", content_type="text/plain")
            return response(body=Path("tests/fixtures/offer_split.html").read_text())

        return fetcher_for(handler, **kwargs)[0]

    source = Path(db.connection.execute("PRAGMA database_list").fetchone()[2])
    destination = tmp_path / "audit.sqlite3"
    report = audit(source, destination, Settings(), fetcher_factory=factory)
    assert report["search_calls"] == report["groq_calls"] == report["drafts"] == 0
    assert len(report["candidates"]) == 2
    assert report["candidates"][1]["outcome"] == "suppressed_or_lifecycle_blocked"
    assert report["candidates"][0]["states"]["confirmed_course_offering"]
    assert not report["candidates"][0]["states"]["compliance_approved_outreach"]
    with pytest.raises(ValueError, match="must be new"):
        audit(source, destination, Settings(), fetcher_factory=factory)
