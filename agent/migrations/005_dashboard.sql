CREATE TABLE dashboard_settings (
 id INTEGER PRIMARY KEY CHECK(id=1), values_json TEXT NOT NULL CHECK(json_valid(values_json)),
 updated_at TEXT NOT NULL
);
CREATE TABLE discovery_runs (
 id INTEGER PRIMARY KEY, submission_key TEXT NOT NULL UNIQUE,
 config_json TEXT NOT NULL CHECK(json_valid(config_json)),
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','complete','partial')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE discovery_steps (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES discovery_runs(id),
 stage TEXT NOT NULL CHECK(stage IN ('search','scrape','verify','qualify','compliance')),
 target INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','done','partial','failed','blocked','skipped')),
 attempts INTEGER NOT NULL DEFAULT 0, result_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(result_json)),
 updated_at TEXT NOT NULL, UNIQUE(run_id,stage,target)
);
CREATE TABLE discovery_leads (
 run_id INTEGER NOT NULL REFERENCES discovery_runs(id), lead_id INTEGER NOT NULL REFERENCES leads(id),
 PRIMARY KEY(run_id,lead_id)
);
CREATE TABLE ui_actions (
 action_key TEXT PRIMARY KEY, operation TEXT NOT NULL, payload_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','done','failed')),
 result_id INTEGER, created_at TEXT NOT NULL
);
CREATE INDEX discovery_steps_run ON discovery_steps(run_id,id);
