"""Bounded, anonymous HTTP with DNS-pinned connections and fail-closed robots."""

import ipaddress
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import dns.exception
import dns.resolver
import httpx
from protego import Protego
from pydantic import Field

from agent.models import Model
from agent.verify import normalize_domain

USER_AGENT = "LeadForgeBot/0.2"
RESTRICTED = re.compile(
    r"(?:^|/)(?:login|log-in|signin|sign-in|logout|signout|auth|oauth|admin|wp-admin|"
    r"wp-login\.php|account|accounts|dashboard|checkout|cart|private|members|membership)(?:/|$)",
    re.IGNORECASE,
)


class FetchError(ValueError):
    """Safe, non-secret failure category suitable for persisted crawl diagnostics."""


class CrawlLimits(Model):
    max_pages: int = Field(default=8, ge=1, le=8)
    max_redirects: int = Field(default=5, ge=0, le=5)
    max_bytes: int = Field(default=2_000_000, ge=1, le=2_000_000)
    timeout: float = Field(default=10, gt=0, le=10)
    delay: float = Field(default=1.5, ge=1, le=2)
    max_requests: int = Field(default=64, ge=1, le=64)


def public_url(value: str) -> str:
    """Canonical public-content URL. DNS safety is checked separately on every request."""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise FetchError("unsafe_url")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError()
        if parts.username is not None or parts.password is not None or "%" in parts.netloc:
            raise ValueError()
        if parts.port not in {None, 80 if parts.scheme == "http" else 443}:
            raise ValueError()
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        normalize_domain(host)  # Reject all IP literals, including public IPs.
        if host.rsplit(".", 1)[1].isdigit() or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError()
        path = parts.path or "/"
        decoded = unquote(unquote(path))
        if RESTRICTED.search(decoded) or re.search(r"[\\\x00-\x1f\x7f]", decoded):
            raise ValueError()
        # Queries may contain authentication tokens, state-changing actions, or crawl traps.
        if parts.query:
            raise ValueError()
        return str(httpx.URL(urlunsplit((parts.scheme, host, path, "", ""))))
    except (ValueError, UnicodeError, httpx.InvalidURL) as exc:
        raise FetchError("unsafe_or_restricted_url") from exc


def same_site(url: str, domain: str) -> bool:
    return normalize_domain(url) == domain


def resolve_public(host: str, timeout: float = 5) -> list[str]:
    resolver = dns.resolver.Resolver()
    addresses = []
    deadline = time.monotonic() + timeout
    try:
        for kind in ("A", "AAAA"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("dns_timeout")
            try:
                addresses.extend(
                    str(r) for r in resolver.resolve(host, kind, lifetime=remaining, search=False)
                )
            except dns.resolver.NoAnswer:
                continue
    except (dns.exception.DNSException, OSError) as exc:
        raise FetchError("dns_unavailable") from exc
    return checked_addresses(addresses)


def checked_addresses(addresses: list[str]) -> list[str]:
    if not addresses:
        raise FetchError("dns_no_addresses")
    for value in addresses:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError as exc:
            raise FetchError("invalid_dns_address") from exc
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise FetchError("unsafe_dns_address")
        if isinstance(ip, ipaddress.IPv6Address) and (
            ip.ipv4_mapped
            or ip.sixtofour
            or ip.teredo
            or ip in ipaddress.ip_network("64:ff9b::/96")
            or ip in ipaddress.ip_network("64:ff9b:1::/48")
        ):
            raise FetchError("unsafe_dns_address")
    return list(dict.fromkeys(addresses))


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    headers: httpx.Headers
    content: bytes

    @property
    def text(self) -> str:
        return httpx.Response(self.status, headers=self.headers, content=self.content).text


class SafeFetcher:
    def __init__(
        self,
        limits: CrawlLimits | None = None,
        *,
        resolver: Callable[[str], list[str]] = resolve_public,
        transport_factory: Callable[[], httpx.BaseTransport] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limits = limits or CrawlLimits()
        self.resolver = resolver
        self.transport_factory = transport_factory or (
            lambda: httpx.HTTPTransport(retries=0, trust_env=False)
        )
        self.sleep, self.clock = sleep, clock
        self.last_request: dict[str, float] = {}
        self.robots: dict[str, Protego | None] = {}
        self.requests = 0

    def begin_crawl(self) -> None:
        self.requests = 0
        self.robots.clear()  # Recheck policy on resume; retain per-host delay state.

    def _request(self, url: str, delay: float) -> Fetched:
        url = public_url(url)
        if self.requests >= self.limits.max_requests:
            raise FetchError("request_budget_exhausted")
        host = urlsplit(url).hostname
        wait = delay - (self.clock() - self.last_request.get(host, -float("inf")))
        if wait > 60:
            raise FetchError("crawl_delay_requires_later_retry")
        if wait > 0:
            self.sleep(wait)
        # Resolve immediately before connecting and pin the connection to this checked IP.
        addresses = checked_addresses(self.resolver(host))
        pinned = httpx.URL(url).copy_with(host=addresses[0])
        self.requests += 1
        started = self.clock()
        try:
            # New anonymous client per hop: never persist cookies, auth or environment proxies.
            with (
                httpx.Client(
                    transport=self.transport_factory(),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self.limits.timeout,
                ) as client,
                client.stream(
                    "GET",
                    pinned,
                    headers={
                        "Host": host,
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,text/plain",
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                    },
                    extensions={"sni_hostname": host},
                ) as response,
            ):
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise FetchError("compressed_response_rejected")
                length = response.headers.get("content-length")
                if length and (
                    len(length) > 10 or not length.isdigit() or int(length) > self.limits.max_bytes
                ):
                    raise FetchError("response_too_large")
                content = bytearray()
                for chunk in response.iter_raw():
                    if self.clock() - started > self.limits.timeout:
                        raise FetchError("response_deadline_exceeded")
                    if len(content) + len(chunk) > self.limits.max_bytes:
                        raise FetchError("response_too_large")
                    content.extend(chunk)
                return Fetched(url, response.status_code, response.headers, bytes(content))
        except httpx.HTTPError as exc:
            raise FetchError(f"http_{type(exc).__name__}") from exc
        finally:
            self.last_request[host] = self.clock()

    def _redirect_target(self, current: str, location: str, domain: str) -> str:
        target = public_url(urljoin(current, location))
        if not same_site(target, domain):
            raise FetchError("cross_domain_redirect")
        if current.startswith("https:") and target.startswith("http:"):
            raise FetchError("https_downgrade")
        return target

    def _policy(self, url: str, domain: str) -> Protego:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            current = origin + "/robots.txt"
            try:
                for hop in range(self.limits.max_redirects + 1):
                    response = self._request(current, self.limits.delay)
                    if response.status in {301, 302, 303, 307, 308}:
                        if hop == self.limits.max_redirects or not response.headers.get("location"):
                            raise FetchError("robots_redirect_limit")
                        current = self._redirect_target(
                            current, response.headers["location"], domain
                        )
                        if urlsplit(current).path != "/robots.txt":
                            raise FetchError("robots_redirect_not_policy")
                        continue
                    if response.status in {404, 410}:
                        policy = Protego.parse("")
                    elif response.status == 200:
                        if (
                            "text/plain" not in response.headers.get("content-type", "").lower()
                            or "<html" in response.text.lower()
                        ):
                            raise FetchError("invalid_robots_content")
                        lines = [
                            line.split("#", 1)[0].strip() for line in response.text.splitlines()
                        ]
                        lines = [line for line in lines if line]
                        if any(":" not in line for line in lines) or (
                            lines
                            and not any(
                                line.lower().startswith(("user-agent:", "sitemap:"))
                                for line in lines
                            )
                        ):
                            raise FetchError("invalid_robots_content")
                        policy = Protego.parse(response.text)
                    else:
                        raise FetchError("robots_unavailable")
                    self.robots[origin] = policy
                    break
            except FetchError:
                self.robots[origin] = None
                raise
        policy = self.robots[origin]
        if policy is None:
            raise FetchError("robots_unavailable")
        return policy

    def fetch(self, url: str, domain: str) -> Fetched:
        current = public_url(url)
        seen = set()
        for hop in range(self.limits.max_redirects + 1):
            if not same_site(current, domain):
                raise FetchError("outside_site")
            if current in seen:
                raise FetchError("redirect_loop")
            seen.add(current)
            policy = self._policy(current, domain)
            if not policy.can_fetch(current, USER_AGENT):
                raise FetchError("robots_disallowed")
            delay = max(self.limits.delay, policy.crawl_delay(USER_AGENT) or 0)
            rate = policy.request_rate(USER_AGENT)
            if policy.visit_time(USER_AGENT) or (rate and (rate.start_time or rate.end_time)):
                raise FetchError("robots_time_window_not_supported")
            if rate:
                delay = max(delay, rate.seconds / rate.requests)
            if delay > 60:
                raise FetchError("crawl_delay_requires_later_retry")
            response = self._request(current, delay)
            if response.status not in {301, 302, 303, 307, 308}:
                return response
            if hop == self.limits.max_redirects or not response.headers.get("location"):
                raise FetchError("redirect_limit")
            current = self._redirect_target(current, response.headers["location"], domain)
        raise FetchError("redirect_limit")
