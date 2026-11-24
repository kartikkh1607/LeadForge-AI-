"""Explicit resumable discovery stages. No approval, consent inference or sending."""

import json
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.compliance import compliance_is_current
from agent.config import Settings
from agent.db import Database
from agent.models import utc_now
from agent.operations import ActionError, mutation_lock
from agent.qualify import current_qualification, qualify_lead
from agent.safe_http import CrawlLimits, SafeFetcher
from agent.scrape import crawl_lead
from agent.search import QUERY_TEMPLATES, SearchClient, discover
from agent.verify import verify_email


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    niche: str = Field(min_length=1, max_length=200)
    provider: Literal["tavily", "serper"] = "tavily"
    results_per_query: int = Field(default=5, ge=1, le=20)
    query_count: int = Field(default=3, ge=1, le=3)
    max_businesses: int = Field(default=10, ge=1, le=30)
    max_pages: int = Field(default=4, ge=1, le=8)

    @field_validator("niche")
    @classmethod
    def niche_nonblank(cls, value):
        if not value.strip():
            raise ValueError("Enter a niche")
        return value.strip()


def create_run(db: Database, config: RunConfig, submission_key: str) -> int:
    if not submission_key.strip() or len(submission_key) > 100:
        raise ActionError("A bounded submission key is required")
    with mutation_lock(db), db.connection:
        existing = db.connection.execute(
            "SELECT id,config_json FROM discovery_runs WHERE submission_key=?", (submission_key,)
        ).fetchone()
        if existing:
            if RunConfig.model_validate_json(existing["config_json"]) != config:
                raise ActionError("This submission was already used with different inputs")
            return existing["id"]
        now = utc_now().isoformat()
        run_id = db.connection.execute(
            "INSERT INTO discovery_runs(submission_key,config_json,created_at,updated_at) "
            "VALUES (?,?,?,?)",
            (submission_key, config.model_dump_json(), now, now),
        ).lastrowid
        for index in range(config.query_count):
            db.connection.execute(
                "INSERT INTO discovery_steps(run_id,stage,target,updated_at) VALUES (?,'search',?,?)",
                (run_id, index, now),
            )
    return run_id


def run_snapshot(db: Database, run_id: int) -> dict:
    row = db.connection.execute("SELECT * FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ActionError("Discovery run not found")
    result = dict(row)
    result["config"] = json.loads(result.pop("config_json"))
    result["steps"] = [
        dict(r)
        for r in db.connection.execute(
            "SELECT * FROM discovery_steps WHERE run_id=? ORDER BY id", (run_id,)
        )
    ]
    for step in result["steps"]:
        step["result"] = json.loads(step.pop("result_json"))
    result["lead_ids"] = [
        r[0]
        for r in db.connection.execute(
            "SELECT lead_id FROM discovery_leads WHERE run_id=? ORDER BY lead_id", (run_id,)
        )
    ]
    return result


def retry_run(db: Database, run_id: int) -> None:
    """Explicitly retry failures only; successful search queries remain cached."""
    with mutation_lock(db), db.connection:
        run_snapshot(db, run_id)
        db.connection.execute(
            "UPDATE discovery_steps SET status='pending' WHERE run_id=? "
            "AND status IN ('failed','partial','running') AND attempts<3",
            (run_id,),
        )
        # Reassess downstream local stages after changed crawl/DNS evidence.
        db.connection.execute(
            "UPDATE discovery_steps SET status='pending' WHERE run_id=? "
            "AND stage IN ('qualify','compliance')",
            (run_id,),
        )
        db.connection.execute(
            "UPDATE discovery_runs SET status='pending',updated_at=? WHERE id=?",
            (utc_now().isoformat(), run_id),
        )


class Pipeline:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        search_factory=SearchClient,
        fetcher_factory=SafeFetcher,
        crawler=crawl_lead,
        verifier=verify_email,
    ):
        self.db, self.settings = db, settings
        self.search_factory, self.fetcher_factory = search_factory, fetcher_factory
        self.crawler, self.verifier = crawler, verifier

    def advance(self, run_id: int) -> dict:
        """Perform at most one persisted stage, only from an explicit user action."""
        db = self.db
        with mutation_lock(db):
            snapshot = run_snapshot(db, run_id)
            config = RunConfig.model_validate(snapshot["config"])
            step = next(
                (s for s in snapshot["steps"] if s["status"] in {"pending", "running"}), None
            )
            if step is None:
                self._finish(run_id)
                return run_snapshot(db, run_id)
            # A running record with the OS lock available is an interrupted operation.
            if step["attempts"] >= 3:
                self._save(step["id"], "failed", {"reason": "Attempt limit reached"})
                self._finish(run_id)
                return run_snapshot(db, run_id)
            with db.connection:
                db.connection.execute(
                    "UPDATE discovery_steps SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",
                    (utc_now().isoformat(), step["id"]),
                )
                db.connection.execute(
                    "UPDATE discovery_runs SET status='running' WHERE id=?", (run_id,)
                )
            try:
                status, result = self._execute(run_id, step, config)
            except Exception:  # noqa: BLE001 -- persist sanitized errors at the stage boundary
                # Never store raw exception text, request headers, or provider response bodies.
                status, result = (
                    "failed",
                    {"reason": "Stage failed. Check configuration and retry explicitly."},
                )
            self._save(step["id"], status, result)
            self._finish(run_id)
            return run_snapshot(db, run_id)

    def run(self, run_id: int, progress: Callable[[dict], None] | None = None) -> dict:
        # Finite ceiling: three searches plus four steps for each of thirty businesses.
        for _ in range(124):
            snapshot = self.advance(run_id)
            if progress:
                progress(snapshot)
            if snapshot["status"] in {"complete", "partial"}:
                return snapshot
        raise ActionError("Run budget reached; inspect saved progress")

    def _save(self, step_id, status, result):
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE discovery_steps SET status=?,result_json=?,updated_at=? WHERE id=?",
                (status, json.dumps(result), utc_now().isoformat(), step_id),
            )

    def _finish(self, run_id):
        statuses = [
            r[0]
            for r in self.db.connection.execute(
                "SELECT status FROM discovery_steps WHERE run_id=?", (run_id,)
            )
        ]
        state = (
            "running"
            if any(s in {"pending", "running"} for s in statuses)
            else ("partial" if any(s in {"failed", "partial"} for s in statuses) else "complete")
        )
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE discovery_runs SET status=?,updated_at=? WHERE id=?",
                (state, utc_now().isoformat(), run_id),
            )

    def _execute(self, run_id, step, config):
        db = self.db
        if step["stage"] == "search":
            remaining = (
                config.max_businesses
                - db.connection.execute(
                    "SELECT count(*) FROM discovery_leads WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            if remaining <= 0:
                return "skipped", {"reason": "Business limit reached"}
            settings = self.settings.model_copy(
                update={
                    "search_provider": config.provider,
                    "search_results_per_query": min(config.results_per_query, remaining),
                }
            )
            if (
                not settings.search_api_key
                or not settings.search_api_key.get_secret_value().strip()
            ):
                return "failed", {"reason": "SEARCH_API_KEY is not configured"}
            client = self.search_factory(settings)

            # Bound even injected/malformed adapters before existing discovery persists hits.
            class BoundedSearch:
                def search(self, query):
                    return client.search(query)[: min(config.results_per_query, remaining)]

            result = discover(
                db,
                BoundedSearch(),
                config.niche,
                [QUERY_TEMPLATES[step["target"]]],
                settings=settings,
            )
            with db.connection:
                for lead_id in dict.fromkeys(result.lead_ids):
                    db.connection.execute(
                        "INSERT OR IGNORE INTO discovery_leads VALUES (?,?)", (run_id, lead_id)
                    )
                    for stage in ("scrape", "verify", "qualify", "compliance"):
                        db.connection.execute(
                            "INSERT OR IGNORE INTO discovery_steps(run_id,stage,target,updated_at) "
                            "VALUES (?,?,?,?)",
                            (run_id, stage, lead_id, utc_now().isoformat()),
                        )
            return ("partial" if result.errors else "done"), {
                "added": result.added,
                "duplicates": result.duplicates,
                "skipped": result.skipped,
                "lead_ids": result.lead_ids,
                "reason": "Search provider failed; check credentials and retry"
                if result.errors
                else "Search completed",
            }
        lead = db.get_lead(step["target"])
        if db.is_suppressed(lead.email, lead.domain):
            return "blocked", {"reason": "Suppressed recipient"}
        if lead.stage not in {"new", "qualified"}:
            return "blocked", {"reason": "Lead lifecycle excludes discovery"}
        if step["stage"] == "scrape":
            if lead.stage != "new":
                return "skipped", {"reason": "Qualified lead retained; no automatic recrawl"}
            fetcher = self.fetcher_factory(limits=CrawlLimits(max_pages=config.max_pages))
            result = self.crawler(db, lead.id, fetcher, retry_failed=step["attempts"] > 0)
            return ("partial" if result.errors else "done"), {
                "pages": len(result.pages),
                "cached_pages": result.cached_pages,
                "requests": result.requests,
                "errors": result.errors,
            }
        if step["stage"] == "verify":
            if not lead.email:
                return "blocked", {"reason": "No discovered contact address"}
            row = db.connection.execute(
                "SELECT dns_retry_pending,dns_checked_at FROM leads WHERE id=?", (lead.id,)
            ).fetchone()
            if row["dns_checked_at"] and not row["dns_retry_pending"]:
                return "done", {
                    "reason": "Persisted DNS result reused",
                    "email_status": lead.email_status,
                }
            result = self.verifier(lead.email, timeout=self.settings.dns_timeout_seconds)
            db.save_verification(lead.id, result)
            return ("partial" if result.retryable else "done"), {
                "email_status": db.get_lead(lead.id).email_status,
                "reason": result.reason,
                "retry_pending": result.retryable,
            }
        if step["stage"] == "qualify":
            result = current_qualification(db, lead.id) or qualify_lead(db, lead.id)
            return "done", result.model_dump(mode="json")
        eligible = compliance_is_current(db, lead.id)
        return ("done" if eligible else "blocked"), {
            "eligible": eligible,
            "reason": "Reviewed compliance current"
            if eligible
            else "Human compliance review required in Review",
        }
