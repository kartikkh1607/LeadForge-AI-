"""Persist only explicitly allowed non-secret dashboard preferences in SQLite."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.config import Settings
from agent.db import Database
from agent.models import utc_now
from agent.operations import ActionError, fingerprint, mutation_lock


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    search_provider: Literal["tavily", "serper"] = "tavily"
    search_results_per_query: int = Field(default=10, ge=1, le=20)
    groq_model: str = Field(default="openai/gpt-oss-120b", min_length=1, max_length=120)
    dry_run: bool = True
    paused: bool = True
    postal_address: str = Field(default="", max_length=500)
    postal_address_confirmed: bool = False


def effective_settings(db: Database, base: Settings) -> Settings:
    row = db.connection.execute("SELECT values_json FROM dashboard_settings WHERE id=1").fetchone()
    if not row:
        return base
    preferences = Preferences.model_validate_json(row[0])
    return Settings.model_validate({**base.model_dump(), **preferences.model_dump()})


def preferences_for(settings: Settings) -> Preferences:
    return Preferences(**{key: getattr(settings, key) for key in Preferences.model_fields})


def save_preferences(
    db: Database,
    base: Settings,
    values: dict,
    *,
    submission_key: str | None = None,
    expected: str | None = None,
) -> Settings:
    preferences = Preferences.model_validate(values)
    with mutation_lock(db):
        previous = effective_settings(db, base)
        digest = fingerprint(values)
        if submission_key:
            row = db.connection.execute(
                "SELECT * FROM ui_actions WHERE action_key=?", (submission_key,)
            ).fetchone()
            if row:
                if row["operation"] != "settings" or row["payload_hash"] != digest:
                    raise ActionError("Settings submission was already used with different values")
                return previous
        if expected is not None and expected != fingerprint(preferences_for(previous).model_dump()):
            raise ActionError("Settings changed in another session. Refresh before saving.")
        changed_address = preferences.postal_address.strip() != previous.postal_address.strip()
        # A checkbox carried over from an old address must not confirm a replacement.
        if changed_address or not preferences.postal_address.strip():
            preferences.postal_address_confirmed = False
        with db.connection:
            db.connection.execute(
                "INSERT INTO dashboard_settings VALUES (1,?,?) ON CONFLICT(id) DO UPDATE "
                "SET values_json=excluded.values_json,updated_at=excluded.updated_at",
                (preferences.model_dump_json(), utc_now().isoformat()),
            )
            if changed_address:
                db.connection.execute(
                    "UPDATE messages SET status='draft',approved_at=NULL,requires_manual_review=1,"
                    "truthful_identity_confirmed=0,opt_out_confirmed=0 WHERE status='approved'"
                )
            if submission_key:
                db.connection.execute(
                    "INSERT INTO ui_actions VALUES (?,'settings',?,'done',NULL,?)",
                    (submission_key, digest, utc_now().isoformat()),
                )
    return effective_settings(db, base)
