"""Pure source matching helpers; scraped text is data only."""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence

from agent.models import Evidence, Message


def draft_hash(
    subject: str, body: str, evidence_json: str, personalization_json: str | None
) -> str:
    data = [
        subject,
        body,
        json.loads(evidence_json),
        json.loads(personalization_json) if personalization_json else None,
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def normalized_text(text: str) -> str:
    return " ".join(text.casefold().split())


def evidence_matches(evidence: Sequence[Evidence], sources: Mapping[str, str]) -> bool:
    return bool(evidence) and all(
        bool(normalized_text(item.quote))
        and normalized_text(item.quote) in normalized_text(sources.get(item.url, ""))
        for item in evidence
    )


def draft_evidence_matches(message: Message, sources: Mapping[str, str]) -> bool:
    """Require a sourced hook and two sourced questions, never confidence alone."""
    if not evidence_matches(message.evidence, sources):
        return False
    if any(normalized_text(item.claim) != normalized_text(item.quote) for item in message.evidence):
        return False
    if not message.hook_line.strip() or len(message.example_questions) != 2:
        return False
    if len(set(message.example_questions)) != 2:
        return False
    claims = {normalized_text(item.claim) for item in message.evidence}
    return all(
        normalized_text(claim) in claims
        for claim in (message.hook_line, *message.example_questions)
    ) and any(
        item.claim == message.hook_line
        and item.quote == message.hook_evidence
        and item.url == message.hook_source_url
        for item in message.evidence
    )


def validate_draft(subject: str, body: str) -> None:
    if not subject.strip() or not body.strip():
        raise ValueError("Subject and body required")
    if any(char in subject + body for char in ("\u2014", "[", "]", "{", "}", "<", ">")):
        raise ValueError("Draft contains prohibited punctuation or placeholders")
    if "\n" in subject or "\r" in subject:
        raise ValueError("Subject must not contain header newlines")
    if len(body.split()) >= 170:
        raise ValueError("Body must be under 170 words")
    if len(re.findall(r"(?:https?://|www\.)\S+", body, flags=re.IGNORECASE)) > 1:
        raise ValueError("At most one link allowed")
