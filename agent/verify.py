"""Syntax and DNS checks only; no SMTP probing or mailbox-existence claims."""

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import dns.exception
import dns.resolver
from email_validator import EmailNotValidError, validate_email


def normalize_domain(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value if "://" in value else "//" + value)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise ValueError("Only HTTP(S) domain URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials are not a domain")
    host = parsed.hostname
    if not host:
        raise ValueError("Missing domain")
    # Access port to reject malformed ports too.
    _ = parsed.port
    host = host.rstrip(".").encode("idna").decode("ascii").lower()
    host = host.removeprefix("www.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("A business domain must not be an IP address")
    if len(host) > 253 or "." not in host:
        raise ValueError("Expected a fully qualified domain")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in host.split(".")):
        raise ValueError("Invalid domain labels")
    return host


def normalize_email(value: str) -> str:
    """Conservative v1 ASCII local parts; preserve local-part case in storage."""
    try:
        return validate_email(
            value.strip(), check_deliverability=False, allow_smtputf8=False
        ).ascii_email
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc


class Resolver(Protocol):
    def resolve(self, qname: str, rdtype: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class VerificationResult:
    # None means no definitive status: retain the previous stored email_status.
    status: str | None
    reason: str
    retryable: bool = False


def verify_email(
    email: str, resolver: Resolver | None = None, timeout: float = 5
) -> VerificationResult:
    try:
        email = normalize_email(email)
    except ValueError as exc:
        return VerificationResult("invalid", str(exc))
    domain = email.rsplit("@", 1)[1]
    resolver = resolver if resolver is not None else dns.resolver.Resolver()
    try:
        try:
            records = list(resolver.resolve(domain, "MX", lifetime=timeout, search=False))
        except dns.resolver.NoAnswer:
            records = []
        if any(str(record.exchange) == "." for record in records):
            return VerificationResult("invalid", "Null MX: domain does not accept mail")
        if records:
            return VerificationResult("domain_mail_capable", "MX found; mailbox unconfirmed")
        # RFC 5321 implicit MX supports both IPv4 and IPv6.
        for record_type in ("A", "AAAA"):
            try:
                if list(resolver.resolve(domain, record_type, lifetime=timeout, search=False)):
                    return VerificationResult(
                        "domain_mail_capable", "Implicit MX address found; mailbox unconfirmed"
                    )
            except dns.resolver.NoAnswer:
                continue
        return VerificationResult("invalid", "No MX or address records")
    except dns.resolver.NXDOMAIN:
        return VerificationResult("invalid", "Domain does not exist")
    except (dns.exception.DNSException, OSError) as exc:
        return VerificationResult(None, f"DNS lookup incomplete: {type(exc).__name__}", True)
