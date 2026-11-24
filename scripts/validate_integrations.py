"""Explicit bounded search/Groq checks. No imports of Gmail or sending operations."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from agent.compliance import compliance_is_current
from agent.config import load_settings
from agent.db import Database
from agent.personalize import GroqPersonalizer, build_context, build_message, validate_slots
from agent.pipeline import Pipeline, RunConfig, create_run
from agent.qualify import current_qualification, verified_faqs
from agent.settings_store import effective_settings
from agent.source_context import persisted_pages

NICHES = (
    "Watercolor courses",
    "Photography courses",
    "Music courses",
    "Language-learning courses",
    "Professional coaching courses",
)


def discovery_report(db, snapshot):
    records = []
    for lead_id in snapshot["lead_ids"]:
        lead = db.get_lead(lead_id)
        pages = persisted_pages(db, lead_id)
        faqs, conflicts = verified_faqs(pages, db.sources(lead_id))
        qualification = current_qualification(db, lead_id)
        records.append(
            {
                "lead_id": lead_id,
                "business": lead.business or lead.domain,
                "website": lead.website,
                "courses": [o.model_dump() for p in pages for o in p.courses],
                "public_contacts": [o.model_dump() for p in pages for o in p.emails],
                "faq_count": len(faqs),
                "faq_conflicts": bool(conflicts),
                "faq_quality": "sufficient_count" if len(faqs) >= 5 else "limited_or_missing",
                "score": lead.score,
                "qualification": qualification.model_dump(mode="json") if qualification else None,
                "confirmed_course_offer": bool(
                    qualification and qualification.criteria["relevant_course"].points == 25
                ),
                "valid_contact_source": bool(
                    qualification and qualification.criteria["direct_email"].points == 15
                ),
                "evidence_ready": bool(qualification and qualification.qualified),
                "source_urls": list(db.sources(lead_id)),
                "chatbot_evidence": [o.model_dump() for p in pages for o in p.chatbots],
                "compliance_blockers": []
                if compliance_is_current(db, lead_id)
                else ["Current human compliance review required"],
            }
        )
    return {
        "run_id": snapshot["id"],
        "status": snapshot["status"],
        "businesses": records,
        "step_statuses": [
            {
                "stage": s["stage"],
                "status": s["status"],
                "target": s["target"],
                "result": s["result"],
            }
            for s in snapshot["steps"]
        ],
        "search_requests": sum(s["attempts"] for s in snapshot["steps"] if s["stage"] == "search"),
        "crawl_requests": sum(
            s["result"].get("requests", 0) for s in snapshot["steps"] if s["stage"] == "scrape"
        ),
    }


def validate_groq(db, lead_id, settings, generator=None):
    """Validate without saving drafts/approvals; production build_message enforces all gates."""
    generator = generator or GroqPersonalizer(settings)
    result = {
        "lead_id": lead_id,
        "structured_valid": False,
        "evidence_valid": False,
        "template_valid": False,
        "unsupported_claim_rejected": None,
        "latency_seconds": None,
        "tokens": None,
        "failure_stage": None,
    }
    stage = "readiness"
    try:
        if not compliance_is_current(db, lead_id):
            result["outcome"] = "blocked_compliance"
            return result
        lead = db.get_lead(lead_id)
        qualification = current_qualification(db, lead_id)
        if (
            lead.stage != "qualified"
            or not qualification
            or not qualification.qualified
            or lead.email_status != "domain_mail_capable"
            or not settings.postal_address.strip()
        ):
            result["outcome"] = "blocked_draft_readiness"
            return result
        stage = "context"
        context = build_context(db, lead_id)
        stage = "provider_or_structure"
        slots = generator.generate(context)
        result.update(generator.last_metrics)
        stage = "evidence"
        validate_slots(slots, context, db.sources(lead_id))
        result["evidence_valid"] = True
        stage = "template"
        build_message(db, lead_id, slots, settings)
        result["template_valid"] = True
        bad = slots.model_copy(update={"hook_line": "An unsupported invented claim."})
        try:
            validate_slots(bad, context, db.sources(lead_id))
            result["unsupported_claim_rejected"] = False
        except ValueError:
            result["unsupported_claim_rejected"] = True
        result["outcome"] = "validated"
    except Exception:  # noqa: BLE001 -- never include provider output or exception strings
        result.update(getattr(generator, "last_metrics", {}))
        result["failure_stage"] = stage
        result["outcome"] = "provider_or_validation_failed"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["search", "groq"])
    parser.add_argument("--live", action="store_true", help="Explicitly allow provider calls")
    parser.add_argument("--niche", choices=NICHES, default=NICHES[0])
    parser.add_argument(
        "--database", type=Path, default=Path("data/integration-validation.sqlite3")
    )
    parser.add_argument("--lead-id", type=int, action="append", default=[])
    args = parser.parse_args()
    if not args.live:
        print(
            json.dumps(
                {
                    "status": "not_run",
                    "niches": NICHES,
                    "max_candidates": 10,
                    "reason": "Use --live for explicit bounded provider validation",
                }
            )
        )
        return
    settings = load_settings()
    key = settings.search_api_key if args.mode == "search" else settings.groq_api_key
    if not key or not key.get_secret_value().strip():
        print(json.dumps({"status": "not_run", "reason": "Provider credential unavailable"}))
        return
    if len(args.lead_id) > 3 or len(set(args.lead_id)) != len(args.lead_id):
        parser.error("Select at most three distinct reviewed leads")
    if args.mode == "search":
        with Database(args.database) as db:
            db.migrate()
            settings = effective_settings(db, settings)
            config = RunConfig(
                niche=args.niche,
                provider=settings.search_provider,
                max_businesses=10,
                results_per_query=5,
                max_pages=4,
            )
            run_id = create_run(db, config, "audit-" + uuid4().hex)
            print(json.dumps(discovery_report(db, Pipeline(db, settings).run(run_id)), indent=2))
        return
    # Synthetic evidence and review live only in this disposable in-memory database.
    from scripts.dashboard_smoke import seed_demo

    with Database(":memory:") as fixture:
        fixture.migrate()
        seeded = seed_demo(fixture)
        synthetic_settings = settings.model_copy(
            update={"postal_address": "123 Example Street, Test City"}
        )
        synthetic = validate_groq(fixture, seeded["lead_id"], synthetic_settings)
    report = {"synthetic": synthetic, "reviewed_leads": []}
    if synthetic.get("outcome") == "validated" and args.lead_id:
        with Database(args.database, read_only=True) as db:
            settings = effective_settings(db, settings)
            report["reviewed_leads"] = [validate_groq(db, i, settings) for i in args.lead_id]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- prevent raw config/provider exceptions in terminal logs
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "Check local configuration and database; details withheld",
                }
            )
        )
        raise SystemExit(1) from None
