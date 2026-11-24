CREATE TABLE crawl_pages (
 lead_id INTEGER NOT NULL REFERENCES leads(id),
 requested_url TEXT NOT NULL,
 final_url TEXT,
 state TEXT NOT NULL CHECK(state IN ('ok','error')),
 attempts INTEGER NOT NULL DEFAULT 1 CHECK(attempts BETWEEN 1 AND 3),
 attempted_at TEXT NOT NULL,
 error TEXT,
 result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
 PRIMARY KEY(lead_id,requested_url)
);
CREATE INDEX crawl_pages_state ON crawl_pages(lead_id,state);
