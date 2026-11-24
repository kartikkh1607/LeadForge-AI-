"""Three synthetic drafts through the real Groq SDK with an offline HTTP transport."""

import json
from pathlib import Path

import httpx
from groq import Groq

from agent.compliance import review_compliance
from agent.config import Settings
from agent.db import Database
from agent.models import Compliance, Evidence, Lead, utc_now
from agent.personalize import GroqPersonalizer, Slots, personalize_lead
from agent.qualify import qualify_lead
from agent.scrape import extract_page
from agent.verify import VerificationResult


def main() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "tests/fixtures/qualified_creator.html"
    ).read_text(encoding="utf-8")
    settings = Settings(postal_address="123 Example Street, Test City")
    for variant in ("initial", "upgrade", "unnamed"):
        url = "https://willow-art.com/"
        content = html
        if variant == "upgrade":
            content += '<script src="https://code.tidio.co/test.js"></script>'
        if variant == "unnamed":
            content = content.replace('itemprop="name"', 'class="unidentified"')
        page = extract_page(content, url)
        with Database(":memory:") as db:
            db.migrate()
            lead = db.add_lead(Lead(domain="willow-art.com", niche="watercolor"))
            db.save_crawl_page(lead.id, url, page=page)
            db.apply_scraped_facts(lead.id, [page])
            db.save_verification(lead.id, VerificationResult("domain_mail_capable", "Fixture MX"))
            qualification = qualify_lead(db, lead.id)
            review_compliance(
                db,
                Compliance(
                    lead_id=lead.id,
                    country="US",
                    subscriber_type="company",
                    contact_source_url=url,
                    reviewed_by="Synthetic fixture reviewer",
                    reviewed_at=utc_now(),
                    country_evidence="United States",
                    subscriber_evidence="Willow Art School LLC",
                    supporting_evidence=[
                        Evidence(claim=q, quote=q, url=url)
                        for q in ("United States", "Willow Art School LLC")
                    ],
                ),
            )
            claims = ["Start today.", "When can I start?", "How long is access?"]
            first_name = None if variant == "unnamed" else "Ada"
            slots = Slots(
                first_name=first_name,
                hook_line=claims[0],
                q1=claims[1],
                q2=claims[2],
                evidence=[
                    Evidence(claim=q, quote=q, url=url)
                    for q in claims + ([first_name] if first_name else [])
                ],
            )

            payload = slots.model_dump_json()

            def respond(request, payload=payload):
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": payload,
                                },
                            }
                        ]
                    },
                )

            with Groq(
                api_key="offline-fixture",
                max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond)),
            ) as client:
                message = personalize_lead(
                    db, lead.id, settings, GroqPersonalizer(settings, client)
                )
            assert message.status == "draft" and message.sent_at is None
            assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            print(
                json.dumps(
                    {
                        "fixture_variant": variant,
                        "qualification": qualification.model_dump(mode="json"),
                        "message": message.model_dump(mode="json"),
                        "body_words": len(message.body.split()),
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
