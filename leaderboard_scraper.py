"""
SharpWallet -- Leaderboard Scraper
Discovers active wallets from high-volume resolved Polymarket markets,
filters out noise, and runs the full scoring pipeline on each candidate.

Strategy: Instead of relying on a leaderboard API (which is no longer public),
we scan trades from the highest-volume resolved markets to find wallets with
meaningful trading activity, then score them.
"""

import asyncio
import aiohttp
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from supabase import create_client
from wallet_analyzer import (
    PolymarketClient, WalletScorer, WalletScore, print_report,
    DATA_API, GAMMA_API,
)

# -- Configuration ------------------------------------------------------------

MIN_MARKETS      = int(os.environ.get("MIN_MARKETS", "30"))
MIN_VOLUME_USD   = float(os.environ.get("MIN_VOLUME_USD", "500"))
MAX_WALLETS      = int(os.environ.get("MAX_WALLETS", "200"))
MARKETS_TO_SCAN  = int(os.environ.get("MARKETS_TO_SCAN", "100"))
RATE_LIMIT       = 0.15

# -- Wallet Discovery ---------------------------------------------------------

async def discover_wallets(session: aiohttp.ClientSession) -> list[dict]:
    """
    Discover active wallets by scanning trades from high-volume resolved markets.
    Returns list of {address, n_trades, total_volume, markets_seen}.
    """
    print("Discovering wallets from high-volume resolved markets...")

    # Step 1: Get resolved markets sorted by volume
    resolved_markets = []
    offset = 0
    batch = 50

    while len(resolved_markets) < MARKETS_TO_SCAN:
        async with session.get(f"{GAMMA_API}/markets", params={
            "limit": batch,
            "offset": offset,
            "closed": "true",
            "active": "false",
            "order": "volume",
            "ascending": "false",
        }) as resp:
            if resp.status != 200:
                print(f"  Gamma API returned {resp.status}")
                break
            markets = await resp.json()

        if not markets:
            break

        for m in markets:
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
                is_resolved = any(float(p) >= 0.99 for p in prices)
            except (json.JSONDecodeError, ValueError):
                is_resolved = False

            if is_resolved and float(m.get("volume", 0)) > 1000:
                resolved_markets.append(m)

        offset += batch
        await asyncio.sleep(RATE_LIMIT)
        print(f"  Scanned {offset} markets, found {len(resolved_markets)} resolved")

        if len(markets) < batch:
            break

    print(f"Found {len(resolved_markets)} resolved markets with volume > $1k")

    # Step 2: Scan trades from these markets to find active wallets
    wallet_stats = defaultdict(lambda: {
        "n_trades": 0,
        "total_volume": 0.0,
        "markets_seen": set(),
    })

    for i, m in enumerate(resolved_markets[:MARKETS_TO_SCAN]):
        cid = m.get("conditionId", "")
        if not cid:
            continue

        offset = 0
        while True:
            async with session.get(f"{DATA_API}/trades", params={
                "conditionId": cid,
                "limit": 500,
                "offset": offset,
            }) as resp:
                if resp.status != 200:
                    break
                trades = await resp.json()

            if not trades:
                break

            for t in trades:
                wallet = t.get("proxyWallet", "")
                if not wallet:
                    continue
                size = float(t.get("size", 0))
                price = float(t.get("price", 0))
                wallet_stats[wallet]["n_trades"] += 1
                wallet_stats[wallet]["total_volume"] += size * price
                wallet_stats[wallet]["markets_seen"].add(cid)

            if len(trades) < 500:
                break
            offset += len(trades)
            await asyncio.sleep(RATE_LIMIT)

        if (i + 1) % 10 == 0:
            print(f"  Scanned trades from {i+1}/{min(len(resolved_markets), MARKETS_TO_SCAN)} markets "
                  f"({len(wallet_stats)} unique wallets found)")
        await asyncio.sleep(RATE_LIMIT)

    # Convert to list
    wallets = []
    for addr, stats in wallet_stats.items():
        wallets.append({
            "address":      addr,
            "n_trades":     stats["n_trades"],
            "total_volume": stats["total_volume"],
            "markets_seen": len(stats["markets_seen"]),
        })

    print(f"\nTotal unique wallets discovered: {len(wallets)}")
    return wallets

# -- Filter -------------------------------------------------------------------

def passes_filter(wallet: dict) -> tuple[bool, str]:
    """Quick filter before expensive scoring."""
    if not wallet["address"] or not wallet["address"].startswith("0x"):
        return False, "invalid address"

    if wallet["markets_seen"] < MIN_MARKETS:
        return False, f"only {wallet['markets_seen']} markets (min {MIN_MARKETS})"

    if wallet["total_volume"] < MIN_VOLUME_USD:
        return False, f"only ${wallet['total_volume']:.0f} volume (min ${MIN_VOLUME_USD:.0f})"

    return True, ""

# -- Supabase Writer ----------------------------------------------------------

def save_wallet_to_db(sb, wallet: dict, score: WalletScore):
    """Upsert wallet and its scores into Supabase."""
    try:
        sb.table("wallets").upsert({
            "wallet_address":   score.wallet_address,
            "account_age_days": score.account_age_days,
            "last_active_at":   score.computed_at,
            "is_sharp":         score.tier in ("tier_1_sharp", "tier_2_sharp"),
            "is_monitored":     score.tier == "tier_1_sharp",
        }).execute()

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
                "wallet_address":      score.wallet_address,
                "category":            cat.category,
                "n_markets":           cat.n_markets,
                "n_wins":              cat.n_wins,
                "win_rate_bayesian":   cat.win_rate_bayesian,
                "avg_clv":             cat.avg_clv,
                "pnl_per_market":      cat.pnl_per_market,
                "calibration_score":   cat.calibration_score,
                "total_pnl":           cat.total_pnl,
            }).execute()

        return True
    except Exception as e:
        print(f"  DB save failed for {score.wallet_address[:10]}: {e}")
        return False

# -- Main Batch Runner --------------------------------------------------------

async def run_batch():
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")

    if not supabase_url or not supabase_key:
        print("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY")
        return

    sb = create_client(supabase_url, supabase_key)

    async with aiohttp.ClientSession() as session:
        client = PolymarketClient(session)
        scorer = WalletScorer(client)

        # 1. Discover wallets from resolved markets
        discovered = await discover_wallets(session)

        # 2. Apply filter
        candidates = []
        rejected = 0
        for w in discovered:
            passes, reason = passes_filter(w)
            if passes:
                candidates.append(w)
            else:
                rejected += 1

        # Sort by number of markets seen (most active first)
        candidates.sort(key=lambda w: w["markets_seen"], reverse=True)
        candidates = candidates[:MAX_WALLETS]

        print(f"\n{len(candidates)} candidates after filter ({rejected} rejected)")
        print("-" * 50)

        # 3. Score each candidate
        results = []
        for i, wallet in enumerate(candidates):
            addr = wallet["address"]

            print(f"\n[{i+1}/{len(candidates)}] Scoring {addr[:12]}... "
                  f"({wallet['markets_seen']} markets, ${wallet['total_volume']:,.0f} vol)")

            try:
                score = await scorer.score_wallet(addr)

                if score.total_markets == 0:
                    print(f"  No resolved data, skipping")
                    continue

                print_report(score)
                save_wallet_to_db(sb, wallet, score)
                results.append((wallet, score))

            except Exception as e:
                print(f"  Failed to score {addr[:12]}: {e}")
                continue

            await asyncio.sleep(2)

        # 4. Summary
        print("\n" + "=" * 60)
        print("  BATCH COMPLETE -- RESULTS SUMMARY")
        print("=" * 60)

        tier1 = [(w, s) for w, s in results if s.tier == "tier_1_sharp"]
        tier2 = [(w, s) for w, s in results if s.tier == "tier_2_sharp"]

        print(f"  Tier 1 Sharps : {len(tier1)}")
        print(f"  Tier 2 Sharps : {len(tier2)}")
        print(f"  Total Scored  : {len(results)}")
        print()

        if results:
            print("  TOP 10 BY COMPOSITE SCORE:")
            top10 = sorted(results, key=lambda x: x[1].composite_score, reverse=True)[:10]
            for rank, (w, s) in enumerate(top10, 1):
                print(f"  {rank:2}. {s.wallet_address[:14]}...  {s.composite_score:5.1f}/100  "
                      f"{s.tier:<20} {s.total_markets:,} markets")

        print("=" * 60)
        print("All results saved to Supabase")

if __name__ == "__main__":
    asyncio.run(run_batch())
