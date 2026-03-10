-- Polymarket Sharp Wallet Tracker — Supabase Schema
-- Run this in the Supabase SQL Editor

-- ============================================================
-- 1. WALLETS — master list of tracked wallets
-- ============================================================
CREATE TABLE IF NOT EXISTS wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,                          -- optional human label ("whale_42", "politics_sniper")
    first_seen      TIMESTAMPTZ DEFAULT now(),
    total_bets      INTEGER DEFAULT 0,
    total_volume    NUMERIC(18,2) DEFAULT 0,       -- total USD wagered
    is_tracked      BOOLEAN DEFAULT TRUE,          -- actively monitored via WebSocket
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- 2. WALLET_SCORES — aggregate sharpness metrics per wallet
-- ============================================================
CREATE TABLE IF NOT EXISTS wallet_scores (
    address         TEXT PRIMARY KEY REFERENCES wallets(address) ON DELETE CASCADE,
    total_bets      INTEGER DEFAULT 0,
    scored_bets     INTEGER DEFAULT 0,              -- how many bets had CLV computed
    win_rate        NUMERIC(5,4) DEFAULT 0,        -- 0.0000 to 1.0000
    clv             NUMERIC(8,4) DEFAULT 0,        -- Closing Line Value (positive = sharp)
    roi             NUMERIC(8,4) DEFAULT 0,        -- Return on investment
    calibration     NUMERIC(5,4) DEFAULT 0,        -- how well-calibrated are their odds
    avg_edge        NUMERIC(8,4) DEFAULT 0,        -- average edge at time of bet
    kelly_fraction  NUMERIC(5,4) DEFAULT 0,        -- implied Kelly sizing
    sharpe_ratio    NUMERIC(8,4) DEFAULT 0,        -- risk-adjusted return
    rank            INTEGER,                        -- overall rank (1 = sharpest)
    tier            TEXT DEFAULT 'unknown',         -- 'elite', 'sharp', 'moderate', 'noise'
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- 3. WALLET_CATEGORY_SCORES — per-category breakdown
-- ============================================================
CREATE TABLE IF NOT EXISTS wallet_category_scores (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    address         TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
    category        TEXT NOT NULL,                  -- 'sports', 'politics', 'crypto', 'entertainment', 'science', 'other'
    total_bets      INTEGER DEFAULT 0,
    win_rate        NUMERIC(5,4) DEFAULT 0,
    clv             NUMERIC(8,4) DEFAULT 0,
    roi             NUMERIC(8,4) DEFAULT 0,
    calibration     NUMERIC(5,4) DEFAULT 0,
    avg_edge        NUMERIC(8,4) DEFAULT 0,
    rank            INTEGER,                        -- rank within this category
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(address, category)
);

-- ============================================================
-- 4. BETS — individual bet history
-- ============================================================
CREATE TABLE IF NOT EXISTS bets (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    address         TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
    market_slug     TEXT NOT NULL,                  -- polymarket market slug
    market_title    TEXT,                           -- human-readable title
    category        TEXT,                           -- market category
    outcome         TEXT NOT NULL,                  -- 'Yes' or 'No' (or token name)
    side            TEXT NOT NULL,                  -- 'BUY' or 'SELL'
    price           NUMERIC(8,6) NOT NULL,          -- price paid (0-1)
    size            NUMERIC(18,6) NOT NULL,          -- number of shares
    amount_usd      NUMERIC(18,2),                  -- total USD value
    timestamp       TIMESTAMPTZ NOT NULL,
    -- post-resolution fields (filled later)
    resolved        BOOLEAN DEFAULT FALSE,
    won             BOOLEAN,
    closing_price   NUMERIC(8,6),                   -- price at market close
    clv             NUMERIC(8,4),                    -- closing line value for this bet
    pnl             NUMERIC(18,2),                   -- profit/loss in USD
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bets_address ON bets(address);
CREATE INDEX IF NOT EXISTS idx_bets_timestamp ON bets(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_bets_market ON bets(market_slug);
CREATE INDEX IF NOT EXISTS idx_bets_category ON bets(category);

-- ============================================================
-- 5. ALERTS — real-time notifications for sharp wallet activity
-- ============================================================
CREATE TABLE IF NOT EXISTS alerts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    address         TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
    wallet_label    TEXT,
    wallet_tier     TEXT,
    market_slug     TEXT NOT NULL,
    market_title    TEXT,
    category        TEXT,
    outcome         TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           NUMERIC(8,6) NOT NULL,
    size            NUMERIC(18,6),
    amount_usd      NUMERIC(18,2),
    -- arb gap detection
    pinnacle_price  NUMERIC(8,6),                   -- equivalent Pinnacle line (if available)
    arb_gap         NUMERIC(8,4),                    -- price difference (positive = Polymarket cheaper)
    -- meta
    alert_type      TEXT DEFAULT 'bet',              -- 'bet', 'large_position', 'arb_gap', 'cluster'
    sent_telegram   BOOLEAN DEFAULT FALSE,
    timestamp       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type);

-- ============================================================
-- 6. MARKETS — cached market metadata
-- ============================================================
CREATE TABLE IF NOT EXISTS markets (
    slug            TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    category        TEXT,
    end_date        TIMESTAMPTZ,
    resolved        BOOLEAN DEFAULT FALSE,
    resolution      TEXT,                           -- winning outcome
    current_price   NUMERIC(8,6),                   -- latest Yes price
    volume          NUMERIC(18,2),                  -- total market volume
    liquidity       NUMERIC(18,2),
    condition_id    TEXT,                            -- CLOB condition ID
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- 7. RLS — Row Level Security (public read, service write)
-- ============================================================

-- Enable RLS on all tables
ALTER TABLE wallets ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallet_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallet_category_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE bets ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE markets ENABLE ROW LEVEL SECURITY;

-- Public read access (dashboard uses anon key)
CREATE POLICY "Public read wallets" ON wallets FOR SELECT USING (true);
CREATE POLICY "Public read wallet_scores" ON wallet_scores FOR SELECT USING (true);
CREATE POLICY "Public read wallet_category_scores" ON wallet_category_scores FOR SELECT USING (true);
CREATE POLICY "Public read bets" ON bets FOR SELECT USING (true);
CREATE POLICY "Public read alerts" ON alerts FOR SELECT USING (true);
CREATE POLICY "Public read markets" ON markets FOR SELECT USING (true);

-- Service role write access (scripts use service key)
CREATE POLICY "Service insert wallets" ON wallets FOR INSERT WITH CHECK (true);
CREATE POLICY "Service update wallets" ON wallets FOR UPDATE USING (true);
CREATE POLICY "Service insert wallet_scores" ON wallet_scores FOR INSERT WITH CHECK (true);
CREATE POLICY "Service update wallet_scores" ON wallet_scores FOR UPDATE USING (true);
CREATE POLICY "Service insert wallet_category_scores" ON wallet_category_scores FOR INSERT WITH CHECK (true);
CREATE POLICY "Service update wallet_category_scores" ON wallet_category_scores FOR UPDATE USING (true);
CREATE POLICY "Service insert bets" ON bets FOR INSERT WITH CHECK (true);
CREATE POLICY "Service update bets" ON bets FOR UPDATE USING (true);
CREATE POLICY "Service insert alerts" ON alerts FOR INSERT WITH CHECK (true);
CREATE POLICY "Service update alerts" ON alerts FOR UPDATE USING (true);
CREATE POLICY "Service insert markets" ON markets FOR INSERT WITH CHECK (true);
CREATE POLICY "Service update markets" ON markets FOR UPDATE USING (true);

-- ============================================================
-- 8. REALTIME — enable Supabase Realtime for dashboard
-- ============================================================
ALTER PUBLICATION supabase_realtime ADD TABLE alerts;
ALTER PUBLICATION supabase_realtime ADD TABLE wallet_scores;

-- ============================================================
-- 9. HELPER FUNCTIONS
-- ============================================================

-- Function to get top wallets by category
CREATE OR REPLACE FUNCTION get_top_wallets_by_category(cat TEXT, lim INTEGER DEFAULT 20)
RETURNS TABLE (
    address TEXT,
    label TEXT,
    tier TEXT,
    total_bets INTEGER,
    win_rate NUMERIC,
    clv NUMERIC,
    roi NUMERIC,
    category_rank INTEGER
) LANGUAGE sql STABLE AS $$
    SELECT
        w.address,
        w.label,
        ws.tier,
        wcs.total_bets,
        wcs.win_rate,
        wcs.clv,
        wcs.roi,
        wcs.rank as category_rank
    FROM wallet_category_scores wcs
    JOIN wallets w ON w.address = wcs.address
    LEFT JOIN wallet_scores ws ON ws.address = wcs.address
    WHERE wcs.category = cat
    ORDER BY wcs.clv DESC
    LIMIT lim;
$$;

-- Function to get recent alerts with wallet context
CREATE OR REPLACE FUNCTION get_recent_alerts(lim INTEGER DEFAULT 50)
RETURNS TABLE (
    id BIGINT,
    address TEXT,
    wallet_label TEXT,
    wallet_tier TEXT,
    market_title TEXT,
    category TEXT,
    outcome TEXT,
    side TEXT,
    price NUMERIC,
    amount_usd NUMERIC,
    arb_gap NUMERIC,
    alert_type TEXT,
    timestamp TIMESTAMPTZ
) LANGUAGE sql STABLE AS $$
    SELECT
        a.id, a.address, a.wallet_label, a.wallet_tier,
        a.market_title, a.category, a.outcome, a.side,
        a.price, a.amount_usd, a.arb_gap, a.alert_type,
        a.timestamp
    FROM alerts a
    ORDER BY a.timestamp DESC
    LIMIT lim;
$$;
