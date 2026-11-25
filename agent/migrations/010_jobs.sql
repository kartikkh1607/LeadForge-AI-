CREATE TABLE job_boards (
 id INTEGER PRIMARY KEY,
 ats TEXT NOT NULL CHECK(ats IN ('greenhouse','lever','ashby')),
 slug TEXT NOT NULL CHECK(length(slug) BETWEEN 1 AND 100),
 company TEXT NOT NULL DEFAULT '',
 added_at TEXT NOT NULL,
 last_fetched_at TEXT,
 last_status TEXT NOT NULL DEFAULT 'never',
 UNIQUE(ats, slug)
);
CREATE TABLE job_postings (
 id INTEGER PRIMARY KEY,
 board_id INTEGER REFERENCES job_boards(id),
 source TEXT NOT NULL,
 external_id TEXT NOT NULL,
 company TEXT NOT NULL,
 title TEXT NOT NULL,
 location TEXT NOT NULL DEFAULT '',
 remote INTEGER NOT NULL DEFAULT 0 CHECK(remote IN (0,1)),
 url TEXT NOT NULL,
 level TEXT NOT NULL CHECK(level IN ('intern','entry','mid','senior','unknown')),
 eligibility TEXT NOT NULL CHECK(eligibility IN
  ('remote_global','remote_india','india_onsite','remote_restricted','onsite_abroad','unknown')),
 score INTEGER NOT NULL DEFAULT 0 CHECK(score BETWEEN 0 AND 100),
 reasons_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(reasons_json)),
 description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=8000),
 compensation TEXT NOT NULL DEFAULT '',
 posted_at TEXT,
 first_seen_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'new' CHECK(status IN
  ('new','shortlisted','applied','emailed','interview','offer','rejected','hidden','closed')),
 note TEXT NOT NULL DEFAULT '' CHECK(length(note)<=1000),
 UNIQUE(source, external_id)
);
CREATE INDEX job_postings_rank ON job_postings(status, score DESC);
