"""LeadForge's local Streamlit presentation layer. Never sends email."""

import uuid

import streamlit as st

from agent.config import load_settings
from agent.dashboard import Dashboard, pipeline_view, selected_slots
from agent.db import Database
from agent.models import LeadStage
from agent.operations import ActionError, fingerprint
from agent.pipeline import Pipeline, RunConfig, create_run, retry_run, run_snapshot
from agent.search import QUERY_TEMPLATES
from agent.settings_store import preferences_for, save_preferences


def action_key(name):
    return st.session_state.setdefault("action:" + name, uuid.uuid4().hex)


def perform(name, callback, success="Saved. Review the updated state below."):
    try:
        callback()
        st.session_state["notice"] = ("success", success)
    except ActionError as exc:
        st.session_state["notice"] = ("warning", str(exc))
    except Exception:  # noqa: BLE001 -- never render secrets from arbitrary provider exceptions
        st.session_state["notice"] = (
            "error",
            "Action failed. Check inputs and configuration; no email was sent.",
        )
    st.session_state.pop("action:" + name, None)
    st.rerun()


def table(rows):
    if rows:
        st.dataframe(
            rows,
            hide_index=True,
            width="stretch",
            column_config={
                key: st.column_config.LinkColumn(key.replace("_", " ").title())
                for key in ("url", "source_url", "website", "requested_url")
                if any(key in row for row in rows)
            },
        )
    else:
        st.info("No records yet.")


def show_discover(service):
    st.title("Discover")
    st.caption("Find course businesses, collect website evidence, and assess fit.")
    settings = service.settings
    if not settings.search_api_key:
        st.warning("Search is not configured. Add SEARCH_API_KEY locally before starting.")
    with st.form("discover"):
        niche = st.text_input("Target niche", placeholder="e.g. watercolor courses", max_chars=200)
        a, b, c = st.columns(3)
        provider = a.selectbox(
            "Search provider",
            ["tavily", "serper"],
            index=["tavily", "serper"].index(settings.search_provider),
        )
        results = b.number_input("Results per query", 1, 20, settings.search_results_per_query)
        queries = c.number_input("Search queries", 1, len(QUERY_TEMPLATES), len(QUERY_TEMPLATES))
        a, b = st.columns(2)
        businesses = a.number_input("Maximum businesses", 1, 30, 20)
        pages = b.number_input("Pages per business", 1, 8, 8)
        st.caption(
            "Starting makes search, public website and DNS requests. Compliance review and drafting remain explicit steps."
        )
        start = st.form_submit_button("Start discovery", type="primary")
    if start:

        def start_run():
            config = RunConfig(
                niche=niche,
                provider=provider,
                results_per_query=results,
                query_count=queries,
                max_businesses=businesses,
                max_pages=pages,
            )
            run_id = create_run(service.db, config, action_key("discovery_request"))
            st.session_state["active_run"] = run_id
            execute_pipeline(service, run_id)

        perform(
            "discover", start_run, "Discovery progress saved. Review results and compliance below."
        )
    if st.button(
        "Prepare a new discovery request",
        help="Use after a completed run to intentionally search again.",
    ):
        st.session_state.pop("action:discovery_request", None)
        st.success("Ready for a new request. Enter a niche and start explicitly.")
    history = service.history()
    if not history:
        st.info("Your first discovery run will appear here. No requests run until you start.")
        return
    ids = [r["id"] for r in history]
    active = st.session_state.get("active_run", ids[0])
    run_id = st.selectbox("Discovery run", ids, index=ids.index(active) if active in ids else 0)
    snapshot = run_snapshot(service.db, run_id)
    done = sum(s["status"] not in {"pending", "running"} for s in snapshot["steps"])
    st.progress(
        done / max(1, len(snapshot["steps"])),
        text=f"{done}/{len(snapshot['steps'])} stages recorded · {snapshot['status']}",
    )
    a, b = st.columns(2)
    if a.button("Resume pending stages", disabled=snapshot["status"] in {"complete", "partial"}):
        perform(
            "resume", lambda: execute_pipeline(service, run_id), "Run resumed from saved progress."
        )
    if b.button(
        "Retry failed stages",
        help="Explicit retry, capped at three attempts. Successful searches stay cached.",
    ):

        def retry():
            retry_run(service.db, run_id)
            execute_pipeline(service, run_id)

        perform("retry", retry, "Retry results saved.")
    table(
        [
            {
                "stage": s["stage"],
                "target": s["target"],
                "status": s["status"],
                "attempts": s["attempts"],
                "result": str(s["result"]),
            }
            for s in snapshot["steps"]
        ]
    )
    st.subheader("Discovered businesses")
    table([r for r in service.leads() if r["id"] in snapshot["lead_ids"]])
    st.caption("Open Review for original pages, FAQ evidence, DNS results and compliance controls.")


def execute_pipeline(service, run_id):
    progress = st.progress(0, text="Starting saved stages…")

    def update(snapshot):
        completed = sum(s["status"] not in {"pending", "running"} for s in snapshot["steps"])
        progress.progress(
            completed / max(1, len(snapshot["steps"])),
            text=f"{completed} stages recorded · {snapshot['status']}",
        )

    Pipeline(service.db, service.settings).run(run_id, update)


def choose_lead(service, key):
    rows = service.leads()
    if not rows:
        st.info("No businesses yet. Start in Discover.")
        return None
    labels = {r["id"]: f"{r['business']} · {r['score']}/100 · {r['stage']}" for r in rows}
    return st.selectbox("Business", list(labels), format_func=labels.get, key=key)


def compliance_form(service, view, prefix):
    lead, record = view["lead"], view["compliance"]
    if record:
        st.text(f"{record.reason}\nReviewer: {record.reviewed_by or 'Not reviewed'}")
        table([e.model_dump() for e in record.supporting_evidence])
    urls = list(view["sources"])
    if not urls:
        st.info("Persisted website evidence is required before a compliance review.")
        return
    name = f"{prefix}:compliance:{lead.id}:{view['token'][:12]}"
    with st.form(name):
        st.caption(
            "Classify only from source evidence. A public email is not consent; unknowns stay blocked."
        )
        country = st.selectbox(
            "Recipient country", ["Unknown", "US", "UK", "CA", "AU", "EU / other unsupported"]
        )
        subscriber = st.selectbox(
            "Subscriber classification", ["unknown", "company", "sole_trader_or_individual"]
        )
        contact_url = st.selectbox("Contact address source", urls)
        country_url = st.selectbox("Country evidence source", urls)
        country_quote = st.text_input("Exact country evidence quote")
        subscriber_url = st.selectbox("Subscriber evidence source", urls)
        subscriber_quote = st.text_input("Exact subscriber evidence quote")
        role_url = st.selectbox("Role evidence source (CA/AU)", urls)
        role_quote = st.text_input("Exact business-role evidence quote (CA/AU)")
        published = st.checkbox("I verified conspicuous publication of this address")
        refusal = st.selectbox(
            "Statement refusing unsolicited messages",
            ["Not reviewed", "Present", "Reviewed and absent"],
        )
        relevant = st.checkbox("I verified this pitch is relevant to the recipient's business role")
        reviewer = st.text_input("Reviewer name")
        confirmed = st.checkbox("I reviewed these sources and classifications myself")
        save = st.form_submit_button("Save compliance review")
    if save:
        if not confirmed:
            st.warning("Confirm that you reviewed the source evidence before saving.")
            return
        evidence = [
            {"claim": label, "quote": quote, "url": url}
            for label, quote, url in (
                ("Country", country_quote, country_url),
                ("Subscriber", subscriber_quote, subscriber_url),
                ("Business role", role_quote, role_url),
            )
            if quote.strip()
        ]
        values = {
            "country": None
            if country == "Unknown"
            else "DE"
            if country.startswith("EU")
            else country,
            "subscriber_type": subscriber,
            "contact_source_url": contact_url,
            "country_evidence": country_quote,
            "subscriber_evidence": subscriber_quote,
            "role_evidence": role_quote,
            "supporting_evidence": evidence,
            "conspicuously_published": published,
            "role_relevant": relevant,
            "no_unsolicited_statement": {
                "Not reviewed": None,
                "Present": True,
                "Reviewed and absent": False,
            }[refusal],
            "reviewed_by": reviewer,
        }
        perform(
            name,
            lambda: service.compliance_review(lead.id, view["token"], action_key(name), values),
            "Compliance review saved. Eligibility is determined by policy and evidence.",
        )


def show_review(service):
    st.title("Review")
    st.caption("Inspect the evidence. Make an explicit decision for each business.")
    lead_id = choose_lead(service, "review_lead")
    if lead_id is None:
        return
    view = service.inspect(lead_id)
    lead, token = view["lead"], view["token"]
    a, b, c = st.columns(3)
    a.metric("Qualification", f"{lead.score}/100")
    b.metric("Compliance", "Eligible" if view["eligible"] else "Review required")
    c.metric("Mail domain", lead.email_status.replace("_", " "))
    st.text(f"Website: {lead.website or lead.domain}\nRecipient: {lead.email or 'Not discovered'}")
    contacts = [
        e.model_dump() for page in view["pages"] for e in page.emails if e.value == lead.email
    ]
    with st.expander("Qualification, contact and verification evidence", expanded=True):
        if view["qualification"]:
            table(
                [
                    {
                        "criterion": key,
                        "points": value.points,
                        "reason": value.reason,
                        "evidence": str([e.model_dump() for e in value.evidence]),
                    }
                    for key, value in view["qualification"].criteria.items()
                ]
            )
        else:
            st.warning("Qualification is missing or stale.")
        table(contacts)
        st.json(view["dns"])
        if st.button("Reassess qualification"):
            perform("reassess", lambda: service.reassess(lead_id, token, action_key("reassess")))
    with st.expander("Original website pages and FAQ content"):
        for url, text in view["sources"].items():
            st.text(url)
            st.text(text)
        table([q.model_dump() for page in view["pages"] for q in page.faqs])
        table(
            [
                {k: r[k] for k in ("requested_url", "state", "attempts", "error")}
                for r in view["crawl"]
            ]
        )
    with st.expander("Human compliance review"):
        compliance_form(service, view, "review")
    if not view["messages"]:
        st.info("No draft yet. Resolve compliance and verification, then request a grounded draft.")
        if st.button("Generate grounded draft", disabled=not view["eligible"], type="primary"):
            perform(
                "generate",
                lambda: service.generate(lead_id, token, action_key("generate")),
                "Draft generated. Human approval is still required.",
            )
    for message in view["messages"]:
        st.subheader(f"{message.kind.title()} message · {message.status}")
        st.caption(f"Version {message.version} · {len(message.body.split())} words")
        validation = view["validations"][message.id]
        if validation == "Valid grounded draft":
            st.success(validation)
        else:
            st.warning(validation)
        st.text(message.subject)
        st.text(message.body)
        with st.expander("Supporting quotes and source URLs"):
            table([e.model_dump() for e in message.evidence])
            if message.personalization and message.personalization.get("subject_evidence"):
                st.json(message.personalization["subject_evidence"])
            for missing in message.missing_information:
                st.text(missing)
        if message.status in {"draft", "approved"}:
            draft_controls(service, view, message)
    with st.expander("Suppress this recipient"):
        name = f"suppress:{lead_id}:{token[:12]}"
        with st.form(name):
            kind = st.selectbox("Suppression scope", ["email", "domain"])
            reason = st.selectbox("Suppression reason", ["manual", "opt_out"])
            st.caption(
                "Cancels pending messages. Opt-outs are permanent and also suppress matching domains."
            )
            if st.form_submit_button("Suppress recipient"):
                perform(
                    name, lambda: service.suppress(lead_id, token, action_key(name), kind, reason)
                )


def draft_controls(service, view, message):
    token = view["token"]
    suffix = f"{message.id}:{token[:12]}"
    context = view["slot_choices"]
    if context and message.personalization:
        with st.expander("Edit grounded personalization"):
            name = "slots:" + suffix
            saved = message.personalization["slots"]
            faqs = context["faqs"]

            def selected(field, value):
                return next((i for i, q in enumerate(faqs) if q[field] == value), 0)

            with st.form(name):
                hook = st.selectbox(
                    "Hook quote",
                    range(len(faqs)),
                    index=selected("answer", saved["hook_line"]),
                    format_func=lambda i: faqs[i]["answer"],
                )
                q1 = st.selectbox(
                    "First FAQ question",
                    range(len(faqs)),
                    index=selected("question", saved["q1"]),
                    format_func=lambda i: faqs[i]["question"],
                )
                q2 = st.selectbox(
                    "Second FAQ question",
                    range(len(faqs)),
                    index=selected("question", saved["q2"]),
                    format_func=lambda i: faqs[i]["question"],
                )
                names = ["Hi there"] + [n["first_name"] for n in context["names"]]
                first = st.selectbox(
                    "Recipient first name",
                    range(len(names)),
                    index=names.index(saved["first_name"]) if saved["first_name"] in names else 0,
                    format_func=lambda i: names[i],
                )
                if st.form_submit_button("Save grounded slots"):
                    perform(
                        name,
                        lambda: service.edit(
                            message.id,
                            token,
                            action_key(name),
                            slots=selected_slots(context, hook, q1, q2, first),
                        ),
                    )
    with st.expander("Edit subject and body"):
        name = "text:" + suffix
        with st.form(name):
            subject = st.text_input("Subject", message.subject)
            body = st.text_area("Body", message.body, height=300)
            st.caption(
                "Saving revokes approval. Text must match the grounded template to pass revalidation; use grounded slots to change factual claims."
            )
            if st.form_submit_button("Save text and revalidate"):
                perform(
                    name,
                    lambda: service.edit(
                        message.id, token, action_key(name), subject=subject, body=body
                    ),
                    "Edit saved and approval revoked. Check validation before approving.",
                )
    name = "approve:" + suffix
    with st.form(name):
        identity = st.checkbox("I confirm the sender identity and subject are truthful")
        opt_out = st.checkbox("I confirm the opt-out instruction works and will be honored")
        approve = st.form_submit_button(
            "Approve draft",
            type="primary",
            disabled=message.status != "draft"
            or view["validations"][message.id] != "Valid grounded draft",
        )
    if approve:
        perform(
            name,
            lambda: service.approve(
                message.id,
                token,
                action_key(name),
                identity_confirmed=identity,
                opt_out_confirmed=opt_out,
            ),
            "Draft approved for review records. Nothing was sent.",
        )
    name = "reject:" + suffix
    with st.form(name):
        reason = st.text_input("Rejection reason")
        if st.form_submit_button("Reject draft"):
            perform(name, lambda: service.reject(message.id, token, action_key(name), reason))


def show_send(service):
    st.title("Send preview")
    st.info(
        "Read-only preview. General prospect sending is disabled. Owner tests require the separate interactive CLI."
    )
    preview = service.send_preview()
    a, b, c, d = st.columns(4)
    a.metric("Daily limit", preview["daily_limit"])
    b.metric("Dry-run", "On" if preview["dry_run"] else "Off")
    c.metric("Global pause", "On" if preview["paused"] else "Off")
    d.metric("Postal address", "Confirmed" if preview["postal_ready"] else "Not ready")
    st.caption(
        f"Asia/Kolkata quota used/reserved: {preview['quota_used']}. Uncertain outcomes retain a reservation."
    )
    st.subheader("Gmail connection (last explicit check)")
    table(preview["gmail"])
    st.caption(
        "No record means not connected. Status is cached; use python -m agent.send_worker check to verify. This screen never accesses credentials or Gmail."
    )
    st.subheader("Owner-only outbound queue")
    table(preview["queue"])
    st.subheader("Owner test dry-run diagnostics")
    table(preview["owner_diagnostics"])
    st.subheader("Approved messages")
    table([r for r in preview["messages"] if r["status"] == "approved"])
    st.subheader("All message blockers")
    table(preview["messages"])
    st.subheader("Suppression records")
    table(service.suppressions())


def show_tracker(service):
    st.title("Tracker")
    st.caption("Current records from your SQLite database. No estimated delivery metrics.")
    messages = service.messages()
    a, b, c = st.columns(3)
    a.metric("Businesses", len(service.leads()))
    b.metric("Drafts", sum(m.status == "draft" for m in messages))
    c.metric("Approved", sum(m.status == "approved" for m in messages))
    a, b, c, d = st.columns(4)
    stage = a.selectbox("Lifecycle stage", ["All"] + [s.value for s in LeadStage])
    score = b.number_input("Minimum score", 0, 100, 0)
    sort = c.selectbox("Sort by", ["newest", "score", "business", "stage"])
    descending = d.checkbox("Descending", True)
    table(
        service.leads(
            stage=None if stage == "All" else stage,
            minimum_score=score,
            sort=sort,
            descending=descending,
        )
    )
    show_pipeline(service)
    st.subheader("Suppression records")
    table(service.suppressions())
    st.subheader("Discovery history")
    table(service.history())


def show_pipeline(service):
    view = pipeline_view(service.db, service.settings)
    stats = view["summary"]
    st.subheader("Outreach pipeline")
    st.caption(
        "Recorded facts only. Both buttons below are read-only: they never send, label or "
        "delete mail."
    )
    left, right = st.columns(2)
    subject = left.text_input(
        "Subject contains",
        "quick idea",
        help="Which sent emails to import. Leave empty to import every sent email.",
    )
    days = right.number_input("Look back (days)", 1, 365, 180)
    left, right = st.columns(2)
    if left.button(
        "Import sent emails from Gmail",
        help="Marks leads you already emailed from Gmail as contacted, so they are tracked "
        "and never emailed twice.",
    ):

        def run_import():
            report = service.import_sent(subject=subject, days=int(days))
            st.session_state["import_unmatched"] = report.unmatched
            st.session_state["import_summary"] = report.summary()

        perform("import_sent", run_import, success="Import finished. See the summary below.")
    if right.button(
        "Sync replies, bounces and opt-outs",
        help="Reads the Gmail threads of contacted leads and records what came back.",
    ):

        def run_sync():
            report = service.sync_inbox()
            st.session_state["sync_summary"] = (
                f"Checked {report.threads_checked} threads; recorded {len(report.events)} new "
                "events: "
                + ", ".join(
                    f"{k}={report.count(k)}"
                    for k in ("reply", "opt_out", "bounce_hard", "spam_block", "auto_reply")
                )
            )

        perform("sync_inbox", run_sync, success="Sync finished. See the summary below.")
    for key in ("import_summary", "sync_summary"):
        if key in st.session_state:
            st.info(st.session_state[key])
    if st.session_state.get("import_unmatched"):
        with st.expander("Sent to addresses that are not LeadForge leads"):
            st.write(sorted(set(st.session_state["import_unmatched"])))
    a, b, c, d = st.columns(4)
    a.metric("Contacted", stats["contacted"])
    b.metric("Bounced", stats["bounced"])
    c.metric("Replied", stats["replied"])
    d.metric("Reply rate", f"{stats['reply_rate']:.0%}")
    a, b, c, d = st.columns(4)
    a.metric("Interested", stats["interested"])
    b.metric("Demos sent", stats["demo_sent"])
    c.metric("Won", stats["won"])
    d.metric("Revenue (USD)", f"{stats['revenue_usd']:,.0f}")
    st.subheader("Inbound replies, bounces and opt-outs")
    table(view["inbound"])
    st.subheader("Follow-up eligibility")
    table(view["followups"])
    st.subheader("Conversion history")
    table(view["conversions"])


def show_jobs(service):
    from agent import jobs

    db = service.db
    st.title("Jobs")
    st.caption(
        "Internships and entry-level roles from companies' own public job boards (Greenhouse, "
        "Lever, Ashby), ranked for someone applying from India. Read-only: nothing is applied "
        "for or emailed."
    )
    if not jobs.boards(db):
        jobs.seed_boards(db)
    board_rows = jobs.boards(db)
    a, b, c = st.columns(3)
    a.metric("Company boards", len(board_rows))
    b.metric(
        "Open roles tracked",
        db.connection.execute(
            "SELECT count(*) FROM job_postings WHERE status!='closed'"
        ).fetchone()[0],
    )
    c.metric(
        "Shortlisted",
        db.connection.execute(
            "SELECT count(*) FROM job_postings WHERE status='shortlisted'"
        ).fetchone()[0],
    )
    if st.button(
        "Refresh all job boards",
        type="primary",
        help="Fetches every board's open roles. Takes about a minute for 50 boards.",
    ):

        def run_refresh():
            with st.spinner("Reading job boards…"):
                st.session_state["jobs_summary"] = jobs.refresh(db).summary()

        perform("jobs_refresh", run_refresh, success="Job boards refreshed.")
    if "jobs_summary" in st.session_state:
        st.info(st.session_state["jobs_summary"])

    a, b, c, d = st.columns(4)
    levels = a.multiselect(
        "Level", ["intern", "entry", "mid", "senior", "unknown"], ["intern", "entry"]
    )
    eligible = b.multiselect(
        "Can I apply from India?",
        [
            "remote_global",
            "remote_india",
            "india_onsite",
            "remote_restricted",
            "onsite_abroad",
            "unknown",
        ],
        ["remote_global", "remote_india", "india_onsite"],
    )
    statuses = c.multiselect(
        "Status", list(jobs.STATUSES), ["new", "shortlisted", "applied", "emailed", "interview"]
    )
    score = d.number_input("Minimum score", 0, 100, 0)
    query = st.text_input("Search title, company or description", "")
    rows = jobs.postings(
        db,
        levels=levels,
        eligibility=eligible,
        min_score=int(score),
        statuses=statuses,
        query=query.strip(),
    )
    st.caption(
        f"{len(rows)} roles match. Score = level fit (40) + where you can work from "
        "(35) + overlap with your skills (25)."
    )
    table(rows)

    with st.expander("Update a role's status"), st.form("job_status"):
        job_id = st.number_input("Role id (from the table)", 1, step=1)
        status = st.selectbox("Status", list(jobs.STATUSES), index=1)
        note = st.text_input("Note (optional)", max_chars=1000)
        if st.form_submit_button("Save status"):
            perform("job_status", lambda: jobs.set_status(db, int(job_id), status, note))

    with (
        st.expander("Add a role found elsewhere (Wellfound, LinkedIn, referrals)"),
        st.form("job_manual"),
    ):
        company = st.text_input("Company")
        title = st.text_input("Role title")
        url = st.text_input("Link (https://…)")
        location = st.text_input("Location", placeholder="Remote · Everywhere")
        remote = st.checkbox("Remote")
        note = st.text_input("Note (optional)", max_chars=1000)
        if st.form_submit_button("Add role"):
            perform(
                "job_manual",
                lambda: jobs.add_manual(
                    db,
                    company=company,
                    title=title,
                    url=url,
                    location=location,
                    remote=remote,
                    note=note,
                ),
            )

    with st.expander(f"Company boards ({len(board_rows)})"):
        table(
            [
                {k: r[k] for k in ("ats", "slug", "last_status", "last_fetched_at")}
                for r in board_rows
            ]
        )
        with st.form("job_boards"):
            text = st.text_area(
                "Add boards, one per line",
                placeholder="ashby:cohere\nhttps://jobs.lever.co/company\nboards.greenhouse.io/company",
            )
            if st.form_submit_button("Add boards"):
                perform(
                    "job_boards",
                    lambda: jobs.add_boards(db, text.splitlines()),
                    success="Boards added. Click Refresh to fetch their roles.",
                )


def show_settings(service):
    st.title("Settings")
    settings = service.settings
    from agent.readiness import readiness

    st.subheader("System readiness")
    report = readiness(service.db, service.base_settings)
    table([{"check": k, "status": str(v)} for k, v in report["checks"].items()])
    st.caption(
        "Local checks only. Configured does not mean live-verified. Vault inspection requires the readiness CLI; reruns never refresh credentials or call Gmail."
    )
    with st.expander("Outstanding launch blockers"):
        for blocker in report["launch_blockers"]:
            st.write(blocker)
    a, b, c = st.columns(3)
    a.metric(
        "Search API",
        "Configured"
        if settings.search_api_key and settings.search_api_key.get_secret_value().strip()
        else "Missing",
    )
    b.metric(
        "Groq API",
        "Configured"
        if settings.groq_api_key and settings.groq_api_key.get_secret_value().strip()
        else "Missing",
    )
    c.metric("Daily limit", settings.daily_limit)
    st.caption(
        "Keys stay in environment variables or your local .env. This screen never displays or stores API keys."
    )
    settings_token = fingerprint(preferences_for(settings).model_dump())
    settings_form = "settings:" + settings_token[:12]
    with st.form(settings_form):
        provider = st.selectbox(
            "Default search provider",
            ["tavily", "serper"],
            index=["tavily", "serper"].index(settings.search_provider),
        )
        results = st.number_input(
            "Default results per query", 1, 20, settings.search_results_per_query
        )
        model = st.text_input("Groq model", settings.groq_model)
        dry = st.checkbox("Dry-run enabled", settings.dry_run)
        paused = st.checkbox("Global pause", settings.paused)
        address = st.text_area("Physical postal address", settings.postal_address, max_chars=500)
        confirmed = st.checkbox(
            "I have verified this is a valid physical postal address",
            settings.postal_address_confirmed,
        )
        st.caption(
            "Changing the address revokes message approval and resets address confirmation. Save it, then confirm the new address separately."
        )
        save = st.form_submit_button("Save settings", type="primary")
    if save:
        values = preferences_for(settings).model_dump()
        values.update(
            search_provider=provider,
            search_results_per_query=results,
            groq_model=model,
            dry_run=dry,
            paused=paused,
            postal_address=address,
            postal_address_confirmed=confirmed,
        )
        perform(
            settings_form,
            lambda: save_preferences(
                service.db,
                service.base_settings,
                values,
                submission_key=action_key(settings_form),
                expected=settings_token,
            ),
        )
    with st.expander("Compliance review controls"):
        lead_id = choose_lead(service, "settings_lead")
        if lead_id is not None:
            compliance_form(service, service.inspect(lead_id), "settings")


def main():
    st.set_page_config(page_title="LeadForge AI", page_icon="🔎", layout="wide")
    st.sidebar.title("LeadForge AI")
    st.sidebar.caption("Evidence-led outreach workspace")
    section = st.sidebar.radio(
        "Workspace", ["Discover", "Review", "Send (preview only)", "Tracker", "Jobs", "Settings"]
    )
    st.sidebar.divider()
    st.sidebar.info("Phase 7 · Owner tests only\n\nNo sending from this dashboard.")
    if "notice" in st.session_state:
        level, message = st.session_state.pop("notice")
        getattr(st, level)(message)
    try:
        base = load_settings()
        with Database(base.database_path) as db:
            db.migrate()
            service = Dashboard(db, base)
            {
                "Discover": show_discover,
                "Review": show_review,
                "Send (preview only)": show_send,
                "Tracker": show_tracker,
                "Jobs": show_jobs,
                "Settings": show_settings,
            }[section](service)
    except Exception:  # noqa: BLE001 -- safe outer UI boundary for configuration/database errors
        st.error(
            "Unable to load the workspace. Check configuration, database access and migration compatibility. Secrets are not shown."
        )


if __name__ == "__main__":
    main()
