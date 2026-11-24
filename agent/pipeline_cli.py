"""Follow-up drafting and conversion tracking CLI. Never sends email.

  python -m agent.pipeline_cli due
  python -m agent.pipeline_cli draft --lead-id 12
  python -m agent.pipeline_cli progress --lead-id 12 --stage demo_sent --note "Sent demo link"
  python -m agent.pipeline_cli progress --lead-id 12 --stage won --value 150
  python -m agent.pipeline_cli summary

Drafted follow-ups still need approval in the dashboard and the owner-only send flow.
"""

import argparse
import json

from agent.config import load_settings
from agent.conversions import STAGES, ConversionError, record_progress, summary
from agent.db import Database
from agent.followups import FollowupError, draft_followup, due_followups
from agent.settings_store import effective_settings


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("command", choices=["due", "draft", "progress", "summary"])
    parser.add_argument("--lead-id", type=int)
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--note", default="")
    parser.add_argument("--value", type=float)
    args = parser.parse_args(argv)
    base = load_settings()
    with Database(base.database_path) as db:
        db.migrate()
        settings = effective_settings(db, base)
        try:
            if args.command == "due":
                for row in due_followups(db, settings):
                    state = "DUE" if row["due"] else "blocked: " + ", ".join(row["blockers"])
                    print(f"lead {row['lead_id']}: {state}")
            elif args.command == "draft":
                if not args.lead_id:
                    parser.error("--lead-id required")
                message = draft_followup(db, args.lead_id, settings)
                print(f"Follow-up draft {message.id} created. Review and approve it; nothing sent.")
            elif args.command == "progress":
                if not args.lead_id or not args.stage:
                    parser.error("--lead-id and --stage required")
                latest = record_progress(db, args.lead_id, args.stage, args.note, args.value)
                print(f"Lead {args.lead_id} is now at '{latest}'.")
            else:
                print(json.dumps(summary(db), indent=2))
        except (FollowupError, ConversionError, ValueError) as exc:
            parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
