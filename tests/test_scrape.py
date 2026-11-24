from pathlib import Path

import httpx
import pytest
from test_safe_http import fetcher_for, response

from agent.db import Database
from agent.models import Lead, Suppression
from agent.safe_http import CrawlLimits, FetchError
from agent.scrape import crawl_lead, extract_page

HTML = Path("tests/fixtures/creator.html").read_text(encoding="utf-8")


def test_structured_extraction_and_no_invention():
    page = extract_page(HTML, "https://willow-art.com/")
    assert page.business_names[0].value == "Willow Art School"
    assert page.owner_names[0].value == "Ada Rivers"
    assert [e.value for e in page.emails] == [
        "ada@willow-art.com",
        "hello@willow-art.com",
        "support@willow-art.com",
    ]
    assert [(q.question, q.answer) for q in page.faqs] == [
        ("When can I start?", "Start today."),
        ("How long do I have access?", "Lifetime access."),
    ]
    assert page.courses[0].value == "Watercolor Foundations Course"
    assert page.chatbots[0].value == "tidio"
    assert page.links == ["https://willow-art.com/student-help", "https://willow-art.com/contact"]
    assert all(e.source_url == page.url for e in page.emails)
    assert "Hidden contact" not in page.text and "const fake" not in page.text


def test_cloudflare_both_forms_and_bad_encoding():
    email = "ada@willow-art.com"
    encoded = bytes([42, *(ord(c) ^ 42 for c in email)]).hex()
    html = f'<a data-cfemail="{encoded}"></a><a data-cfemail="invalid"></a>'
    html += f'<a href="/cdn-cgi/l/email-protection#{encoded}">Contact</a>'
    assert extract_page(html, "https://willow-art.com/").emails[0].value == email
    assert (
        extract_page(html.split("</a>", 2)[-1], "https://willow-art.com/").emails[0].value == email
    )


def test_malformed_html_short_faq_and_chatbot_false_positive():
    page = extract_page(
        "<h2>Access?</h2><p>Forever.</p><p>We discuss Intercom.</p>"
        '<script src="https://code.tidio.co.evil.com/fake.js"></script>',
        "https://willow-art.com/faq",
    )
    assert page.faqs[0].answer == "Forever."
    assert not page.chatbots and not page.business_names and not page.owner_names


@pytest.mark.parametrize(
    "html",
    ['<input type="password">', '<form id="challenge-form">', "<title>Access denied</title>"],
)
def test_restricted_content_not_saved(html):
    with pytest.raises(FetchError, match="restricted"):
        extract_page(html, "https://willow-art.com/")


def test_crawl_persists_partial_success_and_resumes(db):
    lead = db.add_lead(Lead(domain="willow-art.com", website="https://willow-art.com/"))
    paths = []
    fail = [True]

    def handler(request):
        path = request.url.path
        paths.append(path)
        if path == "/robots.txt":
            return response(body="User-agent: *\nDisallow: /contact", content_type="text/plain")
        if path == "/":
            return response(body=HTML)
        if path == "/student-help":
            if fail[0]:
                raise httpx.ReadTimeout("offline")
            return response(body="<h2>Can I download lessons?</h2><p>Yes.</p>")
        return response(404)

    fetcher, _ = fetcher_for(handler)
    result = crawl_lead(db, lead.id, fetcher)
    assert len(db.crawl_records(lead.id)) == 8
    assert "/contact" not in paths
    assert len(result.pages) == 1
    assert db.get_lead(lead.id).email == "ada@willow-art.com"
    assert db.get_lead(lead.id).stage == "new"
    assert db.get_lead(lead.id).email_status == "unconfirmed_mailbox"
    assert db.get_compliance(lead.id) is None
    source = db.connection.execute(
        "SELECT * FROM source_evidence WHERE lead_id=?", (lead.id,)
    ).fetchone()
    assert source["url"] == "https://willow-art.com/"
    timestamp = source["fetched_at"]
    paths.clear()
    cached = crawl_lead(db, lead.id, fetcher)
    assert paths == [] and cached.cached_pages >= 1
    fail[0] = False
    resumed = crawl_lead(db, lead.id, fetcher, retry_failed=True)
    assert len(resumed.pages) == 2 and "/" not in paths
    assert (
        db.connection.execute(
            "SELECT fetched_at FROM source_evidence WHERE url=?", ("https://willow-art.com/",)
        ).fetchone()[0]
        == timestamp
    )


def test_maximum_pages_and_duplicate_redirect_final_url(db):
    lead = db.add_lead(Lead(domain="willow-art.com"))

    def handler(request):
        if request.url.path == "/robots.txt":
            return response(404)
        if request.url.path == "/":
            links = "".join(f'<a href="/faq/{n}">FAQ</a>' for n in range(40))
            return response(body=links)
        return response(302, location="/")

    fetcher, _ = fetcher_for(handler)
    result = crawl_lead(db, lead.id, fetcher)
    assert len(db.crawl_records(lead.id)) == 8
    assert len(result.pages) == 1
    assert result.requests <= 64


def test_retry_budget_and_network_unavailable(db):
    lead = db.add_lead(Lead(domain="willow-art.com"))

    def handler(request):
        raise httpx.ConnectError("offline")

    fetcher, _ = fetcher_for(handler, limits=CrawlLimits(max_pages=1))
    for _ in range(4):
        crawl_lead(db, lead.id, fetcher, retry_failed=True)
    record = next(iter(db.crawl_records(lead.id).values()))
    assert record["attempts"] == 3
    assert not db.sources(lead.id)


def test_suppression_and_contacted_leads_never_fetched(db):
    lead = db.add_lead(Lead(domain="willow-art.com"))
    db.suppress(Suppression(kind="domain", value=lead.domain, reason="opt_out"))

    def handler(request):
        pytest.fail("Suppressed lead fetched")

    fetcher, _ = fetcher_for(handler)
    assert crawl_lead(db, lead.id, fetcher).errors
    other = db.add_lead(Lead(domain="other.com", stage="contacted"))
    assert crawl_lead(db, other.id, fetcher).errors


def test_duplicate_email_is_preserved_as_evidence_but_not_assigned(db):
    db.add_lead(Lead(domain="existing.com", email="ada@willow-art.com"))
    lead = db.add_lead(Lead(domain="willow-art.com"))
    page = extract_page("<p>ada@willow-art.com</p>", "https://willow-art.com/")
    db.save_crawl_page(lead.id, page.url, page=page)
    db.apply_scraped_facts(lead.id, [page])
    assert db.get_lead(lead.id).email is None
    assert "ada@willow-art.com" in db.sources(lead.id)[page.url]


def test_migration_from_phase1_preserves_existing_records(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    original = Path("agent/migrations/001_foundation.sql")
    (old / original.name).write_bytes(original.read_bytes())
    with Database(tmp_path / "upgrade.sqlite3") as db:
        db.migrate(old)
        lead = db.add_lead(Lead(domain="willow-art.com", notes="keep me"))
        db.migrate()
        db.migrate()
        assert db.get_lead(lead.id).notes == "keep me"
        assert db.crawl_records(lead.id) == {}


def test_source_changes_still_revoke_approval(db, approved):
    page = extract_page("<p>New source evidence.</p>", "https://example.com/faq")
    db.save_crawl_page(approved.lead_id, page.url, page=page)
    assert db.get_message(approved.id).status == "draft"
    assert not db.get_compliance(approved.lead_id).eligible
