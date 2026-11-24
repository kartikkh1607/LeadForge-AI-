import json
import sqlite3
from unittest.mock import Mock

import pytest

from agent.config import Settings
from agent.db import Database
from agent.readiness import database_health, readiness
from scripts.validate_integrations import NICHES, discovery_report, validate_groq


def test_readiness_no_side_effects_or_secrets(db):
    settings = Settings(search_api_key="sensitive-search", groq_api_key="sensitive-groq")
    before = db.connection.total_changes
    vault = Mock(side_effect=AssertionError("No vault access on rerun"))
    for _ in range(2):
        result = readiness(db, settings, store_factory=vault)
        assert result["checks"]["database"] == "healthy"
        assert "sensitive" not in json.dumps(result)
        assert result["checks"]["gmail_live"] == "not_verified"
    assert before == db.connection.total_changes
    vault.assert_not_called()


@pytest.mark.parametrize(
    "raw,status", [(None, "missing"), ("invalid-secret-token", "unavailable_or_invalid")]
)
def test_vault_metadata_only(db, raw, status):
    store = Mock()
    store.get.return_value = raw
    result = readiness(
        db,
        Settings(gmail_account="owner@example.com"),
        inspect_vault=True,
        store_factory=lambda: store,
    )
    assert result["checks"]["gmail_credentials"] == status
    assert "secret-token" not in json.dumps(result)
    store.put.assert_not_called()


def test_expired_credentials_no_refresh(db, monkeypatch):
    credentials = Mock(
        valid=False,
        scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
        ],
    )
    monkeypatch.setattr(
        "agent.readiness.Credentials.from_authorized_user_info", lambda _: credentials
    )
    store = Mock()
    store.get.return_value = "{}"
    report = readiness(
        db,
        Settings(gmail_account="owner@example.com"),
        inspect_vault=True,
        store_factory=lambda: store,
    )
    assert report["checks"]["gmail_credentials"].startswith("expired")
    credentials.refresh.assert_not_called()


def test_vault_error_is_sanitized(db):
    report = readiness(
        db, Settings(), inspect_vault=True, store_factory=Mock(side_effect=RuntimeError("secret"))
    )
    assert report["checks"]["vault"] == "unavailable"
    assert "secret" not in json.dumps(report)


def test_database_read_only_and_checksum(db, tmp_path):
    path = db.connection.execute("PRAGMA database_list").fetchone()[2]
    with Database(path, read_only=True) as other:
        assert database_health(other) == "healthy"
        with pytest.raises(sqlite3.OperationalError):
            other.connection.execute("DELETE FROM leads")
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        Database(missing, read_only=True)
    assert not missing.exists()
    with db.connection:
        db.connection.execute("UPDATE schema_migrations SET checksum='bad'")
    assert database_health(db) == "migration_history_changed"


def test_report_pipeline_evidence(db):
    from test_pipeline import fixtures

    from agent.pipeline import RunConfig, create_run

    pipeline, _ = fixtures(db)
    snapshot = pipeline.run(
        create_run(db, RunConfig(niche="watercolor", max_businesses=10), "audit")
    )
    report = discovery_report(db, snapshot)
    lead = report["businesses"][0]
    assert lead["score"] == 100 and lead["courses"] and lead["public_contacts"]
    assert lead["faq_count"] >= 5 and lead["compliance_blockers"]
    assert len(NICHES) == 5
    assert not db.connection.execute("SELECT 1 FROM outbound_queue").fetchone()


def test_groq_metrics_and_no_persistence(db, phase3):
    from test_personalize import generator

    lead, _, slots, settings = phase3
    before = db.connection.total_changes
    report = validate_groq(db, lead.id, settings, generator(settings, slots.model_dump_json()))
    assert report["outcome"] == "validated"
    assert report["evidence_valid"] and report["template_valid"] and report["structured_valid"]
    assert report["unsupported_claim_rejected"]
    assert report["latency_seconds"] >= 0 and report["tokens"] is None
    assert before == db.connection.total_changes


def test_unreviewed_groq_does_not_call_api(db, lead):
    generator = Mock()
    assert validate_groq(db, lead.id, Settings(), generator)["outcome"] == "blocked_compliance"
    generator.generate.assert_not_called()


def test_live_observed_answer_instead_of_question_evidence_rejected(db, phase3):
    from test_personalize import generator

    lead, _, slots, settings = phase3
    payload = slots.model_dump(mode="json")
    payload["evidence"] = [
        {"claim": value, "quote": value, "url": "https://willow-art.com/"}
        for value in ["Start today.", "Lifetime access.", "Ada"]
    ]
    report = validate_groq(db, lead.id, settings, generator(settings, json.dumps(payload)))
    assert report["structured_valid"] and not report["evidence_valid"]
    assert report["failure_stage"] == "evidence"
    assert report["outcome"] == "provider_or_validation_failed"
    assert not db.connection.execute("SELECT 1 FROM messages").fetchone()


def test_groq_token_measurement(db, phase3):
    from agent.personalize import GroqPersonalizer

    lead, _, slots, settings = phase3
    client = Mock()
    client.chat.completions.create.return_value = Mock(
        usage=Mock(total_tokens=77),
        choices=[Mock(finish_reason="stop", message=Mock(content=slots.model_dump_json()))],
    )
    report = validate_groq(db, lead.id, settings, GroqPersonalizer(settings, client))
    assert report["tokens"] == 77 and report["outcome"] == "validated"


def test_validation_commands_without_keys_do_not_create_database(tmp_path, monkeypatch, capsys):
    from scripts import validate_integrations as cli

    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    for mode in ("search", "groq"):
        monkeypatch.setattr(
            "sys.argv", ["validate", mode, "--live", "--database", str(tmp_path / "absent.db")]
        )
        cli.main()
        assert "credential unavailable" in capsys.readouterr().out
    assert not (tmp_path / "absent.db").exists()
