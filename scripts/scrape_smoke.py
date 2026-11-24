"""Controlled two-page-per-site smoke test. --live is explicit; default is fixtures."""

import argparse
import json
from pathlib import Path

import httpx

from agent.db import Database
from agent.models import Lead
from agent.safe_http import CrawlLimits, SafeFetcher
from agent.scrape import crawl_lead
from agent.verify import normalize_domain


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("data/phase2-smoke.json"))
    args = parser.parse_args()
    sites = (
        json.loads(Path("scripts/smoke_sites.json").read_text())
        if args.live
        else ["https://willow-art.com/", "https://studio-two.com/", "https://studio-three.com/"]
    )
    html = Path("tests/fixtures/creator.html").read_text(encoding="utf-8")

    def mock(request):
        body = "User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else html
        return httpx.Response(
            200,
            headers={
                "content-type": "text/plain" if request.url.path == "/robots.txt" else "text/html"
            },
            stream=httpx.ByteStream(body.encode()),
        )

    reports = []
    with Database(":memory:") as db:
        db.migrate()
        for site in sites:
            lead = db.add_lead(Lead(domain=normalize_domain(site), website=site))
            options = (
                {}
                if args.live
                else {
                    "resolver": lambda host: ["93.184.215.14"],
                    "transport_factory": lambda: httpx.MockTransport(mock),
                    "sleep": lambda seconds: None,
                }
            )
            result = crawl_lead(db, lead.id, SafeFetcher(CrawlLimits(max_pages=2), **options))
            reports.append({"site": site, **result.model_dump(mode="json")})
            print(
                f"{site}: {len(result.pages)} pages, {result.requests} requests, "
                f"{len(result.errors)} errors"
            )
    report = {"mode": "live" if args.live else "fixtures", "sites": reports}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
