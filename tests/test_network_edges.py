import dns.exception
import dns.resolver
import httpx
import pytest
from test_safe_http import fetcher_for, response

from agent.safe_http import FetchError, resolve_public


@pytest.mark.parametrize(
    "answers,expected",
    [
        ({"A": ["93.184.215.14"], "AAAA": dns.resolver.NoAnswer()}, ["93.184.215.14"]),
        (
            {"A": dns.resolver.NoAnswer(), "AAAA": ["2606:4700:4700::1111"]},
            ["2606:4700:4700::1111"],
        ),
    ],
)
def test_dns_backend_resolves_both_families(monkeypatch, answers, expected):
    calls = []

    class Resolver:
        def resolve(self, host, kind, **kwargs):
            calls.append(kind)
            assert kwargs["search"] is False and 0 < kwargs["lifetime"] <= 5
            answer = answers[kind]
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(dns.resolver, "Resolver", Resolver)
    assert resolve_public("creator.com") == expected
    assert calls == ["A", "AAAA"]


@pytest.mark.parametrize(
    "failure", [dns.resolver.NXDOMAIN(), dns.exception.Timeout(), dns.resolver.NoNameservers()]
)
def test_dns_failures_fail_closed(monkeypatch, failure):
    class Resolver:
        def resolve(self, *args, **kwargs):
            raise failure

    monkeypatch.setattr(dns.resolver, "Resolver", Resolver)
    with pytest.raises(FetchError, match="dns_unavailable"):
        resolve_public("creator.com")


@pytest.mark.parametrize(
    "body,content_type",
    [
        ("<html>Sign in</html>", "text/html"),
        ("Access denied", "text/plain"),
        ("User-agent: *\nDisallow /", "text/plain"),
    ],
)
def test_invalid_robots_cannot_allow_content(body, content_type):
    fetcher, _ = fetcher_for(lambda req: response(body=body, content_type=content_type))
    with pytest.raises(FetchError, match="invalid_robots"):
        fetcher.fetch("https://creator.com/", "creator.com")
    assert fetcher.requests == 1


def test_user_agent_specific_rules_are_enforced():
    def handler(request):
        return response(
            body="User-agent: *\nAllow: /\n\nUser-agent: LeadForgeBot\nDisallow: /",
            content_type="text/plain",
        )

    fetcher, _ = fetcher_for(handler)
    with pytest.raises(FetchError, match="robots_disallowed"):
        fetcher.fetch("https://creator.com/", "creator.com")


def test_www_redirect_checks_new_origin_policy():
    calls = []

    def handler(request):
        calls.append((request.headers["host"], request.url.path))
        if request.url.path == "/robots.txt":
            if request.headers["host"] == "www.creator.com":
                return response(body="User-agent: *\nDisallow: /", content_type="text/plain")
            return response(404)
        return response(302, location="https://www.creator.com/faq")

    fetcher, _ = fetcher_for(handler)
    with pytest.raises(FetchError, match="robots_disallowed"):
        fetcher.fetch("https://creator.com/", "creator.com")
    assert calls[-1] == ("www.creator.com", "/robots.txt")


def test_robots_redirect_cannot_fetch_arbitrary_content():
    fetcher, _ = fetcher_for(lambda req: response(302, location="/secret"))
    with pytest.raises(FetchError, match="robots_redirect_not_policy"):
        fetcher.fetch("https://creator.com/", "creator.com")
    assert fetcher.requests == 1


def test_redirect_loop_is_bounded():
    fetcher, _ = fetcher_for(
        lambda req: response(404) if req.url.path == "/robots.txt" else response(302, location="/")
    )
    with pytest.raises(FetchError, match="redirect_loop"):
        fetcher.fetch("https://creator.com/", "creator.com")
    assert fetcher.requests == 2


def test_trickling_body_deadline():
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            clock.now += 11
            yield b"trickled body"

    def handler(request):
        if request.url.path == "/robots.txt":
            return response(404)
        return httpx.Response(200, stream=Stream())

    fetcher, clock = fetcher_for(handler)
    with pytest.raises(FetchError, match="deadline"):
        fetcher.fetch("https://creator.com/", "creator.com")


def test_request_rate_delay():
    def handler(request):
        if request.url.path == "/robots.txt":
            return response(body="User-agent: *\nRequest-rate: 1/10s", content_type="text/plain")
        return response()

    fetcher, clock = fetcher_for(handler)
    fetcher.fetch("https://creator.com/", "creator.com")
    assert clock.waits == [10]


def test_large_crawl_delay_defers_without_capping():
    fetcher, _ = fetcher_for(
        lambda req: response(body="User-agent: *\nCrawl-delay: 120", content_type="text/plain")
    )
    with pytest.raises(FetchError, match="later_retry"):
        fetcher.fetch("https://creator.com/", "creator.com")
    assert fetcher.requests == 1
