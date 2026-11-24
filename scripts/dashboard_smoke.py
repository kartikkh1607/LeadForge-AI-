"""Offline dashboard walkthrough fixture. Never load credentials or contact websites."""

import argparse
import json
from pathlib import Path

import httpx
from groq import Groq

from agent.config import Settings
from agent.dashboard import Dashboard, selected_slots
from agent.db import Database
from agent.personalize import GroqPersonalizer
from agent.pipeline import Pipeline, RunConfig, create_run
from agent.scrape import extract_page
from agent.settings_store import preferences_for, save_preferences
from agent.verify import VerificationResult
from agent.web_models import CrawlResult, SearchHit


def seed_demo(db):
    settings = Settings(postal_address="123 Example Street, Test City", search_api_key="offline")
    url = "https://willow-art.com/"

    class Search:
        def search(self, query):
            return [SearchHit(url=url, query=query)]

    def crawl(database, lead_id, fetcher, retry_failed=False):
        html = (
            Path(__file__).resolve().parents[1] / "tests/fixtures/qualified_creator.html"
        ).read_text(encoding="utf-8")
        page = extract_page(html, url)
        database.save_crawl_page(lead_id, url, page=page)
        database.apply_scraped_facts(lead_id, [page])
        return CrawlResult(lead_id=lead_id, pages=[page])

    run_id = create_run(db, RunConfig(niche="watercolor", query_count=1), "offline-demo")
    result = Pipeline(
        db,
        settings,
        search_factory=lambda s: Search(),
        crawler=crawl,
        verifier=lambda *a, **kw: VerificationResult(
            "domain_mail_capable", "Synthetic DNS fixture"
        ),
    ).run(run_id)
    lead_id = result["lead_ids"][0]
    save_preferences(db, settings, preferences_for(settings).model_dump())
    service = Dashboard(db, settings)
    service.compliance_review(
        lead_id,
        service.review_token(lead_id),
        "fixture-compliance",
        {
            "country": "US",
            "subscriber_type": "company",
            "contact_source_url": url,
            "reviewed_by": "Synthetic fixture reviewer",
            "country_evidence": "United States",
            "subscriber_evidence": "Willow Art School LLC",
            "supporting_evidence": [
                {"claim": q, "quote": q, "url": url}
                for q in ("United States", "Willow Art School LLC")
            ],
        },
    )
    slots = selected_slots(service.inspect(lead_id)["slot_choices"], 0, 0, 1, 1)

    def response(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(slots)},
                    }
                ]
            },
        )

    with Groq(
        api_key="offline",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(response)),
    ) as client:
        message_id = service.generate(
            lead_id,
            service.review_token(lead_id),
            "fixture-draft",
            GroqPersonalizer(settings, client),
        )
    assert db.get_message(message_id).status == "draft"
    assert not any(row["sendable"] for row in service.send_preview()["messages"])
    assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()
    return {
        "run_id": run_id,
        "lead_id": lead_id,
        "message_id": message_id,
        "score": db.get_lead(lead_id).score,
        "status": "draft",
        "network_requests": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, help="Create a NEW isolated fixture database for UI walkthrough"
    )
    args = parser.parse_args()
    if args.database and args.database.exists():
        parser.error(
            "Fixture path already exists. Choose a new path; existing data is never overwritten."
        )
    with Database(args.database or ":memory:") as db:
        db.migrate()
        db.migrate()
        print(json.dumps(seed_demo(db), indent=2))
    print("Offline dashboard smoke passed. Synthetic fixtures only; no emails or network requests.")


if __name__ == "__main__":
    main()
