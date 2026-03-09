"""
Refresh Polymarket usernames and re-categorize bets for all tracked wallets.

Usage:
    python refresh_profiles.py          # refresh all wallets
    python refresh_profiles.py --top 10 # refresh top 10 only
"""
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from wallet_analyzer import get_supabase, categorize_market

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
            return data.get("name") or data.get("pseudonym")
    except Exception:
        pass
    return None


def refresh_usernames(limit=None):
    sb = get_supabase()

    # Fetch all wallets
    q = sb.table("wallets").select("address, label").order("updated_at", desc=True)
    if limit:
        q = q.limit(limit)
    result = q.execute()
    wallets = result.data or []

    print(f"Refreshing usernames for {len(wallets)} wallets...")
    updated = 0

    for w in wallets:
        addr = w["address"]
        old_label = w.get("label", "")

        username = fetch_polymarket_username(addr)
        if username and username != old_label:
            sb.table("wallets").update({"label": username}).eq("address", addr).execute()
            print(f"  {addr[:12]}... : {old_label!r} -> {username!r}")
            updated += 1
        else:
            status = f"kept {old_label!r}" if old_label else "no profile"
            print(f"  {addr[:12]}... : {status}")

        time.sleep(0.3)  # rate limit

    print(f"\nUpdated {updated}/{len(wallets)} wallet labels")


def recategorize_bets(limit=None):
    """Re-categorize all bets with the improved categorizer."""
    sb = get_supabase()

    # Fetch all bets
    q = sb.table("bets").select("id, market_title, category")
    if limit:
        q = q.limit(limit * 50)  # ~50 bets per wallet
    result = q.execute()
    bets = result.data or []

    print(f"\nRe-categorizing {len(bets)} bets...")
    changed = 0
    cat_counts = {}

    for b in bets:
        old_cat = b.get("category", "other")
        new_cat = categorize_market(b.get("market_title", ""))
        cat_counts[new_cat] = cat_counts.get(new_cat, 0) + 1

        if new_cat != old_cat:
            sb.table("bets").update({"category": new_cat}).eq("id", b["id"]).execute()
            changed += 1

    print(f"Changed {changed}/{len(bets)} bet categories")
    print("\nCategory distribution:")
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        pct = count / max(len(bets), 1) * 100
        print(f"  {cat:20s} {count:5d} ({pct:.1f}%)")


def recalc_category_scores():
    """Recalculate wallet_category_scores from bets table."""
    sb = get_supabase()

    # Get all addresses
    wallets = sb.table("wallets").select("address").execute().data or []

    print(f"\nRecalculating category scores for {len(wallets)} wallets...")

    for w in wallets:
        addr = w["address"]
        bets = sb.table("bets").select("*").eq("address", addr).execute().data or []

        # Group by category
        by_cat = {}
        for b in bets:
            cat = b.get("category", "other")
            if cat not in by_cat:
                by_cat[cat] = []
            by_cat[cat].append(b)

        for cat, cat_bets in by_cat.items():
            resolved = [b for b in cat_bets if b.get("resolved") and b.get("won") is not None]
            wins = sum(1 for b in resolved if b["won"])
            wr = wins / max(len(resolved), 1) if resolved else 0
            clvs = [b["clv"] for b in cat_bets if b.get("clv") is not None]
            avg_clv = sum(clvs) / max(len(clvs), 1) if clvs else 0
            total_wagered = sum(float(b.get("amount_usd", 0) or 0) for b in cat_bets)
            total_pnl = sum(
                (1 - float(b["price"])) * float(b["size"]) if b["won"] else -float(b["price"]) * float(b["size"])
                for b in resolved if b.get("won") is not None
            )
            roi = total_pnl / max(total_wagered, 1)

            row = {
                "address": addr,
                "category": cat,
                "total_bets": len(cat_bets),
                "win_rate": round(wr, 4),
                "clv": round(avg_clv, 4),
                "roi": round(roi, 4),
            }
            try:
                sb.table("wallet_category_scores").upsert(
                    row, on_conflict="address,category"
                ).execute()
            except Exception as e:
                print(f"  Warning: {addr[:10]} {cat}: {e}")

    print("  Done recalculating category scores")


if __name__ == "__main__":
    top = None
    if "--top" in sys.argv:
        idx = sys.argv.index("--top")
        top = int(sys.argv[idx + 1])

    refresh_usernames(limit=top)
    recategorize_bets(limit=top)
    recalc_category_scores()
    print("\nAll done!")
