"""Phase 7: manual conversion tracking after a prospect replies.

Every step is recorded by you, never inferred from email text. Won/lost also set
the lead's lifecycle stage, which (via the existing trigger) cancels any pending
follow-up.
"""

from agent.models import utc_now

STAGES = ("interested", "demo_built", "demo_sent", "proposal_sent", "won", "lost")
ENGAGED = {"replied", "won", "lost"}


class ConversionError(ValueError):
    """Safe user-facing reason a conversion step was refused."""


def record_progress(db, lead_id, stage, note="", value_usd=None, now=None):
    if stage not in STAGES:
        raise ConversionError("Unknown conversion stage")
    lead = db.get_lead(lead_id)
    if lead.stage not in ENGAGED:
        raise ConversionError("Record progress only after the prospect has replied")
    if value_usd is not None and stage != "won":
        raise ConversionError("A value can only be recorded for a won project")
    if value_usd is not None and value_usd < 0:
        raise ConversionError("Value cannot be negative")
    when = (now or utc_now()).isoformat()
    with db.connection:
        db.connection.execute(
            "INSERT INTO conversion_events(lead_id,stage,note,value_usd,recorded_at) "
            "VALUES (?,?,?,?,?)",
            (lead_id, stage, note.strip()[:1000], value_usd, when),
        )
        if stage in {"won", "lost"}:
            db.connection.execute("UPDATE leads SET stage=? WHERE id=?", (stage, lead_id))
    return latest_stage(db, lead_id)


def latest_stage(db, lead_id):
    row = db.connection.execute(
        "SELECT stage FROM conversion_events WHERE lead_id=? ORDER BY recorded_at DESC, id DESC "
        "LIMIT 1",
        (lead_id,),
    ).fetchone()
    return row[0] if row else None


def summary(db):
    """Funnel counts from recorded facts only; no estimated delivery or open rates."""

    def one(query, params=()):
        return db.connection.execute(query, params).fetchone()[0] or 0

    contacted = one("SELECT count(DISTINCT lead_id) FROM messages WHERE sent_at IS NOT NULL")
    bounced = one(
        "SELECT count(DISTINCT lead_id) FROM inbound_events "
        "WHERE kind IN ('bounce_hard','spam_block')"
    )
    replied = one(
        "SELECT count(DISTINCT lead_id) FROM inbound_events WHERE kind IN ('reply','opt_out')"
    )
    opted_out = one("SELECT count(DISTINCT lead_id) FROM inbound_events WHERE kind='opt_out'")
    delivered = max(contacted - bounced, 0)
    funnel = {
        stage: one("SELECT count(DISTINCT lead_id) FROM conversion_events WHERE stage=?", (stage,))
        for stage in STAGES
    }
    revenue = one("SELECT sum(value_usd) FROM conversion_events WHERE stage='won'")
    return {
        "contacted": contacted,
        "bounced": bounced,
        "delivered": delivered,
        "replied": replied,
        "opted_out": opted_out,
        "reply_rate": round(replied / delivered, 3) if delivered else 0.0,
        **funnel,
        "revenue_usd": float(revenue),
    }
