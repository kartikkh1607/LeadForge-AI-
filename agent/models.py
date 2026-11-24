"""Validated boundary models; database constraints provide a second boundary."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.verify import normalize_domain, normalize_email


def utc_now() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EmailStatus(StrEnum):
    INVALID = "invalid"
    DOMAIN_MAIL_CAPABLE = "domain_mail_capable"
    UNCONFIRMED_MAILBOX = "unconfirmed_mailbox"
    BOUNCED = "bounced"


class LeadStage(StrEnum):
    NEW = "new"
    QUALIFIED = "qualified"
    SKIPPED = "skipped"
    CONTACTED = "contacted"
    REPLIED = "replied"
    NOT_INTERESTED = "not_interested"
    WON = "won"
    LOST = "lost"


class MessageStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    BOUNCED = "bounced"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Lead(Model):
    id: int | None = None
    name: str = ""
    business: str = ""
    domain: str
    website: str = ""
    email: str | None = None
    niche: str = ""
    source_query: str = ""
    source_url: str = ""
    faq_text: str = ""
    has_chatbot: bool = False
    score: int = Field(default=0, ge=0, le=100)
    email_status: EmailStatus = EmailStatus.UNCONFIRMED_MAILBOX
    stage: LeadStage = LeadStage.NEW
    notes: str = ""
    created_at: datetime = Field(default_factory=utc_now)

    _domain = field_validator("domain")(normalize_domain)

    @field_validator("email")
    @classmethod
    def email_valid(cls, value: str | None) -> str | None:
        return normalize_email(value) if value is not None else None

    @field_validator("faq_text")
    @classmethod
    def truncate_faq(cls, value: str) -> str:
        return value[:4000]


class Evidence(Model):
    claim: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    url: str = Field(min_length=1)


class SourceEvidence(Model):
    id: int | None = None
    lead_id: int
    url: str = Field(min_length=1)
    text: str
    fetched_at: datetime = Field(default_factory=utc_now)


class Message(Model):
    id: int | None = None
    lead_id: int
    kind: Literal["initial", "followup"] = "initial"
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    status: MessageStatus = MessageStatus.DRAFT
    rfc822_message_id: str | None = None
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    hook_line: str = ""
    hook_evidence: str = ""
    hook_source_url: str = ""
    example_questions: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    missing_information: list[str] = Field(default_factory=list)
    requires_manual_review: bool = True
    truthful_identity_confirmed: bool = False
    opt_out_confirmed: bool = False
    personalization: dict | None = None
    content_hash: str = ""
    version: int = 1


class Suppression(Model):
    id: int | None = None
    kind: Literal["email", "domain"]
    value: str
    reason: Literal["opt_out", "bounce", "spam_block", "manual"]
    created_at: datetime = Field(default_factory=utc_now)


class Compliance(Model):
    lead_id: int
    country: str | None = None
    subscriber_type: Literal["company", "sole_trader_or_individual", "unknown"] = "unknown"
    contact_source_url: str = ""
    eligible: bool = False
    reason: str = "Not assessed"
    reviewed_at: datetime | None = None
    reviewed_by: str = ""
    country_evidence: str = ""
    subscriber_evidence: str = ""
    conspicuously_published: bool = False
    no_unsolicited_statement: bool | None = None
    role_relevant: bool = False
    assessed_email: str | None = None
    assessed_domain: str | None = None
    supporting_evidence: list[Evidence] = Field(default_factory=list)
    source_hash: str = ""
    role_evidence: str = ""
