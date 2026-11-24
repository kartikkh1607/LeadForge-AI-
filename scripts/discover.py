"""Explicit CLI for Phase 2 only; discovers and optionally scrapes new leads."""

import argparse

from agent.config import load_settings
from agent.db import Database
from agent.scrape import crawl_lead
from agent.search import SearchClient, discover


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover leads; never drafts or sends emails")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--niche")
    group.add_argument("--lead-id", type=int, help="Resume an existing new lead")
    parser.add_argument("--scrape", action="store_true", help="Scrape discovered leads")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    with Database(settings.database_path) as db:
        db.migrate()
        if args.niche:
            result = discover(db, SearchClient(settings), args.niche)
            print(result.model_dump_json(indent=2))
            lead_ids = result.lead_ids if args.scrape else []
        else:
            lead_ids = [args.lead_id]
        for lead_id in lead_ids:
            result = crawl_lead(db, lead_id, retry_failed=args.retry_failed)
            print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
