# SharpWallet System — Technical Audit Package

## Overview

SharpWallet is a Polymarket wallet tracking and scoring system that:
1. Discovers active traders from Polymarket's data API
2. Scores them on "sharpness" (edge quality) using proprietary metrics
3. Monitors their trades in real-time via WebSocket
4. Sends Telegram alerts when sharp wallets trade
5. Displays everything on a live dashboard

**Architecture:**
- **Backend:** Python scripts running on Railway (worker process)
- **Database:** Supabase (PostgreSQL + REST API)
- **Frontend:** Static HTML dashboard on Vercel
- **Alerts:** Telegram Bot API

---

## Repository Structure

```
sharpwallet/                 # github.com/SwissPhil1/sharpwallet (private)
├── scheduler.py             # Main entry point (Railway worker)
├── batch_score.py           # Batch wallet discovery + scoring
├── wallet_analyzer.py       # Core scoring engine
├── ws_monitor.py            # WebSocket real-time trade monitor
├── leaderboard_scraper.py   # Legacy batch scorer
├── refresh_profiles.py      # Profile/category refresh utility
├── seed_data.py             # Test data seeder
├── apply_schema.py          # Schema migration tool
├── index.html               # Dashboard (served by Vercel)
├── vercel.json              # Vercel deployment config
├── Procfile                 # Railway: `worker: python scheduler.py`
├── requirements.txt         # Python dependencies
└── sql/
    └── 001_schema.sql       # Database schema
```

---

## Data Flow

```
Polymarket APIs                    Supabase DB                  Dashboard
─────────────                     ───────────                  ─────────

data-api.polymarket.com/trades     wallets table               index.html
   │  (discover traders)            │ address (PK)              (Vercel)
   ▼                                │ label                        │
batch_score.py ──────────────────►  │ total_bets                   │
   │  quick_score() per wallet      │ total_volume                 │
   │                                │ is_tracked                   │
   │                                                               │
   ▼                               wallet_scores table             │
gamma-api (trades, positions)       │ address (PK)                 │
   │                                │ clv, win_rate, roi    ◄──────┘
   ▼                                │ calibration, sharpe     reads via
wallet_analyzer.py                  │ kelly, avg_edge         Supabase JS
   │  compute_clv()                 │ tier, rank
   │  compute_calibration()         │
   │  assign_tier()                wallet_category_scores
   ▼                                │ (address, category) PK
save_to_supabase()                  │ clv, win_rate, roi
                                    │ total_bets

                                   bets table
ws.polymarket.com (WebSocket)       │ id (PK)
   │  real-time trades              │ address, market_slug
   ▼                                │ category, price, size
ws_monitor.py                       │ side, outcome
   │  match tracked wallets         │ clv, won, resolved
   │  calculate CLV                 │ closing_price, timestamp
   ▼
Telegram Bot API                   trade_alerts table
   │  send alerts                   │ id (PK)
   ▼                                │ address, market_title
@The_Sharpest_bot                   │ price, size, side
                                    │ alert_type
```

---

## External APIs Used

| API | Endpoint | Auth | Purpose |
|-----|----------|------|---------|
| Polymarket Data API | `data-api.polymarket.com/trades` | None | Discover active traders |
| Polymarket Data API | `data-api.polymarket.com/trades?user=X` | None | Fetch trade history |
| Polymarket Data API | `data-api.polymarket.com/positions?user=X` | None | Fetch open positions + PnL |
| Polymarket Gamma API | `gamma-api.polymarket.com/users?username=X` | None | Username → address lookup |
| Polymarket Gamma API | `gamma-api.polymarket.com/public-profile?address=X` | None | Address → username lookup |
| Polymarket Gamma API | `gamma-api.polymarket.com/markets?slug=X` | None | Market metadata |
| Polymarket CLOB API | `clob.polymarket.com/profile/X` | None | Profile lookup |
| Polymarket WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/...` | None | Real-time trade stream |
| Supabase | `{project}.supabase.co/rest/v1/` | Service key | Database CRUD |
| Telegram Bot API | `api.telegram.org/bot{token}/sendMessage` | Bot token | Alert delivery |

---

## Database Schema

```sql
-- Wallets being tracked
CREATE TABLE wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,                -- Polymarket username or short address
    first_seen      TIMESTAMPTZ DEFAULT now(),
    total_bets      INTEGER DEFAULT 0,
    total_volume    NUMERIC(18,2) DEFAULT 0,
    is_tracked      BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Aggregate scoring metrics
CREATE TABLE wallet_scores (
    address         TEXT PRIMARY KEY REFERENCES wallets(address),
    total_bets      INTEGER DEFAULT 0,
    win_rate        NUMERIC(8,4) DEFAULT 0,
    clv             NUMERIC(8,4) DEFAULT 0,
    roi             NUMERIC(8,4) DEFAULT 0,
    calibration     NUMERIC(8,4) DEFAULT 0,
    avg_edge        NUMERIC(8,4) DEFAULT 0,
    sharpe_ratio    NUMERIC(8,4) DEFAULT 0,
    kelly_fraction  NUMERIC(8,4) DEFAULT 0,
    tier            TEXT DEFAULT 'unknown',
    rank            INTEGER,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Per-category performance
CREATE TABLE wallet_category_scores (
    address     TEXT REFERENCES wallets(address),
    category    TEXT NOT NULL,
    total_bets  INTEGER DEFAULT 0,
    win_rate    NUMERIC(8,4) DEFAULT 0,
    clv         NUMERIC(8,4) DEFAULT 0,
    roi         NUMERIC(8,4) DEFAULT 0,
    PRIMARY KEY (address, category)
);

-- Individual bets
CREATE TABLE bets (
    id              BIGSERIAL PRIMARY KEY,
    address         TEXT REFERENCES wallets(address),
    market_slug     TEXT,
    market_title    TEXT,
    category        TEXT DEFAULT 'other',
    outcome         TEXT,
    side            TEXT,
    price           NUMERIC(8,4),
    size            NUMERIC(18,4),
    amount_usd      NUMERIC(18,2),
    timestamp       TIMESTAMPTZ,
    resolved        BOOLEAN DEFAULT FALSE,
    won             BOOLEAN,
    closing_price   NUMERIC(8,4),
    clv             NUMERIC(8,4),
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Trade alerts sent
CREATE TABLE trade_alerts (
    id              BIGSERIAL PRIMARY KEY,
    address         TEXT REFERENCES wallets(address),
    market_title    TEXT,
    market_slug     TEXT,
    side            TEXT,
    price           NUMERIC(8,4),
    size            NUMERIC(18,4),
    amount_usd      NUMERIC(18,2),
    alert_type      TEXT DEFAULT 'trade',
    sent_at         TIMESTAMPTZ DEFAULT now()
);
```

---

## Metric Definitions & Formulas

### 1. CLV (Closing Line Value)
**Purpose:** Measures how much value the trader captured vs the closing price.

**Formula:**
```
BUY side:  CLV = closing_price - entry_price
SELL side: CLV = entry_price - closing_price
```

**Example:** Buy at $0.34, market closes at $1.00 → CLV = +0.66 (66%)

**Known issue:** CLV is only computed when `closing_price` is available. In practice, closing_price comes from position data which is often only populated for winning/resolved bets. This creates survivorship bias — CLV tends to be inflated because losing bets often have `CLV=None` and are excluded from the average.

**Stored as:** Decimal value (0.4155 = 41.55%), displayed as percentage on dashboard.

### 2. Win Rate
**Formula:**
```
win_rate = wins / resolved_bets
```

- Only considers bets where `closing_price ∈ {0, 1}` (fully resolved markets)
- Open positions excluded entirely
- Trades in unresolved markets excluded

**Known issue:** Only a fraction of bets may be resolved at any given time (e.g., 208/400 for kch123), and the `total_bets` count on the wallet row comes from a different source (profile API), creating confusion.

### 3. ROI (Return on Investment)
**Formula:**
```
Per winning bet PnL:  (1 - entry_price) * size
Per losing bet PnL:   -entry_price * size
ROI = total_pnl / total_wagered
```

**Override chain (wallet_analyzer.py only):**
1. Trade-level calculation (above)
2. Position-level override: `sum(cashPnl) / sum(|initialValue|)` from Polymarket positions API
3. Profile-level override: `profile_pnl / profile_volume` scraped from Polymarket profile page

**batch_score.py** only does steps 1-2, creating potential inconsistencies.

### 4. Calibration
**Formula:** Mean absolute error between implied probability and actual outcome per price bucket.

```
Buckets: [0-10%), [10-20%), ..., [90-100%)
Implied probability for bucket i: (i + 0.5) / 10
Actual win rate: wins_in_bucket / total_in_bucket
Error: |actual - implied|
Calibration = mean(errors across buckets)
```

**Interpretation:** Lower = better. A perfectly calibrated trader would have calibration ≈ 0.

### 5. Sharpe Ratio
**Formula:**
```
sharpe = ROI / max(0.01, calibration)
```

**NOTE:** This is a non-standard Sharpe ratio. Traditional Sharpe = (return - risk_free_rate) / volatility. Here, calibration error is used as a volatility proxy.

### 6. Average Edge
**Formula:**
```
avg_edge = avg_clv * 0.7 + (win_rate - 0.5) * 0.3
```

Blended signal: 70% weight on CLV, 30% on win rate excess over 50%.

### 7. Kelly Fraction
**Formula:**
```
kelly = max(0, (win_rate * (1 + avg_clv) - 1) / max(avg_clv, 0.01))
```

Optimal bet sizing fraction from the Kelly criterion. Clipped to 0 minimum.

### 8. Tier Assignment
```
if total_bets < 5:           → "unknown"
if CLV > 5% AND WR > 55%:   → "elite"
if CLV > 2% AND WR > 52%:   → "sharp"
if CLV > 0  AND WR > 48%:   → "moderate"
else:                        → "noise"

# Override for high-volume profitable traders:
if ROI > 5% AND volume > $100k: → "elite"
if ROI > 2% AND volume > $100k: → "sharp"
```

---

## Known Bugs & Issues

### Critical

1. **CLV survivorship bias** — CLV is only computed for bets with closing_price data. Losing bets often lack this data, so avg CLV is calculated predominantly from winning bets. This inflates CLV across the board. Fix: compute CLV from all resolved bets (where closing_price = 0 or 1).

2. **Trade count mismatch** — The API fetches up to 500 trades per wallet, but `total_bets` in `wallet_scores` comes from profile scraping (e.g., 2032). All per-bet metrics (CLV, win_rate, categories) are computed from only the fetched 400-500, not all 2032.

3. **ROI inconsistency** — `wallet_analyzer.py` has three ROI override stages; `batch_score.py` has two. The same wallet scored by different paths gets different ROI values.

### Moderate

4. **Category scores 0% for small categories** — If no bets in a category are resolved, that category shows 0% for all metrics (CLV, WR, ROI), which is misleading. Better to show "N/A" or exclude.

5. **Sharpe ratio is non-standard** — Using calibration error as denominator is unconventional and may confuse finance professionals.

6. **Calibration default 0.5** — When no resolved bets exist, calibration defaults to 0.5 (very high error), which artificially suppresses Sharpe ratio.

### Minor

7. **CLV returns 0 for missing data** — `compute_clv()` returns 0 when prices are None instead of None, which could dilute averages.

8. **Kelly sensitivity** — When avg_clv < 0.01, the denominator becomes 0.01, making Kelly extremely sensitive to small CLV fluctuations.

---

## Market Categorization

Markets are classified by keyword matching on title + tags:

| Category | Example Keywords |
|----------|-----------------|
| Politics | trump, biden, election, tariff, nato, ukraine, sanctions |
| Crypto | bitcoin, ethereum, solana, defi, etf |
| Sports | nfl, nba, team names, "X vs Y", super bowl |
| Entertainment | oscar, grammy, netflix, billboard |
| Science/Tech | openai, chatgpt, spacex, nasa, nvidia |
| Economy | fed, interest rate, inflation, gdp, s&p 500 |
| Weather | hurricane, earthquake, temperature |
| Other | Default fallback |

---

## Environment Variables Required

| Variable | Description |
|----------|------------|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service role key (full write access) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for @The_Sharpest_bot |
| `TELEGRAM_CHAT_ID` | Chat/group ID for alert delivery |
| `RESCORE_INTERVAL_HOURS` | Hours between batch rescoring (default: 168 = weekly) |

---

## Audit Checklist

- [ ] Verify CLV calculation is not biased by only including winning bets
- [ ] Verify win_rate denominator (resolved bets vs total bets)
- [ ] Verify ROI calculation chain and consistency across scoring paths
- [ ] Verify tier thresholds make sense for the Polymarket domain
- [ ] Verify category keywords cover the actual market distribution
- [ ] Verify real-time WebSocket trade matching logic
- [ ] Verify Telegram alerts fire correctly for tracked wallets
- [ ] Verify dashboard reads from correct Supabase tables
- [ ] Verify Supabase RLS policies are secure (no public write access)
- [ ] Stress test: what happens with 1000+ wallets? Rate limiting?
- [ ] Review: is the "Sharpe ratio" definition useful or misleading?
- [ ] Review: should CLV be volume-weighted instead of simple average?
