from datetime import date

import pytest

from agent.models import Lead, SourceEvidence, Suppression
from agent.qualify import current_qualification, evaluate_qualification, qualify_lead
from agent.scrape import extract_page


def evaluate(html, *, niche="watercolor", email="ada@willow-art.com"):
    url = "https://willow-art.com/"
    page = extract_page(html, url)
    lead = Lead(id=1, domain="willow-art.com", niche=niche, email=email)
    return evaluate_qualification(lead, [page], {url: page.text}, today=date(2026, 11, 20))


def test_full_score_audit_and_persistence(db, phase3):
    lead, _, _, _ = phase3
    result = current_qualification(db, lead.id)
    assert result.score == 100 and result.qualified
    assert [c.points for c in result.criteria.values()] == [25, 25, 20, 15, 15]
    assert all(c.evidence and c.reason for c in result.criteria.values())
    assert db.get_lead(lead.id).score == 100 and db.get_lead(lead.id).stage == "qualified"


@pytest.mark.parametrize(
    "html,expected",
    [
        ("", 0),
        ("<p>watercolor online course</p>", 0),
        (
            (
                "<h1>Watercolor Course</h1><p>An online watercolor course. Enroll now.</p>"
                "<h2>How do I access it?</h2><p>Use your login.</p><p>ada@willow-art.com</p>"
            ),
            60,
        ),
        (
            (
                "<h1>Watercolor Course</h1><p>An online watercolor course. Enroll now.</p>"
                "<p>ada@willow-art.com. Enrollment is open.</p>"
            ),
            55,
        ),
    ],
)
def test_score_threshold_and_missing_signals(html, expected):
    result = evaluate(html)
    assert result.score == expected
    assert not result.qualified  # One FAQ is insufficient for two grounded questions.


def test_unknown_niche_or_format_does_not_earn_course_points():
    assert (
        evaluate("<h1>Watercolor Course</h1><p>Enroll now.</p>").criteria["relevant_course"].points
        == 0
    )
    assert (
        evaluate(
            "<h1>Watercolor Course</h1><p>An online watercolor course. Enroll now.</p>", niche=""
        )
        .criteria["relevant_course"]
        .points
        == 0
    )


def test_closed_course_is_contradictory_not_positive():
    result = evaluate(
        "<h1>Watercolor Course</h1><p>An online watercolor course. Enroll now.</p>"
        "<p>Enrollment is closed. Enrollment is open.</p>"
    )
    assert result.criteria["relevant_course"].points == 0
    assert result.criteria["recent_activity"].points == 0
    assert "course_availability_conflict" in result.blockers


@pytest.mark.parametrize(
    "html,points",
    [
        ("<p>Copyright 2026</p>", 0),
        ("<p>Updated 2025</p>", 0),
        ("<p>Updated 2026</p>", 15),
        ("<p>Enrollment is not open.</p>", 0),
    ],
)
def test_activity_requires_positive_current_signal(html, points):
    assert evaluate(html).criteria["recent_activity"].points == points


def test_contradictory_faq_and_duplicate_questions_not_counted():
    html = "<h2>How long is access?</h2><p>One year.</p>"
    html += "<h2>How long is access?</h2><p>Forever.</p>"
    result = evaluate(html * 5)
    assert result.criteria["five_faqs"].points == 0
    assert result.criteria["support_faqs"].points == 0
    assert "contradictory_faq_answers" in result.blockers


def test_stale_pages_cannot_qualify_and_scores_do_not_override_suppression(db, phase3):
    lead, page, _, _ = phase3
    db.save_source(SourceEvidence(lead_id=lead.id, url=page.url, text="Unavailable"))
    assert current_qualification(db, lead.id) is None
    assert qualify_lead(db, lead.id).score == 0
    db.suppress(Suppression(kind="domain", value=lead.domain, reason="opt_out"))
    result = qualify_lead(db, lead.id)
    assert not result.qualified and "suppressed" in result.blockers
    assert db.is_suppressed(lead.email, lead.domain)


def test_qualification_never_reopens_terminal_lead(db, phase3):
    lead, _, _, _ = phase3
    db.update_stage(lead.id, "replied")
    result = qualify_lead(db, lead.id)
    assert not result.qualified and db.get_lead(lead.id).stage == "replied"
