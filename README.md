# LeadForge AI

An evidence-led, human-approved cold outreach engine for freelancers. LeadForge finds
small independent online-course creators, verifies what they actually sell from their
own public pages, drafts a personalised email grounded only in quoted source text, and
tracks replies, bounces, follow-ups and won projects.

Nothing is ever sent without your explicit approval, and every claim in a draft must
match text on the prospect's site.

## How it works

| Stage | What happens | Module |
| --- | --- | --- |
| Discover | Bounded live search (Tavily or Serper) with platform/directory filtering | `agent/search.py` |
| Crawl | robots.txt-respecting crawl of contact, FAQ and course pages | `agent/scrape.py`, `agent/safe_http.py` |
| Extract | Course offers, FAQ pairs and published contacts, each with source quotes | `agent/course_extraction.py`, `agent/extraction.py` |
| Qualify | Transparent points-based score; no fabricated signals | `agent/qualify.py` |
| Comply | Human compliance review recorded per lead | `agent/compliance.py` |
| Draft | Groq fills template slots that must match quoted evidence | `agent/personalize.py` |
| Approve | You edit and approve every message | Streamlit dashboard |
| Send | Owner-only Gmail sending with locks, quotas, reconciliation | `agent/sender.py`, `agent/gmail.py` |
| Sync | Read-only Gmail sync: replies, bounces, opt-outs, auto-replies | `agent/gmail_sync.py` |
| Follow up | One template follow-up per lead, separately approved, threaded | `agent/followups.py` |
| Convert | Interested, demo, proposal, won/lost and revenue tracking | `agent/conversions.py` |

## Safety defaults

- `DRY_RUN=true` and `PAUSED=true` until you change them.
- Sending is restricted to owner test addresses (`OWNER_TEST_ADDRESSES`) at this stage.
- Opt-outs are permanent at the database level; bounces and spam blocks are suppressed.
- Follow-ups require a Gmail sync in the last 24 hours and no reply, bounce or opt-out.
- Daily send cap (`DAILY_LIMIT`, max 15) with randomised delays.
- OAuth tokens live in the OS keychain, never in files or the database.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kartikkh1607/LeadForge-AI-.git
cd LeadForge-AI-
uv sync
cp .env.example .env    # then fill in GROQ_API_KEY, SEARCH_API_KEY, GMAIL_ACCOUNT, etc.
uv run python -m agent.db          # create/migrate the SQLite database
uv run python -m agent.readiness   # check what is configured
```

## Run

```bash
# Dashboard: discover, review, approve, track
uv run streamlit run app.py --server.address 127.0.0.1 --server.port 8501

# Gmail (requires a Google Cloud desktop OAuth client saved as credentials.json)
uv run python -m agent.send_worker connect     # one-time OAuth
uv run python -m agent.send_worker check
uv run python -m agent.send_worker sync        # pull replies/bounces/opt-outs (read-only)

# Follow-ups and conversions
uv run python -m agent.pipeline_cli due
uv run python -m agent.pipeline_cli draft --lead-id 12
uv run python -m agent.pipeline_cli progress --lead-id 12 --stage demo_sent --note "Sent demo"
uv run python -m agent.pipeline_cli progress --lead-id 12 --stage won --value 150
uv run python -m agent.pipeline_cli summary
```

## Tests

```bash
uv run pytest -q        # 439 tests, no network access
uv run ruff check .
```

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 to 4 | Database, discovery, qualification, personalisation, dashboard | Done |
| 5A | Owner-only Gmail sending infrastructure | Done |
| 5B.1 to 5B.3 | Live Tavily/Groq validation, search precision | Done |
| 5B.4 | Course-offer and FAQ extraction false negatives | Code done; live re-audit pending |
| 5B.5 | Validate real leads and drafts against source pages | Pending (needs live run) |
| 5B.6 | Owner-only live Gmail test | Pending (needs your OAuth client) |
| 6 | Gmail sync: replies, bounces, opt-outs, suppression | Done (mocked tests) |
| 7 | Approved follow-ups and conversion tracking | Done (mocked tests) |
| 8 | Final acceptance testing before any external outreach | Pending |

Detailed phase reports and live-audit metrics are in [`docs/PHASE_NOTES.md`](docs/PHASE_NOTES.md).
The full specification is in [`OUTREACH_AGENT_SPEC.md`](OUTREACH_AGENT_SPEC.md).
