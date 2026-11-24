"""Presentation-independent dashboard queries and explicit, replay-safe actions."""

import json

from agent.compliance import compliance_is_current, review_compliance
from agent.config import Settings
from agent.db import Database
from agent.models import Compliance, Evidence, Suppression, utc_now
from agent.operations import ActionError, fingerprint, mutation_lock
from agent.personalize import (
    Slots,
    build_context,
    personalize_lead,
    revise_draft,
    validate_managed_message,
)
from agent.qualify import current_qualification, qualify_lead
from agent.safety import check_send_eligibility
from agent.sender import owner_blockers, queue_rows, quota_used
from agent.settings_store import effective_settings
from agent.source_context import context_hash, persisted_pages


class Dashboard:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.base_settings = db, settings

    @property
    def settings(self):
        return effective_settings(self.db, self.base_settings)

    def leads(self, *, stage=None, minimum_score=0, sort="newest", descending=True):
        columns = {"newest": "id", "score": "score", "business": "business", "stage": "stage"}
        if sort not in columns or not 0 <= minimum_score <= 100:
            raise ActionError("Invalid tracker filter")
        rows = self.db.connection.execute(
            "SELECT id FROM leads WHERE score>=? AND (? IS NULL OR stage=?) ORDER BY "
            + columns[sort]
            + (" DESC" if descending else " ASC"),
            (minimum_score, stage, stage),
        ).fetchall()
        result = []
        for row in rows:
            lead = self.db.get_lead(row[0])
            record = self.db.get_compliance(lead.id)
            eligible = compliance_is_current(self.db, lead.id)
            qualification = current_qualification(self.db, lead.id)
            result.append(
                {
                    "id": lead.id,
                    "business": lead.business or lead.domain,
                    "domain": lead.domain,
                    "website": lead.website,
                    "email": lead.email,
                    "stage": lead.stage,
                    "score": lead.score,
                    "qualification_current": qualification is not None,
                    "email_status": lead.email_status,
                    "compliance_eligible": eligible,
                    "compliance_reason": "Current reviewed evidence"
                    if eligible
                    else (
                        record.reason
                        if record and not record.eligible
                        else "Human review required or stale"
                    ),
                    "suppressed": self.db.is_suppressed(lead.email, lead.domain),
                }
            )
        return result

    def messages(self, lead_id=None):
        return [
            self.db.get_message(row[0])
            for row in self.db.connection.execute(
                "SELECT id FROM messages WHERE (? IS NULL OR lead_id=?) ORDER BY id DESC",
                (lead_id, lead_id),
            )
        ]

    def history(self):
        result = []
        for row in self.db.connection.execute(
            "SELECT id,status,config_json,created_at,updated_at FROM discovery_runs ORDER BY id DESC"
        ):
            entry = dict(row)
            config = json.loads(entry.pop("config_json"))
            entry.update(
                niche=config["niche"],
                provider=config["provider"],
                business_limit=config["max_businesses"],
            )
            result.append(entry)
        return result

    def suppressions(self):
        return [
            dict(row)
            for row in self.db.connection.execute(
                "SELECT kind,value,reason,created_at FROM suppression ORDER BY id DESC"
            )
        ]

    def review_token(self, lead_id):
        record = self.db.get_compliance(lead_id)
        return fingerprint(
            {
                "context": context_hash(self.db, lead_id),
                "messages": [m.model_dump(mode="json") for m in self.messages(lead_id)],
                "compliance": record.model_dump(mode="json") if record else None,
                "settings": self.settings.model_dump(exclude={"groq_api_key", "search_api_key"}),
            }
        )

    def inspect(self, lead_id):
        lead = self.db.get_lead(lead_id)
        qualification = current_qualification(self.db, lead_id)
        record = self.db.get_compliance(lead_id)
        messages = self.messages(lead_id)
        validations = {message.id: self.validation(message.id) for message in messages}
        try:
            slots = build_context(self.db, lead_id)
        except ValueError:
            slots = None
        return {
            "lead": lead,
            "qualification": qualification,
            "compliance": record,
            "eligible": compliance_is_current(self.db, lead_id),
            "pages": persisted_pages(self.db, lead_id),
            "sources": self.db.sources(lead_id),
            "crawl": list(self.db.crawl_records(lead_id).values()),
            "messages": messages,
            "validations": validations,
            "slot_choices": slots,
            "token": self.review_token(lead_id),
            "dns": dict(
                self.db.connection.execute(
                    "SELECT dns_reason,dns_checked_at,dns_retry_pending FROM leads WHERE id=?",
                    (lead_id,),
                ).fetchone()
            ),
        }

    def validation(self, message_id):
        message = self.db.get_message(message_id)
        try:
            validate_managed_message(self.db, message)
            if (
                not message.personalization
                or message.personalization["postal_address"] != self.settings.postal_address.strip()
            ):
                return "Postal address changed; rebuild from grounded slots"
        except ValueError as exc:
            # Validator messages are fixed code-owned text; never echo API responses.
            return str(exc)
        return "Valid grounded draft"

    def send_preview(self):
        settings = self.settings
        rows = []
        for message in self.messages():
            lead = self.db.get_lead(message.lead_id)
            blockers = list(check_send_eligibility(self.db, message.id, settings).blockers)
            rows.append(
                {
                    "message_id": message.id,
                    "business": lead.business or lead.domain,
                    "email": lead.email,
                    "status": message.status,
                    "compliance_eligible": compliance_is_current(self.db, lead.id),
                    "suppressed": self.db.is_suppressed(lead.email, lead.domain),
                    "blockers": ", ".join([*blockers, "sending_not_implemented"]),
                    "sendable": False,
                }
            )
        return {
            "messages": rows,
            "daily_limit": settings.daily_limit,
            "quota_used": quota_used(self.db, utc_now()),
            "queue": queue_rows(self.db),
            "gmail": [
                dict(r)
                for r in self.db.connection.execute(
                    "SELECT * FROM gmail_connection WHERE account=?",
                    (settings.gmail_account.casefold(),),
                )
            ],
            "owner_diagnostics": [
                {"message_id": m.id, "blockers": ", ".join(owner_blockers(self.db, m.id, settings))}
                for m in self.messages()
            ],
            "dry_run": settings.dry_run,
            "paused": settings.paused,
            "postal_ready": bool(
                settings.postal_address.strip() and settings.postal_address_confirmed
            ),
        }

    def _action(self, key, operation, payload, callback):
        if not key.strip() or len(key) > 100:
            raise ActionError("Action key required")
        digest = fingerprint(payload)
        with mutation_lock(self.db):
            row = self.db.connection.execute(
                "SELECT * FROM ui_actions WHERE action_key=?", (key,)
            ).fetchone()
            if row:
                if row["operation"] != operation or row["payload_hash"] != digest:
                    raise ActionError("Submission already used; refresh before trying again")
                if row["status"] == "done":
                    return row["result_id"]
                raise ActionError(
                    "Previous action did not finish. Refresh and inspect saved state before retrying."
                )
            with self.db.connection:
                self.db.connection.execute(
                    "INSERT INTO ui_actions VALUES (?,?,?,'running',NULL,?)",
                    (key, operation, digest, utc_now().isoformat()),
                )
            try:
                result_id = callback()
            except Exception as exc:
                with self.db.connection:
                    self.db.connection.execute(
                        "UPDATE ui_actions SET status='failed' WHERE action_key=?", (key,)
                    )
                if isinstance(exc, ActionError):
                    raise
                raise ActionError(
                    "Action blocked. Check current evidence, compliance, configuration and draft validation."
                ) from None
            with self.db.connection:
                self.db.connection.execute(
                    "UPDATE ui_actions SET status='done',result_id=? WHERE action_key=?",
                    (result_id, key),
                )
            return result_id

    def _fresh(self, lead_id, token):
        if token != self.review_token(lead_id):
            raise ActionError(
                "Evidence or draft changed since this form opened. Refresh and review again."
            )

    def generate(self, lead_id, token, key, generator=None):
        def perform():
            self._fresh(lead_id, token)
            existing = self.messages(lead_id)
            if any(m.kind == "initial" for m in existing):
                raise ActionError("An initial message already exists. Review it instead.")
            return personalize_lead(self.db, lead_id, self.settings, generator).id

        return self._action(key, "generate", [lead_id, token], perform)

    def edit(self, message_id, token, key, *, slots=None, subject=None, body=None):
        def perform():
            message = self.db.get_message(message_id)
            self._fresh(message.lead_id, token)
            if slots is not None:
                return revise_draft(
                    self.db, message_id, Slots.model_validate(slots), self.settings
                ).id
            return self.db.edit_message(message_id, subject, body).id

        payload = [message_id, token, slots, subject, body]
        return self._action(key, "edit", payload, perform)

    def approve(self, message_id, token, key, *, identity_confirmed, opt_out_confirmed):
        def perform():
            message = self.db.get_message(message_id)
            self._fresh(message.lead_id, token)
            if self.validation(message_id) != "Valid grounded draft":
                raise ActionError(
                    "Draft validation failed. Rebuild from grounded slots before approval."
                )
            return self.db.approve_message(
                message_id,
                truthful_identity_confirmed=identity_confirmed,
                opt_out_confirmed=opt_out_confirmed,
            ).id

        return self._action(
            key, "approve", [message_id, token, identity_confirmed, opt_out_confirmed], perform
        )

    def reject(self, message_id, token, key, reason):
        def perform():
            self._fresh(self.db.get_message(message_id).lead_id, token)
            return self.db.reject_message(message_id, reason).id

        return self._action(key, "reject", [message_id, token, reason], perform)

    def suppress(self, lead_id, token, key, kind, reason="manual"):
        def perform():
            self._fresh(lead_id, token)
            lead = self.db.get_lead(lead_id)
            value = lead.email if kind == "email" else lead.domain
            if not value:
                raise ActionError("No email is available to suppress")
            self.db.suppress(Suppression(kind=kind, value=value, reason=reason))
            return lead_id

        return self._action(key, "suppress", [lead_id, token, kind, reason], perform)

    def compliance_review(self, lead_id, token, key, values):
        def perform():
            self._fresh(lead_id, token)
            record = Compliance(**values, lead_id=lead_id, reviewed_at=utc_now())
            review_compliance(self.db, record)
            return lead_id

        return self._action(key, "compliance", [lead_id, token, values], perform)

    def reassess(self, lead_id, token, key):
        def perform():
            self._fresh(lead_id, token)
            qualify_lead(self.db, lead_id)
            return lead_id

        return self._action(key, "qualify", [lead_id, token], perform)


def selected_slots(context, hook_index, q1_index, q2_index, name_index=0):
    """Build evidence only from persisted choices; UI cannot supply an arbitrary URL."""
    hook, q1, q2 = (context["faqs"][i] for i in (hook_index, q1_index, q2_index))
    first = context["names"][name_index - 1] if name_index else None
    pairs = [
        (hook["answer"], hook["source_url"]),
        (q1["question"], q1["source_url"]),
        (q2["question"], q2["source_url"]),
    ]
    if first:
        pairs.append((first["first_name"], first["url"]))
    return Slots(
        first_name=first["first_name"] if first else None,
        hook_line=hook["answer"],
        q1=q1["question"],
        q2=q2["question"],
        evidence=[Evidence(claim=text, quote=text, url=url) for text, url in pairs],
    ).model_dump(mode="json")


def pipeline_view(db, settings):
    """Read-only Phase 6/7 data for the Tracker page."""
    from agent.conversions import summary
    from agent.followups import due_followups
    from agent.gmail_sync import inbound_rows

    return {
        "summary": summary(db),
        "inbound": inbound_rows(db),
        "followups": due_followups(db, settings),
        "conversions": [
            dict(r)
            for r in db.connection.execute(
                "SELECT lead_id,stage,note,value_usd,recorded_at FROM conversion_events "
                "ORDER BY recorded_at DESC"
            )
        ],
    }
