CREATE TABLE outbound_queue (
 message_id INTEGER PRIMARY KEY REFERENCES messages(id),
 version INTEGER NOT NULL, content_hash TEXT NOT NULL, approved_at TEXT NOT NULL,
 recipient TEXT NOT NULL, account TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','sending','uncertain','sent','rejected')),
 rfc_message_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
 attempted_at TEXT, quota_day TEXT, next_send_at TEXT,
 outcome TEXT, gmail_message_id TEXT, gmail_thread_id TEXT, sent_at TEXT
);
CREATE TABLE gmail_connection (
 account TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE INDEX outbound_state ON outbound_queue(state);
CREATE TABLE outbound_events (
 id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES outbound_queue(message_id),
 state TEXT NOT NULL, outcome TEXT NOT NULL, recorded_at TEXT NOT NULL
);
