"""
Shared scoring module — single source of truth for all wallet scoring logic.

All other scripts (batch_score.py, wallet_analyzer.py, ws_monitor.py,
seed_data.py, api/rescore.py) import from here.
"""
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_ANON_KEY", "")
GAMMA_URL = os.environ.get("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
CLOB_URL = os.environ.get("POLYMARKET_API_URL", "https://clob.polymarket.com")
DATA_API = "https://data-api.polymarket.com"

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

MIN_CALIBRATION_BETS = 20


# ── Supabase REST helpers ──────────────────────────────────

def sb_query(table, params=""):
    """Query Supabase via REST API."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{params}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        timeout=10,
    )
    return r.json() if r.ok else []


def sb_upsert(table, data):
    """Upsert data to Supabase via REST API."""
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=SB_HEADERS,
        json=data,
        timeout=10,
    )
    return r.ok


# ── Market categorization ──────────────────────────────────

def categorize_market(title, tags=None):
    """Categorize a market based on title and tags."""
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
    # "X vs Y" pattern (common in sports markets)
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
    """Closing Line Value — how much better was entry vs close."""
    if closing_price is None or entry_price is None:
        return 0
    if side == "BUY":
        return float(closing_price - entry_price)
    return float(entry_price - closing_price)


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
    """Assign tier based on CLV + win rate + sample size."""
    if total_bets < 5:
        return "unknown"
    if clv > 0.05 and win_rate > 0.55:
        return "elite"
    if clv > 0.02 and win_rate > 0.52:
        return "sharp"
    if clv > 0 and win_rate > 0.48:
        return "moderate"
    return "noise"


# ── Polymarket API ─────────────────────────────────────────

def resolve_username(address):
    """Fetch the Polymarket display name for a wallet address."""
    try:
        r = requests.get(
            f"{GAMMA_URL}/public-profile",
            params={"address": address},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            return data.get("name") or data.get("pseudonym") or None
    except Exception:
        pass
    try:
        r = requests.get(f"{GAMMA_URL}/profiles/{address}", timeout=10)
        if r.ok:
            p = r.json()
            return p.get("username") or p.get("name") or None
    except Exception:
        pass
    return None


def resolve_address(identifier):
    """Resolve a username or address to a wallet address + profile info."""
    if identifier.startswith("0x") and len(identifier) >= 40:
        return identifier, {"username": None, "address": identifier}

    try:
        r = requests.get(f"{GAMMA_URL}/users", params={"username": identifier}, timeout=10)
        if r.ok:
            users = r.json()
            if isinstance(users, list) and users:
                u = users[0]
                return u.get("proxyWallet") or u.get("address") or u.get("id"), u
            elif isinstance(users, dict) and users.get("address"):
                return users.get("proxyWallet") or users["address"], users
    except Exception:
        pass

    try:
        r = requests.get(f"{GAMMA_URL}/profiles/{identifier}", timeout=10)
        if r.ok:
            u = r.json()
            addr = u.get("proxyWallet") or u.get("address") or u.get("id")
            if addr:
                return addr, u
    except Exception:
        pass

    try:
        r = requests.get(f"{CLOB_URL}/profile/{identifier}", timeout=10)
        if r.ok:
            u = r.json()
            addr = u.get("proxyWallet") or u.get("address")
            if addr:
                return addr, u
    except Exception:
        pass

    return identifier, {"username": identifier, "address": identifier}


def fetch_user_trades(address, limit=10000):
    """Fetch trades from Polymarket data API with pagination."""
    all_trades = []
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
        except Exception:
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
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": address, "sizeThreshold": 0, "limit": 500},
            timeout=15,
        )
        if r.ok:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


# ── Core scoring pipeline ──────────────────────────────────

def score_wallet(address, existing_label=None):
    """
    Full scoring pipeline for a single wallet.
    Returns a structured report dict, or None if insufficient data.
    """
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
        outcome = t.get("outcome", "Yes")
        title = t.get("title", "")
        slug = t.get("slug", t.get("eventSlug", ""))
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

        # Parse timestamp
        ts = t.get("timestamp")
        if ts:
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            elif isinstance(ts, str) and ts.isdigit():
                ts = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        else:
            ts = datetime.now(timezone.utc).isoformat()

        bet = {
            "address": address,
            "market_slug": (slug or "")[:200],
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
            "clv": clv,
        }
        bets.append(bet)
        by_cat[category].append(bet)

    # Overall metrics
    resolved = [b for b in bets if b["resolved"] and b["won"] is not None]
    wins = sum(1 for b in resolved if b["won"])
    win_rate = wins / max(len(resolved), 1) if resolved else 0
    clvs = [b["clv"] for b in bets if b["clv"] is not None]
    avg_clv = sum(clvs) / max(len(clvs), 1) if clvs else 0
    total_wagered = sum(b["amount_usd"] for b in bets if b["amount_usd"])
    total_pnl = sum(
        (1 - b["price"]) * b["size"] if b["won"] else -b["price"] * b["size"]
        for b in resolved if b["won"] is not None
    )
    roi = total_pnl / max(total_wagered, 1) if total_wagered else 0

    # Position-level PnL override
    total_pos_pnl = sum(p.get("cashPnl", 0) for p in position_pnl.values())
    total_pos_value = sum(abs(p.get("initialValue", 0)) for p in position_pnl.values())
    if total_pos_value > 0:
        roi = total_pos_pnl / total_pos_value
    current_roi = (total_pnl + total_pos_pnl) / max(total_wagered, 1)

    # Calibration
    cal_data = [(b["price"], b["won"]) for b in resolved if b["won"] is not None and b["price"] > 0]
    calibration = compute_calibration(cal_data)

    # Derived metrics
    avg_edge = avg_clv * 0.7 + (win_rate - 0.5) * 0.3
    if calibration is not None:
        sharpe = roi / max(0.01, calibration) if calibration > 0 else 0
        kelly = max(0, (win_rate * (1 + avg_clv) - 1) / max(avg_clv, 0.01))
    else:
        sharpe = None
        kelly = None

    tier = assign_tier(avg_clv, win_rate, len(bets))

    # Override tier for profitable high-volume traders
    if roi > 0.05 and total_wagered > 100000:
        tier = "elite"
    elif roi > 0.02 and total_wagered > 100000:
        tier = "sharp"

    # Category breakdown
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

    # Resolve username
    username = existing_label
    if not username or username.startswith(("elite_", "sharp_", "moderate_", "noise_", "unknown_")):
        username = resolve_username(address)
    label = username or f"{tier}_{address[:6]}"

    return {
        "address": address,
        "username": username,
        "label": label,
        "total_bets": len(bets),
        "total_volume": round(total_wagered, 2),
        "resolved_bets": len(resolved),
        "wins": wins,
        "win_rate": round(win_rate, 4),
        "clv": round(avg_clv, 4),
        "roi": round(roi, 4),
        "current_roi": round(current_roi, 4),
        "calibration": round(calibration, 4) if calibration is not None else None,
        "avg_edge": round(avg_edge, 4),
        "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
        "kelly_fraction": round(kelly, 4) if kelly is not None else None,
        "tier": tier,
        "categories": cat_scores,
        "open_positions": len(positions),
        "bets": bets,
    }


# ── Persistence ────────────────────────────────────────────

def save_to_supabase(report):
    """Save a wallet scoring report to Supabase (REST API)."""
    addr = report["address"]
    now = datetime.now(timezone.utc).isoformat()

    # 1. Upsert wallet
    sb_upsert("wallets", {
        "address": addr,
        "label": report.get("label") or report.get("username") or f"{report['tier']}_{addr[:6]}",
        "total_bets": report["total_bets"],
        "total_volume": report.get("total_volume", 0),
        "is_tracked": report["tier"] in ("elite", "sharp"),
        "updated_at": now,
    })

    # 2. Upsert wallet_scores
    scored_bets = len([b for b in report.get("bets", []) if b.get("clv") is not None])
    sb_upsert("wallet_scores", {
        "address": addr,
        "total_bets": report["total_bets"],
        "scored_bets": scored_bets,
        "win_rate": report["win_rate"],
        "clv": report["clv"],
        "roi": report["roi"],
        "current_roi": report.get("current_roi", report["roi"]),
        "calibration": report["calibration"],
        "avg_edge": report["avg_edge"],
        "kelly_fraction": report["kelly_fraction"],
        "sharpe_ratio": report["sharpe_ratio"],
        "tier": report["tier"],
        "updated_at": now,
    })

    # 3. Upsert category scores
    for cat, cs in report.get("categories", {}).items():
        sb_upsert("wallet_category_scores", {
            "address": addr,
            "category": cat,
            "total_bets": cs["total_bets"],
            "win_rate": cs["win_rate"],
            "clv": cs["clv"],
            "roi": cs["roi"],
            "updated_at": now,
        })


def save_to_supabase_client(report, supabase_client):
    """Save a wallet scoring report using the Supabase Python client."""
    sb = supabase_client
    addr = report["address"]
    now = datetime.now(timezone.utc).isoformat()

    wallet_row = {
        "address": addr,
        "label": report.get("label") or report.get("username") or f"{report['tier']}_{addr[:6]}",
        "total_bets": report["total_bets"],
        "total_volume": report.get("total_volume", 0),
        "is_tracked": report["tier"] in ("elite", "sharp"),
        "updated_at": now,
    }
    try:
        sb.table("wallets").upsert(wallet_row, on_conflict="address").execute()
    except Exception as e:
        print(f"  Warning: wallet upsert failed: {e}")

    scored_bets = len([b for b in report.get("bets", []) if b.get("clv") is not None])
    score_row = {
        "address": addr,
        "total_bets": report["total_bets"],
        "scored_bets": scored_bets,
        "win_rate": report["win_rate"],
        "clv": report["clv"],
        "roi": report["roi"],
        "current_roi": report.get("current_roi", report["roi"]),
        "calibration": report["calibration"],
        "avg_edge": report["avg_edge"],
        "kelly_fraction": report["kelly_fraction"],
        "sharpe_ratio": report["sharpe_ratio"],
        "tier": report["tier"],
        "updated_at": now,
    }
    try:
        sb.table("wallet_scores").upsert(score_row, on_conflict="address").execute()
    except Exception as e:
        if "scored_bets" in str(e) or "current_roi" in str(e):
            score_row.pop("scored_bets", None)
            score_row.pop("current_roi", None)
            sb.table("wallet_scores").upsert(score_row, on_conflict="address").execute()
        else:
            print(f"  Warning: scores upsert failed: {e}")

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
        except Exception:
            pass

    # Insert bets
    bet_rows = []
    for b in report.get("bets", [])[:500]:
        bet_rows.append({
            "address": b["address"],
            "market_slug": b["market_slug"],
            "market_title": b["market_title"],
            "category": b["category"],
            "outcome": b.get("outcome") or "Yes",
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
        for i in range(0, len(bet_rows), batch_size):
            batch = bet_rows[i:i + batch_size]
            try:
                sb.table("bets").insert(batch).execute()
            except Exception:
                pass
