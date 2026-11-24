"""Explicit, interactive owner-test CLI, read-only Gmail sync and sent-mail import.

Default: offline diagnostics.
  python -m agent.send_worker import-sent            # record emails sent from Gmail
  python -m agent.send_worker import-sent --subject ""   # every sent email, last 180 days
  python -m agent.send_worker sync                   # pull replies, bounces, opt-outs
"""

import argparse
import sys

from agent.config import load_settings
from agent.db import Database
from agent.gmail import CredentialStore, GmailError, GmailTransport, authorize, load_credentials
from agent.models import utc_now
from agent.operations import ActionError
from agent.sender import (
    confirmation,
    enqueue,
    owner_blockers,
    owners,
    prepare_owner_test,
    queue_rows,
    quota_used,
    reconcile,
    send_one,
)
from agent.settings_store import effective_settings, preferences_for, save_preferences


def connection_status(db, account, status):
    with db.connection:
        db.connection.execute(
            "INSERT INTO gmail_connection VALUES (?,?,?) ON CONFLICT(account) DO UPDATE "
            "SET status=excluded.status,checked_at=excluded.checked_at",
            (account.casefold(), status, utc_now().isoformat()),
        )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command",
        choices=[
            "diagnose",
            "connect",
            "check",
            "prepare-owner-test",
            "enqueue",
            "send-owner-test",
            "reconcile",
            "sync",
            "import-sent",
        ],
        nargs="?",
        default="diagnose",
    )
    parser.add_argument("--message-id", type=int)
    parser.add_argument(
        "--subject",
        default="quick idea",
        help="import-sent: only sent emails whose subject contains this ('' for all)",
    )
    parser.add_argument("--days", type=int, default=180, help="import-sent: look-back window")
    args = parser.parse_args()
    base = load_settings()
    with Database(base.database_path) as db:
        db.migrate()
        settings = effective_settings(db, base)
        try:
            if args.command == "diagnose":
                print(
                    f"Dry-run={settings.dry_run}; paused={settings.paused}; quota={quota_used(db, utc_now())}/{settings.daily_limit}"
                )
                for row in queue_rows(db):
                    print(row["message_id"], row["state"], row["outcome"])
                if args.message_id:
                    print(owner_blockers(db, args.message_id, settings))
                return
            if args.command == "prepare-owner-test":
                if not args.message_id:
                    raise ActionError("--message-id required")
                message = prepare_owner_test(db, args.message_id, base)
                print(
                    f"Owner test draft {message.id} prepared. Review and approve it in the dashboard; nothing sent."
                )
                return
            if args.command == "enqueue":
                if not args.message_id:
                    raise ActionError("--message-id required")
                print(enqueue(db, args.message_id, base))
                return
            if not settings.gmail_account:
                raise ActionError("GMAIL_ACCOUNT required")
            if args.command == "send-owner-test":
                if not args.message_id or not sys.stdin.isatty():
                    raise ActionError(
                        "Live owner tests require --message-id and an interactive terminal"
                    )
                row = next((r for r in queue_rows(db) if r["message_id"] == args.message_id), None)
                if not row or row["state"] != "pending":
                    raise ActionError("Pending queue row required")
                blockers = owner_blockers(db, args.message_id, settings)
                if blockers:
                    raise ActionError(", ".join(blockers))
                message = db.get_message(args.message_id)
                print(
                    f"From: {row['account']}\nTo: {row['recipient']}\n{message.subject}\n\n{message.body}"
                )
                print(
                    "Confirm you own this recipient and authorize this ONE live email. Type exactly:"
                )
                print(confirmation(row))
                consent = input("> ")
                if consent != confirmation(row):
                    raise ActionError("Confirmation did not match; nothing sent")
            store = CredentialStore()
            if args.command == "connect":
                authorize(settings.gmail_client_file, settings.gmail_account, store)
                connection_status(db, settings.gmail_account, "connected")
                print("Gmail connected; no email sent")
                return
            credentials = load_credentials(settings.gmail_account, store)
            transport = GmailTransport(
                credentials.token, owner_addresses=owners(settings), account=settings.gmail_account
            )
            try:
                if transport.profile() != settings.gmail_account.casefold():
                    raise GmailError("Gmail account mismatch")
                connection_status(db, settings.gmail_account, "connected")
                if args.command in {"reconcile", "send-owner-test"}:
                    reconcile(db, transport, settings.gmail_account)
                if args.command == "send-owner-test":
                    print(send_one(db, args.message_id, base, transport, consent))
                elif args.command == "check":
                    print("Gmail connection verified; no email sent")
                elif args.command == "import-sent":
                    from agent.gmail_import import import_sent

                    report = import_sent(
                        db, transport, settings.gmail_account, subject=args.subject, days=args.days
                    )
                    print(report.summary())
                    for email in report.unmatched:
                        print("  not in LeadForge:", email)
                elif args.command == "sync":
                    from agent.gmail_sync import sync

                    report = sync(db, transport, settings.gmail_account)
                    print(
                        f"Checked {report.threads_checked} threads; recorded "
                        f"{len(report.events)} new events "
                        + ", ".join(
                            f"{k}={report.count(k)}"
                            for k in ("reply", "opt_out", "bounce_hard", "spam_block", "auto_reply")
                        )
                        + (
                            f"; skipped {len(report.skipped_threads)}"
                            if report.skipped_threads
                            else ""
                        )
                        + ". Read-only; nothing sent."
                    )
            finally:
                transport.close()
        except GmailError as exc:
            connection_status(db, settings.gmail_account, "reconnect_required")
            parser.exit(1, str(exc) + "\n")
        except (ActionError, ValueError) as exc:
            parser.exit(1, str(exc) + "\n")
        finally:
            if args.command == "send-owner-test":
                try:
                    values = preferences_for(effective_settings(db, base)).model_dump()
                    values.update(paused=True, dry_run=True)
                    save_preferences(db, base, values)
                except Exception:  # noqa: BLE001 -- never expose settings values in error output
                    print(
                        "Could not restore safety settings. Manually enable global pause and dry-run before continuing.",
                        file=sys.stderr,
                    )


if __name__ == "__main__":
    main()
