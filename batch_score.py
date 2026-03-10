"""
Batch wallet scorer — discover active traders from Polymarket data API
and score them using wallet_analyzer.py logic.

Usage:
    python batch_score.py              # discover + score top 30 traders
    python batch_score.py --limit 50   # score top 50
"""
import os
import sys
import time
import json
from pathlib import Path
from collections import defaultdict

# Ensure imports work
sys.path.insert(0, str(Path(__file__).parent))
from wallet_analyzer import (
    fetch_user_trades, fetch_user_positions, categorize_market,
    compute_clv, compute_calibration, assign_tier, save_to_supabase,
    get_supabase, SUPABASE_URL, SUPABASE_KEY,
)

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


def fetch_polymarket_username(address):
    """Fetch the Polymarket display name for a wallet address."""
    try:
        r = requests.get(
            f"{GAMMA_API}/public-profile",
            params={"address": address},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            name = data.get("name") or data.get("pseudonym")
            return name if name else None
    except Exception:
        pass
    return None


def discover_active_traders(min_trades=5, pages=8):
    """Pull recent trades from data-api to find active wallets."""
    trader_stats = defaultdict(lambda: {"trades": 0, "volume": 0.0, "markets": set()})

    for offset in range(0, pages * 500, 500):
        try:
            r = requests.get(
                f"{DATA_API}/trades",
                params={"limit": 500, "offset": offset},
                timeout=15,
            )
            if not r.ok:
                break
            trades = r.json()
            if not trades:
                break
            for t in trades:
                addr = (t.get("proxyWallet") or "").lower()
                if not addr or not addr.startswith("0x"):
                    continue
                trader_stats[addr]["trades"] += 1
                trader_stats[addr]["volume"] += float(t.get("size", 0)) * float(t.get("price", 0))
                trader_stats[addr]["markets"].add(t.get("conditionId", ""))
            print(f"  Page {offset // 500 + 1}: {len(trades)} trades, {len(trader_stats)} wallets")
            time.sleep(0.3)
        except Exception as e:
            print(f"  Page error: {e}")
            break

    # Convert sets to counts and filter
    results = []
    for addr, s in trader_stats.items():
        if s["trades"] >= min_trades:
            results.append({
                "address": addr,
                "trades": s["trades"],
                "volume": s["volume"],
                "markets": len(s["markets"]),
            })
    results.sort(key=lambda x: x["trades"], reverse=True)
    return results


def quick_score(address):
    """Score a wallet — lighter version of analyze_wallet for batch use."""
    trades = fetch_user_trades(address, limit=2000)
    if not trades or len(trades) < 3:
        return None

    positions = fetch_user_positions(address)
    position_pnl = {}
    for p in positions:
        cid = p.get("conditionId", "")
        position_pnl[cid] = {
            "cashPnl": float(p.get("cashPnl", 0) or 0),
            "percentPnl": float(p.get("percentPnl", 0) or 0),
            "curPrice": float(p.get("curPrice", 0) or 0),
            "avgPrice": float(p.get("avgPrice", 0) or 0),
            "initialValue": float(p.get("initialValue", 0) or 0),
        }

    bets = []
    by_cat = defaultdict(list)
    for t in trades:
        price = float(t.get("price", 0) or 0)
        size = float(t.get("size", 0) or 0)
        side = t.get("side", "BUY")
        title = t.get("title", "")
        condition_id = t.get("conditionId", "")
        category = categorize_market(title)
        pos = position_pnl.get(condition_id, {})
        closing_price = pos.get("curPrice") if pos else None
        won = None
        if pos and pos.get("percentPnl", 0) != 0:
            won = pos["cashPnl"] > 0

        # Impute closing_price for resolved bets (binary markets: 0 or 1)
        if closing_price is None and won is not None:
            if side == "BUY":
                closing_price = 1.0 if won else 0.0
            else:
                closing_price = 0.0 if won else 1.0

        clv = compute_clv(price, closing_price, side) if closing_price is not None else None
        amount_usd = round(price * size, 2)

        bet = {
            "address": address,
            "market_slug": t.get("slug", t.get("eventSlug", ""))[:200],
            "market_title": title[:500],
            "category": category,
            "outcome": t.get("outcome", "Yes"),
            "side": side,
            "price": price,
            "size": size,
            "amount_usd": amount_usd,
            "timestamp": t.get("timestamp"),
            "resolved": closing_price is not None and closing_price in (0, 1),
            "won": won,
            "closing_price": closing_price,
            "clv": clv,
        }
        bets.append(bet)
        by_cat[category].append(bet)

    resolved = [b for b in bets if b["resolved"] and b["won"] is not None]
    wins = sum(1 for b in resolved if b["won"])
    win_rate = wins / max(len(resolved), 1) if resolved else 0
    clvs = [b["clv"] for b in bets if b["clv"] is not None]
    avg_clv = sum(clvs) / max(len(clvs), 1) if clvs else 0
    total_wagered = sum(b["amount_usd"] for b in bets if b["amount_usd"])
    total_pnl_val = sum(
        (1 - b["price"]) * b["size"] if b["won"] else -b["price"] * b["size"]
        for b in resolved if b["won"] is not None
    )
    roi = total_pnl_val / max(total_wagered, 1) if total_wagered else 0

    # Position-level PnL override
    total_pos_pnl = sum(p.get("cashPnl", 0) for p in position_pnl.values())
    total_pos_value = sum(abs(p.get("initialValue", 0)) for p in position_pnl.values())
    if total_pos_value > 0:
        roi = total_pos_pnl / total_pos_value

    cal_data = [(b["price"], b["won"]) for b in resolved if b["won"] is not None and b["price"] > 0]
    calibration = compute_calibration(cal_data)
    avg_edge = avg_clv * 0.7 + (win_rate - 0.5) * 0.3
    if calibration is not None:
        sharpe = roi / max(0.01, calibration) if calibration > 0 else 0
        kelly = max(0, (win_rate * (1 + avg_clv) - 1) / max(avg_clv, 0.01))
    else:
        sharpe = None
        kelly = None
    tier = assign_tier(avg_clv, win_rate, len(bets))

    if roi > 0.05 and total_wagered > 100000:
        tier = "elite"
    elif roi > 0.02 and total_wagered > 100000:
        tier = "sharp"

    cat_scores = {}
    for cat, cat_bets in by_cat.items():
        cat_resolved = [b for b in cat_bets if b["resolved"] and b["won"] is not None]
        cat_wins = sum(1 for b in cat_resolved if b["won"])
        cat_wr = cat_wins / max(len(cat_resolved), 1) if cat_resolved else 0
        cat_clvs = [b["clv"] for b in cat_bets if b["clv"] is not None]
        cat_avg_clv = sum(cat_clvs) / max(len(cat_clvs), 1) if cat_clvs else 0
        cat_wagered = sum(b["amount_usd"] for b in cat_bets)
        cat_pnl = sum(
            (1 - b["price"]) * b["size"] if b["won"] else -b["price"] * b["size"]
            for b in cat_resolved if b["won"] is not None
        )
        cat_roi = cat_pnl / max(cat_wagered, 1)
        cat_scores[cat] = {
            "category": cat,
            "total_bets": len(cat_bets),
            "win_rate": round(cat_wr, 4),
            "clv": round(cat_avg_clv, 4),
            "roi": round(cat_roi, 4),
        }

    username = fetch_polymarket_username(address)

    return {
        "address": address,
        "username": username,
        "total_bets": len(bets),
        "total_volume": round(total_wagered, 2),
        "resolved_bets": len(resolved),
        "wins": wins,
        "win_rate": round(win_rate, 4),
        "clv": round(avg_clv, 4),
        "roi": round(roi, 4),
        "calibration": round(calibration, 4) if calibration is not None else None,
        "avg_edge": round(avg_edge, 4),
        "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
        "kelly_fraction": round(kelly, 4) if kelly is not None else None,
        "tier": tier,
        "categories": cat_scores,
        "top_markets": [],
        "open_positions": len(positions),
        "bets": bets,
    }


def main():
    limit = 30
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        limit = int(sys.argv[idx + 1])

    print("=" * 60)
    print(f"BATCH WALLET SCORER — targeting {limit} wallets")
    print("=" * 60)

    # Step 1: Discover traders
    print("\n[1] Discovering active traders from recent trades...")
    candidates = discover_active_traders(min_trades=5, pages=8)
    print(f"  Found {len(candidates)} candidates with 5+ trades")

    # Step 2: Score each one
    print(f"\n[2] Scoring top {limit} wallets...")
    scored = 0
    failed = 0

    for i, c in enumerate(candidates[:limit + 10]):  # extra buffer for failures
        if scored >= limit:
            break
        addr = c["address"]
        print(f"\n  [{scored + 1}/{limit}] {addr[:16]}... ({c['trades']} trades, ${c['volume']:.0f} vol)")

        try:
            report = quick_score(addr)
            if report and report["total_bets"] >= 3:
                save_to_supabase(report)
                print(f"    -> {report['tier'].upper()} | CLV={report['clv']:+.4f} | WR={report['win_rate']:.1%} | ROI={report['roi']:+.1%} | {report['total_bets']} bets")
                scored += 1
            else:
                print(f"    -> skipped (insufficient data)")
                failed += 1
        except Exception as e:
            print(f"    -> error: {e}")
            failed += 1

        time.sleep(0.5)  # rate limit

    print(f"\n{'=' * 60}")
    print(f"BATCH COMPLETE: {scored} scored, {failed} failed")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
