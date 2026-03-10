"""
Wallet Analyzer — analyze a specific Polymarket wallet by username or address.

Usage:
    python wallet_analyzer.py kch123              # by Polymarket username
    python wallet_analyzer.py 0xabc123...         # by wallet address
    python wallet_analyzer.py kch123 --save       # analyze + save to Supabase
"""
import os
import sys
import json
import time
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_ANON_KEY"]
GAMMA_URL = os.environ.get("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
CLOB_URL = os.environ.get("POLYMARKET_API_URL", "https://clob.polymarket.com")

# Lazy-init Supabase client (only if --save)
_supabase = None

def get_supabase():
    global _supabase
    if _supabase is None:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


# ── Polymarket API ──────────────────────────────────────────

def resolve_address(identifier):
    """Resolve a username or address to a wallet address + profile info."""
    # Already an address
    if identifier.startswith("0x") and len(identifier) >= 40:
        return identifier, {"username": None, "address": identifier}

    # Look up by username via Gamma API
    try:
        r = requests.get(f"{GAMMA_URL}/users", params={"username": identifier}, timeout=10)
        if r.ok:
            users = r.json()
            if isinstance(users, list) and users:
                u = users[0]
                return u.get("proxyWallet") or u.get("address") or u.get("id"), u
            elif isinstance(users, dict) and users.get("address"):
                return users.get("proxyWallet") or users["address"], users
    except Exception as e:
        print(f"  Warning: Gamma user lookup failed: {e}")

    # Try profile endpoint
    try:
        r = requests.get(f"{GAMMA_URL}/profiles/{identifier}", timeout=10)
        if r.ok:
            u = r.json()
            addr = u.get("proxyWallet") or u.get("address") or u.get("id")
            if addr:
                return addr, u
    except Exception:
        pass

    # Try CLOB API user lookup
    try:
        r = requests.get(f"{CLOB_URL}/profile/{identifier}", timeout=10)
        if r.ok:
            u = r.json()
            addr = u.get("proxyWallet") or u.get("address")
            if addr:
                return addr, u
    except Exception:
        pass

    # Try scraping the Polymarket profile page
    try:
        import re
        r = requests.get(f"https://polymarket.com/@{identifier}", timeout=10, allow_redirects=True)
        if r.ok:
            # Look for proxyWallet in page source
            match = re.search(r'"proxyWallet"\s*:\s*"(0x[a-fA-F0-9]{40})"', r.text)
            if match:
                addr = match.group(1)
                return addr, {"username": identifier, "address": addr}
            # Fallback: any 0x address
            addrs = re.findall(r'0x[a-fA-F0-9]{40}', r.text)
            if addrs:
                return addrs[0], {"username": identifier, "address": addrs[0]}
    except Exception:
        pass

    # Could be a partial address or label — return as-is
    return identifier, {"username": identifier, "address": identifier}


DATA_API = "https://data-api.polymarket.com"


def fetch_user_trades(address, limit=10000):
    """Fetch trades from Polymarket data API with pagination."""
    all_trades = []
    cursor = None
    max_pages = max(1, limit // 200)

    offset = 0
    for page in range(max_pages):
        params = {"user": address, "limit": 200, "offset": offset}
        try:
            r = requests.get(f"{DATA_API}/trades", params=params, timeout=15)
            if not r.ok:
                break
            data = r.json()
            if not isinstance(data, list) or not data:
                break
            all_trades.extend(data)
            offset += len(data)
            if len(data) < 200:
                break
        except Exception as e:
            print(f"  Warning: trade fetch page {page}: {e}")
            break
        time.sleep(0.3)

    # Deduplicate by transaction hash + asset
    seen = set()
    unique = []
    for t in all_trades:
        tid = t.get("transactionHash", "") + t.get("asset", "") + str(t.get("timestamp", ""))
        if tid not in seen:
            seen.add(tid)
            unique.append(t)

    return unique


def fetch_user_positions(address):
    """Fetch current open positions from data API."""
    try:
        r = requests.get(f"{DATA_API}/positions", params={"user": address, "sizeThreshold": 0}, timeout=15)
        if r.ok:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def fetch_user_stats(address):
    """Fetch aggregate stats from Polymarket profile."""
    stats = {}
    try:
        r = requests.get(f"https://polymarket.com/api/profile/userData?address={address}", timeout=10)
        if r.ok:
            stats["profile"] = r.json()
    except Exception:
        pass
    try:
        r = requests.get(f"{DATA_API}/positions", params={"user": address, "sizeThreshold": 0}, timeout=10)
        if r.ok:
            stats["positions"] = r.json()
    except Exception:
        pass
    return stats


def fetch_market_info(condition_id):
    """Get market metadata from Gamma API."""
    try:
        r = requests.get(f"{GAMMA_URL}/markets", params={"condition_id": condition_id}, timeout=10)
        if r.ok:
            markets = r.json()
            if isinstance(markets, list) and markets:
                return markets[0]
    except Exception:
        pass
    return None


def categorize_market(title, tags=None):
    """Categorize a market based on title and tags."""
    import re
    combined = ((title or "") + " " + " ".join(tags or [])).lower()

    # Politics / Geopolitics
    if any(w in combined for w in [
        "trump", "biden", "election", "congress", "president", "democrat", "republican",
        "vote", "senate", "governor", "political", "gop", "dnc", "rnc", "kamala",
        "desantis", "vance", "obama", "pelosi", "mcconnell", "newsom", "gavin",
        "midterm", "primary", "ballot", "electoral", "impeach", "scotus",
        "supreme court", "legislation", "bill pass", "executive order",
        "cabinet", "secretary of", "attorney general", "speaker of the house",
        "government shutdown", "debt ceiling", "tariff", "sanctions",
        "nato", "ukraine", "russia", "china", "iran", "israel", "gaza", "war",
        "ceasefire", "invasion", "missile", "nuclear", "geopolit", "diplomacy",
        "un resolution", "peace deal", "military", "troops", "border",
        "immigration", "refugee", "asylum", "caravan",
        "approval rating", "poll", "favorability",
    ]):
        return "politics"

    # Crypto / Web3
    if any(w in combined for w in [
        "bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol",
        "token", "defi", "nft", "blockchain", "altcoin", "memecoin", "meme coin",
        "dogecoin", "doge", "shiba", "pepe", "xrp", "ripple", "cardano", "ada",
        "polkadot", "avalanche avax", "matic", "polygon", "binance", "bnb",
        "coinbase", "sec crypto", "etf bitcoin", "bitcoin etf", "eth etf",
        "halving", "mining", "staking", "airdrop", "layer 2", "rollup",
        "uniswap", "opensea", "web3", "dao ", "decentralized",
        "$btc", "$eth", "$sol", "market cap crypto",
    ]):
        return "crypto"

    # Sports
    if any(w in combined for w in [
        "nfl", "nba", "mlb", "nhl", "mls", "epl", "premier league", "la liga",
        "bundesliga", "serie a", "ligue 1", "champions league", "uefa",
        "soccer", "football", "basketball", "baseball", "hockey", "tennis",
        "ufc", "mma", "boxing", "golf", "pga", "f1", "formula 1", "nascar",
        "cricket", "ipl", "rugby", "olympics", "olympic", "world cup",
        "super bowl", "championship", "playoffs", "finals", "semifinal",
        "spread:", "moneyline", "over/under", "point spread", "total points",
        "win the ", "wins the ", "make the playoffs", "win the series",
        "mvp", "rookie of the year", "scoring leader", "batting average",
        "touchdown", "home run", "goal scorer", "grand slam",
        "world series", "stanley cup", "march madness", "ncaa",
    ]):
        return "sports"
    # "X vs Y" or "X vs. Y" pattern (common in sports markets)
    if re.search(r'\b\w+\s+vs\.?\s+\w+\b', combined):
        return "sports"
    # Team names
    if any(w in combined for w in [
        "oilers", "knights", "celtics", "cavaliers", "lakers", "warriors",
        "bears", "rams", "seahawks", "chiefs", "eagles", "49ers", "packers",
        "cowboys", "steelers", "ravens", "bills", "dolphins", "patriots",
        "hurricanes", "flames", "bruins", "penguins", "lightning", "sabres",
        "capitals", "devils", "islanders", "sharks", "blackhawks", "stars",
        "wild", "blues", "ducks", "pelicans", "hawks", "nuggets",
        "bucks", "suns", "heat", "knicks", "nets", "sixers", "mavericks",
        "yankees", "dodgers", "red sox", "astros", "braves", "padres",
        "manchester united", "man city", "liverpool", "chelsea", "arsenal",
        "real madrid", "barcelona", "bayern", "psg", "juventus", "inter milan",
    ]):
        return "sports"

    # Entertainment / Culture / Pop culture
    if any(w in combined for w in [
        "movie", "oscar", "grammy", "emmy", "golden globe", "celebrity",
        "entertainment", "netflix", "disney", "box office", "album",
        "spotify", "taylor swift", "kanye", "drake", "beyonce",
        "tv show", "series finale", "award", "nominee", "billboard",
        "viral", "tiktok", "youtube", "streamer", "podcast",
        "kardashian", "elon musk tweet", "twitter", "x.com",
    ]):
        return "entertainment"

    # Science / Tech / AI
    if any(w in combined for w in [
        "ai ", "openai", "chatgpt", "gpt-", "claude", "gemini", "llm",
        "artificial intelligence", "machine learning", "deepmind", "anthropic",
        "climate", "nasa", "science", "spacex", "launch", "rocket", "mars",
        "moon landing", "asteroid", "vaccine", "pandemic", "covid", "virus",
        "fda approval", "drug trial", "medical", "gene therapy", "crispr",
        "quantum", "fusion energy", "breakthrough", "study finds",
        "apple", "google", "microsoft", "meta", "amazon", "nvidia", "tesla stock",
        "ipo ", "tech stock", "semiconductor", "chip",
    ]):
        return "science_tech"

    # Economy / Finance (non-crypto)
    if any(w in combined for w in [
        "fed ", "federal reserve", "interest rate", "inflation", "cpi",
        "gdp", "recession", "unemployment", "jobs report", "stock market",
        "s&p 500", "dow jones", "nasdaq", "treasury", "bond yield",
        "oil price", "gold price", "commodity", "trade war", "deficit",
        "housing market", "real estate", "mortgage rate",
    ]):
        return "economy"

    # Weather / Natural events
    if any(w in combined for w in [
        "hurricane", "earthquake", "tornado", "wildfire", "flood",
        "temperature", "weather", "storm", "el nino", "drought",
        "hottest", "coldest", "record heat", "snowfall",
    ]):
        return "weather"

    return "other"


# ── Sharpness metrics ──────────────────────────────────────

def compute_clv(entry_price, closing_price, side):
    """Closing Line Value."""
    if closing_price is None or entry_price is None:
        return 0
    if side == "BUY":
        return float(closing_price - entry_price)
    return float(entry_price - closing_price)


MIN_CALIBRATION_BETS = 20


def compute_calibration(bets_with_prices):
    """Calibration score (lower = better). Returns None if insufficient data."""
    if not bets_with_prices or len(bets_with_prices) < MIN_CALIBRATION_BETS:
        return None
    buckets = defaultdict(list)
    for price, won in bets_with_prices:
        bucket = min(9, int(price * 10))
        buckets[bucket].append(1 if won else 0)
    total_error = 0
    count = 0
    for bucket, outcomes in buckets.items():
        implied = (bucket + 0.5) / 10
        actual = sum(outcomes) / len(outcomes)
        total_error += abs(actual - implied)
        count += 1
    return round(total_error / max(count, 1), 4) if count else None


def assign_tier(clv, win_rate, total_bets):
    if total_bets < 5:
        return "unknown"
    if clv > 0.05 and win_rate > 0.55:
        return "elite"
    if clv > 0.02 and win_rate > 0.52:
        return "sharp"
    if clv > 0 and win_rate > 0.48:
        return "moderate"
    return "noise"


# ── Analysis pipeline ──────────────────────────────────────

def analyze_wallet(address, profile=None):
    """Full analysis of a single wallet. Returns structured report dict."""
    print(f"\n{'=' * 60}")
    print(f"WALLET ANALYSIS: {address}")
    if profile and profile.get("username"):
        print(f"Username: {profile['username']}")
    print(f"{'=' * 60}")

    # Fetch trades
    print("\n[1/4] Fetching trade history...")
    trades = fetch_user_trades(address)
    print(f"  Found {len(trades)} trades")

    if not trades:
        print("\n  No trades found for this wallet.")
        print("  This could mean:")
        print("    - The username/address is incorrect")
        print("    - The wallet has no CLOB trades (may use AMM)")
        print("    - API rate limiting")
        return None

    # Fetch positions (for PnL data)
    print("\n[2/4] Fetching open positions...")
    positions = fetch_user_positions(address)
    print(f"  Found {len(positions)} open positions")

    # Build position PnL map
    position_pnl = {}
    for p in positions:
        cid = p.get("conditionId", "")
        position_pnl[cid] = {
            "cashPnl": float(p.get("cashPnl", 0) or 0),
            "percentPnl": float(p.get("percentPnl", 0) or 0),
            "currentValue": float(p.get("currentValue", 0) or 0),
            "initialValue": float(p.get("initialValue", 0) or 0),
            "curPrice": float(p.get("curPrice", 0) or 0),
            "avgPrice": float(p.get("avgPrice", 0) or 0),
        }

    # Process trades (data API already includes market info)
    print("\n[3/4] Processing trades...")
    bets = []

    for i, t in enumerate(trades):
        price = float(t.get("price", 0) or 0)
        size = float(t.get("size", 0) or 0)
        side = t.get("side", "BUY")
        outcome = t.get("outcome", "Yes")
        ts = t.get("timestamp")

        # Parse timestamp
        if ts:
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            elif isinstance(ts, str) and ts.isdigit():
                ts = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        else:
            ts = datetime.now(timezone.utc).isoformat()

        title = t.get("title", "")
        slug = t.get("slug", t.get("eventSlug", ""))
        condition_id = t.get("conditionId", "")
        category = categorize_market(title)

        # Check if we have position data for PnL
        pos = position_pnl.get(condition_id, {})
        closing_price = pos.get("curPrice") if pos else None

        # Determine win using position PnL
        won = None
        if pos and pos.get("percentPnl", 0) != 0:
            won = pos["cashPnl"] > 0

        # Impute closing_price for resolved bets (binary markets: 0 or 1)
        if closing_price is None and won is not None:
            if side == "BUY":
                closing_price = 1.0 if won else 0.0
            else:
                closing_price = 0.0 if won else 1.0

        amount_usd = round(price * size, 2)

        bets.append({
            "address": address,
            "market_slug": slug[:200],
            "market_title": title[:500],
            "category": category,
            "outcome": outcome,
            "side": side,
            "price": price,
            "size": size,
            "amount_usd": amount_usd,
            "timestamp": ts,
            "resolved": closing_price is not None and closing_price in (0, 1),
            "won": won,
            "closing_price": closing_price,
            "clv": compute_clv(price, closing_price, side) if closing_price is not None else None,
        })

    # Compute metrics
    print(f"\n[4/4] Computing sharpness metrics...")

    resolved = [b for b in bets if b["resolved"] and b["won"] is not None]
    total = len(bets)
    wins = sum(1 for b in resolved if b["won"])
    win_rate = wins / max(len(resolved), 1) if resolved else 0

    # CLV
    clvs = [b["clv"] for b in bets if b["clv"] is not None]
    avg_clv = sum(clvs) / max(len(clvs), 1) if clvs else 0

    # ROI
    total_wagered = sum(b["amount_usd"] for b in bets if b["amount_usd"])
    total_pnl = sum(
        (1 - b["price"]) * b["size"] if b["won"] else -b["price"] * b["size"]
        for b in resolved if b["won"] is not None
    )
    roi = total_pnl / max(total_wagered, 1)

    # Calibration
    cal_data = [(b["price"], b["won"]) for b in resolved if b["won"] is not None and b["price"] > 0]
    calibration = compute_calibration(cal_data)

    # Avg edge
    avg_edge = avg_clv * 0.7 + (win_rate - 0.5) * 0.3

    # Sharpe (edge consistency) and Kelly — require sufficient calibration data
    if calibration is not None:
        sharpe = roi / max(0.01, calibration) if calibration > 0 else 0
        kelly = max(0, (win_rate * (1 + avg_clv) - 1) / max(avg_clv, 0.01))
    else:
        sharpe = None
        kelly = None

    tier = assign_tier(avg_clv, win_rate, total)

    # Category breakdown
    by_cat = defaultdict(list)
    for b in bets:
        by_cat[b["category"]].append(b)

    cat_scores = {}
    for cat, cat_bets in by_cat.items():
        cat_resolved = [b for b in cat_bets if b["resolved"] and b["won"] is not None]
        cat_wins = sum(1 for b in cat_resolved if b["won"])
        cat_wr = cat_wins / max(len(cat_resolved), 1) if cat_resolved else 0
        cat_clvs = [b["clv"] for b in cat_bets if b["clv"] is not None]
        cat_avg_clv = sum(cat_clvs) / max(len(cat_clvs), 1) if cat_clvs else 0
        cat_wagered = sum(b["amount_usd"] for b in cat_bets if b["amount_usd"])
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

    # Top markets by position size
    market_summary = defaultdict(lambda: {"bets": 0, "volume": 0, "title": ""})
    for b in bets:
        ms = market_summary[b["market_slug"]]
        ms["bets"] += 1
        ms["volume"] += b["amount_usd"]
        ms["title"] = b["market_title"]

    top_markets = sorted(market_summary.items(), key=lambda x: x[1]["volume"], reverse=True)[:10]

    # Use position-level PnL if available
    total_position_pnl = sum(p.get("cashPnl", 0) for p in position_pnl.values())
    total_position_value = sum(abs(p.get("initialValue", 0)) for p in position_pnl.values())
    if total_position_value > 0:
        roi = total_position_pnl / total_position_value

    # Evaluate tier
    tier = assign_tier(avg_clv, win_rate, total)
    # Override tier for profitable high-volume traders
    if roi > 0.02 and total_wagered > 100000:
        if roi > 0.05:
            tier = "elite"
        elif roi > 0.02:
            tier = "sharp"

    # Fetch Polymarket username if we don't have one
    username = profile.get("username") if profile else None
    if not username:
        try:
            r = requests.get(
                f"{GAMMA_URL}/public-profile",
                params={"address": address},
                timeout=10,
            )
            if r.ok:
                pdata = r.json()
                username = pdata.get("name") or pdata.get("pseudonym")
        except Exception:
            pass

    # Build report
    report = {
        "address": address,
        "username": username,
        "total_bets": total,
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
        "top_markets": top_markets,
        "open_positions": len(positions),
        "bets": bets,
    }

    # Print report
    print_report(report)
    return report


def print_report(r):
    """Pretty-print the analysis report."""
    tier_colors = {"elite": "\033[92m", "sharp": "\033[96m", "moderate": "\033[93m", "noise": "\033[90m", "unknown": "\033[90m"}
    reset = "\033[0m"
    tc = tier_colors.get(r["tier"], "")

    print(f"\n{'=' * 60}")
    print(f"  SHARPNESS REPORT")
    print(f"{'=' * 60}")
    print(f"  Address:    {r['address']}")
    if r.get("username"):
        print(f"  Username:   {r['username']}")
    print(f"  Tier:       {tc}{r['tier'].upper()}{reset}")
    print(f"{'─' * 60}")
    print(f"  Total Bets:      {r['total_bets']}")
    print(f"  Total Volume:    ${r['total_volume']:,.2f}")
    print(f"  Resolved Bets:   {r['resolved_bets']}")
    print(f"  Wins:            {r['wins']}")
    print(f"{'─' * 60}")
    clv_sign = "+" if r["clv"] > 0 else ""
    roi_sign = "+" if r["roi"] > 0 else ""
    print(f"  Win Rate:        {r['win_rate'] * 100:.1f}%")
    print(f"  CLV:             {clv_sign}{r['clv'] * 100:.2f}%")
    print(f"  ROI:             {roi_sign}{r['roi'] * 100:.1f}%")
    print(f"  Calibration:     {r['calibration']:.4f}" if r['calibration'] is not None else "  Calibration:     N/A (< 20 resolved bets)")
    print(f"  Avg Edge:        {r['avg_edge'] * 100:.2f}%")
    print(f"  Consistency:     {r['sharpe_ratio']:.2f}" if r['sharpe_ratio'] is not None else "  Consistency:     N/A")
    print(f"  Kelly Fraction:  {r['kelly_fraction']:.4f}" if r['kelly_fraction'] is not None else "  Kelly Fraction:  N/A")
    print(f"  Open Positions:  {r['open_positions']}")

    if r.get("categories"):
        print(f"\n{'─' * 60}")
        print(f"  CATEGORY BREAKDOWN")
        print(f"  {'Category':<15} {'Bets':>5} {'Win%':>7} {'CLV':>8} {'ROI':>8}")
        for cat, cs in sorted(r["categories"].items(), key=lambda x: x[1]["clv"], reverse=True):
            clv_s = f"{'+' if cs['clv'] > 0 else ''}{cs['clv'] * 100:.1f}%"
            roi_s = f"{'+' if cs['roi'] > 0 else ''}{cs['roi'] * 100:.1f}%"
            print(f"  {cat:<15} {cs['total_bets']:>5} {cs['win_rate'] * 100:>6.1f}% {clv_s:>8} {roi_s:>8}")

    if r.get("top_markets"):
        print(f"\n{'─' * 60}")
        print(f"  TOP MARKETS BY VOLUME")
        for slug, info in r["top_markets"][:5]:
            title = info["title"][:50]
            print(f"  ${info['volume']:>10,.2f}  ({info['bets']} bets)  {title}")

    print(f"\n{'=' * 60}")


def save_to_supabase(report):
    """Save the analysis results to Supabase."""
    sb = get_supabase()
    addr = report["address"]
    now = datetime.now(timezone.utc).isoformat()

    print("\nSaving to Supabase...")

    # 1. Upsert wallet
    wallet_row = {
        "address": addr,
        "label": report.get("username") or f"{report['tier']}_{addr[:6]}",
        "total_bets": report["total_bets"],
        "total_volume": report["total_volume"],
        "is_tracked": report["tier"] in ("elite", "sharp"),
        "updated_at": now,
    }
    try:
        sb.table("wallets").upsert(wallet_row, on_conflict="address").execute()
        print(f"  Wallet saved")
    except Exception as e:
        print(f"  Warning: wallet upsert failed: {e}")

    # 2. Upsert wallet_scores
    scored_bets = len([b for b in report.get("bets", []) if b.get("clv") is not None])
    score_row = {
        "address": addr,
        "total_bets": report["total_bets"],
        "scored_bets": scored_bets,
        "win_rate": report["win_rate"],
        "clv": report["clv"],
        "roi": report["roi"],
        "calibration": report["calibration"],
        "avg_edge": report["avg_edge"],
        "kelly_fraction": report["kelly_fraction"],
        "sharpe_ratio": report["sharpe_ratio"],
        "tier": report["tier"],
        "updated_at": now,
    }
    try:
        sb.table("wallet_scores").upsert(score_row, on_conflict="address").execute()
        print(f"  Scores saved (scored {scored_bets} bets with CLV)")
    except Exception as e:
        # Retry without scored_bets if column doesn't exist yet
        if "scored_bets" in str(e):
            del score_row["scored_bets"]
            sb.table("wallet_scores").upsert(score_row, on_conflict="address").execute()
            print(f"  Scores saved (scored_bets column pending)")
        else:
            print(f"  Warning: scores upsert failed: {e}")

    # 3. Upsert category scores
    for cat, cs in report.get("categories", {}).items():
        cat_row = {
            "address": addr,
            "category": cat,
            "total_bets": cs["total_bets"],
            "win_rate": cs["win_rate"],
            "clv": cs["clv"],
            "roi": cs["roi"],
            "updated_at": now,
        }
        try:
            sb.table("wallet_category_scores").upsert(cat_row, on_conflict="address,category").execute()
        except Exception as e:
            print(f"  Warning: category score upsert ({cat}): {e}")
    print(f"  Category scores saved ({len(report.get('categories', {}))} categories)")

    # 4. Insert bets (avoid duplicates by checking timestamp)
    bet_rows = []
    for b in report.get("bets", [])[:500]:  # cap at 500
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
            "clv": b["clv"],
        })

    if bet_rows:
        batch_size = 50
        saved = 0
        for i in range(0, len(bet_rows), batch_size):
            batch = bet_rows[i:i + batch_size]
            try:
                sb.table("bets").insert(batch).execute()
                saved += len(batch)
            except Exception as e:
                print(f"  Warning: bets batch {i}: {e}")
        print(f"  Bets saved ({saved}/{len(bet_rows)})")

    print(f"\n  Done! View at dashboard or query Supabase.")


# ── CLI entry point ────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python wallet_analyzer.py <username_or_address> [--save]")
        print()
        print("Examples:")
        print("  python wallet_analyzer.py kch123")
        print("  python wallet_analyzer.py 0xabc123... --save")
        print()
        print("Flags:")
        print("  --save    Save results to Supabase")
        sys.exit(1)

    identifier = sys.argv[1]
    save = "--save" in sys.argv

    # Resolve address
    print(f"\nResolving '{identifier}'...")
    address, profile = resolve_address(identifier)
    print(f"  Address: {address}")
    if profile.get("username"):
        print(f"  Username: {profile['username']}")

    # Run analysis
    report = analyze_wallet(address, profile)

    if report and save:
        save_to_supabase(report)
    elif report and not save:
        print(f"\n  Tip: run with --save to store results in Supabase")


if __name__ == "__main__":
    main()
