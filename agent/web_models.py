"""Typed discovery/extraction records; every observation retains its source."""

from datetime import datetime

from pydantic import Field

from agent.models import Model, utc_now


class Observation(Model):
    value: str
    source_url: str
    quote: str
    method: str
    landing_url: str | None = None
    enrollment_text: str | None = None
    enrollment_url: str | None = None
    evidence_classification: str = "observation"
    supporting_text: list[str] = Field(default_factory=list)


class FAQ(Model):
    question: str
    answer: str
    source_url: str


class PageData(Model):
    url: str
    fetched_at: datetime = Field(default_factory=utc_now)
    text: str = ""
    business_names: list[Observation] = Field(default_factory=list)
    owner_names: list[Observation] = Field(default_factory=list)
    emails: list[Observation] = Field(default_factory=list)
    faqs: list[FAQ] = Field(default_factory=list)
    courses: list[Observation] = Field(default_factory=list)
    chatbots: list[Observation] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


class CrawlResult(Model):
    lead_id: int
    pages: list[PageData] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    cached_pages: int = 0
    requests: int = 0


class SearchHit(Model):
    url: str
    title: str = ""
    query: str


class DiscoveryResult(Model):
    lead_ids: list[int] = Field(default_factory=list)
    added: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
