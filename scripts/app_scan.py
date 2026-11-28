"""Find small businesses whose Android app has gone stale on Google Play. Never emails.

uv run python -m scripts.app_scan "gym app" "dental clinic app" "pizza restaurant app"
uv run python -m scripts.app_scan --countries "United States" "Canada" "church app"
uv run python -m scripts.app_scan --list
"""

import argparse
from datetime import date

from agent import apps
from agent.config import load_settings
from agent.db import Database


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("queries", nargs="*", help="Play Store searches, e.g. 'gym app'")
    parser.add_argument("--countries", nargs="+", default=list(apps.DEFAULT_COUNTRIES))
    parser.add_argument(
        "--cutoff",
        default=apps.DEFAULT_CUTOFF.isoformat(),
        help="Only apps last updated on or before this date (YYYY-MM-DD)",
    )
    parser.add_argument("--per-query", type=int, default=30)
    parser.add_argument("--list", action="store_true", help="Print stored leads and exit")
    args = parser.parse_args(argv)
    settings = load_settings()
    with Database(settings.database_path) as db:
        db.migrate()
        if not args.list:
            if not args.queries:
                parser.error("give at least one search, or --list")
            report = apps.scan(
                db,
                args.queries,
                cutoff=date.fromisoformat(args.cutoff),
                countries=tuple(args.countries),
                per_query=args.per_query,
            )
            print(report.summary())
        for row in apps.app_leads(db, statuses=("new",)):
            print(
                f"{row['id']:>4}  {row['updated_on']}  {row['installs']:>6}  "
                f"{row['title'][:40]:<40}  {row['email']}"
            )


if __name__ == "__main__":
    main()
