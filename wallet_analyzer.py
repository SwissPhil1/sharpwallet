“””
SharpWallet — Wallet Analyzer
Pulls complete trade history for a wallet from Polymarket CLOB API
and calculates CLV, PnL, calibration, and composite sharp score.

Usage:
python wallet_analyzer.py –wallet 0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee
python wallet_analyzer.py –wallet 0x6a72… –save-db  # also write to Supabase
“””

import asyncio
import aiohttp
import argparse
import json
import math
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict

import pandas as pd
from supabase import create_client

# ── Configuration ────────────────────────────────────────────────────────────

CLOB_API     = “https://clob.polymarket.com”
GAMMA_API    = “https://gamma-api.polymarket.com”
DATA_API     = “https://data-api.polymarket.com”

PAGE_SIZE    = 500          # max records per API page
RATE_LIMIT   = 0.12         # seconds between requests (~8 req/s, under 10 limit)
DECAY_HALF_LIFE_DAYS = 180  # recency weighting: 6 months = 50% weight

# Bayesian prior for win rate shrinkage (conservative: assume 50% baseline)

PRIOR_ALPHA  = 10
PRIOR_BETA   = 10

# Composite score weights (must sum to 1.0)

WEIGHTS = {
“clv”:        0.35,
“pnl”:        0.25,
“calibration”: 0.20,
“win_rate”:   0.20,
}

# ── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class Trade:
market_id:    str
outcome_id:   str          # token ID on Polymarket
side:         str          # “BUY” or “SELL”
price:        float        # entry price in cents (0.0–1.0)
size:         float        # USDC amount
timestamp:    datetime
category:     str = “unknown”
question:     str = “”
outcome_won:  Optional[bool] = None
closing_price: Optional[float] = None  # market price 1hr before resolution
pnl:          Optional[float] = None   # realized profit/loss in USDC
clv:          Optional[float] = None   # closing line value

@dataclass
class CategoryScore:
category:       str
n_markets:      int   = 0
n_wins:         int   = 0
total_staked:   float = 0.0
total_pnl:      float = 0.0
clv_values:     list  = field(default_factory=list)
calibration_buckets: dict = field(default_factory=lambda: defaultdict(lambda: {“n”: 0, “wins”: 0}))

```
@property
def win_rate_raw(self) -> float:
    return self.n_wins / self.n_markets if self.n_markets > 0 else 0.0

@property
def win_rate_bayesian(self) -> float:
    """Shrink toward 50% prior based on sample size."""
    return (PRIOR_ALPHA + self.n_wins) / (PRIOR_ALPHA + PRIOR_BETA + self.n_markets)

@property
def avg_clv(self) -> float:
    return sum(self.clv_values) / len(self.clv_values) if self.clv_values else 0.0

@property
def pnl_per_market(self) -> float:
    return self.total_pnl / self.n_markets if self.n_markets > 0 else 0.0

@property
def calibration_score(self) -> float:
    """
    Brier-style calibration: 1.0 = perfect, 0.0 = terrible.
    For each probability bucket, penalize deviation from expected win rate.
    """
    if not self.calibration_buckets:
        return 0.5  # neutral if no data
    
    total_error = 0.0
    total_n = 0
    for bucket_mid, data in self.calibration_buckets.items():
        n = data["n"]
        if n == 0:
            continue
        actual_rate = data["wins"] / n
        expected_rate = bucket_mid
        # Weighted mean squared error
        total_error += n * (actual_rate - expected_rate) ** 2
        total_n += n
    
    if total_n == 0:
        return 0.5
    
    mse = total_error / total_n
    # Convert MSE to 0-1 score (MSE of 0.25 = random baseline)
    return max(0.0, 1.0 - (mse / 0.25))
```

@dataclass
class WalletScore:
wallet_address: str
total_markets:  int   = 0
total_staked:   float = 0.0
total_pnl:      float = 0.0
account_age_days: int = 0
category_scores: dict = field(default_factory=dict)  # category -> CategoryScore
composite_score: float = 0.0
tier:           str   = “unranked”
computed_at:    str   = “”

# ── API Client ───────────────────────────────────────────────────────────────

class PolymarketClient:
def **init**(self, session: aiohttp.ClientSession):
self.session = session
self._market_cache: dict[str, dict] = {}

```
async def _get(self, url: str, params: dict = None) -> dict | list:
    await asyncio.sleep(RATE_LIMIT)
    async with self.session.get(url, params=params) as resp:
        if resp.status == 429:
            print(f"  Rate limited — waiting 5s...")
            await asyncio.sleep(5)
            return await self._get(url, params)
        resp.raise_for_status()
        return await resp.json()

async def get_all_trades(self, wallet: str) -> list[Trade]:
    """
    Pull every trade for a wallet from the CLOB API.
    Handles pagination automatically.
    """
    trades = []
    cursor = None
    page = 0

    print(f"\n📥 Fetching trades for {wallet[:10]}...")

    while True:
        params = {
            "maker": wallet,
            "limit": PAGE_SIZE,
        }
        if cursor:
            params["next_cursor"] = cursor

        data = await self._get(f"{CLOB_API}/trades", params=params)

        # CLOB API returns {"data": [...], "next_cursor": "..."}
        records = data.get("data", [])
        if not records:
            break

        for r in records:
            try:
                trades.append(Trade(
                    market_id  = r["market"],
                    outcome_id = r["asset_id"],
                    side       = r["side"],          # BUY / SELL
                    price      = float(r["price"]),
                    size       = float(r["size"]),
                    timestamp  = datetime.fromtimestamp(
                                     int(r["match_time"]), tz=timezone.utc
                                 ),
                ))
            except (KeyError, ValueError) as e:
                print(f"  ⚠️  Skipping malformed trade record: {e}")
                continue

        page += 1
        cursor = data.get("next_cursor")
        print(f"  Page {page}: {len(records)} trades (total: {len(trades)})")

        if not cursor or cursor == "LTE=":  # LTE= is Polymarket's "end" sentinel
            break

    print(f"✅ Total trades fetched: {len(trades)}")
    return trades

async def get_market(self, market_id: str) -> dict:
    """Fetch market metadata including category, question, resolution."""
    if market_id in self._market_cache:
        return self._market_cache[market_id]

    try:
        data = await self._get(f"{GAMMA_API}/markets/{market_id}")
        self._market_cache[market_id] = data
        return data
    except Exception as e:
        print(f"  ⚠️  Could not fetch market {market_id}: {e}")
        return {}

async def get_market_history(self, token_id: str) -> list[dict]:
    """
    Get price history for a specific outcome token.
    Used to find the closing price (1hr before resolution) for CLV calculation.
    """
    try:
        data = await self._get(
            f"{CLOB_API}/prices-history",
            params={"market": token_id, "interval": "1h", "fidelity": 60}
        )
        return data.get("history", [])
    except Exception:
        return []
```

# ── CLV Calculator ───────────────────────────────────────────────────────────

def calculate_clv(entry_price: float, closing_price: float, side: str) -> float:
“””
Closing Line Value: did you beat the market’s final assessment?

```
For a BUY at entry_price=0.40 with closing_price=0.55:
    CLV = 0.55 - 0.40 = +0.15  (you got a better price than market settled at)

For a SELL (taking profits / exiting) we invert:
    CLV = entry_price - closing_price

CLV > 0 = you had edge over the crowd
CLV < 0 = market moved against your thesis
"""
if side == "BUY":
    return closing_price - entry_price
else:  # SELL
    return entry_price - closing_price
```

def get_calibration_bucket(price: float) -> float:
“”“Map a price to its bucket midpoint (0.05, 0.15, 0.25, …, 0.95).”””
bucket = math.floor(price * 10) / 10
return min(0.9, max(0.0, bucket)) + 0.05

def recency_weight(trade_timestamp: datetime) -> float:
“””
Exponential decay weight based on age.
Trades from today = 1.0
Trades from 6 months ago = 0.5
Trades from 12 months ago = 0.25
“””
age_days = (datetime.now(timezone.utc) - trade_timestamp).days
return math.exp(-math.log(2) * age_days / DECAY_HALF_LIFE_DAYS)

# ── Scorer ───────────────────────────────────────────────────────────────────

class WalletScorer:
def **init**(self, client: PolymarketClient):
self.client = client

```
async def score_wallet(self, wallet: str) -> WalletScore:
    score = WalletScore(wallet_address=wallet)
    trades = await self.client.get_all_trades(wallet)

    if not trades:
        print("❌ No trades found for this wallet.")
        return score

    # Group trades by market
    by_market: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_market[t.market_id].append(t)

    score.total_markets = len(by_market)
    score.account_age_days = (
        datetime.now(timezone.utc) - min(t.timestamp for t in trades)
    ).days

    print(f"\n🔍 Scoring {score.total_markets} markets...")
    
    cat_scores: dict[str, CategoryScore] = defaultdict(
        lambda: CategoryScore(category="unknown")
    )

    # Enrich each market with metadata + closing prices
    for i, (market_id, market_trades) in enumerate(by_market.items()):
        if i % 50 == 0:
            print(f"  Processing market {i+1}/{score.total_markets}...")

        # Get market metadata
        market_meta = await self.client.get_market(market_id)
        category = market_meta.get("category", "unknown").lower()
        question = market_meta.get("question", "")
        resolved = market_meta.get("resolved", False)
        winning_outcome = market_meta.get("winner", None)  # token ID of winner

        if not resolved:
            continue  # skip unresolved markets for scoring

        # Get closing price for CLV (price 1hr before resolution)
        # Use the first outcome token found in this market's trades
        outcome_token = market_trades[0].outcome_id
        history = await self.client.get_market_history(outcome_token)
        
        closing_price = None
        if history:
            # Last price point before resolution
            closing_price = history[-1].get("p", None)

        # Determine if wallet was on winning side
        # A BUY trade on the winning outcome = win
        buy_trades = [t for t in market_trades if t.side == "BUY"]
        if not buy_trades:
            continue

        # Net position: total bought - total sold
        total_bought = sum(t.size for t in market_trades if t.side == "BUY")
        total_sold   = sum(t.size for t in market_trades if t.side == "SELL")
        net_staked   = total_bought - total_sold

        # Average entry price (weighted by size)
        avg_entry = sum(t.price * t.size for t in buy_trades) / total_bought

        # Did they win?
        outcome_won = (outcome_token == winning_outcome) if winning_outcome else None

        # PnL calculation
        if outcome_won is True:
            # Won: received (shares * $1.00) - staked
            pnl = total_bought - net_staked
        elif outcome_won is False:
            pnl = -net_staked
        else:
            pnl = None

        # CLV
        clv = None
        if closing_price is not None:
            clv = calculate_clv(avg_entry, closing_price, "BUY")

        # Recency weight for this market
        latest_trade = max(market_trades, key=lambda t: t.timestamp)
        weight = recency_weight(latest_trade.timestamp)

        # Update category score
        cat = cat_scores[category]
        cat.category = category
        cat.n_markets += 1
        cat.total_staked += net_staked * weight
        
        if pnl is not None:
            cat.total_pnl += pnl * weight
            score.total_pnl += pnl

        if outcome_won is True:
            cat.n_wins += 1

        if clv is not None:
            cat.clv_values.append(clv * weight)

        # Calibration bucket
        bucket = get_calibration_bucket(avg_entry)
        cat.calibration_buckets[bucket]["n"] += 1
        if outcome_won:
            cat.calibration_buckets[bucket]["wins"] += 1

        score.total_staked += net_staked

    score.category_scores = dict(cat_scores)
    score.composite_score = self._compute_composite(score)
    score.tier = self._assign_tier(score)
    score.computed_at = datetime.now(timezone.utc).isoformat()

    return score

def _compute_composite(self, score: WalletScore) -> float:
    """
    Weighted composite across all categories, normalized 0–100.
    """
    if not score.category_scores:
        return 0.0

    # Aggregate across categories (weighted by n_markets per category)
    total_markets = sum(c.n_markets for c in score.category_scores.values())
    if total_markets == 0:
        return 0.0

    weighted_clv        = 0.0
    weighted_pnl        = 0.0
    weighted_calibration = 0.0
    weighted_winrate    = 0.0

    for cat in score.category_scores.values():
        w = cat.n_markets / total_markets  # category weight by sample size

        # Normalize CLV to 0-1 scale (CLV of +0.10 = excellent)
        norm_clv = min(1.0, max(0.0, (cat.avg_clv + 0.15) / 0.30))

        # Normalize PnL per market (cap at $5,000/market = top score)
        norm_pnl = min(1.0, max(0.0, cat.pnl_per_market / 5000))

        weighted_clv         += w * norm_clv
        weighted_pnl         += w * norm_pnl
        weighted_calibration += w * cat.calibration_score
        weighted_winrate     += w * cat.win_rate_bayesian

    composite = (
        WEIGHTS["clv"]         * weighted_clv +
        WEIGHTS["pnl"]         * weighted_pnl +
        WEIGHTS["calibration"] * weighted_calibration +
        WEIGHTS["win_rate"]    * weighted_winrate
    )

    return round(composite * 100, 2)  # scale to 0–100

def _assign_tier(self, score: WalletScore) -> str:
    if score.total_markets < 15:
        return "insufficient_data"
    elif score.total_markets < 30:
        return "watch"
    elif score.composite_score >= 75 and score.total_markets >= 100:
        return "tier_1_sharp"
    elif score.composite_score >= 60 and score.total_markets >= 50:
        return "tier_2_sharp"
    elif score.composite_score >= 45:
        return "tier_3_emerging"
    else:
        return "not_sharp"
```

# ── Reporter ─────────────────────────────────────────────────────────────────

def print_report(score: WalletScore):
print(”\n” + “═” * 60)
print(f”  WALLET SCORE REPORT”)
print(f”  {score.wallet_address}”)
print(“═” * 60)
print(f”  Composite Score : {score.composite_score:.1f} / 100”)
print(f”  Tier            : {score.tier.upper()}”)
print(f”  Total Markets   : {score.total_markets:,}”)
print(f”  Total Staked    : ${score.total_staked:,.0f}”)
print(f”  Total PnL       : ${score.total_pnl:,.0f}”)
print(f”  Account Age     : {score.account_age_days} days”)
print()
print(”  CATEGORY BREAKDOWN:”)
print(f”  {‘Category’:<20} {‘Markets’:>7} {‘Win%’:>6} {‘Avg CLV’:>8} {‘PnL/Mkt’:>10} {‘Calibr’:>8}”)
print(f”  {’-’*20} {’-’*7} {’-’*6} {’-’*8} {’-’*10} {’-’*8}”)

```
# Sort by number of markets descending
sorted_cats = sorted(
    score.category_scores.values(),
    key=lambda c: c.n_markets,
    reverse=True
)

for cat in sorted_cats:
    if cat.n_markets < 3:
        continue
    print(
        f"  {cat.category:<20} "
        f"{cat.n_markets:>7,} "
        f"{cat.win_rate_bayesian*100:>5.1f}% "
        f"{cat.avg_clv*100:>+7.1f}¢ "
        f"${cat.pnl_per_market:>9,.0f} "
        f"{cat.calibration_score:>7.2f}"
    )

print("═" * 60)
```

# ── Supabase Writer ───────────────────────────────────────────────────────────

def save_to_supabase(score: WalletScore):
url = os.environ.get(“SUPABASE_URL”)
key = os.environ.get(“SUPABASE_SERVICE_KEY”)
if not url or not key:
print(“⚠️  SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping DB save”)
return

```
sb = create_client(url, key)

# Upsert wallet summary
sb.table("wallet_scores").upsert({
    "wallet_address":  score.wallet_address,
    "composite_score": score.composite_score,
    "tier":            score.tier,
    "total_markets":   score.total_markets,
    "total_staked":    score.total_staked,
    "total_pnl":       score.total_pnl,
    "account_age_days": score.account_age_days,
    "computed_at":     score.computed_at,
}).execute()

# Upsert per-category scores
for cat in score.category_scores.values():
    if cat.n_markets < 3:
        continue
    sb.table("wallet_category_scores").upsert({
        "wallet_address":     score.wallet_address,
        "category":           cat.category,
        "n_markets":          cat.n_markets,
        "n_wins":             cat.n_wins,
        "win_rate_bayesian":  cat.win_rate_bayesian,
        "avg_clv":            cat.avg_clv,
        "pnl_per_market":     cat.pnl_per_market,
        "calibration_score":  cat.calibration_score,
        "total_pnl":          cat.total_pnl,
    }).execute()

print("✅ Saved to Supabase")
```

# ── Entry Point ───────────────────────────────────────────────────────────────

async def main():
parser = argparse.ArgumentParser(description=“SharpWallet — Wallet Scorer”)
parser.add_argument(”–wallet”, required=True, help=“Wallet address (0x…)”)
parser.add_argument(”–save-db”, action=“store_true”, help=“Save results to Supabase”)
parser.add_argument(”–output-json”, help=“Save raw score to JSON file”)
args = parser.parse_args()

```
wallet = args.wallet.lower()

async with aiohttp.ClientSession() as session:
    client = PolymarketClient(session)
    scorer = WalletScorer(client)

    score = await scorer.score_wallet(wallet)
    print_report(score)

    if args.save_db:
        save_to_supabase(score)

    if args.output_json:
        with open(args.output_json, "w") as f:
            # Convert dataclass to dict, handle nested structures
            output = asdict(score)
            # Convert defaultdicts to regular dicts for JSON serialization
            json.dump(output, f, indent=2, default=str)
        print(f"📄 Score saved to {args.output_json}")
```

if **name** == “**main**”:
asyncio.run(main())
