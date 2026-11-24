"""Conservative implementation of project policy, not a legal determination."""

import re
from typing import TYPE_CHECKING

from agent.evidence import evidence_matches
from agent.models import Compliance, utc_now
from agent.source_context import context_hash, persisted_pages

if TYPE_CHECKING:
    from agent.db import Database


def evaluate_compliance(record: Compliance) -> tuple[bool, str]:
    if not record.reviewed_at or not record.reviewed_by.strip():
        return False, "Human review is required"
    if not record.contact_source_url.startswith(("https://", "http://")):
        return False, "Contact source URL required"
    if not record.country_evidence.strip() or not record.subscriber_evidence.strip():
        return False, "Country and subscriber evidence required"
    if record.subscriber_type == "unknown":
        return False, "Unknown subscriber type"
    country = (record.country or "").strip().upper()
    if country == "US":
        return True, "Reviewed US recipient; message safeguards still required"
    if country in {"GB", "UK"}:
        return (
            (True, "Reviewed UK company")
            if record.subscriber_type == "company"
            else (False, "UK individual/sole trader excluded")
        )
    if country in {"CA", "AU"}:
        if (
            record.conspicuously_published
            and record.no_unsolicited_statement is False
            and record.role_relevant
        ):
            return True, "Reviewed published business contact with relevant role"
        return False, "Publication, no refusal statement, and role relevance required"
    return False, "Unknown or unsupported country (including EU): excluded in v1"


def validate_review_sources(db: "Database", record: Compliance) -> None:
    sources = db.sources(record.lead_id)
    if record.source_hash != context_hash(db, record.lead_id):
        raise ValueError("Compliance source snapshot is stale")
    if not evidence_matches(record.supporting_evidence, sources):
        raise ValueError("Compliance quotes must match persisted sources")
    quotes = {item.quote for item in record.supporting_evidence}
    if record.country_evidence not in quotes or record.subscriber_evidence not in quotes:
        raise ValueError("Country and subscriber decisions require cited source quotes")
    if record.contact_source_url not in sources:
        raise ValueError("Contact source must be persisted")
    lead = db.get_lead(record.lead_id)
    published = any(
        email.value.casefold() == (lead.email or "").casefold()
        and email.source_url == record.contact_source_url
        for page in persisted_pages(db, lead.id)
        for email in page.emails
    )
    if not published:
        raise ValueError("Selected contact must be supported by the cited site page")
    if (
        not record.reviewed_at
        or record.reviewed_at.utcoffset() is None
        or record.reviewed_at > utc_now()
        or not record.reviewed_by.strip()
    ):
        raise ValueError("Explicit reviewer and nonfuture timezone-aware review timestamp required")
    if (
        (record.country or "").strip().upper() in {"UK", "GB"}
        and record.subscriber_type == "company"
        and not re.search(r"\b(?:Ltd|Limited|LLP|PLC)\b", record.subscriber_evidence, re.IGNORECASE)
    ):
        raise ValueError("UK company review requires explicit Ltd/LLP/PLC evidence")
    if (record.country or "").strip().upper() in {"CA", "AU"} and record.role_relevant:
        if not record.role_evidence.strip() or record.role_evidence not in quotes:
            raise ValueError("Role relevance requires a source quote and human review")
        if re.search(
            r"no unsolicited|do not send unsolicited|no marketing emails",
            " ".join(sources.values()),
            re.IGNORECASE,
        ):
            raise ValueError("Published refusal conflicts with eligibility assertion")


def review_compliance(db: "Database", record: Compliance) -> Compliance:
    """Explicit human review only; discovery and LLM output never call this automatically."""
    record = record.model_copy(update={"source_hash": context_hash(db, record.lead_id)})
    validate_review_sources(db, record)
    return db.save_compliance(record)


def compliance_is_current(db: "Database", lead_id: int) -> bool:
    record = db.get_compliance(lead_id)
    lead = db.get_lead(lead_id)
    if (
        not record
        or not record.eligible
        or not evaluate_compliance(record)[0]
        or record.assessed_email != lead.email
        or record.assessed_domain != lead.domain
        or db.is_suppressed(lead.email, lead.domain)
    ):
        return False
    try:
        validate_review_sources(db, record)
    except ValueError:
        return False
    return True
