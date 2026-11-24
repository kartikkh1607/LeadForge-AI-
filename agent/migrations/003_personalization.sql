CREATE TABLE qualifications (
 id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
 result_json TEXT NOT NULL CHECK(json_valid(result_json)), context_hash TEXT NOT NULL,
 assessed_at TEXT NOT NULL
);
CREATE INDEX qualifications_lead ON qualifications(lead_id,id);
ALTER TABLE compliance ADD COLUMN supporting_evidence_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(supporting_evidence_json));
ALTER TABLE compliance ADD COLUMN source_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE compliance ADD COLUMN role_evidence TEXT NOT NULL DEFAULT '';
CREATE TABLE compliance_reviews (
 id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
 record_json TEXT NOT NULL CHECK(json_valid(record_json)), created_at TEXT NOT NULL
);
DROP TRIGGER cancel_followups;
DROP TRIGGER invalidate_recipient_approval;
DROP TRIGGER suppress_pending;
CREATE TABLE messages_new (
 id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
 kind TEXT NOT NULL CHECK(kind IN ('initial','followup')),
 subject TEXT NOT NULL CHECK(length(trim(subject))>0),
 body TEXT NOT NULL CHECK(length(trim(body))>0),
 evidence_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_json)),
 status TEXT NOT NULL DEFAULT 'draft'
 CHECK(status IN ('draft','approved','sending','sent','failed','bounced','cancelled','rejected')),
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
 personalization_json TEXT CHECK(personalization_json IS NULL OR
 (json_valid(personalization_json) AND json_type(personalization_json)='object'
 AND json_extract(personalization_json,'$.template_version') IS 1)),
 content_hash TEXT NOT NULL DEFAULT '',
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1),
 UNIQUE(lead_id,kind),
 CHECK(status NOT IN ('approved','sending','sent') OR approved_at IS NOT NULL),
 CHECK(status NOT IN ('sending','sent') OR rfc822_message_id IS NOT NULL),
 CHECK(status != 'sent' OR (sent_at IS NOT NULL AND gmail_message_id IS NOT NULL)),
 CHECK(status != 'approved' OR requires_manual_review=0)
);

INSERT INTO messages_new SELECT *,NULL,'',1 FROM messages;
DROP TABLE messages;
ALTER TABLE messages_new RENAME TO messages;
CREATE INDEX messages_queue ON messages(status,created_at);
CREATE INDEX messages_sent ON messages(sent_at);
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
CREATE TRIGGER invalidate_message_approval AFTER UPDATE OF subject,body,evidence_json,personalization_json ON messages
 WHEN OLD.status='approved' AND
 (NEW.subject!=OLD.subject OR NEW.body!=OLD.body OR NEW.evidence_json!=OLD.evidence_json OR NEW.personalization_json IS NOT OLD.personalization_json)
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

CREATE TABLE message_versions (
 message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 version INTEGER NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
 evidence_json TEXT NOT NULL, personalization_json TEXT, content_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(message_id,version)
);
UPDATE messages SET content_hash=draft_hash(subject,body,evidence_json,personalization_json);
INSERT INTO message_versions SELECT id,version,subject,body,evidence_json,personalization_json,
 content_hash,created_at FROM messages;
CREATE TRIGGER draft_initial_version AFTER INSERT ON messages
 BEGIN
 UPDATE messages SET content_hash=draft_hash(NEW.subject,NEW.body,NEW.evidence_json,NEW.personalization_json) WHERE id=NEW.id;
 INSERT INTO message_versions SELECT id,version,subject,body,evidence_json,personalization_json,
 content_hash,created_at FROM messages WHERE id=NEW.id;
 END;
CREATE TRIGGER draft_content_version AFTER UPDATE OF subject,body,evidence_json,personalization_json ON messages
 WHEN NEW.subject!=OLD.subject OR NEW.body!=OLD.body OR NEW.evidence_json!=OLD.evidence_json
 OR NEW.personalization_json IS NOT OLD.personalization_json
 BEGIN
 UPDATE messages SET version=OLD.version+1,
 content_hash=draft_hash(NEW.subject,NEW.body,NEW.evidence_json,NEW.personalization_json) WHERE id=NEW.id;
 INSERT INTO message_versions SELECT id,version,subject,body,evidence_json,personalization_json,
 content_hash,strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM messages WHERE id=NEW.id;
 END;
CREATE TRIGGER source_compliance_invalidated AFTER UPDATE OF text ON source_evidence
 WHEN NEW.text!=OLD.text
 BEGIN
 UPDATE compliance SET eligible=0,reason='Source changed: review required' WHERE lead_id=NEW.lead_id;
 UPDATE messages SET status='draft',approved_at=NULL,requires_manual_review=1
 WHERE lead_id=NEW.lead_id AND status='approved';
 END;
CREATE TRIGGER preserve_managed_draft BEFORE UPDATE OF personalization_json ON messages
 WHEN OLD.personalization_json IS NOT NULL AND NEW.personalization_json IS NULL
 BEGIN SELECT RAISE(ABORT,'Managed draft validation cannot be removed'); END;
