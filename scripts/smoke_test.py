"""Offline smoke test using an in-memory database and synthetic data only."""

from agent.config import Settings
from agent.db import Database
from agent.models import Lead


def main() -> None:
    assert Settings().dry_run
    with Database(":memory:") as db:
        db.migrate()
        db.migrate()
        lead = db.add_lead(Lead(domain="https://www.example.com/"))
        assert db.get_lead(lead.id).domain == "example.com"
        assert db.get_compliance(lead.id) is None
    print("Offline smoke test passed. Dry-run defaults on; no mail or network calls.")


if __name__ == "__main__":
    main()
