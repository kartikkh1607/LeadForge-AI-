import json
from datetime import date
from pathlib import Path

import pytest
from test_safe_http import fetcher_for, response

from agent.config import Settings
from agent.models import Lead
from agent.qualify import evaluate_qualification
from agent.safe_http import CrawlLimits
from agent.scrape import crawl_lead, extract_page
from agent.search import discover, excluded_domain
from agent.verify import normalize_domain
from agent.web_models import SearchHit


def fixture(name, url="https://independent-art.com/course"):
    return extract_page(Path(f"tests/fixtures/precision_{name}.html").read_text(), url)


def test_independent_creator_with_visible_structured_faq_and_nested_offer():
    page = fixture("creator")
    offer = page.courses[0]
    assert offer.value == "Watercolor Foundations Course"
    assert offer.landing_url == page.url and offer.enrollment_text == "Enroll for $40"
    assert "Self-paced online watercolor" in offer.quote
    assert [(f.question, f.answer) for f in page.faqs[:2]] == [
        ("How long is access?", "Forever."),
        ("Can I download videos?", "No."),
    ]
    lead = Lead(
        id=1, domain="independent-art.com", niche="watercolor", email="artist@independent-art.com"
    )
    result = evaluate_qualification(lead, [page], {page.url: page.text}, today=date(2026, 11, 22))
    assert result.qualified and result.score == 85 and not result.blockers


def test_hosted_creator_preserves_tenant_and_exact_host_exclusions():
    domain = normalize_domain("https://WWW.Michele.THINKIFIC.com./course")
    assert domain == "michele.thinkific.com"
    assert not excluded_domain(domain, Settings())
    assert excluded_domain("thinkific.com", Settings())
    assert not excluded_domain("notthinkific.com", Settings())
    assert excluded_domain("child.mapquest.com", Settings())
    assert not excluded_domain("mapquest.com.evil.com", Settings())
    page = fixture("hosted_creator", "https://michele.thinkific.com/course")
    lead = Lead(id=1, domain=domain, niche="watercolor", email="artist@independent-art.com")
    result = evaluate_qualification(lead, [page], {page.url: page.text}, today=date(2026, 11, 22))
    assert result.qualified and result.score == 60


def test_platform_directory_and_misleading_navigation():
    assert not fixture("platform").faqs
    assert not fixture("directory").faqs
    assert [(q.question, q.answer) for q in fixture("navigation").faqs] == [
        ("How long is access?", "Forever.")
    ]


def test_structured_faq_cannot_supply_invisible_or_invented_evidence():
    data = {
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": "How long is access?",
                "acceptedAnswer": {"text": "Forever."},
            }
        ],
    }
    html = '<script type="application/ld+json">' + json.dumps(data) + "</script>"
    assert not extract_page(html, "https://independent-art.com/").faqs
    assert not extract_page(
        '<script type="application/ld+json">broken</script>', "https://independent-art.com/"
    ).faqs


def test_score_cannot_override_hard_evidence_requirements():
    page = fixture("creator")
    lead = Lead(
        id=1, domain="independent-art.com", niche="watercolor", email="artist@independent-art.com"
    )
    page.courses = []
    result = evaluate_qualification(lead, [page], {page.url: page.text}, today=date(2026, 11, 22))
    assert result.score == 60 and not result.qualified
    assert "relevant_course_required" in result.blockers
    page = fixture("creator")
    page.emails = []
    result = evaluate_qualification(lead, [page], {page.url: page.text}, today=date(2026, 11, 22))
    assert result.score == 70 and not result.qualified
    assert "valid_contact_source_required" in result.blockers


def test_all_search_occurrences_survive_historical_deduplication(db):
    class Search:
        def search(self, query):
            return [
                SearchHit(
                    url="https://www.independent-art.com/course", query=query, title="Creator"
                ),
                SearchHit(url="https://thinkific.com/faq", query=query),
                SearchHit(url="https://michele.thinkific.com/course", query=query),
            ]

    first = discover(db, Search(), "watercolor")
    second = discover(db, Search(), "watercolor")
    assert first.added == 2 and second.added == 0
    rows = db.connection.execute("SELECT * FROM search_observations").fetchall()
    assert len(rows) == 18 and len({r["query"] for r in rows}) == 3
    assert {r["provider"] for r in rows} == {"tavily"}
    assert db.connection.execute("SELECT count(*) FROM leads").fetchone()[0] == 2
    assert not db.sources(first.lead_ids[0])


@pytest.mark.parametrize("case", ["robots_unavailable", "response_too_large"])
def test_live_observed_fetch_failures_remain_fail_closed(db, case):
    def handler(request):
        if request.url.path == "/robots.txt":
            return (
                response(503)
                if case == "robots_unavailable"
                else response(body="User-agent: *\nAllow: /", content_type="text/plain")
            )
        return response(body=b"x" * 2_000_001)

    fetcher, _ = fetcher_for(handler, limits=CrawlLimits(max_pages=1))
    lead = db.add_lead(Lead(domain="independent-art.com", website="https://independent-art.com/"))
    result = crawl_lead(db, lead.id, fetcher)
    assert not result.pages and not db.sources(lead.id)
    assert list(result.errors.values()) == [case]


def test_search_landing_page_cannot_be_starved_by_homepage_links(db):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return response(body="User-agent: *\nAllow: /", content_type="text/plain")
        if request.url.path == "/course":
            return response(body=Path("tests/fixtures/precision_creator.html").read_text())
        return response(body='<a href="/about">About</a><a href="/blog/help">Help</a>')

    fetcher, _ = fetcher_for(handler, limits=CrawlLimits(max_pages=1))
    lead = db.add_lead(
        Lead(domain="independent-art.com", website="https://independent-art.com/course")
    )
    result = crawl_lead(db, lead.id, fetcher)
    assert paths == ["/robots.txt", "/course"]
    assert result.pages[0].courses[0].enrollment_text


def test_old_numeric_qualification_is_stale_after_policy_upgrade(db, phase3):
    from agent.qualify import current_qualification

    lead, _, _, _ = phase3
    row = db.connection.execute(
        "SELECT id,result_json FROM qualifications WHERE lead_id=? ORDER BY id DESC", (lead.id,)
    ).fetchone()
    old = json.loads(row["result_json"])
    old.pop("policy_version")
    with db.connection:
        db.connection.execute(
            "UPDATE qualifications SET result_json=? WHERE id=?", (json.dumps(old), row["id"])
        )
    assert current_qualification(db, lead.id) is None
