-- ============================================================
-- SharpWallet — Supabase Schema
-- Run this in Supabase SQL editor to initialize all tables
-- ============================================================

-- ── Wallets ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wallets (
    wallet_address  TEXT PRIMARY KEY,
    username        TEXT,                    -- Polymarket display name if known
    first_seen_at   TIMESTAMPTZ,
    last_active_at  TIMESTAMPTZ,
    account_age_days INT,
    is_monitored    BOOLEAN DEFAULT FALSE,   -- actively watching via WebSocket
    is_sharp        BOOLEAN DEFAULT FALSE,   -- passed our scoring threshold
    notes           TEXT,                    -- manual annotations
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Wallet Composite Scores ───────────────────────────────────
CREATE TABLE IF NOT EXISTS wallet_scores (
    wallet_address  TEXT PRIMARY KEY REFERENCES wallets(wallet_address),
    composite_score NUMERIC(5,2),            -- 0–100
    tier            TEXT,                    -- tier_1_sharp, tier_2_sharp, etc.
    total_markets   INT,
    total_staked    NUMERIC(15,2),
    total_pnl       NUMERIC(15,2),
    account_age_days INT,
    computed_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Per-Category Scores ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS wallet_category_scores (
    id              BIGSERIAL PRIMARY KEY,
    wallet_address  TEXT REFERENCES wallets(wallet_address),
    category        TEXT,                    -- sports, politics, crypto, etc.
    n_markets       INT,
    n_wins          INT,
    win_rate_bayesian NUMERIC(5,4),          -- 0.0–1.0, Bayesian shrunk
    avg_clv         NUMERIC(6,4),            -- closing line value, can be negative
    pnl_per_market  NUMERIC(10,2),
    calibration_score NUMERIC(5,4),          -- 0.0–1.0
    total_pnl       NUMERIC(15,2),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(wallet_address, category)
);

-- ── Markets ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS markets (
    market_id       TEXT PRIMARY KEY,        -- Polymarket condition ID
    question        TEXT,
    category        TEXT,
    subcategory     TEXT,
    outcome_yes_token TEXT,                  -- CLOB token ID for YES
    outcome_no_token  TEXT,                  -- CLOB token ID for NO
    resolved        BOOLEAN DEFAULT FALSE,
    winning_outcome TEXT,                    -- token ID of winner
    closing_price   NUMERIC(5,4),           -- last price before resolution
    volume_usdc     NUMERIC(15,2),
    created_at      TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    fetched_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Individual Trades ─────────────────────────────────────────
-- Only stored for monitored/sharp wallets (not all wallets)
CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    wallet_address  TEXT REFERENCES wallets(wallet_address),
    market_id       TEXT REFERENCES markets(market_id),
    outcome_id      TEXT,                    -- CLOB token ID
    side            TEXT,                    -- BUY / SELL
    price           NUMERIC(5,4),            -- 0.0–1.0
    size_usdc       NUMERIC(12,2),
    traded_at       TIMESTAMPTZ,
    clv             NUMERIC(6,4),            -- computed after resolution
    outcome_won     BOOLEAN,                 -- NULL until resolved
    pnl             NUMERIC(12,2),           -- NULL until resolved
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Alerts ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    wallet_address  TEXT REFERENCES wallets(wallet_address),
    market_id       TEXT,
    market_question TEXT,
    category        TEXT,
    entry_price     NUMERIC(5,4),
    size_usdc       NUMERIC(12,2),
    wallet_score_at_trigger  NUMERIC(5,2),
    category_score_at_trigger NUMERIC(5,4),  -- CLV for this category
    sportsbook_price NUMERIC(5,4),           -- Pinnacle implied prob if available
    arb_gap         NUMERIC(5,4),            -- diff between Polymarket and sportsbook
    telegram_sent   BOOLEAN DEFAULT FALSE,
    telegram_message_id BIGINT,
    triggered_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── Sportsbook Snapshots ──────────────────────────────────────
-- For Phase 3: cross-reference log
CREATE TABLE IF NOT EXISTS sportsbook_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT,
    event_name      TEXT,
    pinnacle_home_prob NUMERIC(5,4),
    pinnacle_away_prob NUMERIC(5,4),
    polymarket_home_prob NUMERIC(5,4),
    polymarket_away_prob NUMERIC(5,4),
    gap_home        NUMERIC(5,4),            -- pinnacle - polymarket
    gap_away        NUMERIC(5,4),
    snapped_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Indexes ───────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_trades_wallet      ON trades(wallet_address, traded_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_market      ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_wallet_scores_tier ON wallet_scores(tier, composite_score DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_triggered   ON alerts(triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_markets_resolved   ON markets(resolved_at DESC, category);
CREATE INDEX IF NOT EXISTS idx_cat_scores_wallet  ON wallet_category_scores(wallet_address);

-- ── Useful Views ──────────────────────────────────────────────

-- Sharp wallets at a glance
CREATE OR REPLACE VIEW sharp_wallets AS
SELECT 
    w.wallet_address,
    w.username,
    ws.composite_score,
    ws.tier,
    ws.total_markets,
    ws.total_pnl,
    ws.account_age_days,
    ws.computed_at
FROM wallets w
JOIN wallet_scores ws ON w.wallet_address = ws.wallet_address
WHERE ws.tier IN ('tier_1_sharp', 'tier_2_sharp')
ORDER BY ws.composite_score DESC;

-- Category leaders: who is sharpest in each category?
CREATE OR REPLACE VIEW category_leaders AS
SELECT 
    wcs.category,
    wcs.wallet_address,
    w.username,
    wcs.n_markets,
    wcs.win_rate_bayesian,
    wcs.avg_clv,
    wcs.pnl_per_market,
    wcs.calibration_score,
    ws.composite_score
FROM wallet_category_scores wcs
JOIN wallets w ON wcs.wallet_address = w.wallet_address
JOIN wallet_scores ws ON wcs.wallet_address = ws.wallet_address
WHERE wcs.n_markets >= 15
ORDER BY wcs.category, wcs.avg_clv DESC;

-- Recent alerts with context
CREATE OR REPLACE VIEW recent_alerts AS
SELECT 
    a.triggered_at,
    w.username,
    a.wallet_address,
    a.market_question,
    a.category,
    a.entry_price,
    a.size_usdc,
    a.wallet_score_at_trigger,
    a.arb_gap,
    a.telegram_sent
FROM alerts a
JOIN wallets w ON a.wallet_address = w.wallet_address
ORDER BY a.triggered_at DESC
LIMIT 100;
