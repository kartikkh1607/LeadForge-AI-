import pytest
from pydantic import ValidationError

from agent.config import DAILY_LIMIT, SEND_TIMEZONE, Settings, load_settings
from agent.evidence import evidence_matches, validate_draft
from agent.models import Evidence, Lead
from agent.scrape import decode_cfemail


def test_configuration_defaults_and_no_global_mutation(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTAL_ADDRESS", raising=False)
    env = tmp_path / ".env"
    env.write_text("DRY_RUN=false\nPOSTAL_ADDRESS=Test address\nGROQ_API_KEY=test-secret\n")
    settings = load_settings(env, {"DRY_RUN": "true"})
    assert settings.dry_run
    assert settings.postal_address == "Test address"
    assert "test-secret" not in repr(settings)
    assert Settings().dry_run
    assert not Settings().postal_address_confirmed
    assert DAILY_LIMIT == 15 and SEND_TIMEZONE == "Asia/Kolkata"


@pytest.mark.parametrize("values", [{"DRY_RUN": "maybe"}, {"DNS_TIMEOUT_SECONDS": "0"}])
def test_bad_config_fails_closed(values):
    with pytest.raises(ValidationError):
        load_settings(None, values)


def test_models_validate_and_truncate():
    assert len(Lead(domain="example.com", faq_text="x" * 5000).faq_text) == 4000
    with pytest.raises(ValidationError):
        Lead(domain="example.com", score=101)
    with pytest.raises(ValidationError):
        Lead(domain="example.com", stage="drafted")


def test_cfemail():
    email = "hello@example.com"
    key = 0x42
    encoded = bytes([key, *(ord(c) ^ key for c in email)]).hex()
    assert decode_cfemail(encoded) == email


@pytest.mark.parametrize("value", ["", "12", "xyz", "001"])
def test_bad_cfemail(value):
    with pytest.raises(ValueError):
        decode_cfemail(value)


def test_evidence_requires_matching_url_and_nonempty_quote():
    evidence = [Evidence(claim="Access", quote="Lifetime  ACCESS", url="https://example.com/faq")]
    assert evidence_matches(evidence, {"https://example.com/faq": "Get lifetime\naccess now"})
    assert not evidence_matches(evidence, {"https://other.com": "Lifetime access"})
    assert not evidence_matches([], {})
    assert not evidence_matches([Evidence(claim="a", quote=" ", url="x")], {"x": "abc"})


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("Hello", "bad—dash"),
        ("Hello", "[placeholder]"),
        ("Hello", "{name}"),
        ("Hello\r\nBcc: x", "test"),
        ("Hello", "word " * 170),
        ("Hello", "https://example.com https://example.org"),
        (" ", "body"),
    ],
)
def test_bad_draft(subject, body):
    with pytest.raises(ValueError):
        validate_draft(subject, body)
