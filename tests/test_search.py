import json

import httpx
import pytest

from agent.config import Settings
from agent.models import Lead, Suppression
from agent.search import QUERY_TEMPLATES, SearchClient, SearchError, discover


@pytest.mark.parametrize(
    "provider,field,url_key", [("tavily", "results", "url"), ("serper", "organic", "link")]
)
def test_provider_requests_and_discovery(db, provider, field, url_key):
    requests = []

    def handler(request):
        requests.append(request)
        data = json.loads(request.content)
        assert "watercolor" in data.get("query", data.get("q", ""))
        assert request.url.scheme == "https"
        if provider == "tavily":
            assert request.headers["Authorization"] == "Bearer fake-test-key"
            assert data["include_raw_content"] is False
        else:
            assert request.headers["X-API-KEY"] == "fake-test-key"
        return httpx.Response(
            200,
            json={
                field: [
                    {url_key: "https://www.creator.com/faq", "title": "Creator"},
                    {url_key: "https://creator.com/contact"},
                    {url_key: "https://www.udemy.com/course/art"},
                    {url_key: "http://127.0.0.1/"},
                    {url_key: "javascript:alert(1)"},
                    {url_key: "https://blocked.com/"},
                    {url_key: "https://existing.com/"},
                ]
            },
        )

    db.add_lead(Lead(domain="existing.com", stage="contacted"))
    db.suppress(Suppression(kind="domain", value="blocked.com", reason="manual"))
    client = SearchClient(
        Settings(search_provider=provider, search_api_key="fake-test-key"),
        httpx.MockTransport(handler),
    )
    result = discover(db, client, "watercolor", QUERY_TEMPLATES[:3])
    assert len(requests) == 3
    assert result.added == 1 and result.duplicates == 8 and result.skipped == 12
    lead = db.get_lead(result.lead_ids[0])
    assert lead.domain == "creator.com" and lead.stage == "new"
    assert lead.business == ""  # Search title is not extracted business evidence.
    assert lead.source_url == "https://www.creator.com/faq"
    assert not db.sources(lead.id) and db.get_compliance(lead.id) is None
    assert discover(db, client, "watercolor").added == 0


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_provider_failures_never_expose_secrets_or_follow_redirects(db, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="fake-secret", headers={"location": "https://evil.com"})

    client = SearchClient(Settings(search_api_key="fake-secret"), httpx.MockTransport(handler))
    result = discover(db, client, "art", ["{niche} FAQ"])
    assert len(calls) == 1 and result.errors and result.added == 0
    assert "fake-secret" not in str(result)


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"results":null}'])
def test_malformed_response(body):
    client = SearchClient(
        Settings(search_api_key="test"),
        httpx.MockTransport(lambda req: httpx.Response(200, content=body)),
    )
    with pytest.raises(SearchError):
        client.search("art FAQ")


def test_missing_key_is_clear(db):
    result = discover(db, SearchClient(Settings()), "art", ["{niche} FAQ"])
    assert result.errors == ["SEARCH_API_KEY is required"]


def test_partial_search_failure(db):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("secret response text")
        return httpx.Response(200, json={"results": [{"url": "https://creator.com/"}]})

    result = discover(
        db,
        SearchClient(Settings(search_api_key="test"), httpx.MockTransport(handler)),
        "art",
        ["{niche} FAQ", "{niche} help"],
    )
    assert result.added == 1 and result.errors == ["tavily: ReadTimeout"]


def test_tavily_translates_site_filter_to_include_domains():
    import json as _json

    import httpx

    from agent.config import Settings
    from agent.search import SearchClient

    seen = {}

    def handler(request):
        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"results": [{"url": "https://jobs.lever.co/a/1"}]})

    settings = Settings(search_api_key="k", search_provider="tavily")
    hits = SearchClient(settings, httpx.MockTransport(handler)).search(
        "AI intern Canada site:jobs.lever.co"
    )
    assert seen["query"] == "AI intern Canada"
    assert seen["include_domains"] == ["jobs.lever.co"]
    assert hits[0].url == "https://jobs.lever.co/a/1"
