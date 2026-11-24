CREATE TABLE leads (
 id INTEGER PRIMARY KEY,
 name TEXT NOT NULL DEFAULT '', business TEXT NOT NULL DEFAULT '',
 domain TEXT NOT NULL UNIQUE COLLATE NOCASE,
 website TEXT NOT NULL DEFAULT '', email TEXT UNIQUE COLLATE NOCASE,
 niche TEXT NOT NULL DEFAULT '', source_query TEXT NOT NULL DEFAULT '',
 source_url TEXT NOT NULL DEFAULT '', faq_text TEXT NOT NULL DEFAULT '' CHECK(length(faq_text)<=4000),
 has_chatbot INTEGER NOT NULL DEFAULT 0 CHECK(has_chatbot IN (0,1)),
 score INTEGER NOT NULL DEFAULT 0 CHECK(score BETWEEN 0 AND 100),
 email_status TEXT NOT NULL DEFAULT 'unconfirmed_mailbox'
 CHECK(email_status IN ('invalid','domain_mail_capable','unconfirmed_mailbox','bounced')),
 stage TEXT NOT NULL DEFAULT 'new'
 CHECK(stage IN ('new','qualified','skipped','contacted','replied','not_interested','won','lost')),
 notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 dns_retry_pending INTEGER NOT NULL DEFAULT 0 CHECK(dns_retry_pending IN (0,1)),
 dns_reason TEXT NOT NULL DEFAULT '', dns_checked_at TEXT,
 CHECK(domain=lower(domain) AND domain NOT LIKE 'www.%' AND domain NOT LIKE '%/%')
);
CREATE TABLE messages (
 id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
 kind TEXT NOT NULL CHECK(kind IN ('initial','followup')),
 subject TEXT NOT NULL CHECK(length(trim(subject))>0),
 body TEXT NOT NULL CHECK(length(trim(body))>0),
 evidence_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_json)),
 status TEXT NOT NULL DEFAULT 'draft'
 CHECK(status IN ('draft','approved','sending','sent','failed','bounced','cancelled')),
 rfc822_message_id TEXT UNIQUE, gmail_message_id TEXT UNIQUE, gmail_thread_id TEXT,
 approved_at TEXT, sent_at TEXT, error TEXT, created_at TEXT NOT NULL,
 hook_line TEXT NOT NULL DEFAULT '', hook_evidence TEXT NOT NULL DEFAULT '',
 hook_source_url TEXT NOT NULL DEFAULT '',
 example_questions_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(example_questions_json)),
 confidence REAL CHECK(confidence BETWEEN 0 AND 1),
 missing_information_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(missing_information_json)),
 requires_manual_review INTEGER NOT NULL DEFAULT 1 CHECK(requires_manual_review IN (0,1)),
 truthful_identity_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(truthful_identity_confirmed IN (0,1)),
 opt_out_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(opt_out_confirmed IN (0,1)),
 UNIQUE(lead_id,kind),
 CHECK(status NOT IN ('approved','sending','sent') OR approved_at IS NOT NULL),
 CHECK(status NOT IN ('sending','sent') OR rfc822_message_id IS NOT NULL),
 CHECK(status != 'sent' OR (sent_at IS NOT NULL AND gmail_message_id IS NOT NULL)),
 CHECK(status != 'approved' OR requires_manual_review=0)
);
CREATE TABLE suppression (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('email','domain')),
 value TEXT NOT NULL COLLATE NOCASE,
 reason TEXT NOT NULL CHECK(reason IN ('opt_out','bounce','spam_block','manual')),
 created_at TEXT NOT NULL, UNIQUE(kind,value)
);
CREATE TABLE source_evidence (
 id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
 url TEXT NOT NULL, text TEXT NOT NULL, fetched_at TEXT NOT NULL,
 UNIQUE(lead_id,url)
);
CREATE TABLE compliance (
 lead_id INTEGER PRIMARY KEY REFERENCES leads(id), country TEXT,
 subscriber_type TEXT NOT NULL DEFAULT 'unknown'
 CHECK(subscriber_type IN ('company','sole_trader_or_individual','unknown')),
 contact_source_url TEXT NOT NULL DEFAULT '',
 eligible INTEGER NOT NULL DEFAULT 0 CHECK(eligible IN (0,1)), reason TEXT NOT NULL,
 reviewed_at TEXT, reviewed_by TEXT NOT NULL DEFAULT '',
 country_evidence TEXT NOT NULL DEFAULT '', subscriber_evidence TEXT NOT NULL DEFAULT '',
 conspicuously_published INTEGER NOT NULL DEFAULT 0 CHECK(conspicuously_published IN (0,1)),
 no_unsolicited_statement INTEGER CHECK(no_unsolicited_statement IN (0,1)),
 role_relevant INTEGER NOT NULL DEFAULT 0 CHECK(role_relevant IN (0,1)),
 assessed_email TEXT, assessed_domain TEXT,
 CHECK(eligible=0 OR (reviewed_at IS NOT NULL AND length(trim(reviewed_by))>0))
);
CREATE INDEX messages_queue ON messages(status,created_at);
CREATE INDEX messages_sent ON messages(sent_at);
CREATE TRIGGER permanent_opt_out_delete BEFORE DELETE ON suppression
 WHEN OLD.reason='opt_out'
 BEGIN SELECT RAISE(ABORT,'Opt-outs are permanent'); END;
CREATE TRIGGER permanent_opt_out_update BEFORE UPDATE ON suppression
 WHEN OLD.reason='opt_out'
 BEGIN SELECT RAISE(ABORT,'Opt-outs are permanent'); END;
CREATE TRIGGER cancel_followups AFTER UPDATE OF stage ON leads
 WHEN NEW.stage IN ('replied','not_interested','won','lost','skipped')
 BEGIN
 UPDATE messages SET status='cancelled',approved_at=NULL
 WHERE lead_id=NEW.id AND kind='followup' AND status IN ('draft','approved');
 END;
CREATE TRIGGER invalidate_recipient_approval AFTER UPDATE OF email,domain ON leads
 WHEN NEW.email IS NOT OLD.email OR NEW.domain != OLD.domain
 BEGIN
 UPDATE messages SET status='draft',approved_at=NULL,requires_manual_review=1
 WHERE lead_id=NEW.id AND status='approved';
 UPDATE compliance SET eligible=0,reason='Recipient changed: review required' WHERE lead_id=NEW.id;
 END;
CREATE TRIGGER invalidate_message_approval AFTER UPDATE OF subject,body,evidence_json ON messages
 WHEN OLD.status='approved' AND
 (NEW.subject!=OLD.subject OR NEW.body!=OLD.body OR NEW.evidence_json!=OLD.evidence_json)
 BEGIN
 UPDATE messages SET status='draft',approved_at=NULL,requires_manual_review=1 WHERE id=NEW.id;
 END;
CREATE TRIGGER suppress_pending AFTER INSERT ON suppression
 BEGIN
 UPDATE messages SET status='cancelled',approved_at=NULL
 WHERE status IN ('draft','approved') AND lead_id IN (
 SELECT id FROM leads WHERE (NEW.kind='email' AND email=NEW.value COLLATE NOCASE)
 OR (NEW.kind='domain' AND domain=NEW.value COLLATE NOCASE));
 END;
