"""Lead-yield improvements: contact choice, niche matching, search breadth."""

from datetime import date

from agent.models import Lead
from agent.pipeline import RunConfig
from agent.qualify import evaluate_qualification
from agent.scrape import extract_page
from agent.search import BLOCKED_DOMAINS, QUERY_TEMPLATES
from agent.verify import contact_rank


def test_own_domain_beats_freemail_beats_vendor():
    emails = [
        "support@squarespace.com",
        "studio.owner@gmail.com",
        "info@artist.com",
        "jane@artist.com",
        "hello@artist.com",
        "billing@unrelated-vendor.io",
    ]
    ranked = sorted(emails, key=lambda e: contact_rank(e, "artist.com"))
    assert ranked == [
        "jane@artist.com",
        "hello@artist.com",
        "info@artist.com",
        "studio.owner@gmail.com",
        "billing@unrelated-vendor.io",
        "support@squarespace.com",
    ]


def test_page_lists_business_email_first():
    html = (
        "<p>Site by theme support@elementor.com.</p>"
        "<p>Questions? hello@artist.com or me at jane.paints@gmail.com</p>"
    )
    page = extract_page(html, "https://artist.com/contact")
    assert page.emails[0].value == "hello@artist.com"
    assert page.emails[-1].value == "support@elementor.com"


def test_vendor_address_is_never_adopted_as_lead_contact(db):
    lead = db.add_lead(Lead(domain="artist.com", website="https://artist.com/"))
    page = extract_page("<p>support@squarespace.com</p>", "https://artist.com/contact")
    db.apply_scraped_facts(lead.id, [page])
    assert db.get_lead(lead.id).email is None


def qualify_with(niche, course_html):
    page = extract_page(course_html, "https://coach.com/courses/clarity")
    return evaluate_qualification(
        Lead(id=1, domain="coach.com", niche=niche),
        [page],
        {page.url: page.text},
        today=date(2026, 11, 24),
    )


COACHING = (
    "<main><h1>Clarity Coaching Course</h1><p>A self-paced online coaching course "
    "with video lessons.</p><a href='/enroll/clarity'>Enroll for $97</a></main>"
)


def test_modifier_words_do_not_block_relevant_course():
    # "professional" never appears in the course copy, but the subject ("coaching") does.
    q = qualify_with("Professional coaching courses", COACHING)
    assert q.criteria["relevant_course"].points == 25


def test_subject_word_is_still_required():
    q = qualify_with("Watercolor courses", COACHING)
    assert q.criteria["relevant_course"].points == 0


def test_generic_only_niche_still_needs_a_subject():
    q = qualify_with("Art courses", COACHING)
    assert q.criteria["relevant_course"].points == 0


def test_search_breadth_defaults():
    config = RunConfig(niche="watercolor")
    assert config.query_count == len(QUERY_TEMPLATES) >= 6
    assert config.results_per_query == 10 and config.max_pages == 8
    assert all("{niche}" in t for t in QUERY_TEMPLATES)
    assert not any("workshop" in t for t in QUERY_TEMPLATES)  # in-person noise
    assert {"instagram.com", "reddit.com", "eventbrite.com"} <= set(BLOCKED_DOMAINS)


def test_truncated_duplicate_faq_is_not_a_contradiction():
    from agent.qualify import verified_faqs
    from agent.web_models import FAQ, PageData

    short = PageData(
        url="https://a.com/",
        text="Do I need supplies? A basic set works.",
        faqs=[
            FAQ(
                question="Do I need supplies?",
                answer="A basic set works.",
                source_url="https://a.com/",
            )
        ],
    )
    full = PageData(
        url="https://a.com/faq",
        text="Do I need supplies? A basic set works. Three brushes and cotton paper.",
        faqs=[
            FAQ(
                question="Do I need supplies?",
                answer="A basic set works. Three brushes and cotton paper.",
                source_url="https://a.com/faq",
            )
        ],
    )
    faqs, conflicts = verified_faqs([short, full], {p.url: p.text for p in (short, full)})
    assert conflicts == [] and faqs[0].answer.endswith("cotton paper.")

    other = PageData(
        url="https://a.com/help",
        text="Do I need supplies? No, everything is included.",
        faqs=[
            FAQ(
                question="Do I need supplies?",
                answer="No, everything is included.",
                source_url="https://a.com/help",
            )
        ],
    )
    pages = [short, full, other]
    faqs, conflicts = verified_faqs(pages, {p.url: p.text for p in pages})
    assert conflicts == ["do i need supplies?"] and faqs == []


def test_job_boards_and_news_sites_are_excluded_but_hosted_creators_are_not():
    from agent.config import Settings
    from agent.search import excluded_domain

    settings = Settings()
    assert excluded_domain("ca.indeed.com", settings)
    assert excluded_domain("businessinsider.com", settings)
    assert not excluded_domain("michele.thinkific.com", settings)
    assert excluded_domain("thinkific.com", settings)
