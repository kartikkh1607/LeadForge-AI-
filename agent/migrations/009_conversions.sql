CREATE TABLE conversion_events (
 id INTEGER PRIMARY KEY,
 lead_id INTEGER NOT NULL REFERENCES leads(id),
 stage TEXT NOT NULL
 CHECK(stage IN ('interested','demo_built','demo_sent','proposal_sent','won','lost')),
 note TEXT NOT NULL DEFAULT '' CHECK(length(note)<=1000),
 value_usd REAL CHECK(value_usd IS NULL OR value_usd>=0),
 recorded_at TEXT NOT NULL,
 CHECK(stage='won' OR value_usd IS NULL)
);
CREATE INDEX conversion_lead ON conversion_events(lead_id, recorded_at);
