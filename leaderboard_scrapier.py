“””
SharpWallet — Leaderboard Scraper
Pulls top wallets from Polymarket leaderboard, filters out noise,
and runs the full scoring pipeline on each candidate.
“””

import asyncio
import aiohttp
import os
from datetime import datetime, timezone
from supabase import create_client
from wallet_analyzer import PolymarketClient, WalletScorer, WalletScore, print_report

# ── Configuration ─────────────────────────────────────────────

LEADERBOARD_URL  = “https://data-api.polymarket.com/leaderboard”
GAMMA_PROFILES   = “https://gamma-api.polymarket.com/profiles”

MIN_MARKETS      = int(os.environ.get(“MIN_MARKETS”, 30))
MIN_ACCOUNT_DAYS = int(os.environ.get(“MIN_ACCOUNT_DAYS”, 60))
MAX_WALLETS      = int(os.environ.get(“MAX_WALLETS”, 200))

# ── Leaderboard Fetcher ───────────────────────────────────────

async def fetch_leaderboard(session: aiohttp.ClientSession) -> list[dict]:
“””
Pull top wallets from Polymarket’s leaderboard.
Returns list of {address, username, profit, volume, n_markets}
“””
print(“📋 Fetching Polymarket leaderboard…”)
wallets = []

```
# Fetch both all-time and monthly to get a broad seed list
for timeframe in ["all", "monthly"]:
    try:
        async with session.get(
            LEADERBOARD_URL,
            params={"limit": 100, "offset": 0, "window": timeframe}
        ) as resp:
            if resp.status != 200:
                print(f"  ⚠️  Leaderboard {timeframe} returned {resp.status}")
                continue
            data = await resp.json()
            entries = data if isinstance(data, list) else data.get("data", [])
            
            for entry in entries:
                wallets.append({
                    "address":  entry.get("proxy_wallet_address", "").lower(),
                    "username": entry.get("name", ""),
                    "profit":   float(entry.get("profit", 0)),
                    "volume":   float(entry.get("volume", 0)),
                    "n_markets": int(entry.get("numTrades", 0)),
                    "timeframe": timeframe,
                })
                
            print(f"  ✅ {timeframe}: {len(entries)} wallets fetched")
            await asyncio.sleep(0.5)
            
    except Exception as e:
        print(f"  ❌ Error fetching {timeframe} leaderboard: {e}")

# Deduplicate by address
seen = set()
unique = []
for w in wallets:
    if w["address"] and w["address"] not in seen:
        seen.add(w["address"])
        unique.append(w)

print(f"📋 Total unique wallets from leaderboard: {len(unique)}")
return unique
```

async def fetch_wallet_profile(
session: aiohttp.ClientSession,
address: str
) -> dict:
“”“Get additional profile info: join date, total markets.”””
try:
async with session.get(
f”{GAMMA_PROFILES}”,
params={“address”: address}
) as resp:
if resp.status == 200:
data = await resp.json()
profiles = data if isinstance(data, list) else [data]
if profiles:
return profiles[0]
except Exception:
pass
return {}

# ── Filter ────────────────────────────────────────────────────

def passes_coarse_filter(wallet: dict) -> tuple[bool, str]:
“””
Quick filter before expensive scoring.
Returns (passes, reason_if_rejected)
“””
# Must have a valid address
if not wallet[“address”] or not wallet[“address”].startswith(“0x”):
return False, “invalid address”

```
# Must have meaningful market count
if wallet["n_markets"] < MIN_MARKETS:
    return False, f"only {wallet['n_markets']} markets (min {MIN_MARKETS})"

# Reject obvious whale-gamblers: huge profit but tiny market count
# e.g. majorexploiter: $3.6M profit, 3 markets
if wallet["profit"] > 500_000 and wallet["n_markets"] < 20:
    return False, f"whale gambler pattern: ${wallet['profit']:,.0f} profit, {wallet['n_markets']} markets"

return True, ""
```

# ── Supabase Writer ───────────────────────────────────────────

def save_wallet_to_db(sb, wallet: dict, score: WalletScore):
“”“Upsert wallet and its scores into Supabase.”””
try:
# Upsert wallet record
sb.table(“wallets”).upsert({
“wallet_address”: score.wallet_address,
“username”:       wallet.get(“username”, “”),
“account_age_days”: score.account_age_days,
“last_active_at”: score.computed_at,
“is_sharp”:       score.tier in (“tier_1_sharp”, “tier_2_sharp”),
“is_monitored”:   score.tier == “tier_1_sharp”,
}).execute()

```
    # Upsert composite score
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
    print(f"  ❌ DB save failed for {score.wallet_address[:10]}: {e}")
    return False
```

# ── Main Batch Runner ─────────────────────────────────────────

async def run_batch():
supabase_url = os.environ.get(“SUPABASE_URL”)
supabase_key = os.environ.get(“SUPABASE_SERVICE_KEY”)

```
if not supabase_url or not supabase_key:
    print("❌ Missing SUPABASE_URL or SUPABASE_SERVICE_KEY")
    return

sb = create_client(supabase_url, supabase_key)

async with aiohttp.ClientSession() as session:
    client = PolymarketClient(session)
    scorer = WalletScorer(client)

    # 1. Fetch leaderboard
    leaderboard = await fetch_leaderboard(session)

    # 2. Apply coarse filter
    candidates = []
    rejected = 0
    for w in leaderboard[:MAX_WALLETS]:
        passes, reason = passes_coarse_filter(w)
        if passes:
            candidates.append(w)
        else:
            rejected += 1
            print(f"  ⛔ {w.get('username') or w['address'][:10]}: {reason}")

    print(f"\n✅ {len(candidates)} candidates after filter ({rejected} rejected)")
    print("─" * 50)

    # 3. Score each candidate
    results = []
    for i, wallet in enumerate(candidates):
        addr = wallet["address"]
        name = wallet.get("username") or addr[:10]
        
        print(f"\n[{i+1}/{len(candidates)}] Scoring {name}...")
        
        try:
            score = await scorer.score_wallet(addr)
            
            if score.total_markets == 0:
                print(f"  ⚠️  No data returned, skipping")
                continue

            print_report(score)
            save_wallet_to_db(sb, wallet, score)
            results.append((wallet, score))

        except Exception as e:
            print(f"  ❌ Failed to score {name}: {e}")
            continue

        # Brief pause between wallets to avoid hammering the API
        await asyncio.sleep(2)

    # 4. Summary
    print("\n" + "═" * 60)
    print("  BATCH COMPLETE — RESULTS SUMMARY")
    print("═" * 60)
    
    tier1 = [(w, s) for w, s in results if s.tier == "tier_1_sharp"]
    tier2 = [(w, s) for w, s in results if s.tier == "tier_2_sharp"]
    
    print(f"  Tier 1 Sharps : {len(tier1)}")
    print(f"  Tier 2 Sharps : {len(tier2)}")
    print(f"  Total Scored  : {len(results)}")
    print()
    
    print("  TOP 10 BY COMPOSITE SCORE:")
    top10 = sorted(results, key=lambda x: x[1].composite_score, reverse=True)[:10]
    for rank, (w, s) in enumerate(top10, 1):
        name = w.get("username") or s.wallet_address[:10]
        print(f"  {rank:2}. {name:<25} {s.composite_score:5.1f}/100  "
              f"{s.tier:<20} {s.total_markets:,} markets")
    
    print("═" * 60)
    print(f"✅ All results saved to Supabase")
```

if **name** == “**main**”:
asyncio.run(run_batch())
