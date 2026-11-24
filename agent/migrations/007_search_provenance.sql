CREATE TABLE search_observations (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    result_url TEXT NOT NULL,
    title TEXT NOT NULL,
    outcome TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX search_observations_lead ON search_observations(lead_id);
