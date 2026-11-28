"""Niche search adapters. Provider snippets are discovery hints, never source evidence."""

import json
import re
import sqlite3
from collections.abc import Sequence

import httpx

from agent.config import Settings
from agent.db import Database
from agent.models import Lead, utc_now
from agent.safe_http import FetchError, public_url
from agent.verify import normalize_domain
from agent.web_models import DiscoveryResult, SearchHit

# Ordered best-first: a run with query_count=N uses the first N. Each targets an
# independent creator's own sales/FAQ page rather than marketplaces or blogs.
QUERY_TEMPLATES = (
    '{niche} online course "frequently asked questions"',
    '{niche} "self-paced" course "enroll"',
    '{niche} online course "lifetime access" FAQ',
    '{niche} "online class" "how long do I have access"',
    '{niche} membership "online lessons" FAQ',
    '{niche} course "taught by" "enroll now"',
)
BLOCKED_DOMAINS = (
    "udemy.com",
    "skillshare.com",
    "coursera.org",
    "domestika.org",
    "masterclass.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "etsy.com",
    "amazon.com",
    "amazon.co.uk",
    "amazon.in",
    "amazon.ca",
    "amazon.com.au",
    "instagram.com",
    "pinterest.com",
    "reddit.com",
    "quora.com",
    "medium.com",
    "linkedin.com",
    "eventbrite.com",
    "classcentral.com",
    # Job boards, news and listicle publishers rank for "online course" queries but
    # are never independent course creators.
    "indeed.com",
    "glassdoor.com",
    "businessinsider.com",
    "forbes.com",
    "nytimes.com",
    "theguardian.com",
    "wikihow.com",
    "wikipedia.org",
    "thesprucecrafts.com",
    "creativebloq.com",
    "artistsnetwork.com",
    "craftsy.com",
    "bluprint.com",
)


class SearchError(ValueError):
    pass


def excluded_domain(domain: str, settings: Settings) -> bool:
    """Exact host rules preserve tenant distinctions; tree rules use label boundaries."""
    hosts = {normalize_domain(v) for v in settings.search_excluded_hosts.split(",") if v.strip()}
    trees = set(BLOCKED_DOMAINS) | {
        normalize_domain(v) for v in settings.search_excluded_domains.split(",") if v.strip()
    }
    return domain in hosts or any(domain == d or domain.endswith("." + d) for d in trees)


class SearchClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings, self.transport = settings, transport

    def search(self, query: str) -> list[SearchHit]:
        key = self.settings.search_api_key
        if key is None or not key.get_secret_value().strip():
            raise SearchError("SEARCH_API_KEY is required")
        if not query.strip() or len(query) > 400:
            raise SearchError("Query must contain 1–400 characters")
        provider = self.settings.search_provider
        if provider == "tavily":
            endpoint = "https://api.tavily.com/search"
            headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
            # Tavily ignores Google-style "site:"; translate it to include_domains.
            sites = re.findall(r"\bsite:([\w.-]+)", query)
            plain_query = re.sub(r"\s*\bsite:[\w.-]+", "", query).strip() or query
            payload = {
                "query": plain_query,
                "max_results": self.settings.search_results_per_query,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            }
            if sites:
                payload["include_domains"] = sites
            field, url_key = "results", "url"
        else:
            endpoint = "https://google.serper.dev/search"
            headers = {"X-API-KEY": key.get_secret_value()}
            payload = {"q": query, "num": self.settings.search_results_per_query}
            field, url_key = "organic", "link"
        try:
            with (
                httpx.Client(
                    transport=self.transport, trust_env=False, timeout=10, follow_redirects=False
                ) as client,
                client.stream("POST", endpoint, headers=headers, json=payload) as response,
            ):
                if response.status_code != 200:
                    raise SearchError(f"{provider}: HTTP {response.status_code}")
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > 2_000_000:
                        raise SearchError("Search response too large")
                    body.extend(chunk)
                data = json.loads(body)
            if not isinstance(data, dict) or not isinstance(data.get(field), list):
                raise SearchError("Malformed search response")
            return [
                SearchHit(
                    url=item[url_key],
                    title=item.get("title", "") if isinstance(item.get("title", ""), str) else "",
                    query=query,
                )
                for item in data[field][: self.settings.search_results_per_query]
                if isinstance(item, dict) and isinstance(item.get(url_key), str)
            ]
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, SearchError):
                raise
            # Never persist provider response bodies or request headers containing credentials.
            raise SearchError(f"{provider}: {type(exc).__name__}") from None


def discover(
    db: Database,
    client: SearchClient,
    niche: str,
    templates: Sequence[str] = QUERY_TEMPLATES,
    *,
    settings: Settings | None = None,
) -> DiscoveryResult:
    if not niche.strip() or len(niche) > 200 or not 1 <= len(templates) <= 10:
        raise ValueError("Provide a niche up to 200 characters and 1–10 query templates")
    result = DiscoveryResult()
    settings = settings or getattr(client, "settings", Settings())

    def record(hit, query, outcome, lead_id=None):
        # Only public, credential-free URLs pass here. Snippets never become evidence.
        with db.connection:
            db.connection.execute(
                "INSERT INTO search_observations "
                "(lead_id,provider,query,result_url,title,outcome,observed_at) VALUES (?,?,?,?,?,?,?)",
                (
                    lead_id,
                    settings.search_provider,
                    query,
                    hit.url,
                    hit.title,
                    outcome,
                    utc_now().isoformat(),
                ),
            )

    seen = set()
    for template in templates:
        try:
            query = template.format(niche=niche.strip())
            hits = client.search(query)
        except (SearchError, KeyError, ValueError) as exc:
            result.errors.append(
                str(exc) if isinstance(exc, SearchError) else "Invalid query template"
            )
            continue
        for hit in hits:
            try:
                url = public_url(hit.url)
                domain = normalize_domain(url)
            except (FetchError, ValueError):
                result.skipped += 1
                continue
            if excluded_domain(domain, settings) or db.is_suppressed(None, domain):
                record(hit, query, "excluded_or_suppressed")
                result.skipped += 1
                continue
            if domain in seen:
                existing = db.connection.execute(
                    "SELECT id FROM leads WHERE domain=?", (domain,)
                ).fetchone()
                record(hit, query, "duplicate", existing[0] if existing else None)
                result.duplicates += 1
                continue
            seen.add(domain)
            existing = db.connection.execute(
                "SELECT id FROM leads WHERE domain=?", (domain,)
            ).fetchone()
            if existing:
                record(hit, query, "historical_duplicate", existing["id"])
                result.duplicates += 1
                result.lead_ids.append(existing["id"])
                continue
            try:
                lead = db.add_lead(
                    Lead(
                        domain=domain,
                        website=url,
                        niche=niche.strip(),
                        source_query=query,
                        source_url=url,
                    )
                )
            except sqlite3.IntegrityError:
                # Another process may have discovered the same business concurrently.
                result.duplicates += 1
                continue
            result.lead_ids.append(lead.id)
            record(hit, query, "added", lead.id)
            result.added += 1
    return result
