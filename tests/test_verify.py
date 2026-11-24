from types import SimpleNamespace

import dns.exception
import dns.resolver
import pytest

from agent.verify import normalize_domain, normalize_email, verify_email


class FakeResolver:
    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def resolve(self, domain, kind, **kwargs):
        self.calls.append((domain, kind, kwargs))
        result = self.answers.get(kind, dns.resolver.NoAnswer())
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://WWW.Example.COM/path?q=1", "example.com"),
        ("example.com/", "example.com"),
        ("www.example.com.", "example.com"),
        ("https://bücher.de:443/faq", "xn--bcher-kva.de"),
        ("courses.example.com", "courses.example.com"),
    ],
)
def test_domain_normalization(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "localhost",
        "127.0.0.1",
        "http://[::1]",
        "ftp://example.com",
        "a..com",
        "-foo.com",
        "https://user:secret@example.com",
        "https://example.com:bad",
        "hello world.com",
    ],
)
def test_domain_rejects_invalid(value):
    with pytest.raises(ValueError):
        normalize_domain(value)


def test_email_normalization():
    assert normalize_email(" Owner@EXAMPLE.COM ") == "Owner@example.com"


@pytest.mark.parametrize(
    "email",
    [
        "bad",
        "a@@example.com",
        "a..b@example.com",
        "a@example",
        "a\r\nBcc: x@example.com",
        "Name <a@example.com>",
    ],
)
def test_invalid_syntax_never_queries_dns(email):
    resolver = FakeResolver()
    assert verify_email(email, resolver).status == "invalid"
    assert resolver.calls == []


def test_mx_is_not_mailbox_confirmation():
    resolver = FakeResolver(MX=[SimpleNamespace(preference=10, exchange="mail.example.com.")])
    result = verify_email("a@example.com", resolver, timeout=2)
    assert result.status == "domain_mail_capable"
    assert "unconfirmed" in result.reason
    assert resolver.calls == [("example.com", "MX", {"lifetime": 2, "search": False})]


def test_null_mx_never_falls_back():
    resolver = FakeResolver(MX=[SimpleNamespace(preference=0, exchange=".")], A=["1.2.3.4"])
    assert verify_email("a@example.com", resolver).status == "invalid"
    assert len(resolver.calls) == 1


@pytest.mark.parametrize("record", ["A", "AAAA"])
def test_implicit_mx(record):
    assert (
        verify_email("a@example.com", FakeResolver(**{record: ["address"]})).status
        == "domain_mail_capable"
    )


def test_no_mail_records():
    assert verify_email("a@example.com", FakeResolver()).status == "invalid"


@pytest.mark.parametrize("error", [dns.resolver.NXDOMAIN()])
def test_nxdomain(error):
    assert verify_email("a@example.com", FakeResolver(MX=error)).status == "invalid"


@pytest.mark.parametrize(
    "error", [dns.exception.Timeout(), dns.resolver.NoNameservers(), OSError()]
)
@pytest.mark.parametrize("record", ["MX", "A", "AAAA"])
def test_transient_failures_are_retryable(error, record):
    result = verify_email("a@example.com", FakeResolver(**{record: error}))
    assert result.status is None
    assert result.retryable
