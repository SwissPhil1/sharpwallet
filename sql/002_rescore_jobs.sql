-- Rescore job queue — background processing via Railway scheduler.
-- Dashboard creates jobs, Railway processes them.
-- Run this in the Supabase SQL Editor after deploying.

-- ============================================================
-- RESCORE_JOBS — background job queue
-- ============================================================
CREATE TABLE IF NOT EXISTS rescore_jobs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'pending',    -- 'pending', 'running', 'completed', 'failed'
    total_wallets   INTEGER DEFAULT 0,
    scored          INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    error           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

-- RLS
ALTER TABLE rescore_jobs ENABLE ROW LEVEL SECURITY;

-- Public read (dashboard polls status)
CREATE POLICY "Public read rescore_jobs" ON rescore_jobs FOR SELECT USING (true);

-- Public insert (dashboard creates jobs via anon key)
CREATE POLICY "Public insert rescore_jobs" ON rescore_jobs FOR INSERT WITH CHECK (true);

-- Service update (Railway updates progress)
CREATE POLICY "Service update rescore_jobs" ON rescore_jobs FOR UPDATE USING (true);

-- Index for quick pending-job lookup
CREATE INDEX IF NOT EXISTS idx_rescore_jobs_status ON rescore_jobs(status) WHERE status = 'pending';
