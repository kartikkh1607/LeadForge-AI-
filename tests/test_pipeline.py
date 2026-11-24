import json
from pathlib import Path

import pytest

from agent.config import Settings
from agent.models import Lead, Suppression
from agent.operations import ActionError, mutation_lock
from agent.pipeline import Pipeline, RunConfig, create_run, retry_run, run_snapshot
from agent.scrape import extract_page
from agent.search import QUERY_TEMPLATES
from agent.verify import VerificationResult
from agent.web_models import CrawlResult, SearchHit


def fixtures(db, *, fail_dns=False):
    calls = {"search": [], "scrape": [], "verify": []}

    class Search:
        def search(self, query):
            calls["search"].append(query)
            return [SearchHit(url="https://willow-art.com/", query=query)]

    def crawl(database, lead_id, fetcher, retry_failed=False):
        calls["scrape"].append(lead_id)
        page = extract_page(
            Path("tests/fixtures/qualified_creator.html").read_text(), "https://willow-art.com/"
        )
        database.save_crawl_page(lead_id, page.url, page=page)
        database.apply_scraped_facts(lead_id, [page])
        return CrawlResult(lead_id=lead_id, pages=[page])

    def verify(email, **kwargs):
        calls["verify"].append(email)
        return (
            VerificationResult(None, "Temporary DNS", True)
            if fail_dns
            else VerificationResult("domain_mail_capable", "Fixture MX")
        )

    pipeline = Pipeline(
        db,
        Settings(search_api_key="fixture"),
        search_factory=lambda s: Search(),
        crawler=crawl,
        verifier=verify,
    )
    return pipeline, calls


def test_pipeline_persists_progress_and_stops_at_human_review(db):
    pipeline, calls = fixtures(db)
    run_id = create_run(db, RunConfig(niche="watercolor"), "request")
    snapshot = pipeline.run(run_id)
    assert snapshot["status"] == "complete"
    assert len(snapshot["lead_ids"]) == 1
    lead = db.get_lead(snapshot["lead_ids"][0])
    assert lead.score == 100 and lead.stage == "qualified"
    assert lead.email_status == "domain_mail_capable"
    assert db.get_compliance(lead.id) is None
    assert snapshot["steps"][-1]["status"] == "blocked"
    assert not db.connection.execute("SELECT 1 FROM messages").fetchone()
    assert (
        len(calls["search"]) == len(QUERY_TEMPLATES)
        and len(calls["scrape"]) == len(calls["verify"]) == 1
    )
    pipeline.run(run_id)
    assert len(calls["search"]) == len(QUERY_TEMPLATES)
    assert create_run(db, RunConfig(niche="watercolor"), "request") == run_id
    with pytest.raises(ActionError):
        create_run(db, RunConfig(niche="other"), "request")


def test_missing_key_and_safe_errors_are_persisted(db):
    run_id = create_run(db, RunConfig(niche="art", query_count=1), "key")
    result = Pipeline(db, Settings()).run(run_id)
    assert result["status"] == "partial"
    assert "SEARCH_API_KEY" in result["steps"][0]["result"]["reason"]

    def broken(settings):
        raise RuntimeError("API-key=do-not-disclose")

    retry_run(db, run_id)
    result = Pipeline(db, Settings(search_api_key="secret"), search_factory=broken).run(run_id)
    assert "do-not-disclose" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_interrupted_search_resumes_without_duplicate_leads(db):
    pipeline, calls = fixtures(db)
    run_id = create_run(db, RunConfig(niche="watercolor", query_count=1), "interrupt")
    real = pipeline._execute

    def interrupt(*args):
        real(*args)
        raise KeyboardInterrupt()

    pipeline._execute = interrupt
    with pytest.raises(KeyboardInterrupt):
        pipeline.advance(run_id)
    assert run_snapshot(db, run_id)["steps"][0]["status"] == "running"
    pipeline._execute = real
    snapshot = pipeline.run(run_id)
    assert snapshot["status"] == "complete"
    assert db.connection.execute("SELECT count(*) FROM leads").fetchone()[0] == 1
    assert len(calls["search"]) == 2


def test_interrupted_after_committed_stage_does_not_repeat_stage(db):
    pipeline, calls = fixtures(db)
    run_id = create_run(db, RunConfig(niche="watercolor", query_count=1), "progress")

    def interrupted(snapshot):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        pipeline.run(run_id, interrupted)
    pipeline.run(run_id)
    assert len(calls["search"]) == 1


def test_dns_retry_partial_failure_then_explicit_retry(db):
    pipeline, calls = fixtures(db, fail_dns=True)
    run_id = create_run(db, RunConfig(niche="watercolor", query_count=1), "dns")
    assert pipeline.run(run_id)["status"] == "partial"
    pipeline.verifier = lambda *a, **kw: VerificationResult("domain_mail_capable", "Recovered")
    retry_run(db, run_id)
    snapshot = pipeline.run(run_id)
    assert snapshot["status"] == "complete"
    assert len(calls["search"]) == len(calls["scrape"]) == 1
    assert db.get_lead(snapshot["lead_ids"][0]).email_status == "domain_mail_capable"


def test_suppressed_or_terminal_lead_never_scraped(db):
    pipeline, calls = fixtures(db)
    db.add_lead(Lead(domain="willow-art.com", stage="contacted"))
    run_id = create_run(db, RunConfig(niche="watercolor", query_count=1), "terminal")
    pipeline.run(run_id)
    assert not calls["scrape"] and not calls["verify"]
    db.suppress(Suppression(kind="domain", value="willow-art.com", reason="manual"))
    run_id = create_run(db, RunConfig(niche="watercolor"), "suppressed")
    assert pipeline.run(run_id)["lead_ids"] == []


def test_new_suppression_between_stages_blocks_network(db):
    pipeline, calls = fixtures(db)
    run_id = create_run(db, RunConfig(niche="watercolor", query_count=1), "suppress")
    pipeline.advance(run_id)
    db.suppress(Suppression(kind="domain", value="willow-art.com", reason="opt_out"))
    pipeline.run(run_id)
    assert not calls["scrape"] and not calls["verify"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("query_count", len(QUERY_TEMPLATES) + 1),
        ("max_businesses", 31),
        ("max_pages", 9),
        ("results_per_query", 21),
        ("niche", " "),
    ],
)
def test_run_limits(field, value):
    with pytest.raises(ValueError):
        RunConfig.model_validate({"niche": "art", field: value})


def test_lock_blocks_overlapping_actions(db):
    with mutation_lock(db), pytest.raises(ActionError, match="Another action"):
        create_run(db, RunConfig(niche="art"), "blocked")


def test_attempts_are_bounded(db):
    run_id = create_run(db, RunConfig(niche="art", query_count=1), "bounded")
    pipeline = Pipeline(db, Settings())
    for _ in range(5):
        pipeline.run(run_id)
        retry_run(db, run_id)
    assert run_snapshot(db, run_id)["steps"][0]["attempts"] == 3


def test_business_budget_limits_persistence_and_skips_extra_queries(db):
    class Search:
        def search(self, query):
            return [SearchHit(url=f"https://creator-{n}.com/", query=query) for n in range(100)]

    run_id = create_run(db, RunConfig(niche="art", max_businesses=2), "business-budget")
    pipeline = Pipeline(
        db,
        Settings(search_api_key="fixture"),
        search_factory=lambda s: Search(),
        crawler=lambda database, lead_id, *a, **kw: CrawlResult(lead_id=lead_id),
    )
    snapshot = pipeline.run(run_id)
    assert len(snapshot["lead_ids"]) == 2
    assert db.connection.execute("SELECT count(*) FROM leads").fetchone()[0] == 2
    assert [s["status"] for s in snapshot["steps"] if s["stage"] == "search"] == ["done"] + [
        "skipped"
    ] * (len(QUERY_TEMPLATES) - 1)


def test_partial_scrape_retains_good_evidence_and_other_stages(db):
    pipeline, calls = fixtures(db)
    original = pipeline.crawler

    def partial(*args, **kwargs):
        result = original(*args, **kwargs)
        result.errors["https://willow-art.com/help"] = "http_status_503"
        return result

    pipeline.crawler = partial
    run_id = create_run(db, RunConfig(niche="watercolor", query_count=1), "partial")
    snapshot = pipeline.run(run_id)
    assert snapshot["status"] == "partial"
    assert db.get_lead(snapshot["lead_ids"][0]).score == 100
    assert len(calls["verify"]) == 1
