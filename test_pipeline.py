"""
test_pipeline.py -- Verify Polymarket data pipeline
Pulls resolved markets, finds active wallets, and prints sample trade history.
Run: python test_pipeline.py
"""

import requests
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

RATE_PAUSE = 0.15  # seconds between requests


def fetch_resolved_markets(limit=100):
    """
    Pull recently resolved markets from Gamma API.
    A market is resolved when outcomePrices contains '1' and '0'.
    """
    print(f"Fetching up to {limit} resolved markets from Gamma API...")
    resolved = []
    offset = 0
    batch = 50

    while len(resolved) < limit:
        resp = requests.get(f"{GAMMA_API}/markets", params={
            "limit": batch,
            "offset": offset,
            "closed": "true",
            "active": "false",
            "order": "volume",
            "ascending": "false",
        })
        time.sleep(RATE_PAUSE)

        if resp.status_code != 200:
            print(f"  Gamma API returned {resp.status_code}, stopping.")
            break

        markets = resp.json()
        if not markets:
            break

        for m in markets:
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
                is_resolved = any(float(p) >= 0.99 for p in prices)
            except (json.JSONDecodeError, ValueError):
                is_resolved = False

            if is_resolved:
                resolved.append(m)

        offset += batch
        print(f"  Scanned {offset} markets, found {len(resolved)} resolved so far")

        if len(markets) < batch:
            break

    print(f"Total resolved markets found: {len(resolved)}")
    return resolved[:limit]


def fetch_trades_for_market(condition_id, limit=500):
    """
    Pull trades for a specific market (by conditionId) from the data API.
    """
    all_trades = []
    offset = 0

    while True:
        resp = requests.get(f"{DATA_API}/trades", params={
            "conditionId": condition_id,
            "limit": limit,
            "offset": offset,
        })
        time.sleep(RATE_PAUSE)

        if resp.status_code != 200:
            break

        trades = resp.json()
        if not trades:
            break

        all_trades.extend(trades)
        if len(trades) < limit:
            break
        offset += len(trades)

    return all_trades


def fetch_wallet_trades(wallet_address, limit=200):
    """
    Pull recent trades for a specific wallet.
    """
    resp = requests.get(f"{DATA_API}/trades", params={
        "user": wallet_address,
        "limit": limit,
    })
    time.sleep(RATE_PAUSE)

    if resp.status_code != 200:
        print(f"  Failed to fetch trades for {wallet_address}: {resp.status_code}")
        return []

    return resp.json()


def fetch_wallet_positions(wallet_address):
    """
    Pull all positions (with PnL) for a wallet.
    """
    resp = requests.get(f"{DATA_API}/positions", params={
        "user": wallet_address,
        "sizeThreshold": 0,
        "limit": 500,
    })
    time.sleep(RATE_PAUSE)

    if resp.status_code != 200:
        return []

    return resp.json()


def main():
    print("=" * 70)
    print("  POLYMARKET DATA PIPELINE TEST")
    print("=" * 70)

    # Step 1: Fetch resolved markets
    print("\n--- STEP 1: Fetch Resolved Markets ---")
    markets = fetch_resolved_markets(limit=50)

    if not markets:
        print("FAILED: No resolved markets found. API may be down.")
        return

    print(f"\nSample resolved markets:")
    for m in markets[:5]:
        outcomes = json.loads(m.get("outcomes", "[]"))
        prices = json.loads(m.get("outcomePrices", "[]"))
        winner_idx = next((i for i, p in enumerate(prices) if float(p) >= 0.99), None)
        winner = outcomes[winner_idx] if winner_idx is not None else "?"
        print(f"  [{m['id']}] {m['question'][:65]}")
        print(f"       Winner: {winner} | Volume: ${float(m.get('volume', 0)):,.0f} | Category: {m.get('category', '?')}")

    # Step 2: Find wallets that traded in these markets
    print("\n--- STEP 2: Find Active Wallets ---")
    wallet_trade_count = defaultdict(int)
    wallet_volume = defaultdict(float)

    # Sample trades from top 20 resolved markets by volume
    top_markets = sorted(markets, key=lambda m: float(m.get("volume", 0)), reverse=True)[:20]

    for i, m in enumerate(top_markets):
        cid = m.get("conditionId", "")
        if not cid:
            continue

        trades = fetch_trades_for_market(cid, limit=200)
        print(f"  Market {i+1}/20: {len(trades)} trades | {m['question'][:50]}...")

        for t in trades:
            wallet = t.get("proxyWallet", "")
            if wallet:
                wallet_trade_count[wallet] += 1
                wallet_volume[wallet] += float(t.get("size", 0)) * float(t.get("price", 0))

    print(f"\nUnique wallets found: {len(wallet_trade_count)}")

    # Rank by activity
    top_wallets = sorted(
        wallet_trade_count.items(),
        key=lambda x: x[1],
        reverse=True
    )[:20]

    print(f"\nTop 20 most active wallets across sampled markets:")
    print(f"  {'Wallet':<44} {'Trades':>7} {'Volume':>12}")
    print(f"  {'-'*44} {'-'*7} {'-'*12}")
    for wallet, count in top_wallets:
        vol = wallet_volume[wallet]
        print(f"  {wallet} {count:>7} ${vol:>10,.0f}")

    # Step 3: Deep dive on the most active wallet
    print("\n--- STEP 3: Sample Wallet Deep Dive ---")
    sample_wallet = top_wallets[0][0]
    print(f"Wallet: {sample_wallet}")

    # Get their full trade history
    print("\nRecent trades:")
    trades = fetch_wallet_trades(sample_wallet, limit=50)
    print(f"  Fetched {len(trades)} recent trades")

    if trades:
        # Group by market
        by_market = defaultdict(list)
        for t in trades:
            by_market[t.get("title", "Unknown")].append(t)

        print(f"  Across {len(by_market)} different markets")
        print()

        for title, mkt_trades in list(by_market.items())[:5]:
            print(f"  Market: {title[:65]}")
            for t in mkt_trades[:3]:
                ts = datetime.fromtimestamp(t["timestamp"], tz=timezone.utc)
                print(f"    {t['side']:4} {t['outcome']:5} @ ${float(t['price']):.3f} "
                      f"| Size: ${float(t['size']):,.2f} | {ts.strftime('%Y-%m-%d %H:%M')}")
            if len(mkt_trades) > 3:
                print(f"    ... and {len(mkt_trades) - 3} more trades")
            print()

    # Get their positions with PnL
    print("Positions with PnL:")
    positions = fetch_wallet_positions(sample_wallet)
    print(f"  Fetched {len(positions)} positions")

    if positions:
        # Show resolved positions (where redeemable or cashPnl != 0)
        resolved_pos = [p for p in positions if p.get("redeemable") or abs(float(p.get("cashPnl", 0))) > 0.01]
        print(f"  Resolved/settled positions: {len(resolved_pos)}")

        total_pnl = sum(float(p.get("cashPnl", 0)) for p in positions)
        total_invested = sum(float(p.get("initialValue", 0)) for p in positions)
        print(f"  Total PnL: ${total_pnl:,.2f}")
        print(f"  Total invested: ${total_invested:,.2f}")
        print(f"  ROI: {(total_pnl / total_invested * 100) if total_invested else 0:.1f}%")
        print()

        # Show top 5 positions by PnL
        sorted_pos = sorted(positions, key=lambda p: abs(float(p.get("cashPnl", 0))), reverse=True)
        print("  Top positions by PnL:")
        for p in sorted_pos[:5]:
            pnl = float(p.get("cashPnl", 0))
            print(f"    {'+' if pnl >= 0 else ''}{pnl:>8,.2f} | {p['outcome']:5} @ avg ${float(p.get('avgPrice',0)):.3f} "
                  f"| {p.get('title', '?')[:50]}")

    # Step 4: Data structure validation
    print("\n--- STEP 4: Data Structure Validation ---")
    checks = {
        "Resolved markets fetchable": len(markets) > 0,
        "Markets have conditionId": all(m.get("conditionId") for m in markets[:10]),
        "Markets have outcomePrices": all(m.get("outcomePrices") for m in markets[:10]),
        "Markets have category": all(m.get("category") for m in markets[:10]),
        "Trades have proxyWallet": all(t.get("proxyWallet") for t in trades[:10]) if trades else False,
        "Trades have side/price/size": all(
            t.get("side") and t.get("price") and t.get("size")
            for t in trades[:10]
        ) if trades else False,
        "Trades have conditionId": all(t.get("conditionId") for t in trades[:10]) if trades else False,
        "Positions have PnL fields": all(
            "cashPnl" in p and "avgPrice" in p and "initialValue" in p
            for p in positions[:10]
        ) if positions else False,
        "Positions have outcome info": all(
            "outcome" in p and "title" in p
            for p in positions[:10]
        ) if positions else False,
    }

    all_pass = True
    for check, result in checks.items():
        status = "PASS" if result else "FAIL"
        if not result:
            all_pass = False
        print(f"  [{status}] {check}")

    print()
    if all_pass:
        print("ALL CHECKS PASSED - Data pipeline is working correctly.")
        print("The data-api provides everything needed for wallet scoring:")
        print("  - Resolved markets with winner info (via outcomePrices)")
        print("  - Trade history per wallet (side, price, size, timestamp)")
        print("  - Pre-computed PnL per position (cashPnl, realizedPnl)")
        print("  - Market metadata (title, category, outcomes)")
    else:
        print("SOME CHECKS FAILED - Review the output above.")

    print()
    print("KEY FINDING: The CLOB API requires authentication.")
    print("Use data-api.polymarket.com (public) instead for trades and positions.")
    print("Use gamma-api.polymarket.com (public) for market metadata.")
    print("=" * 70)


if __name__ == "__main__":
    main()
