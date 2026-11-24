"""Suppression API, shared by future drafting and sending components."""

from agent.db import Database
from agent.models import Suppression


def suppress(db: Database, item: Suppression) -> None:
    db.suppress(item)


def is_suppressed(db: Database, email: str | None, domain: str) -> bool:
    return db.is_suppressed(email, domain)
