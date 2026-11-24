"""Read persisted observations only, with source freshness and provenance checks."""

import hashlib
import json
from typing import TYPE_CHECKING

from agent.evidence import normalized_text
from agent.verify import normalize_domain
from agent.web_models import PageData

if TYPE_CHECKING:
    from agent.db import Database


def context_hash(db: "Database", lead_id: int) -> str:
    lead = db.get_lead(lead_id)
    data = {
        "lead": {
            key: getattr(lead, key)
            for key in ("domain", "email", "niche", "name", "business", "has_chatbot")
        },
        "sources": [
            dict(row)
            for row in db.connection.execute(
                "SELECT url,text,fetched_at FROM source_evidence WHERE lead_id=? ORDER BY url",
                (lead_id,),
            )
        ],
        "pages": [
            row["result_json"]
            for row in db.connection.execute(
                "SELECT result_json FROM crawl_pages WHERE lead_id=? AND state='ok' ORDER BY requested_url",
                (lead_id,),
            )
        ],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def persisted_pages(db: "Database", lead_id: int) -> list[PageData]:
    lead = db.get_lead(lead_id)
    sources = db.sources(lead_id)
    pages = {}
    for row in db.crawl_records(lead_id).values():
        if row["state"] != "ok":
            continue
        page = PageData.model_validate_json(row["result_json"])
        if (
            normalize_domain(page.url) == lead.domain
            and page.url in sources
            and normalized_text(page.text) == normalized_text(sources[page.url])
        ):
            pages[page.url] = page
    return list(pages.values())


def quoted(quote: str, url: str, sources: dict[str, str]) -> bool:
    return bool(normalized_text(quote)) and normalized_text(quote) in normalized_text(
        sources.get(url, "")
    )
