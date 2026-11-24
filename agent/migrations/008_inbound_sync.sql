CREATE TABLE inbound_events (
 id INTEGER PRIMARY KEY,
 lead_id INTEGER NOT NULL REFERENCES leads(id),
 message_id INTEGER NOT NULL REFERENCES messages(id),
 gmail_message_id TEXT NOT NULL UNIQUE,
 gmail_thread_id TEXT NOT NULL,
 kind TEXT NOT NULL
 CHECK(kind IN ('reply','auto_reply','opt_out','bounce_hard','bounce_soft','spam_block')),
 sender TEXT NOT NULL DEFAULT '',
 subject TEXT NOT NULL DEFAULT '',
 snippet TEXT NOT NULL DEFAULT '' CHECK(length(snippet)<=500),
 received_at TEXT NOT NULL,
 recorded_at TEXT NOT NULL
);
CREATE INDEX inbound_lead ON inbound_events(lead_id, received_at);
CREATE TABLE gmail_sync_runs (
 id INTEGER PRIMARY KEY,
 account TEXT NOT NULL,
 started_at TEXT NOT NULL,
 finished_at TEXT,
 threads_checked INTEGER NOT NULL DEFAULT 0,
 events_recorded INTEGER NOT NULL DEFAULT 0,
 outcome TEXT NOT NULL DEFAULT 'running'
);
