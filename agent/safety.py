"""Read-only preflight shared by prospect previews and the owner-only worker."""

from dataclasses import dataclass

from agent.compliance import compliance_is_current
from agent.config import Settings
from agent.db import Database
from agent.evidence import draft_evidence_matches, validate_draft


@dataclass(frozen=True)
class SendDecision:
    blockers: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return not self.blockers


def check_send_eligibility(
    db: Database, message_id: int, settings: Settings, *, claimed: bool = False
) -> SendDecision:
    """Always re-read persisted state; a past approval is not a reusable send permit.

    The worker additionally enforces lock, quota, timing, owner restrictions and
    reconciliation. claimed=True checks a committed claim's sending status instead
    of approved; all approval attestations and content checks still apply.
    Calling this function never sends or queues anything.
    """
    message = db.get_message(message_id)
    lead = db.get_lead(message.lead_id)
    blockers = []
    if settings.dry_run:
        blockers.append("dry_run")
    if settings.paused:
        blockers.append("paused")
    if (
        message.status != ("sending" if claimed else "approved")
        or not message.approved_at
        or message.requires_manual_review
    ):
        blockers.append("approval_required")
    if not message.truthful_identity_confirmed or not message.opt_out_confirmed:
        blockers.append("identity_or_opt_out_unreviewed")
    if not settings.postal_address.strip() or not settings.postal_address_confirmed:
        blockers.append("postal_address_required")
    elif settings.postal_address.strip() not in message.body:
        blockers.append("postal_address_missing_from_message")
    if not lead.email or lead.email_status != "domain_mail_capable":
        blockers.append("email_not_mail_capable")
    dns_state = db.connection.execute(
        "SELECT dns_retry_pending FROM leads WHERE id=?", (lead.id,)
    ).fetchone()
    if dns_state["dns_retry_pending"]:
        blockers.append("dns_retry_pending")
    if db.is_suppressed(lead.email, lead.domain):
        blockers.append("suppressed")
    if not compliance_is_current(db, lead.id):
        blockers.append("compliance_ineligible")
    if message.kind == "initial" and lead.stage != "qualified":
        blockers.append("lead_not_qualified")
    if message.kind == "followup":
        # Follow-up timing, thread headers and reply sync are not implemented yet.
        blockers.append("followup_not_implemented")
    if not draft_evidence_matches(message, db.sources(lead.id)):
        blockers.append("evidence_failed")
    try:
        validate_draft(message.subject, message.body)
        from agent.personalize import validate_managed_message

        validate_managed_message(db, message)
    except ValueError:
        blockers.append("invalid_draft")
    return SendDecision(tuple(blockers))
