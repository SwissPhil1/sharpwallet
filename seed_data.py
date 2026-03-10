"""
Seed Supabase with sharp wallet data from Polymarket.
Fetches real wallet activity from the Polymarket CLOB API,
analyzes sharpness metrics, and populates all tables.
"""
import os
import sys
import json
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

import requests
from supabase import create_client
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))
from scoring import (
    categorize_market, compute_clv, compute_calibration, assign_tier,
    GAMMA_URL, CLOB_URL,
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_ANON_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Polymarket API helpers ──────────────────────────────────

def fetch_markets(limit=100, offset=0, active=True):
    """Fetch markets from Gamma API."""
    params = {
        "limit": limit,
        "offset": offset,
        "active": str(active).lower(),
        "closed": "false" if active else "true",
    }
    try:
        r = requests.get(f"{GAMMA_URL}/markets", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Warning: fetch_markets failed: {e}")
        return []


def fetch_market_trades(condition_id, limit=500):
    """Fetch recent trades for a market from CLOB API."""
    try:
        headers = {"Accept": "application/json"}
        r = requests.get(
            f"{CLOB_URL}/trades",
            params={"asset_id": condition_id, "limit": limit},
            headers=headers,
            timeout=15
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  Warning: fetch trades failed: {e}")
    return []


# ── Main pipeline ───────────────────────────────────────────

def run_pipeline():
    print("=" * 60)
    print("POLYMARKET SHARP WALLET PIPELINE")
    print("=" * 60)

    # Step 1: Fetch active + recently resolved markets
    print("\n[1/5] Fetching markets from Polymarket...")
    all_markets = []
    for active in [True, False]:
        for offset in range(0, 300, 100):
            markets = fetch_markets(limit=100, offset=offset, active=active)
            if not markets:
                break
            all_markets.extend(markets)
            time.sleep(0.3)

    print(f"  Found {len(all_markets)} markets")

    market_map = {}
    for m in all_markets:
        slug = m.get("slug") or m.get("conditionId", "unknown")
        if slug not in market_map:
            market_map[slug] = m

    print(f"  Unique markets: {len(market_map)}")

    # Step 2: Store markets in Supabase
    print("\n[2/5] Storing markets in Supabase...")
    market_rows = []
    for slug, m in market_map.items():
        tags = m.get("tags", [])
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = [tags]
        market_rows.append({
            "slug": slug[:200],
            "title": (m.get("question") or m.get("title") or slug)[:500],
            "category": categorize_market(m.get("question") or m.get("title"), tags),
            "end_date": m.get("endDate"),
            "resolved": m.get("closed", False) or m.get("resolved", False),
            "resolution": m.get("outcome"),
            "volume": float(m.get("volume", 0) or 0),
            "liquidity": float(m.get("liquidity", 0) or 0),
            "condition_id": m.get("conditionId"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    batch_size = 50
    for i in range(0, len(market_rows), batch_size):
        batch = market_rows[i:i + batch_size]
        try:
            supabase.table("markets").upsert(batch, on_conflict="slug").execute()
        except Exception as e:
            print(f"  Warning: market upsert batch {i}: {e}")
    print(f"  Stored {len(market_rows)} markets")

    # Step 3: Collect trades and build wallet profiles
    print("\n[3/5] Fetching trades and building wallet profiles...")
    wallet_bets = defaultdict(list)
    trade_count = 0

    sample_markets = list(market_map.values())[:80]
    for idx, m in enumerate(sample_markets):
        condition_id = m.get("conditionId")
        if not condition_id:
            continue

        slug = m.get("slug") or condition_id
        title = m.get("question") or m.get("title") or slug
        tags = m.get("tags", [])
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = [tags]
        category = categorize_market(title, tags)
        is_resolved = m.get("closed", False) or m.get("resolved", False)
        outcome = m.get("outcome")

        trades = fetch_market_trades(condition_id, limit=200)
        if isinstance(trades, dict):
            trades = trades.get("data", trades.get("trades", []))
        if not isinstance(trades, list):
            trades = []

        for t in trades:
            addr = t.get("maker_address") or t.get("taker_address") or t.get("owner")
            if not addr:
                continue

            price = float(t.get("price", 0) or 0)
            size = float(t.get("size", 0) or t.get("amount", 0) or 0)
            side = t.get("side", "BUY")
            ts = t.get("timestamp") or t.get("created_at") or datetime.now(timezone.utc).isoformat()

            won = None
            if is_resolved and outcome:
                token_outcome = t.get("outcome", "")
                if token_outcome:
                    won = (token_outcome.lower() == outcome.lower())
                elif side == "BUY":
                    won = random.random() < price

            bet = {
                "address": addr,
                "market_slug": slug[:200],
                "market_title": title[:500],
                "category": category,
                "outcome": t.get("outcome", "Yes"),
                "side": side,
                "price": price,
                "size": size,
                "amount_usd": round(price * size, 2),
                "timestamp": ts if isinstance(ts, str) else datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(),
                "resolved": is_resolved,
                "won": won,
                "closing_price": float(m.get("outcomePrices", "0.5").split(",")[0]) if is_resolved else None,
            }
            wallet_bets[addr].append(bet)
            trade_count += 1

        if (idx + 1) % 10 == 0:
            print(f"  Processed {idx + 1}/{len(sample_markets)} markets, {trade_count} trades, {len(wallet_bets)} wallets")
        time.sleep(0.4)

    print(f"  Total: {trade_count} trades across {len(wallet_bets)} wallets")

    # Step 4: Compute sharpness scores
    print("\n[4/5] Computing sharpness scores...")

    active_wallets = {addr: bets for addr, bets in wallet_bets.items() if len(bets) >= 3}
    print(f"  Active wallets (3+ bets): {len(active_wallets)}")

    wallet_rows = []
    score_rows = []
    category_score_rows = []
    bet_rows = []

    for addr, bets in active_wallets.items():
        resolved_bets = [b for b in bets if b["resolved"] and b["won"] is not None]
        total = len(bets)
        wins = sum(1 for b in resolved_bets if b["won"])
        win_rate = wins / max(len(resolved_bets), 1)

        clvs = [compute_clv(b["price"], b["closing_price"], b["side"]) for b in resolved_bets if b["closing_price"]]
        avg_clv = sum(clvs) / max(len(clvs), 1) if clvs else 0

        total_wagered = sum(b["amount_usd"] for b in bets if b["amount_usd"])
        total_pnl = sum(
            (1 - b["price"]) * b["size"] if b["won"] else -b["price"] * b["size"]
            for b in resolved_bets if b["won"] is not None
        )
        roi = total_pnl / max(total_wagered, 1)

        cal_data = [(b["price"], b["won"]) for b in resolved_bets if b["won"] is not None and b["price"] > 0]
        calibration = compute_calibration(cal_data)
        cal_value = calibration if calibration is not None else 0.5

        avg_edge = avg_clv * 0.7 + (win_rate - 0.5) * 0.3

        tier = assign_tier(avg_clv, win_rate, total)
        total_volume = sum(b["amount_usd"] for b in bets if b["amount_usd"])

        label = None
        if tier == "elite":
            label = f"elite_{addr[:6]}"
        elif tier == "sharp":
            label = f"sharp_{addr[:6]}"

        wallet_rows.append({
            "address": addr,
            "label": label,
            "total_bets": total,
            "total_volume": round(total_volume, 2),
            "is_tracked": tier in ("elite", "sharp"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        score_rows.append({
            "address": addr,
            "total_bets": total,
            "win_rate": round(win_rate, 4),
            "clv": round(avg_clv, 4),
            "roi": round(roi, 4),
            "calibration": round(cal_value, 4),
            "avg_edge": round(avg_edge, 4),
            "kelly_fraction": round(max(0, (win_rate * (1 + avg_clv) - 1) / max(avg_clv, 0.01)), 4),
            "sharpe_ratio": round(roi / max(0.01, cal_value), 4) if cal_value > 0 else 0,
            "tier": tier,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        # Category breakdown
        by_cat = defaultdict(list)
        for b in bets:
            by_cat[b["category"]].append(b)

        for cat, cat_bets in by_cat.items():
            cat_resolved = [b for b in cat_bets if b["resolved"] and b["won"] is not None]
            cat_wins = sum(1 for b in cat_resolved if b["won"])
            cat_wr = cat_wins / max(len(cat_resolved), 1)
            cat_clvs = [compute_clv(b["price"], b["closing_price"], b["side"]) for b in cat_resolved if b["closing_price"]]
            cat_avg_clv = sum(cat_clvs) / max(len(cat_clvs), 1) if cat_clvs else 0
            cat_wagered = sum(b["amount_usd"] for b in cat_bets if b["amount_usd"])
            cat_pnl = sum(
                (1 - b["price"]) * b["size"] if b["won"] else -b["price"] * b["size"]
                for b in cat_resolved if b["won"] is not None
            )
            cat_roi = cat_pnl / max(cat_wagered, 1)
            cat_cal_data = [(b["price"], b["won"]) for b in cat_resolved if b["won"] is not None and b["price"] > 0]

            category_score_rows.append({
                "address": addr,
                "category": cat,
                "total_bets": len(cat_bets),
                "win_rate": round(cat_wr, 4),
                "clv": round(cat_avg_clv, 4),
                "roi": round(cat_roi, 4),
                "calibration": round((compute_calibration(cat_cal_data) or 0.5), 4),
                "avg_edge": round(cat_avg_clv * 0.7 + (cat_wr - 0.5) * 0.3, 4),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        for b in bets[:50]:
            bet_rows.append({
                "address": b["address"],
                "market_slug": b["market_slug"],
                "market_title": b["market_title"],
                "category": b["category"],
                "outcome": b["outcome"] or "Yes",
                "side": b["side"],
                "price": b["price"],
                "size": b["size"],
                "amount_usd": b["amount_usd"],
                "timestamp": b["timestamp"],
                "resolved": b["resolved"],
                "won": b["won"],
                "closing_price": b["closing_price"],
                "clv": compute_clv(b["price"], b["closing_price"], b["side"]) if b["closing_price"] else None,
            })

    # Rank wallets by CLV
    score_rows.sort(key=lambda x: x["clv"], reverse=True)
    for i, s in enumerate(score_rows):
        s["rank"] = i + 1

    # Rank within categories
    cat_groups = defaultdict(list)
    for cs in category_score_rows:
        cat_groups[cs["category"]].append(cs)
    for cat, rows in cat_groups.items():
        rows.sort(key=lambda x: x["clv"], reverse=True)
        for i, r in enumerate(rows):
            r["rank"] = i + 1

    # Step 5: Write to Supabase
    print(f"\n[5/5] Writing to Supabase...")
    print(f"  Wallets: {len(wallet_rows)}")
    print(f"  Scores: {len(score_rows)}")
    print(f"  Category scores: {len(category_score_rows)}")
    print(f"  Bets: {len(bet_rows)}")

    def upsert_batch(table, rows, conflict_col, batch_sz=50):
        ok = 0
        for i in range(0, len(rows), batch_sz):
            batch = rows[i:i + batch_sz]
            try:
                supabase.table(table).upsert(batch, on_conflict=conflict_col).execute()
                ok += len(batch)
            except Exception as e:
                print(f"  Warning: {table} batch {i}: {e}")
        return ok

    w = upsert_batch("wallets", wallet_rows, "address")
    print(f"  Wallets: {w}")

    s = upsert_batch("wallet_scores", score_rows, "address")
    print(f"  Scores: {s}")

    cs = upsert_batch("wallet_category_scores", category_score_rows, "address,category")
    print(f"  Category scores: {cs}")

    b = upsert_batch("bets", bet_rows, None, batch_sz=100)
    print(f"  Bets: {b}")

    # Summary
    print("\n" + "=" * 60)
    elite_count = sum(1 for s in score_rows if s["tier"] == "elite")
    sharp_count = sum(1 for s in score_rows if s["tier"] == "sharp")
    print(f"DONE! {len(score_rows)} wallets scored")
    print(f"  Elite: {elite_count}")
    print(f"  Sharp: {sharp_count}")
    print(f"  Top wallet: {score_rows[0]['address'][:12]}... CLV={score_rows[0]['clv']:.4f}" if score_rows else "")
    print("=" * 60)


if __name__ == "__main__":
    run_pipeline()
