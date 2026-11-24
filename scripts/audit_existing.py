"""Explicit bounded re-audit of persisted candidates; no search, drafting or Gmail."""

import argparse
import hashlib
import json
from pathlib import Path

from agent.config import load_settings
from agent.db import Database
from agent.models import Lead, Suppression
from agent.qualify import evidence_states, qualify_lead, verified_faqs
from agent.safe_http import CrawlLimits, SafeFetcher
from agent.scrape import crawl_lead
from agent.settings_store import effective_settings
from agent.source_context import persisted_pages
from agent.verify import verify_email


def audit(source, destination, settings, *, fetcher_factory=SafeFetcher, verifier=verify_email):
    """Fresh evidence database preserves the prior audit and never copies approval."""
    if destination.exists():
        raise ValueError("Audit destination must be new")
    with Database(source, read_only=True) as old:
        effective = effective_settings(old, settings)
        if not effective.paused or not effective.dry_run:
            raise ValueError("Pause and dry-run must remain enabled")
        candidates = [
            old.get_lead(r[0]) for r in old.connection.execute("SELECT id FROM leads ORDER BY id")
        ]
        if not 1 <= len(candidates) <= 10:
            raise ValueError("Audit requires one to ten existing candidates")
        suppressed = [dict(r) for r in old.connection.execute("SELECT * FROM suppression")]
        excluded = {
            l.id
            for l in candidates
            if old.is_suppressed(l.email, l.domain) or l.stage not in {"new", "qualified"}
        }
    records = []
    with Database(destination) as db:
        db.migrate()
        for row in suppressed:
            db.suppress(Suppression.model_validate(row))
        for previous in candidates:
            if previous.id in excluded:
                records.append(
                    {
                        "source_lead_id": previous.id,
                        "domain": previous.domain,
                        "outcome": "suppressed_or_lifecycle_blocked",
                    }
                )
                continue
            lead = db.add_lead(
                Lead(
                    domain=previous.domain,
                    website=previous.website,
                    niche=previous.niche,
                    source_query=previous.source_query,
                    source_url=previous.source_url,
                )
            )
            result = crawl_lead(db, lead.id, fetcher_factory(limits=CrawlLimits(max_pages=4)))
            lead = db.get_lead(lead.id)
            if lead.email:
                db.save_verification(
                    lead.id, verifier(lead.email, timeout=settings.dns_timeout_seconds)
                )
            qualification = qualify_lead(db, lead.id)
            pages = persisted_pages(db, lead.id)
            faqs, conflicts = verified_faqs(pages, db.sources(lead.id))
            records.append(
                {
                    "source_lead_id": previous.id,
                    "lead_id": lead.id,
                    "domain": lead.domain,
                    "website": lead.website,
                    "states": evidence_states(db, lead.id),
                    "qualification": qualification.model_dump(mode="json"),
                    "courses": [o.model_dump() for p in pages for o in p.courses],
                    "contacts": [o.model_dump() for p in pages for o in p.emails],
                    "faqs": [f.model_dump() for f in faqs],
                    "faq_conflicts": conflicts,
                    "source_urls": list(db.sources(lead.id)),
                    "requests": result.requests,
                    "errors": result.errors,
                }
            )
        assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()
        assert not db.connection.execute("SELECT 1 FROM messages").fetchone()
    return {
        "candidates": records,
        "search_calls": 0,
        "groq_calls": 0,
        "drafts": 0,
        "crawl_requests": sum(r.get("requests", 0) for r in records),
        "source_pages": sum(len(r.get("source_urls", [])) for r in records),
        "paused": True,
        "dry_run": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/phase5b3-validation.sqlite3"))
    parser.add_argument("--database", type=Path, default=Path("data/phase5b4-validation.sqlite3"))
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"status": "not_run", "max_candidates": 10, "max_pages": 4}))
        return
    settings = load_settings()
    if not settings.paused or not settings.dry_run:
        raise ValueError("Pause and dry-run must be enabled")
    archive = args.database.parent / (args.database.stem + "-html")

    class RecordedFetcher(SafeFetcher):
        def fetch(self, url, domain):
            response = super().fetch(url, domain)
            if response.status == 200 and "html" in response.headers.get("content-type", ""):
                archive.mkdir(parents=True, exist_ok=True)
                name = hashlib.sha256(response.url.encode()).hexdigest()
                (archive / (name + ".html")).write_text(response.text, encoding="utf-8")
                (archive / (name + ".json")).write_text(
                    json.dumps({"url": response.url}), encoding="utf-8"
                )
            return response

    print(
        json.dumps(
            audit(args.source, args.database, settings, fetcher_factory=RecordedFetcher), indent=2
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- config/provider exceptions may contain secrets
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "Audit failed; verify local configuration, source and new destination",
                }
            )
        )
        raise SystemExit(1) from None
