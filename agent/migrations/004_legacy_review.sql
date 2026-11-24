-- Retain historical content, but require Phase 3 evidence before reuse.
UPDATE messages SET status='draft',approved_at=NULL,requires_manual_review=1,
 truthful_identity_confirmed=0,opt_out_confirmed=0
 WHERE status='approved' AND personalization_json IS NULL;
UPDATE compliance SET eligible=0,reason='Persisted source evidence and renewed human review required'
 WHERE source_hash='';
