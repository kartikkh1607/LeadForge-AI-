"""SQLite repository with explicit transactions and versioned, atomic migrations."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from agent.compliance import evaluate_compliance
from agent.config import Settings, load_settings
from agent.evidence import draft_evidence_matches, draft_hash, validate_draft
from agent.models import (
    Compliance,
    Lead,
    LeadStage,
    Message,
    SourceEvidence,
    Suppression,
    utc_now,
)
from agent.verify import VerificationResult, normalize_domain, normalize_email
from agent.web_models import PageData


class Database:
    def __init__(self, path: str | Path, *, read_only: bool = False):
        if str(path) != ":memory:" and not read_only:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        target = Path(path).resolve().as_uri() + "?mode=ro" if read_only else str(path)
        self.connection = sqlite3.connect(target, timeout=10, uri=read_only)
        self.connection.row_factory = sqlite3.Row
        self.connection.create_function("draft_hash", 4, draft_hash, deterministic=True)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=10000")
        if not read_only:
            self.connection.execute("PRAGMA journal_mode=WAL")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def migrate(self, directory: Path | None = None) -> None:
        directory = directory or Path(__file__).with_name("migrations")
        db = self.connection
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            files = sorted(directory.glob("*.sql"))
            applied = dict(db.execute("SELECT version,checksum FROM schema_migrations"))
            if set(applied) - {p.name for p in files}:
                raise ValueError("Database contains unknown migrations; refusing downgrade")
            for path in files:
                script = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(script.encode()).hexdigest()
                if path.name in applied:
                    if applied[path.name] != checksum:
                        raise ValueError(f"Applied migration changed: {path.name}")
                    continue
                statement = ""
                for line in script.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        db.execute(statement)
                        statement = ""
                if statement.strip():
                    raise ValueError(f"Incomplete SQL in {path.name}")
                db.execute(
                    "INSERT INTO schema_migrations VALUES (?,?,?)",
                    (path.name, checksum, utc_now().isoformat()),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise

    def _insert(self, table: str, values: dict) -> int:
        # Table/column names come exclusively from internal model definitions.
        columns = ",".join(values)
        marks = ",".join("?" for _ in values)
        cursor = self.connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
        )
        return cursor.lastrowid

    def add_lead(self, lead: Lead) -> Lead:
        values = lead.model_dump(mode="json", exclude={"id"})
        with self.connection:
            lead_id = self._insert("leads", values)
        return self.get_lead(lead_id)

    def get_lead(self, lead_id: int) -> Lead:
        row = self.connection.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            raise KeyError(lead_id)
        return Lead.model_validate({key: row[key] for key in Lead.model_fields})

    def crawl_records(self, lead_id: int) -> dict[str, dict]:
        return {
            row["requested_url"]: dict(row)
            for row in self.connection.execute(
                "SELECT * FROM crawl_pages WHERE lead_id=? ORDER BY attempted_at,requested_url",
                (lead_id,),
            )
        }

    def save_crawl_page(
        self,
        lead_id: int,
        requested_url: str,
        *,
        page: PageData | None = None,
        error: str | None = None,
    ) -> None:
        """Commit a page and its evidence atomically; failures never erase good evidence."""
        if (page is None) == (error is None):
            raise ValueError("Provide exactly one of page or error")
        with self.connection:
            self.connection.execute(
                "INSERT INTO crawl_pages(lead_id,requested_url,final_url,state,attempted_at,error,"
                "result_json) VALUES (?,?,?,?,?,?,?) ON CONFLICT(lead_id,requested_url) DO UPDATE "
                "SET final_url=excluded.final_url,state=excluded.state,attempts=crawl_pages.attempts+1,"
                "attempted_at=excluded.attempted_at,error=excluded.error,result_json=excluded.result_json",
                (
                    lead_id,
                    requested_url,
                    page.url if page else None,
                    "ok" if page else "error",
                    utc_now().isoformat(),
                    error,
                    page.model_dump_json() if page else None,
                ),
            )
            if page:
                self.connection.execute(
                    "INSERT INTO source_evidence(lead_id,url,text,fetched_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(lead_id,url) DO UPDATE SET text=excluded.text,fetched_at=excluded.fetched_at",
                    (lead_id, page.url, page.text, page.fetched_at.isoformat()),
                )
                self.connection.execute(
                    "UPDATE messages SET status='draft',approved_at=NULL,requires_manual_review=1 "
                    "WHERE lead_id=? AND status='approved'",
                    (lead_id,),
                )
                self.connection.execute(
                    "UPDATE compliance SET eligible=0,reason='Source changed: review required' "
                    "WHERE lead_id=?",
                    (lead_id,),
                )

    def apply_scraped_facts(self, lead_id: int, pages: list[PageData]) -> None:
        """Fill empty identity fields only. Discovery never establishes outreach eligibility."""
        lead = self.get_lead(lead_id)
        if lead.stage != "new" or self.is_suppressed(lead.email, lead.domain) or not pages:
            return
        names = [o.value for p in pages for o in p.business_names]
        owners = [o.value for p in pages for o in p.owner_names]
        emails = [o.value for p in pages for o in p.emails]

        from agent.verify import contact_rank

        email = lead.email
        for candidate in sorted(emails, key=lambda e: contact_rank(e, lead.domain)):
            if contact_rank(candidate, lead.domain)[0] == 3:
                break  # Never adopt a vendor/plugin address as the business contact.
            if email:
                break
            if (
                not self.is_suppressed(candidate, lead.domain)
                and not self.connection.execute(
                    "SELECT 1 FROM leads WHERE email=? COLLATE NOCASE AND id!=?",
                    (candidate, lead_id),
                ).fetchone()
            ):
                email = candidate
        faqs = list(dict.fromkeys(f"{q.question}\n{q.answer}" for p in pages for q in p.faqs))
        with self.connection:
            self.connection.execute(
                "UPDATE leads SET business=?,name=?,email=?,faq_text=?,has_chatbot=?,"
                "email_status=? WHERE id=?",
                (
                    lead.business or next(iter(names), ""),
                    lead.name or next(iter(owners), ""),
                    email,
                    "\n\n".join(faqs)[:4000] or lead.faq_text,
                    lead.has_chatbot or any(p.chatbots for p in pages),
                    "unconfirmed_mailbox" if email != lead.email else lead.email_status,
                    lead_id,
                ),
            )

    def update_stage(self, lead_id: int, stage: LeadStage) -> None:
        stage = LeadStage(stage)
        self.get_lead(lead_id)
        with self.connection:
            self.connection.execute("UPDATE leads SET stage=? WHERE id=?", (stage, lead_id))

    def save_verification(self, lead_id: int, result: VerificationResult) -> None:
        lead = self.get_lead(lead_id)
        # A DNS success must never clear a known bounce.
        status = (
            lead.email_status
            if result.status is None or lead.email_status == "bounced"
            else result.status
        )
        with self.connection:
            self.connection.execute(
                "UPDATE leads SET email_status=?,dns_retry_pending=?,dns_reason=?,dns_checked_at=? "
                "WHERE id=?",
                (status, result.retryable, result.reason, utc_now().isoformat(), lead_id),
            )

    def save_source(self, source: SourceEvidence) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO source_evidence(lead_id,url,text,fetched_at) VALUES (?,?,?,?) "
                "ON CONFLICT(lead_id,url) DO UPDATE SET text=excluded.text,"
                "fetched_at=excluded.fetched_at",
                (source.lead_id, source.url, source.text, source.fetched_at.isoformat()),
            )
            self.connection.execute(
                "UPDATE messages SET status='draft',approved_at=NULL,requires_manual_review=1 "
                "WHERE lead_id=? AND status='approved'",
                (source.lead_id,),
            )

    def sources(self, lead_id: int) -> dict[str, str]:
        return dict(
            self.connection.execute(
                "SELECT url,text FROM source_evidence WHERE lead_id=?", (lead_id,)
            )
        )

    def save_compliance(self, record: Compliance) -> Compliance:
        lead = self.get_lead(record.lead_id)
        if record.source_hash or record.supporting_evidence:
            from agent.compliance import validate_review_sources

            validate_review_sources(self, record)
        eligible, reason = evaluate_compliance(record)
        if eligible and not record.source_hash:
            eligible, reason = False, "Persisted source evidence and renewed human review required"
        if self.is_suppressed(lead.email, lead.domain):
            eligible, reason = False, "Suppressed recipient"
        record = record.model_copy(
            update={
                "eligible": eligible,
                "reason": reason,
                "assessed_email": lead.email,
                "assessed_domain": lead.domain,
            }
        )
        with self.connection:
            self.connection.execute("DELETE FROM compliance WHERE lead_id=?", (record.lead_id,))
            values = record.model_dump(mode="json")
            values["supporting_evidence_json"] = json.dumps(values.pop("supporting_evidence"))
            self._insert("compliance", values)
            self.connection.execute(
                "INSERT INTO compliance_reviews(lead_id,record_json,created_at) VALUES (?,?,?)",
                (record.lead_id, record.model_dump_json(), utc_now().isoformat()),
            )
            self.connection.execute(
                "UPDATE messages SET status='draft',approved_at=NULL,requires_manual_review=1 "
                "WHERE lead_id=? AND status='approved'",
                (record.lead_id,),
            )
        return record

    def get_compliance(self, lead_id: int) -> Compliance | None:
        row = self.connection.execute(
            "SELECT * FROM compliance WHERE lead_id=?", (lead_id,)
        ).fetchone()
        if not row:
            return None
        values = dict(row)
        values["supporting_evidence"] = json.loads(values.pop("supporting_evidence_json"))
        return Compliance.model_validate(values)

    def is_suppressed(self, email: str | None, domain: str) -> bool:
        domains = {normalize_domain(domain)}
        if email:
            email = normalize_email(email).casefold()
            domains.add(normalize_domain(email.rsplit("@", 1)[1]))
        rows = self.connection.execute("SELECT kind,value FROM suppression").fetchall()
        return any(
            (row["kind"] == "email" and row["value"].casefold() == email)
            or (
                row["kind"] == "domain"
                and any(d == row["value"] or d.endswith("." + row["value"]) for d in domains)
            )
            for row in rows
        )

    def suppress(self, item: Suppression) -> None:
        value = (
            normalize_email(item.value).casefold()
            if item.kind == "email"
            else normalize_domain(item.value)
        )
        entries = [(item.kind, value)]
        if item.kind == "email" and item.reason in {"opt_out", "bounce", "spam_block"}:
            entries.append(("domain", normalize_domain(value.rsplit("@", 1)[1])))
            entries.extend(
                ("domain", row["domain"])
                for row in self.connection.execute(
                    "SELECT domain FROM leads WHERE email=? COLLATE NOCASE", (value,)
                )
            )
        with self.connection:
            for kind, target in set(entries):
                existing = self.connection.execute(
                    "SELECT reason FROM suppression WHERE kind=? AND value=?", (kind, target)
                ).fetchone()
                if existing and existing["reason"] == "opt_out":
                    continue
                if existing:
                    self.connection.execute(
                        "UPDATE suppression SET reason=? WHERE kind=? AND value=?",
                        (item.reason, kind, target),
                    )
                else:
                    self.connection.execute(
                        "INSERT INTO suppression(kind,value,reason,created_at) VALUES (?,?,?,?)",
                        (kind, target, item.reason, item.created_at.isoformat()),
                    )
            for row in self.connection.execute("SELECT id,email,domain FROM leads").fetchall():
                if self.is_suppressed(row["email"], row["domain"]):
                    self.connection.execute(
                        "UPDATE messages SET status='cancelled',approved_at=NULL WHERE lead_id=? "
                        "AND status IN ('draft','approved')",
                        (row["id"],),
                    )

    def create_draft(self, message: Message) -> Message:
        if message.status != "draft" or message.approved_at or message.sent_at:
            raise ValueError("New messages must be unapproved drafts")
        lead = self.get_lead(message.lead_id)
        if self.is_suppressed(lead.email, lead.domain):
            raise ValueError("Suppressed recipient")
        if message.kind == "initial" and (
            lead.stage in {"contacted", "replied", "not_interested", "won", "lost"}
            or self.connection.execute(
                "SELECT 1 FROM messages WHERE lead_id=? AND sent_at IS NOT NULL", (lead.id,)
            ).fetchone()
        ):
            raise ValueError("Domain already contacted")
        validate_draft(message.subject, message.body)
        if not draft_evidence_matches(message, self.sources(lead.id)):
            with self.connection:
                self.connection.execute(
                    "UPDATE leads SET notes='evidence failed' WHERE id=?", (lead.id,)
                )
            raise ValueError("Evidence failed")
        if message.personalization:
            from agent.personalize import validate_managed_message

            validate_managed_message(self, message)
        values = message.model_dump(mode="json", exclude={"id", "version", "content_hash"})
        for key in ("evidence", "example_questions", "missing_information"):
            values[key + "_json"] = json.dumps(values.pop(key))
        data = values.pop("personalization")
        values["personalization_json"] = json.dumps(data) if data is not None else None
        with self.connection:
            message_id = self._insert("messages", values)
        return self.get_message(message_id)

    def get_message(self, message_id: int) -> Message:
        row = self.connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise KeyError(message_id)
        values = dict(row)
        for key in ("evidence", "example_questions", "missing_information"):
            values[key] = json.loads(values.pop(key + "_json"))
        data = values.pop("personalization_json")
        values["personalization"] = json.loads(data) if data else None
        return Message.model_validate(values)

    def edit_message(self, message_id: int, subject: str, body: str) -> Message:
        message = self.get_message(message_id)
        if message.status not in {"draft", "approved"}:
            raise ValueError("Only pending drafts may be edited")
        validate_draft(subject, body)
        with self.connection:
            self.connection.execute(
                "UPDATE messages SET subject=?,body=?,status='draft',approved_at=NULL,"
                "requires_manual_review=1,truthful_identity_confirmed=0,opt_out_confirmed=0 "
                "WHERE id=?",
                (subject, body, message_id),
            )
        return self.get_message(message_id)

    def approve_message(
        self,
        message_id: int,
        *,
        truthful_identity_confirmed: bool,
        opt_out_confirmed: bool,
    ) -> Message:
        message = self.get_message(message_id)
        lead = self.get_lead(message.lead_id)
        if message.status != "draft":
            raise ValueError("Only drafts can be approved")
        if not truthful_identity_confirmed or not opt_out_confirmed:
            raise ValueError("Truthful identity/subject and working opt-out must be reviewed")
        if self.is_suppressed(lead.email, lead.domain):
            raise ValueError("Suppressed recipient")
        validate_draft(message.subject, message.body)
        if not draft_evidence_matches(message, self.sources(lead.id)):
            raise ValueError("Evidence failed")
        from agent.personalize import validate_managed_message

        validate_managed_message(self, message)
        with self.connection:
            self.connection.execute(
                "UPDATE messages SET status='approved',approved_at=?,requires_manual_review=0,"
                "truthful_identity_confirmed=1,opt_out_confirmed=1 WHERE id=?",
                (utc_now().isoformat(), message_id),
            )
        return self.get_message(message_id)

    def reject_message(self, message_id: int, reason: str) -> Message:
        if not reason.strip():
            raise ValueError("Rejection reason required")
        if self.get_message(message_id).status not in {"draft", "approved"}:
            raise ValueError("Only pending messages may be rejected")
        with self.connection:
            self.connection.execute(
                "UPDATE messages SET status='rejected',approved_at=NULL,"
                "requires_manual_review=1,error=? WHERE id=?",
                (reason, message_id),
            )
        return self.get_message(message_id)

    def message_versions(self, message_id: int) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM message_versions WHERE message_id=? ORDER BY version", (message_id,)
            )
        ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize/migrate the outreach database only")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    settings: Settings = load_settings()
    path = args.database or settings.database_path
    with Database(path) as db:
        db.migrate()
    print(f"Database ready: {path}")


if __name__ == "__main__":
    main()
