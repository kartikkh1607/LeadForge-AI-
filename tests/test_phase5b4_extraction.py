"""Phase 5B.4 regression fixtures, modelled on the 5B.3 live-audit false negatives."""

from datetime import date
from pathlib import Path

import pytest

from agent.models import Lead
from agent.qualify import evaluate_qualification
from agent.scrape import deobfuscate_email, extract_page


def page(name, url="https://artist.com/courses/loose-florals"):
    return extract_page(Path(f"tests/fixtures/{name}.html").read_text(encoding="utf-8"), url)


def qualify(p, niche="watercolor", email=None):
    return evaluate_qualification(
        Lead(id=1, domain="artist.com", niche=niche, email=email),
        [p],
        {p.url: p.text},
        today=date(2026, 11, 24),
    )


def test_far_split_single_course_page_is_joined():
    p = page("offer_far_split")
    offers = [o for o in p.courses if o.evidence_classification == "online_offer"]
    assert [o.value for o in offers] == ["Loose Florals"]
    offer = offers[0]
    assert offer.method == "single_course_page"
    assert offer.enrollment_url == "https://artist.com/enroll/loose-florals"
    # Footer and nav text never corroborate the offer.
    joined = " ".join(offer.supporting_text)
    assert "gift cards" not in joined and "coming soon" not in joined
    assert all(span in p.text for span in offer.supporting_text)
    assert qualify(p).criteria["relevant_course"].points == 25


def test_home_page_h1_is_not_treated_as_a_course_title():
    p = page("offer_far_split", url="https://artist.com/")
    assert not any(
        o.value == "Loose Florals" and o.method == "single_course_page" for o in p.courses
    )


def test_classed_course_cards_stay_separate():
    p = page("offer_classed_cards", url="https://artist.com/classes")
    florals = [o for o in p.courses if o.value == "Loose Florals"]
    portraits = [o for o in p.courses if o.value == "Studio Portraits"]
    assert florals and florals[0].evidence_classification == "online_offer"
    assert florals[0].enrollment_url == "https://artist.com/courses/loose-florals"
    assert "Portraits" not in " ".join(florals[0].supporting_text)
    assert all(o.evidence_classification != "online_offer" for o in portraits)


def test_catalogue_with_two_course_titles_is_not_joined():
    p = page("offer_catalog_no_join", url="https://artist.com/courses")
    assert not any(o.method == "single_course_page" for o in p.courses)


def test_accordion_widgets_and_multi_paragraph_answers():
    p = page("faq_widgets", url="https://artist.com/faq")
    pairs = {f.question: f.answer for f in p.faqs}
    assert pairs["Do I need special supplies?"] == (
        "A basic watercolor set works. Three brushes. Cotton paper."
    )
    assert pairs["How long do I have access?"] == "Lifetime access."
    assert "Read our blog?" not in pairs
    assert pairs["Is the course good for beginners?"] == (
        "Yes, it starts from zero. Week one covers materials. Week two covers washes."
    )
    assert pairs["Can I get a refund?"].startswith("Within 14 days.")
    assert all(f.answer in p.text for f in p.faqs)


def test_obfuscated_email_is_decoded_and_counts_as_published_contact():
    p = page("faq_widgets", url="https://artist.com/faq")
    found = [e for e in p.emails if e.method == "obfuscated_text"]
    assert [e.value for e in found] == ["studio@watercolorhub.com"]
    assert found[0].quote in p.text
    q = qualify(p, email="studio@watercolorhub.com")
    assert q.criteria["direct_email"].points == 15


@pytest.mark.parametrize(
    "visible",
    ["we are at the market", "meet me at noon dot com", "studio at watercolorhub dot com"],
)
def test_bare_words_are_never_decoded_as_email(visible):
    with pytest.raises(ValueError):
        deobfuscate_email(visible)
    p = extract_page(f"<p>{visible}</p>", "https://artist.com/contact")
    assert not p.emails


def test_tampered_obfuscated_quote_is_rejected():
    p = page("faq_widgets", url="https://artist.com/faq")
    email = next(e for e in p.emails if e.method == "obfuscated_text")
    email.value = "other@watercolorhub.com"
    q = qualify(p, email="other@watercolorhub.com")
    assert q.criteria["direct_email"].points == 0
