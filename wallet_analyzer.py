"""
SharpWallet -- Wallet Analyzer
Pulls complete trade history for a wallet from Polymarket's public data API
and calculates CLV, PnL, calibration, and composite sharp score.

Usage:
python wallet_analyzer.py --wallet 0xebd9018611387df205fc3931bcaf988e897883ea
python wallet_analyzer.py --wallet 0x... --save-db   # also write to Supabase
python wallet_analyzer.py --wallet 0x... --output-json scores.json
"""

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

try:
    from supabase import create_client
except Exception:
    create_client = None

# -- Configuration ------------------------------------------------------------

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

PAGE_SIZE            = 500
RATE_LIMIT           = 0.15   # seconds between requests
DECAY_HALF_LIFE_DAYS = 180    # recency weighting: 6 months = 50% weight

# Bayesian prior for win rate shrinkage (conservative: assume 50% baseline)
PRIOR_ALPHA = 10
PRIOR_BETA  = 10

# Composite score weights (must sum to 1.0)
WEIGHTS = {
    "clv":        0.35,
    "pnl":        0.25,
    "calibration": 0.20,
    "win_rate":   0.20,
}

# -- Data Models --------------------------------------------------------------

@dataclass
class Trade:
    condition_id: str
    asset_id:     str          # token ID on Polymarket
    side:         str          # "BUY" or "SELL"
    price:        float        # entry price (0.0-1.0)
    size:         float        # USDC amount
    timestamp:    datetime
    title:        str = ""
    outcome:      str = ""
    outcome_index: int = 0

@dataclass
class Position:
    condition_id:   str
    asset_id:       str
    outcome:        str
    outcome_index:  int
    size:           float
    avg_price:      float
    initial_value:  float
    current_value:  float
    cash_pnl:       float
    realized_pnl:   float
    cur_price:      float
    redeemable:     bool
    title:          str = ""
    category:       str = "unknown"
    end_date:       str = ""

@dataclass
class CategoryScore:
    category:       str
    n_markets:      int   = 0
    n_wins:         int   = 0
    total_staked:   float = 0.0
    total_pnl:      float = 0.0
    clv_values:     list  = field(default_factory=list)
    calibration_buckets: dict = field(default_factory=lambda: defaultdict(lambda: {"n": 0, "wins": 0}))

    @property
    def win_rate_raw(self) -> float:
        return self.n_wins / self.n_markets if self.n_markets > 0 else 0.0

    @property
    def win_rate_bayesian(self) -> float:
        return (PRIOR_ALPHA + self.n_wins) / (PRIOR_ALPHA + PRIOR_BETA + self.n_markets)

    @property
    def avg_clv(self) -> float:
        return sum(self.clv_values) / len(self.clv_values) if self.clv_values else 0.0

    @property
    def pnl_per_market(self) -> float:
        return self.total_pnl / self.n_markets if self.n_markets > 0 else 0.0

    @property
    def calibration_score(self) -> float:
        """Brier-style calibration: 1.0 = perfect, 0.0 = terrible."""
        if not self.calibration_buckets:
            return 0.5

        total_error = 0.0
        total_n = 0
        for bucket_mid, data in self.calibration_buckets.items():
            n = data["n"]
            if n == 0:
                continue
            actual_rate = data["wins"] / n
            expected_rate = bucket_mid
            total_error += n * (actual_rate - expected_rate) ** 2
            total_n += n

        if total_n == 0:
            return 0.5

        mse = total_error / total_n
        return max(0.0, 1.0 - (mse / 0.25))

@dataclass
class WalletScore:
    wallet_address:  str
    total_markets:   int   = 0
    total_staked:    float = 0.0
    total_pnl:       float = 0.0
    account_age_days: int  = 0
    category_scores: dict  = field(default_factory=dict)
    composite_score: float = 0.0
    tier:            str   = "unranked"
    computed_at:     str   = ""

# -- API Client (uses public data-api, no auth required) ----------------------

class PolymarketClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._market_cache: dict[str, dict] = {}

    async def _get(self, url: str, params: dict = None) -> dict | list:
        await asyncio.sleep(RATE_LIMIT)
        async with self.session.get(url, params=params) as resp:
            if resp.status == 429:
                print("  Rate limited -- waiting 5s...")
                await asyncio.sleep(5)
                return await self._get(url, params)
            resp.raise_for_status()
            return await resp.json()

    async def get_all_trades(self, wallet: str) -> list[Trade]:
        """Pull every trade for a wallet from the public data API."""
        trades = []
        offset = 0

        print(f"\nFetching trades for {wallet[:10]}...")

        while True:
            data = await self._get(f"{DATA_API}/trades", params={
                "user": wallet,
                "limit": PAGE_SIZE,
                "offset": offset,
            })

            if not isinstance(data, list) or not data:
                break

            for r in data:
                try:
                    trades.append(Trade(
                        condition_id  = r["conditionId"],
                        asset_id      = r["asset"],
                        side          = r["side"],
                        price         = float(r["price"]),
                        size          = float(r["size"]),
                        timestamp     = datetime.fromtimestamp(
                                            int(r["timestamp"]), tz=timezone.utc
                                        ),
                        title         = r.get("title", ""),
                        outcome       = r.get("outcome", ""),
                        outcome_index = int(r.get("outcomeIndex", 0)),
                    ))
                except (KeyError, ValueError) as e:
                    print(f"  Skipping malformed trade record: {e}")
                    continue

            print(f"  Fetched {len(data)} trades (total: {len(trades)})")

            if len(data) < PAGE_SIZE:
                break
            offset += len(data)

        print(f"Total trades fetched: {len(trades)}")
        return trades

    async def get_positions(self, wallet: str) -> list[Position]:
        """Pull all positions (with pre-computed PnL) from the data API."""
        positions = []
        offset = 0

        while True:
            data = await self._get(f"{DATA_API}/positions", params={
                "user": wallet,
                "sizeThreshold": 0,
                "limit": PAGE_SIZE,
                "offset": offset,
            })

            if not isinstance(data, list) or not data:
                break

            for r in data:
                try:
                    positions.append(Position(
                        condition_id  = r["conditionId"],
                        asset_id      = r["asset"],
                        outcome       = r.get("outcome", ""),
                        outcome_index = int(r.get("outcomeIndex", 0)),
                        size          = float(r.get("size", 0)),
                        avg_price     = float(r.get("avgPrice", 0)),
                        initial_value = float(r.get("initialValue", 0)),
                        current_value = float(r.get("currentValue", 0)),
                        cash_pnl      = float(r.get("cashPnl", 0)),
                        realized_pnl  = float(r.get("realizedPnl", 0)),
                        cur_price     = float(r.get("curPrice", 0)),
                        redeemable    = bool(r.get("redeemable", False)),
                        title         = r.get("title", ""),
                        end_date      = r.get("endDate", ""),
                    ))
                except (KeyError, ValueError):
                    continue

            if len(data) < PAGE_SIZE:
                break
            offset += len(data)

        return positions

    async def get_market(self, condition_id: str) -> dict:
        """Fetch market metadata from Gamma API (for category, outcomes, etc)."""
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]

        try:
            data = await self._get(f"{GAMMA_API}/markets", params={
                "conditionId": condition_id,
                "limit": 1,
            })
            if isinstance(data, list) and data:
                market = data[0]
                self._market_cache[condition_id] = market
                return market
        except Exception as e:
            print(f"  Could not fetch market {condition_id[:16]}...: {e}")

        return {}

# -- CLV Calculator -----------------------------------------------------------

def calculate_clv(entry_price: float, closing_price: float, side: str) -> float:
    """
    Closing Line Value: did you beat the market's final assessment?
    CLV > 0 = edge over the crowd. CLV < 0 = market moved against you.
    """
    if side == "BUY":
        return closing_price - entry_price
    else:
        return entry_price - closing_price

def get_calibration_bucket(price: float) -> float:
    """Map a price to its bucket midpoint (0.05, 0.15, ..., 0.95)."""
    bucket = math.floor(price * 10) / 10
    return min(0.9, max(0.0, bucket)) + 0.05

def recency_weight(ts: datetime) -> float:
    """Exponential decay: today=1.0, 6mo=0.5, 12mo=0.25."""
    age_days = (datetime.now(timezone.utc) - ts).days
    return math.exp(-math.log(2) * age_days / DECAY_HALF_LIFE_DAYS)

# -- Scorer -------------------------------------------------------------------

class WalletScorer:
    def __init__(self, client: PolymarketClient):
        self.client = client

    async def score_wallet(self, wallet: str) -> WalletScore:
        score = WalletScore(wallet_address=wallet)

        # Fetch trades and positions in parallel
        trades, positions = await asyncio.gather(
            self.client.get_all_trades(wallet),
            self.client.get_positions(wallet),
        )

        if not trades and not positions:
            print("No trades found for this wallet.")
            return score

        # Build position lookup: conditionId -> Position
        pos_by_condition: dict[str, list[Position]] = defaultdict(list)
        for p in positions:
            pos_by_condition[p.condition_id].append(p)

        # Group trades by market (conditionId)
        by_market: dict[str, list[Trade]] = defaultdict(list)
        for t in trades:
            by_market[t.condition_id].append(t)

        score.total_markets = len(by_market)
        if trades:
            score.account_age_days = (
                datetime.now(timezone.utc) - min(t.timestamp for t in trades)
            ).days

        print(f"\nScoring {score.total_markets} markets...")

        cat_scores: dict[str, CategoryScore] = defaultdict(
            lambda: CategoryScore(category="unknown")
        )

        for i, (condition_id, market_trades) in enumerate(by_market.items()):
            if i % 50 == 0 and i > 0:
                print(f"  Processing market {i}/{score.total_markets}...")

            # Get market metadata for category
            market_meta = await self.client.get_market(condition_id)
            category = (market_meta.get("category") or "unknown").lower()

            # Check if market is resolved via positions or outcomePrices
            market_positions = pos_by_condition.get(condition_id, [])

            # Determine resolution from outcomePrices
            outcome_prices_str = market_meta.get("outcomePrices", "[]")
            try:
                outcome_prices = json.loads(outcome_prices_str) if isinstance(outcome_prices_str, str) else outcome_prices_str
                is_resolved = any(float(p) >= 0.99 for p in outcome_prices)
            except (json.JSONDecodeError, ValueError, TypeError):
                is_resolved = False

            if not is_resolved:
                continue

            # Find winning outcome index
            winning_idx = None
            try:
                for idx, p in enumerate(outcome_prices):
                    if float(p) >= 0.99:
                        winning_idx = idx
                        break
            except (ValueError, TypeError):
                pass

            # Get buy trades for this market
            buy_trades = [t for t in market_trades if t.side == "BUY"]
            if not buy_trades:
                continue

            # Net position
            total_bought = sum(t.size for t in market_trades if t.side == "BUY")
            total_sold   = sum(t.size for t in market_trades if t.side == "SELL")
            net_staked   = max(0, total_bought - total_sold)

            # Average entry price (weighted by size)
            avg_entry = sum(t.price * t.size for t in buy_trades) / total_bought

            # Did they win? Check if their outcome matches the winner
            primary_outcome_idx = buy_trades[0].outcome_index
            outcome_won = (primary_outcome_idx == winning_idx) if winning_idx is not None else None

            # PnL from positions (pre-computed by Polymarket)
            pnl = None
            if market_positions:
                pnl = sum(p.cash_pnl for p in market_positions)
            elif outcome_won is True:
                pnl = total_bought * (1.0 - avg_entry)
            elif outcome_won is False:
                pnl = -net_staked * avg_entry

            # CLV: use curPrice from position as closing price proxy
            clv = None
            if market_positions:
                # Use the position's current price as closing price
                closing_price = market_positions[0].cur_price
                if closing_price > 0:
                    clv = calculate_clv(avg_entry, closing_price, "BUY")

            # Recency weight
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
        """Weighted composite across all categories, normalized 0-100."""
        if not score.category_scores:
            return 0.0

        total_markets = sum(c.n_markets for c in score.category_scores.values())
        if total_markets == 0:
            return 0.0

        weighted_clv         = 0.0
        weighted_pnl         = 0.0
        weighted_calibration = 0.0
        weighted_winrate     = 0.0

        for cat in score.category_scores.values():
            w = cat.n_markets / total_markets

            norm_clv = min(1.0, max(0.0, (cat.avg_clv + 0.15) / 0.30))
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

        return round(composite * 100, 2)

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

# -- Reporter -----------------------------------------------------------------

def print_report(score: WalletScore):
    print("\n" + "=" * 60)
    print(f"  WALLET SCORE REPORT")
    print(f"  {score.wallet_address}")
    print("=" * 60)
    print(f"  Composite Score : {score.composite_score:.1f} / 100")
    print(f"  Tier            : {score.tier.upper()}")
    print(f"  Total Markets   : {score.total_markets:,}")
    print(f"  Total Staked    : ${score.total_staked:,.0f}")
    print(f"  Total PnL       : ${score.total_pnl:,.0f}")
    print(f"  Account Age     : {score.account_age_days} days")
    print()
    print("  CATEGORY BREAKDOWN:")
    print(f"  {'Category':<20} {'Markets':>7} {'Win%':>6} {'Avg CLV':>8} {'PnL/Mkt':>10} {'Calibr':>8}")
    print(f"  {'-'*20} {'-'*7} {'-'*6} {'-'*8} {'-'*10} {'-'*8}")

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
            f"{cat.avg_clv*100:>+7.1f}c "
            f"${cat.pnl_per_market:>9,.0f} "
            f"{cat.calibration_score:>7.2f}"
        )

    print("=" * 60)

# -- Supabase Writer ----------------------------------------------------------

def save_to_supabase(score: WalletScore):
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY not set -- skipping DB save")
        return
    if create_client is None:
        print("supabase package not available -- skipping DB save")
        return

    sb = create_client(url, key)

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

    print("Saved to Supabase")

# -- Entry Point --------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="SharpWallet -- Wallet Scorer")
    parser.add_argument("--wallet", required=True, help="Wallet address (0x...)")
    parser.add_argument("--save-db", action="store_true", help="Save results to Supabase")
    parser.add_argument("--output-json", help="Save raw score to JSON file")
    args = parser.parse_args()

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
                output = asdict(score)
                json.dump(output, f, indent=2, default=str)
            print(f"Score saved to {args.output_json}")

if __name__ == "__main__":
    asyncio.run(main())
