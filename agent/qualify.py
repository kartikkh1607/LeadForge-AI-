"""Conservative, auditable 25/25/20/15/15 heuristic; not a conversion predictor."""

import re
from datetime import date, datetime
from typing import TYPE_CHECKING
from urllib.parse import unquote

from pydantic import Field

from agent.evidence import normalized_text
from agent.models import Evidence, Lead, Model, utc_now
from agent.source_context import context_hash, persisted_pages, quoted
from agent.web_models import FAQ, PageData

if TYPE_CHECKING:
    from agent.db import Database

WEIGHTS = {
    "relevant_course": 25,
    "five_faqs": 25,
    "support_faqs": 20,
    "direct_email": 15,
    "recent_activity": 15,
}
THRESHOLD = 60
NICHE_FILLER = frozenset(
    {
        "course",
        "courses",
        "online",
        "learn",
        "learning",
        "class",
        "classes",
        "lesson",
        "lessons",
        "workshop",
        "workshops",
        "program",
        "programs",
        "programme",
        "training",
        "school",
        "professional",
        "beginner",
        "beginners",
        "advanced",
        "intermediate",
        "art",
        "arts",
        "the",
        "a",
        "an",
        "and",
        "for",
        "of",
        "to",
        "in",
        "with",
    }
)
SUPPORT = re.compile(
    r"\b(support|enroll\w*|enrol\w*|log\s?in|access|help|assistance|refund)\b", re.IGNORECASE
)
NEGATIVE = re.compile(
    r"no longer (?:offer|sell)|(?:courses?|enrollment|enrolment) (?:is |are )?"
    r"(?:closed|unavailable|discontinued)|not (?:selling|offering)",
    re.IGNORECASE,
)


class Criterion(Model):
    points: int
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)


class Qualification(Model):
    lead_id: int
    score: int
    qualified: bool
    criteria: dict[str, Criterion]
    blockers: list[str] = Field(default_factory=list)
    context_hash: str = ""
    assessed_at: datetime = Field(default_factory=utc_now)
    policy_version: int = 1
    relevant_course_business: bool = False
    grounded_personalization: bool = False


def verified_faqs(pages: list[PageData], sources: dict[str, str]) -> tuple[list[FAQ], list[str]]:
    grouped = {}
    for page in pages:
        for faq in page.faqs:
            if (
                faq.source_url == page.url
                and faq.question.strip().endswith("?")
                and quoted(faq.question, faq.source_url, sources)
                and quoted(faq.answer, faq.source_url, sources)
            ):
                grouped.setdefault(normalized_text(faq.question), []).append(faq)
    conflicts = []
    chosen = []
    for q, values in grouped.items():
        answers = sorted({normalized_text(v.answer) for v in values}, key=len)
        longest = answers[-1]
        # The same FAQ captured on two pages, one answer cut shorter (e.g. an accordion
        # teaser vs the full FAQ page), is not a contradiction: every answer must be
        # contained in the longest one. Genuinely different answers still conflict.
        if all(a in longest for a in answers):
            chosen.append(next(v for v in values if normalized_text(v.answer) == longest))
        else:
            conflicts.append(q)
    return chosen, conflicts


def evaluate_qualification(
    lead: Lead,
    pages: list[PageData],
    sources: dict[str, str],
    *,
    today: date,
    suppressed: bool = False,
) -> Qualification:
    criteria = {key: Criterion(points=0, reason="No verified positive evidence") for key in WEIGHTS}
    blockers = []
    text = " ".join(sources.values())
    negative = bool(NEGATIVE.search(text))
    terms = set(re.findall(r"\w+", lead.niche.casefold())) - NICHE_FILLER
    # Multi-word niches ("professional coaching") still need their core subject word;
    # modifiers like "professional" or "beginner" rarely appear in a course's own copy.
    terms = terms or set(re.findall(r"\w+", lead.niche.casefold())) - {
        "course",
        "courses",
        "online",
        "the",
        "a",
        "an",
        "and",
        "for",
        "of",
        "to",
        "in",
        "with",
    }
    course_evidence = []
    relevant_business = False
    for page in pages:
        for course in page.courses:
            spans = course.supporting_text or [course.quote]
            value = normalized_text(" ".join(spans))
            supported = (
                course.source_url == page.url
                and quoted(course.value, page.url, sources)
                and all(quoted(span, page.url, sources) for span in spans)
            )
            relevant = bool(supported and terms and terms <= set(re.findall(r"\w+", value)))
            if relevant and course.evidence_classification in {"online_course", "online_offer"}:
                relevant_business = True
            if (
                relevant
                and course.evidence_classification == "online_offer"
                and re.search(r"\b(online|self[- ]paced|videos?|recordings?|recorded)\b", value)
                and re.search(
                    r"\b(enroll|enrol|buy|purchase|join|register|sign up|add to cart)\b|[$£€]\s*\d",
                    value,
                )
            ):
                course_evidence.extend(
                    Evidence(claim="Relevant online course offered", quote=span, url=page.url)
                    for span in spans
                )
    if course_evidence and not negative:
        criteria["relevant_course"] = Criterion(
            points=25, reason="Niche, online format and offer evidenced", evidence=course_evidence
        )
    elif negative:
        criteria[
            "relevant_course"
        ].reason = "Contradictory or closed-course evidence; review required"
        blockers.append("course_availability_conflict")
        criteria["relevant_course"].evidence = [
            Evidence(claim="Conflicting course availability", quote=source, url=url)
            for url, source in sources.items()
            if NEGATIVE.search(source)
        ]
    faqs, conflicts = verified_faqs(pages, sources)
    if conflicts:
        blockers.append("contradictory_faq_answers")
    if len(faqs) >= 5 and not conflicts:
        criteria["five_faqs"] = Criterion(
            points=25,
            reason=f"{len(faqs)} distinct sourced FAQ pairs",
            evidence=[Evidence(claim=q.question, quote=q.question, url=q.source_url) for q in faqs],
        )
    else:
        criteria[
            "five_faqs"
        ].reason = f"{len(faqs)} verified FAQ pairs; five required for these points"
        criteria["five_faqs"].evidence = [
            Evidence(claim=q.question, quote=q.question, url=q.source_url) for q in faqs
        ]
    support = [q for q in faqs if SUPPORT.search(q.question)]
    if support:
        criteria["support_faqs"] = Criterion(
            points=20,
            reason="Support/enrollment/access FAQ present",
            evidence=[
                Evidence(claim=q.question, quote=q.question, url=q.source_url) for q in support
            ],
        )
    for page in pages:
        for email in page.emails:
            supported = quoted(email.value, page.url, sources)
            if email.method == "mailto" and email.quote.lower().startswith("mailto:"):
                published = unquote(email.quote[7:].split("?", 1)[0]).split(",")
                supported = email.value.casefold() in {
                    value.strip().casefold() for value in published
                }
            elif email.method == "cloudflare":
                from agent.scrape import decode_cfemail

                try:
                    supported = (
                        decode_cfemail(email.quote.split("#")[-1]).casefold()
                        == email.value.casefold()
                    )
                except ValueError:
                    supported = False
            elif email.method == "obfuscated_text":
                from agent.scrape import deobfuscate_email

                try:
                    supported = quoted(email.quote, page.url, sources) and (
                        deobfuscate_email(email.quote).casefold() == email.value.casefold()
                    )
                except ValueError:
                    supported = False
            if (
                lead.email
                and email.value.casefold() == lead.email.casefold()
                and email.source_url == page.url
                and email.method in {"text", "mailto", "cloudflare", "obfuscated_text"}
                and supported
                and lead.email_status not in {"invalid", "bounced"}
                and not suppressed
            ):
                # Mailto/CF provenance is preserved in PageData, not necessarily visible text.
                criteria["direct_email"] = Criterion(
                    points=15,
                    reason="Contact extracted from business site; not consent",
                    evidence=[Evidence(claim="Published contact", quote=email.quote, url=page.url)],
                )
    for url, source in sources.items():
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", source):
            activity = re.search(
                r"\b(?:enrollment|enrolment) (?:is )?open\b", sentence, re.IGNORECASE
            ) or re.search(
                rf"\b(?:updated|posted|published|enrollment|enrolment)\b[^.!?]{{0,60}}\b{today.year}\b",
                sentence,
                re.IGNORECASE,
            )
            if (
                activity
                and not negative
                and not re.search(r"not open|opens? next|will open", sentence, re.IGNORECASE)
            ):
                criteria["recent_activity"] = Criterion(
                    points=15,
                    reason="Explicit current activity signal",
                    evidence=[Evidence(claim="Recent activity", quote=sentence.strip(), url=url)],
                )
                break
    if suppressed:
        blockers.append("suppressed")
    if criteria["relevant_course"].points != 25:
        blockers.append("relevant_course_required")
    if criteria["direct_email"].points != 15:
        blockers.append("valid_contact_source_required")
    # Use the same safe-slot constraints as the drafting context, without calling an API.
    from agent.personalize import _safe_slot

    grounded = [f for f in faqs if _safe_slot(f.question) and _safe_slot(f.answer)]
    grounding_ready = (
        len(grounded) >= 2 and not conflicts and any(len(f.answer) <= 700 for f in grounded)
    )
    if not grounding_ready:
        blockers.append("grounded_personalization_required")
    if lead.stage not in {"new", "qualified"}:
        blockers.append("lead_lifecycle_not_eligible")
    score = sum(item.points for item in criteria.values())
    return Qualification(
        lead_id=lead.id,
        score=score,
        qualified=score >= THRESHOLD and not blockers,
        criteria=criteria,
        blockers=blockers,
        policy_version=3,
        relevant_course_business=relevant_business,
        grounded_personalization=bool(grounding_ready),
    )


def qualify_lead(db: "Database", lead_id: int, *, today: date | None = None) -> Qualification:
    lead = db.get_lead(lead_id)
    pages = persisted_pages(db, lead_id)
    result = evaluate_qualification(
        lead,
        pages,
        {page.url: page.text for page in pages},
        today=today or utc_now().date(),
        suppressed=db.is_suppressed(lead.email, lead.domain),
    )
    result.context_hash = context_hash(db, lead_id)
    with db.connection:
        db.connection.execute(
            "INSERT INTO qualifications(lead_id,result_json,context_hash,assessed_at) "
            "VALUES (?,?,?,?)",
            (
                lead_id,
                result.model_dump_json(),
                result.context_hash,
                result.assessed_at.isoformat(),
            ),
        )
        if lead.stage in {"new", "qualified"}:
            db.connection.execute(
                "UPDATE leads SET score=?,stage=? WHERE id=?",
                (result.score, "qualified" if result.qualified else "new", lead_id),
            )
            if not result.qualified:
                db.connection.execute(
                    "UPDATE messages SET status='draft',approved_at=NULL,"
                    "requires_manual_review=1 WHERE lead_id=? AND status='approved'",
                    (lead_id,),
                )
    return result


def current_qualification(db: "Database", lead_id: int) -> Qualification | None:
    row = db.connection.execute(
        "SELECT result_json FROM qualifications WHERE lead_id=? ORDER BY id DESC LIMIT 1",
        (lead_id,),
    ).fetchone()
    if not row:
        return None
    result = Qualification.model_validate_json(row["result_json"])
    return (
        result
        if result.policy_version == 3 and result.context_hash == context_hash(db, lead_id)
        else None
    )


def evidence_states(db: "Database", lead_id: int) -> dict[str, bool]:
    """Independent evidence/review states; none is a sending authorization."""
    from agent.compliance import compliance_is_current

    qualification = current_qualification(db, lead_id)
    lead = db.get_lead(lead_id)
    contact = bool(qualification and qualification.criteria["direct_email"].points == 15)
    return {
        "relevant_course_business": bool(qualification and qualification.relevant_course_business),
        "confirmed_course_offering": bool(
            qualification and qualification.criteria["relevant_course"].points == 25
        ),
        "valid_contact_source": contact,
        "contactable_business": contact and lead.email_status == "domain_mail_capable",
        "grounded_personalization": bool(qualification and qualification.grounded_personalization),
        "evidence_ready_personalization": bool(qualification and qualification.qualified),
        "compliance_approved_outreach": compliance_is_current(db, lead_id),
    }
