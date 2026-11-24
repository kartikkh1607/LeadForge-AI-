# LeadForge AI — Phase 5B.3 discovery precision

## Phase 5B.3 implementation and original-run diagnosis

### Live comparison and final validation (2026-11-22)

One fresh bounded run completed with partial crawl outcomes in
`data/phase5b3-validation.sqlite3` (run 1). The complete report, source URLs,
qualification breakdowns, errors and search provenance are in
`data/5b3-live-results.json`. No extra search was made to fill the ten-candidate cap.

| Metric | Phase 5B.2 | Phase 5B.3 |
| --- | ---: | ---: |
| Candidates | 10 | 8 |
| Generic platform/directory/unrelated vendor results | 6 | 0 |
| Accessible creator/artist-led sites (manual assessment, not ownership verification) | 3 | 6 |
| Other results | 1 inaccessible creator tenant | 1 college, 1 inaccessible site |
| Machine-confirmed relevant course offers | 0 | 0 |
| Valid contact-source candidates | 4 | 4 |
| Source-matched FAQ pairs | 28, including false positives | 43 across 4 sites |
| Heuristic score >=60 | 1 | 1 |
| Qualified under the new evidence requirements | 0 | 0 |
| Compliance-reviewed eligible candidates | 0 | 0 |
| Saved source pages | 26 | 20 |
| Discovery search calls | 3 | 3 |
| Crawl requests, including robots/redirects | 42 | 42 |

The creator-site classification uses retrieved site/artist descriptions, not the
search title alone. It does not certify independent ownership. The six are
WatercolorOnline, Jenna Rainey, Watercolor Academy, Willa Wanders, Adventure Art
Academy and Jun-Pierre. Sessions College is relevant education but not the intended
small independent-creator profile. Art Makes Me Smart remains unverified because
fetching failed. Results are a small non-controlled sample; API rankings can change.

| New candidate | Score | Sourced FAQs | Valid contact source | Main blocker |
| --- | ---: | ---: | --- | --- |
| WatercolorOnline | 0 | 0 | No | HTML contains FAQ questions but no extracted answers; guessed routes 404 |
| Jenna Rainey | 40 | 5 | Yes | Course title, format and purchase evidence not joined into a supported offer |
| Watercolor Academy | 45 | 27 | No | Course-card layout missed; `support (at)` address not decoded |
| Willa Wanders | 60 | 6 | Yes | Course-offer evidence still incomplete; one oversized page |
| Sessions College | 55 | 5 | Yes | Course-offer evidence incomplete; institution outside intended creator profile |
| Adventure Art Academy | 15 | 0 | Yes | Title and $150 visible together, online format elsewhere; three guessed FAQ routes 404 |
| Art Makes Me Smart | 0 | 0 | No | Connection timeout followed by cached unavailable-robots blockers |
| Jun-Pierre ART | 0 | 0 | No | Old workshop URL redirected to artist/about content; no supported offer/contact/FAQ set |

Willa's false navigation pair was removed (7 to 6 FAQ pairs); Jenna improved from
zero to five genuine topical Q&A pairs. Watercolor Academy's 27 pairs are actual
FAQ questions, although some answers capture only the introductory paragraph and
miss a following list. Counts are therefore not a guarantee of answer completeness.
No navigation pair was counted in the inspected new results.

**The live test did not improve automatic course confirmation or draft readiness.**
Although nearby-wrapper extraction passes deterministic tests, several real layouts
separate titles, online format and purchase controls beyond those local wrappers.
Watercolor Academy's visible FAQ text even contains an online-course enrollment
offer that the course-heading detector misses. Missing extracted evidence is not
proof that no course is sold. Retaining the blocker is deliberate; no weaker score
threshold or compliance inference was introduced to obtain a positive result.

API usage this phase: three Tavily basic search calls; credits/cost were not exposed
by the adapter and are unmeasured. No Groq calls and no model tokens were consumed.
All eight candidates require human compliance review. Zero drafts, approvals, Gmail
connections, sends or follow-ups were created. Pause and dry-run remain enabled.

Full regression validation passed 392 tests both before and after the live run. Ruff lint/format checks
pass for 56 Python files. Integrity and foreign-key checks passed on the normal,
original integration and new validation databases after the run. Migration 007
was applied only after verified backups of the existing databases.

Recommended next milestone: capture representative split-layout course cards and
multi-paragraph FAQ fixtures, improve contextual offer assembly with exact source
spans, and prioritize homepage/contact fallback ahead of repeated guessed FAQ paths.
Also distinguish an education institution from the intended creator profile. Repeat
a similarly bounded quality audit before requesting human compliance review; do not
start prospect sending. Phase 5B.3 ends at this report.

The 25/25/20/15/15 scoring weights remain unchanged. Qualification now additionally
requires a verified relevant online course offer, a published contact source and at
least two distinct safe, sourced FAQ pairs. Human compliance review, DNS capability,
postal settings, suppression and all drafting/sending gates remain separate.
Old qualification snapshots without policy version 2 are stale and cannot authorize
drafting. Existing cached crawl observations are retained as historical evidence;
the new live comparison uses a fresh database, not those cached pages.

### What caused the original ten results

All ten lacked current human compliance review. Search snippets were never evidence.
`T` below is the original query `Watercolor courses online course FAQ teachable`;
`F` is `Watercolor courses online course FAQ thinkific`; `E` is
`Watercolor courses online course "hello@"`.

| Original candidate | Query / why returned | Score and extraction/qualification diagnosis |
| --- | --- | --- |
| MapQuest | T; Teachable HQ directory listing | 0; irrelevant directory; robots unavailable, no source pages |
| Tavily | T; company knowledge listing | 40 = FAQ 25 + contact 15; unrelated API vendor, no relevant course |
| CB Insights | T; Teachable alternatives page | 0; company directory; promotional question misidentified as FAQ; business name was a slogan |
| OnlineJobs.ph | T; course-platform developer job | 0; job listing, not course seller; robots unavailable |
| Michele's Thinkific subdomain | F; free pink-rose mini-course landing page | 0; plausible creator tenant, not the platform vendor; robots unavailable, so offer unverified |
| Ruzuku | F; Thinkific comparison article | 40 = FAQ 25 + activity 15; platform vendor; promotional questions and unrelated therapy content |
| Thinkific | F; platform FAQ | 45 = FAQ 25 + support 20; infrastructure provider, not watercolor creator |
| Brighter Day Press | E; intro-to-watercolor product URL | 15 contact points; homepage links displaced the actual product seed; unrelated Kindle FAQs extracted |
| Willa Wanders | E; online watercolor classes page | 60 = FAQ 25 + support 20 + contact 15; two oversized pages; narrow heading extraction missed offer context; `What Is A Willa Journal?` / `FAQs` was navigation, not a valid answer |
| Jenna Rainey | E; watercolor tutorial article | 15 contact points; course headings lacked nearby enrollment/format context; no FAQ pairs found within four pages |

Search overemphasized platform names; the crawler then compounded poor retrieval by
prioritizing homepage links over the search landing page. The qualifier only checked
the score threshold, allowing Willa to be marked qualified without course points
(the separate draft gate correctly blocked it). These are distinct problems.

### Changes and configuration

- Search queries now target online FAQs, self-paced enrollment, and artist workshops
  without requiring platform brand names. The existing three-query/candidate bounds
  still apply. No extra searches are automatically made to fill missing slots.
- `SEARCH_EXCLUDED_DOMAINS` is a comma-separated domain-and-descendants list;
  `SEARCH_EXCLUDED_HOSTS` is an exact-host list, after IDNA/lowercase/www normalization.
  Defaults exclude observed directories/vendors and generic platform roots. Thus
  `thinkific.com` is excluded but `michele.thinkific.com` is not. Existing marketplace
  exclusions remain mandatory. Configure additional values locally as shown in
  `.env.example`; these settings are not overwritten by dashboard preferences.
- Migration `007_search_provenance.sql` records each safe result occurrence, including
  provider, exact submitted query, result URL/title, timestamp, outcome and linked
  lead ID. Cross-query and historical duplicates retain their provenance rather than
  creating another lead. Unsafe URLs are rejected without persisting potential tokens.
  Historical first-discovery fields remain unchanged; missing old occurrences are
  not fabricated. Search metadata never becomes source evidence.
- Crawl the search landing page first; prioritize discovered FAQ/contact/course
  links ahead of guessed routes and unrelated links. Robots, SSRF, DNS pinning,
  response size, delays and request/page ceilings remain unchanged.
- Course observations retain title, source/landing URL, local supporting text and
  visible enrollment/purchase text. Nearby wrappers support nested HTML layouts.
  Observations alone are not confirmed offers: niche, online format, purchase signal
  and source matching must all pass qualification. No inferred price or availability.
- FAQPage JSON-LD is parsed without execution and accepted only when both question
  and answer occur in visible page text. Details/summary, explicit FAQ sections and
  aria-controls accordions are supported. Navigation/footer questions, common
  promotional prompts, navigation-only answers and unrelated generic headings are
  rejected. Short answers such as `No.` and `Forever.` remain valid.
- Evidence-ready means score >=60 plus all new evidence requirements. It does **not**
  mean compliance-reviewed, approved, mailbox-verified or sendable.

Changed implementation files: `agent/config.py`, `agent/search.py`,
`agent/pipeline.py`, `agent/scrape.py`, new `agent/extraction.py`,
`agent/web_models.py`, `agent/qualify.py`, migration 007, and
`scripts/validate_integrations.py`. Changed support files: `.env.example`, this
README, `tests/test_db.py`, `tests/test_qualify.py`, new `tests/test_precision.py`
and five `tests/fixtures/precision_*.html` fixtures. Network-failure fixtures are
deterministic mock responses, including a 2,000,001-byte body.

Backups before migration 007:
`data/backups/outreach-before-5b3-20261122T124317Z.sqlite3` and
`data/backups/integration-validation-before-5b3-20261122T124317Z.sqlite3`.
Both backups and migrated databases passed integrity and foreign-key checks.

## Local setup and live validation status

### Live API validation results (2026-11-22)

Tavily and Groq were contacted successfully with locally configured keys. No Gmail
connection, approval, send or follow-up was performed. Effective pause and dry-run
were verified on for both the normal and integration databases.

- Tavily connectivity: one basic search, max one result, one returned, 1.575 seconds.
  Discovery then used three basic queries and persisted exactly ten candidates.
  Four successful Tavily calls total; provider credit usage was not returned by
  the adapter and was not measured. An initial sandbox connection attempt failed.
- Groq model: `openai/gpt-oss-120b`. Four live synthetic calls consumed 1,139,
  1,255, 1,239 and 1,276 tokens (4,909 total), with measured latencies of 1.766,
  2.133, 1.924 and 1.831 seconds. The first three failed evidence/schema checks;
  all were rejected. An earlier sandbox attempt failed without reported usage.
  The final call passed structure, evidence and template validation, and the local
  injected unsupported-claim check. This is synthetic validation, not a real draft.
- Confirmed integration fix: the prompt now explicitly requires evidence for each
  selected question itself, rather than its answer, and the exact three/four claim
  set. Validators were not relaxed. Validation reports now identify the failing
  stage without exposing provider output or secrets. A regression reproduces the
  observed incorrect evidence list. The complete suite passes **382 tests**;
  Ruff lint and formatting pass (54 Python files).

The live SQLite run is `data/integration-validation.sqlite3`, run 1. It finished
partial with 26 saved source pages and 42 crawler requests (including robots and
redirect requests). The existing four-page per-site limit, 1.5-second default delay,
robots handling, DNS pinning, SSRF and response-size limits remained unchanged.
Database integrity and foreign-key checks passed. Detailed source URLs, contacts,
qualification evidence and stage outcomes are in ignored local
`data/5b2-live-results.json`; provider checks are in `data/5b2-*-*.json`.

| Candidate | Score | Saved pages | Verified FAQ pairs |
| --- | ---: | ---: | ---: |
| MapQuest | 0 | 0 | 0 |
| Tavily | 40 | 4 | 5 |
| CB Insights | 0 | 4 | 1 |
| OnlineJobs.ph | 0 | 0 | 0 |
| In the Studio with Michele (Thinkific subdomain) | 0 | 0 | 0 |
| Ruzuku | 40 | 4 | 8 |
| Thinkific | 45 | 4 | 5 |
| Brighter Day Press | 15 | 4 | 2 |
| Willa Wanders | 60 | 2 | 7 |
| Jenna Rainey | 15 | 4 | 0 |

The search templates produced unrelated directories, software vendors and a job
listing. FAQ counts establish source matching, not semantic quality: promotional
questions and the navigation pair `What Is A Willa Journal?` / `FAQs` inflated counts.
Brighter Day Press's extracted questions concerned Kindle books, not watercolor.
Course headings often lacked the adjacent online-format and purchase evidence
required by the conservative course criterion; none earned its 25 points. These
retrieval/extraction weaknesses remain for targeted improvement, with no claim that
missing extracted facts prove the businesses do not offer courses.

MapQuest, OnlineJobs.ph and Michele's site failed closed with `robots_unavailable`.
Two Willa pages exceeded the response-size limit. No bypass or extra discovery run
was attempted. Public contacts with source links were found at Tavily, Brighter Day
Press, Willa Wanders and Jenna Rainey; their domains had MX records, which does not
verify mailboxes or consent. Willa reached the heuristic threshold but lacked the
mandatory relevant-course evidence and human compliance review. Its real-lead
validation returned `blocked_compliance` before an API call. **Zero real drafts were
generated, saved or approved.** Postal configuration and human review also remain
draft-readiness requirements. Phase 5B.2 is not fully complete; no later phase began.

The setup-only record below predates this live run.

### Latest Phase 5B.2 verification (2026-11-22)

Phase 5B.2 is **pending live credentials and human authorization**, not complete.
The current workspace passes **381 regression tests**, Ruff lint and formatting
(54 Python files). Python is 3.14.4; offline lock verification, installed dependency
compatibility, and a locked sync dry-run passed with no environment changes.
Use a writable local cache and a fresh workspace test directory in restricted shells:

```powershell
uv --cache-dir .uv-cache lock --check --offline
uv --cache-dir .uv-cache pip check
uv --cache-dir .uv-cache sync --locked --offline --dry-run
.venv\Scripts\python.exe -m pytest -q --basetemp .test-tmp/phase5b2-next
.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
```

Ruff now explicitly excludes `.uv-cache` and `.test-tmp`, preventing dependency
cache files from being treated as application source in a checkout without Git metadata.
No application architecture or infrastructure was changed in this verification pass.

The fresh SQLite backup is
`data/backups/outreach-before-5b2-20261122T113124336083Z.sqlite3`.
Backup integrity passed before migration; all six existing migrations, database
integrity and foreign keys were then verified. There are zero leads, messages and
queue rows. Streamlit was launched at `http://127.0.0.1:8501`, its health endpoint
returned 200, and all five screens passed AppTest against the actual local database.
This is a programmatic screen check, not a visual browser inspection.

Both integration commands returned `not_run: Provider credential unavailable`.
Search quality, FAQ/contact extraction, false positives, real qualification scores,
Groq structured output, latency and token usage remain unmeasured. No OAuth or email
send was performed. Pause and dry-run remain on; general sending and follow-ups
remain disabled. The next step is to finish this phase's bounded live checks after
local credential configuration, not to begin a prospect-sending phase.

For Google setup, create/select a Google Cloud project and enable Gmail API.
Configure Google Auth Platform branding and audience; for a personal Gmail account
use External/testing and add your own account as a test user (Internal applies to
eligible Workspace organizations). Under Clients create a **Desktop app** OAuth
client and save its downloaded JSON as `E:\LeadForge AI\credentials.json`.
The app requests `gmail.send` and `gmail.readonly` (the latter supports account and
sent-message reconciliation). Follow the official
[Google quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python).
Do not run Google's sample token-file workflow: this application uses the OS vault.
Set `GMAIL_ACCOUNT` to the intended primary account and `OWNER_TEST_ADDRESSES` to
exact addresses you own. Local presence checks do not verify authentication or
ownership. OAuth requires separate explicit authorization; delivery requires a
second authorization, the existing message confirmation, and human inbox confirmation.

On 2026-11-22, Python 3.14.4, the locked environment and installed dependency
compatibility were verified. The actual `data/outreach.sqlite3` database was backed
up through SQLite's backup API to
`data/backups/outreach-before-5b2-20261122T070355000387Z.sqlite3` before migration.
The backup passed integrity validation. All six migrations were applied and verified;
the local database and queue are healthy, with zero leads and messages at setup.
Pause and dry-run are both enabled.

A local `.env` was created from `.env.example` without overwriting existing settings.
Configure secrets only in that ignored local file, never in chat or tracked files:

| Setting | Required local value / source |
| --- | --- |
| `SEARCH_PROVIDER` | `tavily` (default), or `serper` if using its key |
| `SEARCH_API_KEY` | Key from [Tavily](https://app.tavily.com/) or your Serper dashboard; only one provider is needed |
| `GROQ_API_KEY` | Key from [Groq Console](https://console.groq.com/keys) |
| `GROQ_MODEL` | Keep `openai/gpt-oss-120b` for initial integration validation |
| `GMAIL_ACCOUNT` | Primary Gmail account whose profile must match after authorized OAuth |
| `OWNER_TEST_ADDRESSES` | Comma-separated exact inbox addresses you personally own |
| `GMAIL_CLIENT_FILE` | `credentials.json`: downloaded **Desktop app** client JSON, not a web client or service account |
| `POSTAL_ADDRESS` | Valid physical address; enter/confirm it separately in dashboard Settings |
| `POSTAL_ADDRESS_CONFIRMED` | Initially `false`; requires genuine human verification |
| `DATABASE_PATH` | `data/outreach.sqlite3` |
| `DRY_RUN`, `PAUSED` | Both `true` during setup and ordinary validation |
| `DAILY_LIMIT` | Default `15`; may be lowered (1–15) |
| `SEARCH_RESULTS_PER_QUERY` | Default `10`; bounded validation independently caps the whole run at 10 websites |

Process environment values override `.env`. Saved dashboard preferences override
the non-secret settings they manage, including provider/model, postal address,
pause and dry-run. After editing `.env`, refresh/rerun the dashboard. It reloads
configuration on each script run. Never display or send the contents of `.env`.

The application was started on loopback at `http://127.0.0.1:8501`. Health returned
HTTP 200, and all five screens passed Streamlit AppTest against the actual local
database without creating queue entries. Browser automation was unavailable; this
was not a visual browser inspection. To start it again after closing it:

```powershell
uv run --locked streamlit run app.py --server.address 127.0.0.1 --server.port 8501
uv run --locked python -m agent.readiness
```

At the latest local check, search/Groq keys, desktop OAuth credentials and owner
allowlist were still absent. No live API connection, search, personalization or
delivery result is claimed. Setup remains pending those local values, human
compliance/postal reviews, and separate Gmail connection/send authorizations.

## Clearly marked owner-only tests

The existing template did not support adding a test label without invalidating
grounded-template validation. A narrow preparation command now adds the fixed
`Owner-only test: ` subject prefix and `This is an owner-only delivery test.` body
line, records that intent in the hashed personalization envelope, and revokes prior
approval. It validates the original grounded draft and requires the recipient to
be on the owner allowlist. It never changes the recipient, enqueues, or sends.
All normal evidence, compliance, suppression, length and postal checks still apply.

After genuine owner-address evidence and compliance review produce a draft:

```powershell
uv run --locked python -m agent.send_worker prepare-owner-test --message-id 42
```

Review the newly labeled draft and give both approval attestations again in the
dashboard, then explicitly enqueue it. A changed approval requires a refreshed
pending queue snapshot. Editing/rebuilding grounded slots may remove the label;
run preparation again before approval if needed. Do not substitute synthetic
fixtures or a discovered prospect address for a genuinely eligible owner record.

The `send-owner-test` CLI now restores both persisted pause and dry-run on exit,
including cancellation and credential errors. It retains the exact interactive
confirmation procedure. Forced process termination can prevent cleanup: check
Settings and restore both controls manually after a crash. Inbox receipt requires
your confirmation; Gmail acceptance alone is not proof of receipt. The commands
below remain gated by separate authorization for OAuth and for the live send.

## Integration validation and pre-launch audit

Phase 5B.1 adds local readiness diagnostics and explicitly invoked, bounded search
and Groq validation. General prospect sending and automatic follow-ups remain disabled.
No migration was added or changed in this phase. Existing databases must still have
all six migrations applied before full dashboard/queue use.

Confirmed audit fixes:

- Recheck the current Asia/Kolkata day's quota inside the final dispatch transaction,
  excluding only the message's own reservation. Update its quota day at dispatch.
  A claim crossing midnight can no longer use the previous day's allowance.
- Persist the 60–180 second gap after the API operation completes, so a slow request
  cannot consume the entire gap before the next worker invocation.
- Reject malformed/duplicate required MIME headers and alternate Sender headers
  before network access. Non-string Gmail IDs are ambiguous, not accepted outcomes.
- Sanitize OS credential backend initialization errors. Settings now displays the
  configured daily limit rather than the default constant.

Existing locks, conditional claims, version/hash/approval snapshots, current source
and compliance checks, permanent suppression, persisted Message-IDs, uncertain-send
recovery and account matching remain in place. The audit tests include real local
process locking, concurrent SQLite access, crash-after-acceptance recovery, source
and approval changes, allowlist enforcement and Streamlit rerun boundaries. These
checks cannot establish real provider behavior or prove absence of all concurrency
bugs. Keep one local database; copied databases do not share locks or quota.

### Readiness command and dashboard

```powershell
uv run --locked python -m agent.readiness
uv run --locked python -m agent.readiness --database data/integration-validation.sqlite3
```

The CLI opens SQLite in read-only mode: it does not create or migrate databases.
It checks integrity, foreign keys, migration checksums, effective settings, API-key
presence, desktop-client shape, owner allowlist, postal confirmation, queue health,
pause/dry-run and local OS-vault credentials. It never prints keys, tokens, raw vault
exceptions, credential JSON or provider responses. It does not refresh credentials,
open OAuth, or call Gmail. A local token may still be revoked remotely; configured
or present means **unverified**, not live-ready. Expired credentials are reported.

Settings → System readiness uses the same checks without vault access. Rerunning
Streamlit cannot refresh tokens, connect Gmail, enqueue, approve, or send messages.
Cached Gmail connection status is explicitly distinguished from current verification.
If the schema/preferences are unavailable, pause/dry-run are reported unknown rather
than presenting fallback defaults as verified persisted state.

### Bounded live discovery

Each command below is a separate run capped at **10 candidate websites**, three
search queries and four pages per business, using the existing SSRF, robots, DNS,
scraping, suppression and qualification checks. No messages are created or sent.
Search snippets remain discovery hints; compliance is never automatically approved.
The default validation database is separate from the normal outreach database.

```powershell
uv run --locked python -m scripts.validate_integrations search --live --niche "Watercolor courses"
uv run --locked python -m scripts.validate_integrations search --live --niche "Photography courses"
uv run --locked python -m scripts.validate_integrations search --live --niche "Music courses"
uv run --locked python -m scripts.validate_integrations search --live --niche "Language-learning courses"
uv run --locked python -m scripts.validate_integrations search --live --niche "Professional coaching courses"
```

JSON reports include discovered businesses, source-linked course offerings and
public contacts, verified FAQ counts/conflicts, heuristic scores, chatbot evidence,
compliance blockers and stage outcomes. Missing evidence is not a negative finding:
blocked or failed scraping can explain empty fields. Reports are public-source lead
data, so keep any redirected output local. Run without `--live` to view the plan only.

### Groq validation

```powershell
uv run --locked python -m scripts.validate_integrations groq --live
# After reviewing real leads through the existing dashboard, select at most three:
uv run --locked python -m scripts.validate_integrations groq --live --database data/integration-validation.sqlite3 --lead-id 1 --lead-id 2
```

Every invocation first calls Groq with a synthetic fixture in an in-memory database.
Only a successful synthetic validation permits selected real leads to proceed.
Real leads require current reviewed compliance, qualification, mail capability and
a configured postal address; they are opened read-only, and no draft or approval
is saved. Results report structured validity, evidence/template validity, measured
latency, provider total-token usage (null when absent), and a local injected-invalid-
claim rejection check. The negative check is a code-validator test, not a claim that
Groq itself rejected a fabricated statement. API errors are sanitized. Missing keys
produce `not_run`; no results are invented and no credentials are requested implicitly.

### Audit environment and remaining launch blockers

On 2026-11-22, local diagnostics found no search/Groq keys, Gmail account/token,
desktop OAuth client, owner allowlist or confirmed postal address. The OS credential
backend was available, but its ability to save/retrieve a real Gmail token was not
tested. The default database needs existing migrations; its queue and persisted
preferences cannot yet be assessed. It was not modified by this audit.

Both live validation commands returned `not_run` because credentials were absent.
No real businesses were discovered, no real lead-quality scores or provider latency/
usage were measured, and no Gmail connection or send was attempted. Live search,
Groq output quality, OAuth, revocation behavior, real vault persistence, Sent search
indexing and owner inbox delivery remain unverified. All automated provider tests
use mocks, and existing network-denial fixtures remain enabled.

Validation completed: 377 regression tests passed; six focused migration/upgrade
tests passed, including foreign-key/integrity checks. Ruff lint and formatting,
offline lockfile verification, and foundation/dashboard end-to-end smoke checks
passed. The security/pipeline subset also passed before the final additional
reconciliation and readiness tests, which are included in the 377-test full run.

### Google setup and separately authorized owner test

Only after separate authorization for Gmail setup, use an existing Google Cloud
project (replace `YOUR_PROJECT_ID`):

```powershell
gcloud services enable gmail.googleapis.com --project YOUR_PROJECT_ID
Start-Process 'https://console.cloud.google.com/auth/overview?project=YOUR_PROJECT_ID'
```

Configure consent/audience and your own test user in Google Auth Platform. Create a
Desktop app client under Clients and download its JSON to local `credentials.json`.
These console steps follow Google's [Gmail quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python);
the API-enable command is documented in [Enable Workspace APIs](https://developers.google.com/workspace/guides/enable-apis).
Set `GMAIL_ACCOUNT` and `OWNER_TEST_ADDRESSES` locally, leaving pause/dry-run on.
Back up the database while the app/workers are stopped before explicitly migrating:

```powershell
uv run --locked python -m agent.db
uv run --locked python -m agent.readiness
# Separately authorized connection only; these do not send email:
uv run --locked python -m agent.send_worker connect
uv run --locked python -m agent.send_worker check
```

For a separately authorized single owner send, review a genuine owner-address lead
under all existing compliance/evidence rules, confirm your postal address, and
approve its exact grounded draft with both attestations. Replace 42 below with its ID:

```powershell
uv run --locked python -m agent.send_worker enqueue --message-id 42
uv run --locked python -m agent.send_worker diagnose --message-id 42
```

Inspect blockers and the exact preview. Only for that authorized test, manually
disable pause/dry-run in Settings, then run the interactive command:

```powershell
uv run --locked python -m agent.send_worker send-owner-test --message-id 42
```

Type the exact per-message confirmation; there is no automatic confirmation flag.
Restore pause/dry-run immediately afterward. If the outcome is uncertain, only
reconcile (never reset/retry the row):

```powershell
uv run --locked python -m agent.send_worker reconcile
```

Phase 5B.1 stops at this audit. The Phase 5A and historical sections below remain
implementation history; no general prospect rollout is authorized or implemented.

The complete `OUTREACH_AGENT_SPEC.md` is the project source of truth. Implemented:
the Python foundation, SQLite persistence, typed models, DNS verification, evidence
helpers, safety checks, provider-based discovery, bounded public website scraping,
auditable qualification, reviewed compliance, evidence-checked Groq personalization,
and a local Streamlit dashboard with resumable discovery orchestration.
Phase 5A adds explicit Gmail OAuth and a persistent **owner-only test queue**.
General prospect sending remains disabled. The dashboard never sends email.
`DRY_RUN=true` and `PAUSED=true` remain defaults. Only a separately confirmed,
interactive, one-message CLI operation can send an owner test.

## Phase 5A architecture and boundaries

`agent/gmail.py` implements installed-app OAuth with PKCE and a loopback callback,
OS credential storage, MIME construction, and a mockable HTTPX Gmail adapter.
HTTPX is used directly rather than google-api-python-client so POST retry and
redirect behavior stays explicit. Access/refresh tokens and the OAuth client
secret in the authorized-user record are stored only in the OS credential vault
(Windows Credential Manager, macOS Keychain or Linux Secret Service). Unsupported
or plaintext keyring backends fail closed. No token file is created. The downloaded
desktop client file and `.env` must stay local; the standard filenames are ignored.

`agent/sender.py` owns queueing, eligibility, quota, claiming and reconciliation.
Migration `006_owner_outbound.sql` adds `outbound_queue`, `outbound_events` and cached
connection status without changing migrations 001–005. Back up existing databases
before running `python -m agent.db` to apply it.

- Queue rows bind recipient, authenticated account, approval timestamp, version and
  content hash. Explicit enqueueing while paused/dry-run is allowed; it does not
  authorize delivery. Re-enqueueing refreshes a changed **pending** snapshot and
  confirmation only. Attempted rows cannot be requeued.
- A shared nonblocking OS file lock serializes the worker with dashboard mutations.
  SQLite `BEGIN IMMEDIATE` and conditional state updates make claims atomic across
  connections. OS locks release after crashes; lock files are not deleted.
- The generated RFC Message-ID is persisted before dispatch. The claim commits
  before Gmail is called. Final preflight and the API call hold a SQLite write
  transaction so competing writes cannot change eligibility between check and send.
  This deliberately blocks other writers during a bounded request; readers still
  work in WAL mode. Use one local database, not copies or a network filesystem.
- Every existing approval/evidence/compliance/suppression/postal/DNS/lifecycle check
  runs again. Account and recipient must match the queue snapshot. The adapter also
  rejects extra recipients, Cc/Bcc, resent headers and addresses outside the explicit
  owner allowlist. Ownership is the developer's explicit attestation, not inferred
  from an email address. The primary authenticated Gmail address is the only From.
- `DAILY_LIMIT` is configurable from 1 through 15 (default 15), in `Asia/Kolkata`.
  Initial and historical follow-up sends share the quota. Reservations count too:
  rejected attempts consume that day's slot; unresolved attempts reserve a slot on
  every subsequent day until reconciled. No double counting of a sent queue row.
  A random 60–180 second delay is persisted across invocations; the worker exits
  with a blocker instead of sleeping or scheduling a later send.
- Gmail acceptance records message ID, thread ID, timestamp and sanitized outcome.
  Acceptance is not proof of inbox delivery. A definite 400/401/403/404/413/429
  response is terminal rejection. Timeouts, 5xx, malformed success and other
  unexpected responses remain uncertain; POSTs are never automatically retried.
- Reconciliation searches Sent for the persisted RFC ID, then verifies the exact
  Message-ID header and SENT label and records Gmail's internal timestamp. Empty,
  duplicate, forbidden or failed searches stay uncertain. **Absence from search is
  never evidence that retrying is safe.** A crash after acceptance but before local
  commit follows the same recovery path. Original outcomes remain in the event log.

The Send preview shows cached Gmail status, queue state, quota and owner dry-run
blockers, without reading credentials or calling Gmail. General message previews
remain non-sendable. No Gmail reply/bounce synchronization, alias use, follow-up
delivery, automatic worker loop or general prospect send command exists.

## Gmail OAuth setup (does not send email)

1. Run `uv sync --locked --group dev` and migrate the database. In Google Cloud,
   enable Gmail API, configure the OAuth consent screen and add your own Gmail
   account as a test user. Create a **Desktop app** OAuth client and save its JSON
   as local `credentials.json`. Keep it outside shared folders/backups where possible.
2. Set `GMAIL_ACCOUNT` to the primary Gmail account and `OWNER_TEST_ADDRESSES` to a
   comma-separated list of addresses you personally own. No wildcards, domains,
   display names or automatic account inclusion. Keep `DRY_RUN=true`, `PAUSED=true`.
   `GMAIL_CLIENT_FILE` defaults to `credentials.json`; protect and ignore any custom
   filename yourself. `DAILY_LIMIT=15` may be lowered.
3. Run `uv run --locked python -m agent.send_worker connect` in a local terminal.
   Complete consent in the system browser; the loopback callback times out after
   180 seconds. The authenticated profile must match `GMAIL_ACCOUNT` before tokens
   are saved. This is the [Google desktop OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app).
4. Run `uv run --locked python -m agent.send_worker check` for an explicit connection
   check. Expired credentials refresh through Google Auth; missing scopes or revoked
   credentials require reconnecting. To disconnect, revoke access in your Google
   account and remove the `LeadForge-Gmail` account entry from your OS credential manager.

Only `gmail.send` and `gmail.readonly` are requested. Read-only access is needed for
targeted Message-ID reconciliation: Gmail's `q` search parameter is unavailable
under `gmail.metadata`, per [messages.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list).
No modify, compose, full-mailbox or settings-write scope is requested. Alias discovery
is deferred. Google's current [sendAs.list scope list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.sendAs/list)
accepts `gmail.readonly`, so this scope set needs **no additional scope for read-only
alias listing**. `gmail.settings.basic` is another accepted scope, relevant if a
future implementation removes readonly or needs settings operations; do not add it
for Phase 5A. Alias editing is outside scope.

## Separately authorized owner-only test: exact procedure

Nothing in automated tests or smoke scripts performs these live steps. Do not use
the demo fixture address as a live recipient or substitute a prospect address.

1. Obtain separate authorization for one live owner test and complete OAuth setup
   above. Use a record whose recipient is an explicitly allowlisted address you own.
   That record must pass the unchanged qualification, source evidence and reviewed
   compliance policy. Use genuine supporting data; owner status does not bypass
   country/subscriber/contact evidence. If it cannot pass, stop at diagnostics.
2. Configure and separately confirm your real postal address in Settings. Rebuild
   the grounded draft if the address changes. Inspect its content and evidence,
   then approve with both truthful-identity and working-opt-out attestations.
3. While still paused/dry-run, explicitly enqueue the approved message (replace 42
   with its actual ID):
   ```powershell
   uv run --locked python -m agent.send_worker enqueue --message-id 42
   uv run --locked python -m agent.send_worker diagnose --message-id 42
   ```
   Review the queue and blockers in the dashboard. Enqueueing never sends.
4. Only for the authorized test, manually turn off global pause and dry-run in
   Settings. Persisted dashboard preferences override `.env` for these controls.
   Re-run diagnostics; resolve every blocker without weakening checks.
5. From an interactive terminal run:
   ```powershell
   uv run --locked python -m agent.send_worker send-owner-test --message-id 42
   ```
   Inspect the displayed From, To, subject and body. Type the exact displayed
   `SEND OWNER TEST ...` confirmation (bound to ID, recipient, hash and RFC ID).
   There is no `--yes`, environment confirmation or piped-input bypass. A mismatch
   sends nothing. A valid invocation can send only that one initial message.
6. Restore `PAUSED=true` and `DRY_RUN=true` immediately in Settings. Inspect the
   recorded outcome and your own mailbox. If uncertain or interrupted, run:
   ```powershell
   uv run --locked python -m agent.send_worker reconcile
   ```
   This only reads Gmail and updates local outcomes; it never sends. Live worker
   startup also reconciles outstanding attempts before dispatch. If unresolved,
   leave it uncertain and investigate manually; do not reset/retry the queue row.

Phase 5A stops here. Real Google consent, OS-vault integration and inbox delivery
have not been exercised by automated verification; all provider tests use mocks.
Historical Phase 1–4 documentation below describes the boundaries at those stages;
the Phase 5A sections above supersede its statements that Gmail is unimplemented.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Run from this directory:

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD '.uv-cache'
uv sync --locked --group dev
Copy-Item .env.example .env
uv run --locked python -m agent.db
```

Copy `.env.example` only when `.env` does not already exist. Dependencies are pinned
in `uv.lock`; setup downloads packages. Discovery and live scraping separately use
the network when explicitly invoked. Search requires `SEARCH_API_KEY`; direct scraping,
offline tests, and the foundation require no API keys. `.env`, OAuth files, databases,
caches, and logs are ignored.
The default database is `data/outreach.sqlite3`; override `DATABASE_PATH` or use:

```powershell
uv run --locked python -m agent.db --database data/test.sqlite3
```

`load_settings()` explicitly loads `.env` without mutating the process environment.
Environment variables take precedence. It does no I/O on import, rejects malformed
boolean values, masks API keys in model representations, and defaults `DRY_RUN=true`.
`POSTAL_ADDRESS` is empty and `POSTAL_ADDRESS_CONFIRMED=false` by default. Confirmation
means a human verified a valid physical address, not that software validated delivery.
`DAILY_LIMIT=15`, the Asia/Kolkata day, and the 60–180 second delay remain fixed policy
constants. Their worker enforcement is deferred with all sending functionality.

## Tests

```powershell
uv run --locked pytest -q
uv run --locked ruff check app.py agent tests scripts
uv run --locked ruff format --check app.py agent tests scripts
uv run --locked python -m scripts.smoke_test
uv run --locked python -m scripts.dashboard_smoke
```

For restricted Windows environments where pytest cannot access its default temp root:

```powershell
New-Item -ItemType Directory -Force .test-tmp | Out-Null
$env:PYTEST_DEBUG_TEMPROOT = (Resolve-Path .test-tmp).Path
uv run --locked pytest -q
```

Tests use temporary databases, synthetic `example.com` records, and injected DNS
resolvers. An automatic fixture blocks network socket connections, UDP sends, and
hostname resolution. The smoke check uses only an in-memory SQLite database.

## Layout and boundaries

```text
agent/
  config.py          Explicit settings and the specification's fixed initial template
  models.py          Validated Pydantic models and lifecycle enums
  db.py              Repository and migration CLI
  migrations/        Versioned SQL schema (checksummed; append-only after application)
  verify.py          Domain normalization, email syntax, injected DNS resolver
  compliance.py      Pure eligibility evaluation from explicit reviewed evidence
  suppression.py     Shared suppression API
  evidence.py        Source-specific quote matching and draft validation
  safety.py          Read-only preflight for a future sender
  search.py          Tavily/Serper adapters and domain-deduplicated discovery
  safe_http.py       DNS-pinned anonymous HTTP, robots policies, request budgets
  scrape.py          Pure HTML extraction and resumable crawl orchestration
  web_models.py      Structured source-linked observations and crawl results
scripts/
  smoke_test.py      Offline foundation check
  discover.py        Explicit search and scrape CLI
  scrape_smoke.py    Fixture or controlled live scraping smoke test
  smoke_sites.json   Three configured public smoke sites
templates/           Reserved for later drafting templates
data/                Local, ignored SQLite files
tests/               Offline unit and SQLite integration tests
```

This repository root is the spec's `outreach-agent/` directory; there is no extra
nested project. The remaining specified modules (Gmail, sender/worker, sync,
followup), executable worker scripts, and follow-up templates
are deferred. The initial and upgrade templates are in `config.py`.
No placeholder sender exists.

SQLite uses foreign keys, WAL, a busy timeout, unique business domains and email
addresses, bounded scores, and checked statuses. Each business can have at most one
initial and one follow-up message. Missing email addresses use SQL NULL. Migration
DDL and its version entry commit atomically; changed historical migrations and
unknown migration versions are rejected. Back up the database before future upgrades;
there is no downgrade command.

`Database` is a context manager. Call `migrate()` before repository operations.
Validated models cross the repository boundary; source snapshots and message claims
are persisted separately. JSON stores the message evidence and draft metadata.
Database constraints complement application checks; future application components
must use the repository API rather than editing SQLite directly.

## Safety contract

- Draft creation and approval check suppression. Draft creation requires verified
  quotes from stored text at each cited URL, including a hook and two distinct
  question claims. Invalid evidence saves no message and records `evidence failed`.
  Confidence scores cannot bypass evidence validation.
- Approval is an explicit operation requiring human confirmation of truthful
  identity/subject and a working opt-out. Editing content, changing the recipient,
  replacing source text, or reassessing compliance revokes existing approval.
- Suppression checks both business and mailbox domains, including subdomains.
  Opt-out, permanent-bounce, and spam-block email suppression also records the
  mailbox domain and any matching business domain. Pending messages are cancelled.
  Opt-outs cannot be deleted, downgraded, or overwritten, even by another suppression
  reason. Call `suppress(... reason='bounce')` only for a **permanent** bounce;
  temporary/unknown bounce classification and ingestion belong to the later sync phase.
- A reply or terminal lead stage cancels pending follow-ups. No follow-up can pass
  preflight yet because timing, reply synchronization, and threading are deferred.
- `check_send_eligibility(db, message_id, settings)` rereads persisted state and
  returns named blockers. It requires approval, no suppression, known mail capability,
  no pending DNS retry, reviewed compliance, verified evidence, reviewed truthful
  identity/opt-out, a confirmed nonempty postal address present in the body, and both
  dry-run and pause off. The function has no sending or queueing side effects.
- A passing preflight is **not** an atomic send permit. The future worker must call it
  immediately before sending with freshly loaded settings, while also enforcing its
  lock, daily quota, reply sync, delay, and Message-ID reconciliation. Gmail transport,
  race-safe claiming, and ambiguous-outcome retries are not implemented in Phase 1.

Compliance starts ineligible, including when no record exists. A supplied `eligible=true`
does not override evaluation. Records require a reviewer, timestamp, source URL,
country evidence, and subscriber evidence; they are bound to the assessed recipient.
The policy implements the spec's US/UK/CA/AU branches. Unknown subscriber types,
unknown countries, EU countries, and all other unsupported countries remain blocked.
CA/AU require affirmative publication and relevance evidence plus an explicitly false
refusal-statement flag; missing information never means consent. These are project
policy safeguards, not an independent determination of current law.

## Specification resolutions

| Ambiguity | Phase 1 decision |
| --- | --- |
| Lead `status=new`, `drafted`, or `sent` | Use `leads.stage=new`; Review selects `messages.status=draft`; sent messages correspond to `leads.stage=contacted`. No extra lead statuses. |
| Mail-capable vs. unconfirmed | New leads start `unconfirmed_mailbox`. Successful MX/implicit-MX checks become `domain_mail_capable`, which still does not confirm a mailbox exists. |
| Temporary DNS failures | Preserve the previous email status and persist `dns_retry_pending`, reason, and check time. Block preflight pending a successful retry; no automatic retry worker yet. |
| No MX and no A | Also check AAAA before rejecting: IPv6-only implicit MX is valid. Null MX always rejects without fallback. |
| Cancel pending follow-ups | Add message status `cancelled`; never disguise cancellation as a delivery failure. |
| Early bounce instructions vs. later refinement | Permanent bounces alone justify automatic bounce suppression. Temporary/unknown outcomes must not trigger automatic resend or suppression. |
| `sender.py`, `send_worker`, `run_worker.py` | Reserve `agent.send_worker` as the future canonical CLI, `sender.py` for worker logic, and `scripts/run_worker.py` as a thin optional wrapper. None implemented yet. |
| US eligibility vs. unknown subscriber rule | The explicit unknown-subscriber blocker takes precedence in every country, including US. Manual review must resolve uncertainty with evidence. |
| Follow-up leads `sent` | Later selection will use an initial message's `sent_at` and lead `stage=contacted`, with reply/suppression checks. |

Domain normalization removes `www`, URL paths/ports, trailing DNS dots, and case;
it IDNA-encodes international domains, preserves other subdomains, and rejects IPs,
credentials, and invalid labels. This is an identity normalizer, **not an SSRF
defense**. Phase 2 must independently implement DNS/IP checks and redirect safety.
Mailbox local-part case is retained in storage, while duplicate/suppression comparisons
are deliberately case-insensitive. v1 excludes SMTPUTF8 local parts. DNS semantics
follow [dnspython's resolver API](https://dnspython.readthedocs.io/en/stable/resolver-class.html)
and [RFC 7505 null MX rules](https://www.rfc-editor.org/info/rfc7505/).

## Phase 2 usage

Set `SEARCH_PROVIDER=tavily` (default) or `serper` and `SEARCH_API_KEY` in your local
`.env`. `SEARCH_RESULTS_PER_QUERY` defaults to 10 and is capped at 20. The three
specification query templates are used by default; the Python `discover()` API also
accepts up to ten custom niche templates. Provider endpoints are fixed HTTPS URLs.
API errors are sanitized; credentials and response bodies are never logged.

```powershell
# Discovery only: inserts new domains, never changes existing lead lifecycle.
uv run --locked python -m scripts.discover --niche "watercolor course"
# Explicitly opt into bounded scraping of the discovery results.
uv run --locked python -m scripts.discover --niche "watercolor course" --scrape
# Resume an existing new lead without using a search API.
uv run --locked python -m scripts.discover --lead-id 1
# Explicitly retry failed targets, at most three total attempts per target.
uv run --locked python -m scripts.discover --lead-id 1 --retry-failed
```

Dry-run controls future sending; it does not disable explicitly requested search or
public HTTP GET requests. No scraping operation performs email verification, automatic
qualification, personalization, approval, or sending. Suppressed and non-new leads
are skipped before fetching. Search titles/snippets are not trusted as site evidence.
No consent, country, subscriber type, or legal eligibility is inferred from discovery.

Migration `002_discovery.sql` adds `crawl_pages`; migration 001 is unchanged.
Each requested URL retains final URL, attempt count, status/error, timestamp, and typed
extraction JSON. Successful page text and fetched time are also written to the existing
`source_evidence` table in the same transaction. Business/owner names, emails, questions
and answers, course observations, and chatbot indicators carry source URLs and evidence.
Contact source URLs are in each email observation; they are not mistaken for the search
discovery URL or a reviewed compliance decision. Raw HTML is not stored wholesale;
selected HTML evidence such as chatbot script tags is retained in observations.

The crawler fills empty identity fields, retains previously selected email addresses,
and preserves unique-email constraints across businesses. It stores all extracted
contact evidence even when an email is already assigned elsewhere. Newly selected
emails remain `unconfirmed_mailbox`; lifecycle and compliance are not advanced. New
source evidence invalidates approvals and any existing compliance eligibility.

## Crawl limits and safety decisions

- Maximum **eight distinct requested page URLs per lead**, including failed targets;
  up to five redirects per page or robots request, and 64 total HTTP requests per run.
  Lower limits can be passed through `CrawlLimits`; these ceilings cannot be raised.
  The homepage and search landing page seed the frontier, together with the seven
  specified common paths. Observed relevant internal links take precedence over
  guessed paths so a useful discovered FAQ fits inside the eight-target budget.
- Only HTTP/HTTPS and their standard ports; no IP literals, credentials, query strings,
  restricted/login/account paths, forms, JavaScript execution, cookies, authentication,
  or environment proxies. Cross-domain redirects and HTTPS downgrades are rejected.
  Same-site means the same normalized host with only the `www` prefix difference allowed;
  unrelated subdomains and external learning platforms are not followed.
- Before every request, including robots and redirects, resolve A and AAAA records.
  Any unsafe address in the answer set blocks the request. Private, loopback,
  link-local, reserved, multicast, shared-address, mapped-IP and transition/tunnel
  ranges are rejected. Connect to a validated numeric IP with the original Host and
  TLS SNI/certificate hostname, preventing a second DNS lookup from rebinding the
  connection. HTTPS certificate verification remains enabled.
- `LeadForgeBot/0.2` fetches robots before content on each origin. Protego handles
  wildcard, agent-specific, and longest-match rules. Every content redirect destination
  is checked against its own origin's policy. Only 404/410 mean no robots file;
  401/403/429/5xx, network failures, invalid policy bodies, or unsafe robots redirects
  block crawling. Robots redirects may only target same-site `/robots.txt` URLs.
- Per-host waits default to 1.5 seconds after the preceding response. Honor larger
  `Crawl-delay` and `Request-rate` intervals; delays above 60 seconds defer the crawl
  instead of shortening the site's requested delay. Unsupported visit-time restrictions
  also defer. Use a single crawler process: cross-process rate coordination is not implemented.
- HTTPX connect/read/write/pool timeouts are 10 seconds, with an elapsed deadline
  checked on streamed body chunks. This is not a hard wall-clock process deadline:
  a blocked socket operation can last up to its timeout. DNS has a separate bounded
  lookup budget. Responses are streamed and limited to 2,000,000 bytes. Request identity
  encoding and reject compressed responses to avoid decompression bombs.
- HTML extraction is deterministic and heuristic. Support text emails, mailto links,
  both Cloudflare forms, details/summary FAQs, adjacent question/answer elements,
  explicitly identified people, course headings, and known chatbot script hosts.
  Keep short useful FAQs. Ignore platform emails, image filenames, hidden text,
  script text, no-reply addresses, and nofollow discovery links. Observations are
  untrusted data; metadata/title candidates are labelled by extraction method.

Successful pages are cached with their original timestamps; default resume performs
no refetch of completed targets. Failed targets are retried only with `--retry-failed`.
Policies are refreshed before new network work. There is deliberately no automatic
cache expiry or full refresh API yet. An interrupted run can reuse each committed page;
source evidence remains available when another page fails. Failure categories are
returned per URL rather than silently dropping the lead.

The transport follows the documented [HTTPX IP connection/SNI extension](https://www.python-httpx.org/advanced/extensions/).
Robots matching uses [Protego](https://github.com/scrapy/protego) and conservative
fetch/error handling informed by [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html).
The adapters follow the [Tavily API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
and [Serper API](https://serper.dev/).

## Phase 2 smoke validation

```powershell
# Default: three synthetic sites served by an in-process HTTPX mock, no network.
uv run --locked python -m scripts.scrape_smoke --output data/phase2-fixture-smoke.json
# Explicit live mode: two-page budget per configured public site, no search API.
uv run --locked python -m scripts.scrape_smoke --live --output data/phase2-live-smoke.json
```

The checked-in `scripts/smoke_sites.json` explicitly configures Python.org, Books to
Scrape, and Quotes to Scrape. No prospect websites are used for validation. The live
run on 2026-11-20 obtained two pages from Python.org, two from Books to Scrape, and
one from Quotes to Scrape. The latter site's author-page redirect was blocked as an
HTTPS-to-HTTP downgrade. Each site used three HTTP requests, including robots.
Detailed reports are local ignored files in `data/`; no secrets are included.
Search provider adapters are validated with mocked responses, not live API calls.

Example extracted fixture record (synthetic site; not a real prospect):

```json
{
  "url": "https://willow-art.com/",
  "business": "Willow Art School",
  "email": "ada@willow-art.com",
  "contact_source_url": "https://willow-art.com/",
  "faqs": [{"question": "When can I start?", "answer": "Start today."}],
  "course": "Watercolor Foundations Course",
  "chatbots": ["tidio"]
}
```

This compact display is derived from `PageData`; the full JSON additionally contains
all observations, extraction methods, source quotes, URLs, and fetched timestamps.

## Phase 3: qualification and personalization

Neither phase determines real recipient eligibility or validates an actual postal
address. Human review and real evidence remain required. No real contacts were used.
The spec's later Gmail alias lookup may require an additional settings API scope;
resolve the least-privilege scope when implementing Gmail. The template's word limit
must be checked after slot filling, including the address, in the personalization phase.

Limitations: no JavaScript rendering, CSS layout engine, authenticated crawling, query
URLs, cross-domain canonical merging, structured-data-only extraction, full-site refresh,
or concurrent-worker coordination. These conservative choices can miss valid leads and
FAQs. A script tag is evidence of an integration reference, not proof the chatbot is
active. Human review remains required for extracted facts, identities, and eligibility.

Phase 3 is implemented. `qualify_lead(db, lead_id)` scores persisted, matching crawl
snapshots using 25/25/20/15/15 weights and a 60-point threshold. Every assessment is
retained in `qualifications`, with criterion reasons, quotes and URLs. Unknown niche,
missing online sales evidence, duplicate FAQs, and unsupported contact observations
earn no points. Conflicting course availability or FAQ answers block qualification.
An isolated copyright year is not a credible activity signal; explicit publication,
update or open-enrollment text is required. Failed assessments retain stage `new`
for review; terminal lead stages never reopen.

Use `review_compliance(db, Compliance(...))` for an explicit human review. Supply
country and subscriber quotes in `supporting_evidence`, their source URLs, the
discovered contact source URL, reviewer and timezone-aware review timestamp. UK
company classification requires explicit Ltd/Limited/LLP/PLC evidence. CA/AU need
affirmative publication and role relevance, a sourced role explanation, and an
explicitly false refusal-statement flag. Software cannot establish legal registration
or certify that a refusal statement is absent across an entire website. Unknowns
remain ineligible. Reviews are retained in `compliance_reviews`; suppression wins.

After qualification, mail-domain verification and compliance review:

```python
from agent.config import load_settings
from agent.db import Database
from agent.personalize import personalize_lead, revise_draft
from agent.qualify import qualify_lead

settings = load_settings()
with Database(settings.database_path) as db:
    db.migrate()
    result = qualify_lead(db, lead_id)  # An existing scraped lead ID
    # Human compliance review must already be persisted before this explicit API call.
    draft = personalize_lead(db, lead_id, settings)
    # Review slots/evidence, then revise_draft(db, draft.id, revised_slots, settings).
    # Approval is a separate explicit db.approve_message(...) operation.
```

`GROQ_API_KEY`, `GROQ_MODEL` and a nonempty `POSTAL_ADDRESS` configure generation.
No import triggers network activity. Groq uses JSON output, a bounded timeout and
no automatic retries. Source text is untrusted data in a separate user message;
the system message permits only slot selection and supplies no tools. The model
selects exact FAQ answers/questions and explicitly observed first names. Code
rejects fabricated claims, paraphrases, mismatched URLs, duplicate questions,
injection-like slots, malformed JSON, duplicate keys and truncated responses.
There is no generic fallback. Confidence never establishes evidence or approval.

Exact extractive hooks are deliberately conservative: `Your FAQ says: "..."`.
This avoids treating a quote's mere presence as proof of an unrelated paraphrase.
The persisted envelope includes slot evidence, subject evidence, source-context
hash and template version. Sources, contact identity, niche or chatbot changes
require fresh validation. The fixed template enforces fewer than 170 words,
sender identity, one portfolio link, opt-out text, a postal address, and conditional
"can answer" wording. Detected chatbot integrations select the upgrade variant;
a script reference does not prove a working chatbot. Human confirmation of a valid
physical postal address remains required by future-send preflight.

`personalize_lead` creates only an initial `draft`. One message per lead/kind is
enforced by SQLite and duplicate generation is blocked before calling Groq.
`edit_message` revokes approval; arbitrary edits cannot be reapproved unless they
match a validated template and evidence envelope. Use `revise_draft` to change
grounded slots. Content changes produce SHA-256 hashes and retained versions.
`reject_message` records an explicit rejection reason; suppression cancels pending
messages. Rejected/cancelled messages cannot be approved or silently regenerated.

Migration 003 adds qualification history, reviewed source evidence, managed drafts,
rejected status and version history. Migration 004 revokes legacy approvals lacking
an evidence envelope and legacy compliance lacking source hashes, preserving content.
Historical migrations 001–003 are unchanged. Legacy drafts remain readable/editable
but cannot pass approval or preflight; migrate their content through reviewed slots
before future use. No actual sending exists.

Offline verification (no real contacts, network or API key):

```powershell
uv run --locked python -m scripts.personalize_smoke
```

This prints three synthetic drafts through the real SDK with an HTTP mock: standard,
upgrade, and unnamed-recipient variants. The fixture scores 100/100. Tests cover the
55/60 threshold boundary, missing/conflicting evidence, malicious model output and
page instructions, API failures, suppression during generation, edits, duplicate
drafts, unknown compliance, migration upgrades, integrity and foreign keys.

Limitations: extraction and scoring are conservative heuristics; exact quotations
can sound plain and long valid FAQs can exceed the template's word budget. No
semantic entailment model, automatic source expiry, legal verification,
or sending-worker guarantees are provided. Live Groq availability/model access
has not been tested; all generation validation uses mocks.

## Phase 4: local dashboard

Start the application from the repository root:

```powershell
uv sync --locked --group dev
uv run --locked streamlit run app.py
```

Open `http://127.0.0.1:8501`. The checked-in Streamlit configuration binds to loopback,
disables usage telemetry and uses a restrained blue theme. This is a local personal
workspace, without authentication or a public deployment. No screen has a send action.

| Section | Implemented behavior |
| --- | --- |
| Discover | Niche, provider, 1–3 queries, 1–20 results/query, 1–30 businesses, 1–8 pages/business; explicit start/resume/retry; persisted progress and results. |
| Review | Business/contact provenance, original pages and FAQs, DNS results, qualification reasons, sourced compliance review, explicit Groq generation, grounded slot choices, text edits with validation, approve/reject/suppress. |
| Send (preview only) | Approved messages, all blockers, suppression records, compliance status, postal readiness, daily limit, dry-run and pause; always reports sending unavailable. |
| Tracker | Real lead and message counts, lifecycle/score filters, sorting, compliance blockers, suppression records and discovery history. |
| Settings | Key-presence indicators only; provider/model preferences, fixed daily limit, dry-run/pause controls, postal address and compliance review forms. |

`app.py` contains presentation and form handling. `agent/dashboard.py` owns read
models and explicit actions; it calls the existing Phase 1–3 repository, evidence,
compliance and personalization functions. `agent/pipeline.py` orchestrates existing
search, scraping, DNS and qualification modules. It stops at the human compliance
gate; draft generation is a separate Review action after eligibility is established.
No automatic country inference, approval, sending, or Gmail connection was added.

`agent/settings_store.py` accepts only allowlisted non-secret preferences. Environment
variables still override `.env` when loading base configuration. Once saved in the
dashboard, SQLite preferences override those base **non-secret** fields. API keys
remain exclusively in the environment/`.env`; neither forms nor SQLite store them.
Secrets files, local databases and lock files are ignored. Dry-run and global pause
default to **true**. An explicit saved preference or environment setting can change
them, but neither enables transport in this phase. Pause describes the future sending
gate and does not prevent explicitly requested discovery.

Changing a postal address revokes approvals and resets postal confirmation. Save
the address first, then confirm it separately. Duplicate submissions cannot perform
that second confirmation. Existing drafts must be rebuilt with the new address
through grounded-slot editing. Confirmation remains a human assertion, not postal
delivery validation.

Migration `005_dashboard.sql` adds preferences, discovery runs/steps/memberships and
action receipts. Historical migrations 001–004 are unchanged. Each search query and
each business stage commits progress. Completed stages are reused on reruns;
ordinary rendering performs no discovery, scraping, DNS or Groq requests. Search
and draft uniqueness constraints remain authoritative. Form receipts prevent replay
from repeating an approval after an edit. Review tokens detect changed evidence,
messages, compliance or settings before an action can write.

`agent/operations.py` uses a nonblocking thread and OS file lock around dashboard
mutations. It prevents overlapping dashboard operations on the same local database;
the OS releases the lock if a process exits. A saved `running` stage can be resumed
explicitly after interruption. A query interrupted before its result is recorded may
be requested again; domain uniqueness prevents duplicate businesses. Failed/partial
stages retry only on explicit action, at most three attempts. Successful search
queries are not repeated during retry. Partial page evidence remains available.
Policy-blocked stages appear separately from technical failures.

Edits always revoke approval. Free-form subject/body edits may be saved as drafts,
but unsupported text cannot be approved. Phase 3's strict extractive template remains
the validation standard: use sourced slot selectors to rebuild valid personalization.
Rejection and suppression remain separate terminal message states. All source content
is rendered as text or table data, never executable scraped HTML.

### Reproducible offline UI walkthrough

The demo contains **synthetic data**, including a fictional postal address. It makes
no search, website, DNS or live Groq requests. To create a fresh isolated demo database:

```powershell
# Run once. If this fixture file already exists, skip this command or choose a new path.
uv run --locked python -m scripts.dashboard_smoke --database data/phase4-demo.sqlite3
$env:DATABASE_PATH = Join-Path $PWD 'data/phase4-demo.sqlite3'
uv run --locked streamlit run app.py
```

1. Open Discover. Inspect run 1, the saved stages, and Willow Art School at 100/100.
   Do not start a new discovery run during the offline walkthrough.
2. Open Review. Expand original pages, FAQ evidence, DNS results and compliance.
   Inspect the unapproved draft and exact supporting quotes.
3. In grounded personalization, select another FAQ answer and save. Check that the
   version changes and the message remains a draft. Verify the two attestations,
   then approve. Nothing is sent.
4. Edit the body by adding an unsupported statement and save. Approval disappears,
   validation fails, and Approve is disabled. Rebuild using grounded slots to restore
   a valid draft, then review again.
5. Open Send preview. Confirm dry-run/pause are on, postal readiness is unconfirmed,
   and sending is unavailable. There is no send or Gmail button.
6. Open Tracker and try lifecycle and score filters. Open Settings to inspect
   configuration status; save a preference and reload to verify persistence.
7. Optionally suppress the demo email in Review. Its pending message becomes
   cancelled, and the record appears in Send preview and Tracker.

The smoke command without `--database` uses an in-memory database. With a path it
refuses to overwrite an existing file. Restore your normal `DATABASE_PATH` or close
that PowerShell session after the walkthrough.

### Phase 4 validation and limits

The full suite includes deterministic service and orchestration tests, plus Streamlit
[AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest)
checks of all five empty/populated screens, approval attestations, rerenders, settings
persistence and missing-key errors. Tests prohibit outbound networking. Windows
AppTest permits only asyncio's internal loopback socket-pair creation at its stdlib
call site; application socket connections and DNS remain blocked.

Known limits: execution is synchronous and a bounded crawl can take time; progress
updates between stages. There is no background scheduler. Refreshing resumes only
when explicitly requested. Existing qualified/terminal leads are not automatically
recrawled; Phase 2's cache and lifecycle rules remain in effect. Dashboard locks
coordinate dashboard clients, not unrelated scripts writing directly to SQLite.
The UI is tested through AppTest; the walkthrough is provided instead of pixel-based
browser screenshots. Live provider behavior and actual compliance decisions were
not exercised. No secrets or real prospect data are used in fixtures.

Recommended Phase 5, only after separate authorization: implement the Gmail OAuth
adapter and a separate worker, test solely against the owner's second address,
then add atomic queue claims, fresh eligibility checks, quota/delay/pause enforcement,
Message-ID reconciliation and failure recovery. Reply/bounce synchronization and
follow-ups should remain separately staged. Phase 4 stops here; transport is absent.
