"""Secret-safe local diagnostics. No OAuth, token refresh, provider calls or writes."""

import argparse
import hashlib
import json
from pathlib import Path

from google.oauth2.credentials import Credentials

from agent.config import load_settings
from agent.db import Database
from agent.gmail import SCOPES, CredentialStore
from agent.sender import owner_blockers, owners, queue_rows
from agent.settings_store import effective_settings
from agent.verify import normalize_email


def database_health(db):
    try:
        integrity = db.connection.execute("PRAGMA integrity_check").fetchall()
        foreign_keys = db.connection.execute("PRAGMA foreign_key_check").fetchall()
        if [r[0] for r in integrity] != ["ok"] or foreign_keys:
            return "integrity_failed"
        expected = {
            p.name: hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest()
            for p in Path(__file__).with_name("migrations").glob("*.sql")
        }
        applied = dict(db.connection.execute("SELECT version,checksum FROM schema_migrations"))
        if any(
            name not in expected or checksum != expected[name] for name, checksum in applied.items()
        ):
            return "migration_history_changed"
        return "healthy" if applied == expected else "migration_required"
    except Exception:  # noqa: BLE001 -- diagnostics must not echo database contents/errors
        return "unavailable"


def readiness(db, base, *, inspect_vault=False, store_factory=CredentialStore):
    checks = {
        "database": database_health(db),
        "vault": "not_checked",
        "gmail_credentials": "not_checked",
        "gmail_live": "not_verified",
        "general_sending": "disabled",
        "followups": "disabled",
    }
    try:
        settings = effective_settings(db, base)
    except Exception:  # noqa: BLE001 -- validation errors can include sensitive input
        settings = base
        checks["preferences"] = "invalid_or_unavailable"
    checks["search"] = (
        "configured_unverified"
        if settings.search_api_key and settings.search_api_key.get_secret_value().strip()
        else "missing"
    )
    checks["groq"] = (
        "configured_unverified"
        if settings.groq_api_key and settings.groq_api_key.get_secret_value().strip()
        else "missing"
    )
    checks["postal"] = (
        "confirmed"
        if settings.postal_address.strip() and settings.postal_address_confirmed
        else "missing_or_unconfirmed"
    )
    checks["pause"] = "on" if settings.paused else "off"
    checks["dry_run"] = "on" if settings.dry_run else "off"
    if "preferences" in checks:
        checks["pause"] = checks["dry_run"] = "unknown_preferences_unavailable"
    checks["compliance"] = "per_lead_review_required"
    try:
        cached = db.connection.execute(
            "SELECT status FROM gmail_connection WHERE account=?",
            (settings.gmail_account.casefold(),),
        ).fetchone()
        checks["gmail_last_check"] = (
            "previously_connected_not_reverified"
            if cached and cached[0] == "connected"
            else "reconnect_required"
            if cached and cached[0] == "reconnect_required"
            else "no_verified_record"
        )
    except Exception:  # noqa: BLE001 -- no raw database diagnostics
        checks["gmail_last_check"] = "unavailable"
    try:
        checks["allowlist"] = "configured" if owners(settings) else "empty"
        normalize_email(settings.gmail_account)
        checks["gmail_account"] = "configured"
    except ValueError:
        checks["gmail_account_or_allowlist"] = "invalid_or_missing"
    try:
        data = json.loads(settings.gmail_client_file.read_text(encoding="utf-8"))
        installed = data.get("installed", {})
        checks["desktop_oauth"] = (
            "configured_unverified"
            if installed.get("client_id") and installed.get("client_secret")
            else "invalid_desktop_client"
        )
    except (OSError, ValueError, AttributeError):
        checks["desktop_oauth"] = "missing_or_invalid"
    if inspect_vault:
        try:
            store = store_factory()
            checks["vault"] = "available"
            raw = store.get(settings.gmail_account) if settings.gmail_account else None
            checks["gmail_credentials"] = "missing"
            if raw:
                credentials = Credentials.from_authorized_user_info(json.loads(raw))
                if not set(SCOPES).issubset(credentials.scopes or ()):
                    checks["gmail_credentials"] = "missing_scopes"
                elif not credentials.valid:
                    checks["gmail_credentials"] = "expired_or_invalid_no_refresh_attempted"
                else:
                    checks["gmail_credentials"] = "present_live_status_unverified"
        except Exception:  # noqa: BLE001 -- never log vault or token data
            checks["gmail_credentials"] = "unavailable_or_invalid"
            if checks["vault"] != "available":
                checks["vault"] = "unavailable"
    try:
        rows = queue_rows(db)
        uncertain = sum(r["state"] in {"sending", "uncertain"} for r in rows)
        inconsistent = db.connection.execute(
            "SELECT count(*) FROM messages m LEFT JOIN outbound_queue q ON q.message_id=m.id "
            "WHERE (m.status='sending' AND (q.message_id IS NULL OR q.state NOT IN ('sending','uncertain'))) "
            "OR (q.state='sent' AND (m.status!='sent' OR m.gmail_message_id IS NOT q.gmail_message_id))"
        ).fetchone()[0]
        checks["queue"] = (
            "inconsistent"
            if inconsistent
            else "reconciliation_required"
            if uncertain
            else "healthy"
        )
        checks["queue_pending"] = sum(r["state"] == "pending" for r in rows)
        blocked = 0
        for row in rows:
            if row["state"] != "pending":
                continue
            message = db.get_message(row["message_id"])
            blockers = set(owner_blockers(db, message.id, settings)) - {"paused", "dry_run"}
            if (
                blockers
                or message.version != row["version"]
                or message.content_hash != row["content_hash"]
                or not message.approved_at
                or message.approved_at.isoformat() != row["approved_at"]
                or settings.gmail_account.casefold() != row["account"]
            ):
                blocked += 1
        checks["queue_pending_blocked"] = blocked
    except Exception:  # noqa: BLE001 -- incomplete schema is a blocker, not a traceback
        checks["queue"] = "unavailable"
    blockers = [
        f"{key}: {value}"
        for key, value in checks.items()
        if value not in ("healthy", "confirmed", "configured", "on", "disabled", "available", 0)
        and key != "queue_pending"
    ]
    blockers.append("General prospect sending remains disabled by phase boundary")
    return {"checks": checks, "launch_blockers": blockers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    try:
        settings = load_settings()
        with Database(args.database or settings.database_path, read_only=True) as db:
            report = readiness(db, settings, inspect_vault=True)
    except Exception:  # noqa: BLE001 -- config/SQLite exceptions may contain sensitive inputs
        report = {
            "checks": {"configuration_or_database": "unavailable"},
            "launch_blockers": [
                "Check local configuration and initialize/migrate database explicitly"
            ],
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
