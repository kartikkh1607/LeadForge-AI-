import httpx
import pytest

from agent.safe_http import CrawlLimits, FetchError, SafeFetcher, checked_addresses, public_url


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def response(status=200, body="hello", content_type="text/html", **headers):
    return httpx.Response(
        status,
        headers={"content-type": content_type, **headers},
        stream=httpx.ByteStream(body.encode() if isinstance(body, str) else body),
    )


def fetcher_for(handler, **kwargs):
    clock = Clock()
    fetcher = SafeFetcher(
        resolver=lambda host: ["93.184.215.14"],
        transport_factory=lambda: httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock,
        **kwargs,
    )
    return fetcher, clock


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://creator.com",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://2130706433/",
        "http://127.1/",
        "http://0x7f000001/",
        "http://169.254.169.254/latest/meta-data",
        "https://creator.com:8443/",
        "https://user:password@creator.com/",
        "https://creator.com\\@evil.com/",
        "https://creator.com/\nfaq",
        "https://creator.com/login",
        "https://creator.com/%6cogin",
        "https://creator.com/account/contact",
        "https://creator.com/?token=secret",
        "https://foo.internal/",
        "http://127%2e0%2e0%2e1/",
    ],
)
def test_malicious_urls(url):
    with pytest.raises(FetchError):
        public_url(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "192.0.2.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
    ],
)
def test_nonpublic_and_tunnel_addresses(address):
    with pytest.raises(FetchError):
        checked_addresses(["93.184.215.14", address])


def test_connection_pins_ip_preserves_tls_host_and_no_session():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.215.14"
        assert request.headers["Host"] == "creator.com"
        assert request.extensions["sni_hostname"] == "creator.com"
        assert request.extensions["timeout"]["read"] == 10
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.path == "/robots.txt":
            return response(
                body="User-agent: *\nAllow: /\nCrawl-delay: 3",
                content_type="text/plain",
                **{"set-cookie": "session=private"},
            )
        return response()

    fetcher, clock = fetcher_for(handler)
    assert fetcher.fetch("https://creator.com/", "creator.com").status == 200
    assert len(requests) == 2 and clock.waits == [3]


def test_rebinding_is_blocked_on_next_request():
    calls = []

    def handler(request):
        calls.append(request)
        return response(body="User-agent: *\nAllow: /", content_type="text/plain")

    fetcher, _ = fetcher_for(handler)
    answers = iter([["93.184.215.14"], ["127.0.0.1"]])
    fetcher.resolver = lambda host: next(answers)
    with pytest.raises(FetchError, match="unsafe_dns_address"):
        fetcher.fetch("https://creator.com/", "creator.com")
    assert len(calls) == 1  # Only the safe robots request reached the transport.


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/",
        "https://evil.com/",
        "http://creator.com/faq",
        "/login",
        "https://creator.com:8080/",
    ],
)
def test_unsafe_redirect_never_followed(target):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return response(404)
        return response(302, location=target)

    fetcher, _ = fetcher_for(handler)
    with pytest.raises(FetchError):
        fetcher.fetch("https://creator.com/", "creator.com")
    assert paths == ["/robots.txt", "/"]


def test_redirect_target_must_pass_robots():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return response(body="User-agent: *\nDisallow: /secret", content_type="text/plain")
        return response(302, location="/secret")

    fetcher, _ = fetcher_for(handler)
    with pytest.raises(FetchError, match="robots_disallowed"):
        fetcher.fetch("https://creator.com/", "creator.com")
    assert paths == ["/robots.txt", "/"]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_unavailable_robots_fail_closed(status):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return response(status)

    fetcher, _ = fetcher_for(handler)
    with pytest.raises(FetchError, match="robots_unavailable"):
        fetcher.fetch("https://creator.com/", "creator.com")
    assert paths == ["/robots.txt"]


def test_robots_wildcard_and_longest_match():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return response(
                body="User-agent: *\nDisallow: /faq*\nAllow: /faq/public$",
                content_type="text/plain",
            )
        return response()

    fetcher, _ = fetcher_for(handler)
    assert fetcher.fetch("https://creator.com/faq/public", "creator.com").status == 200
    with pytest.raises(FetchError, match="robots_disallowed"):
        fetcher.fetch("https://creator.com/faq/hidden", "creator.com")
    assert "/faq/hidden" not in paths


def test_redirect_limit_and_loop():
    def handler(request):
        if request.url.path == "/robots.txt":
            return response(404)
        count = int(request.url.path.strip("/") or 0)
        return response(302, location="/" + str(count + 1))

    fetcher, _ = fetcher_for(handler)
    with pytest.raises(FetchError, match="redirect_limit"):
        fetcher.fetch("https://creator.com/", "creator.com")
    assert fetcher.requests == 7  # robots + original + five redirects


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"content-length": "2000001"}, "small"),
        ({}, "x" * 2_000_001),
        ({"content-encoding": "gzip"}, "compressed"),
    ],
)
def test_response_size_and_compression(headers, body):
    def handler(request):
        if request.url.path == "/robots.txt":
            return response(404)
        return response(body=body, **headers)

    fetcher, _ = fetcher_for(handler)
    with pytest.raises(FetchError):
        fetcher.fetch("https://creator.com/", "creator.com")


def test_request_budget():
    fetcher, _ = fetcher_for(lambda req: response(404), limits=CrawlLimits(max_requests=1))
    with pytest.raises(FetchError, match="budget"):
        fetcher.fetch("https://creator.com/", "creator.com")


def test_timeout_is_safe_failure():
    def handler(request):
        raise httpx.ReadTimeout("untrusted text")

    fetcher, _ = fetcher_for(handler)
    with pytest.raises(FetchError, match="http_ReadTimeout"):
        fetcher.fetch("https://creator.com/", "creator.com")
