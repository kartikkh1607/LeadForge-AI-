CREATE TABLE app_leads (
 id INTEGER PRIMARY KEY,
 package TEXT NOT NULL UNIQUE CHECK(length(package) BETWEEN 3 AND 200),
 title TEXT NOT NULL DEFAULT '',
 developer TEXT NOT NULL DEFAULT '',
 support_email TEXT NOT NULL DEFAULT '',
 developer_email TEXT NOT NULL DEFAULT '',
 address TEXT NOT NULL DEFAULT '',
 country TEXT NOT NULL DEFAULT '',
 installs TEXT NOT NULL DEFAULT '',
 updated_on TEXT,
 url TEXT NOT NULL,
 query TEXT NOT NULL DEFAULT '',
 verdict TEXT NOT NULL CHECK(verdict IN
  ('lead','not_stale','builder','too_big','too_small','no_email','wrong_country','suppressed',
   'unparsed')),
 reasons_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(reasons_json)),
 status TEXT NOT NULL DEFAULT 'new' CHECK(status IN
  ('new','drafted','emailed','replied','won','not_interested','skip')),
 note TEXT NOT NULL DEFAULT '' CHECK(length(note)<=1000),
 first_seen_at TEXT NOT NULL,
 last_checked_at TEXT NOT NULL
);
CREATE INDEX app_leads_rank ON app_leads(verdict, status, updated_on);
