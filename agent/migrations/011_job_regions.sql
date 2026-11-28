ALTER TABLE job_postings ADD COLUMN regions TEXT NOT NULL DEFAULT '';
ALTER TABLE job_postings ADD COLUMN visa TEXT NOT NULL DEFAULT 'unknown'
 CHECK(visa IN ('offered','not_offered','unknown'));
