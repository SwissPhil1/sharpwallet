"""
Vercel Python serverless function — re-score a single wallet.

POST /api/rescore  {"address": "0x..."}   -> score one wallet
POST /api/rescore  {"action": "list"}     -> return addresses to score
"""
import json
import os
from collections import defaultdict
from http.server import BaseHTTPRequestHandler
from datetime import datetime, timezone

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}


def sb_query(table, params=""):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{params}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        timeout=10,
    )
    return r.json() if r.ok else []


def sb_upsert(table, data):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=SB_HEADERS,
        json=data,
        timeout=10,
    )
    return r.ok


def resolve_username(address):
    try:
        r = requests.get(f"{GAMMA_URL}/profiles/{address}", timeout=10)
        if r.ok:
            p = r.json()
            return p.get("username") or p.get("name") or None
    except Exception:
        pass
    return None


def categorize_market(title):
    title_lower = (title or "").lower()
    cats = {
        "politics": ["election", "president", "trump", "biden", "congress", "senate",
                      "democrat", "republican", "governor", "political", "vote",
                      "gop", "dnc", "rnc", "poll", "primary", "nominee", "cabinet",
                      "impeach", "legislation", "executive order", "inaugur"],
        "crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "token", "defi",
                    "nft", "blockchain", "solana", "sol", "xrp", "doge", "memecoin",
                    "altcoin", "binance", "coinbase", "sec crypto"],
        "sports": ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball",
                    "baseball", "hockey", "tennis", "golf", "ufc", "boxing", "f1",
                    "formula", "olympics", "world cup", "super bowl", "champion",
                    "playoff", "mvp", "serie a", "premier league", "la liga",
                    "bundesliga", "grand slam", "match", "game", "season"],
        "science": ["climate", "weather", "hurricane", "earthquake", "nasa", "space",
                     "ai ", "artificial intelligence", "gpt", "openai", "google ai",
                     "machine learning", "pandemic", "virus", "vaccine", "fda",
                     "scientific", "research", "study", "temperature"],
    }
    for cat, kws in cats.items():
        if any(kw in title_lower for kw in kws):
            return cat
    return "other"


def compute_clv(entry_price, closing_price, side):
    if closing_price is None or entry_price is None:
        return None
    if side == "BUY":
        return closing_price - entry_price
    else:
        return entry_price - closing_price


def compute_calibration(data):
    if len(data) < 5:
        return 0.0
    bins = defaultdict(list)
    for price, won in data:
        bucket = round(price * 10) / 10
        bins[bucket].append(1 if won else 0)
    total_err = 0
    count = 0
    for bucket, outcomes in bins.items():
        if outcomes:
            actual = sum(outcomes) / len(outcomes)
            total_err += abs(bucket - actual)
            count += 1
    return total_err / max(count, 1)


def assign_tier(avg_clv, win_rate, total_bets):
    if total_bets < 5:
        return "noise"
    if avg_clv >= 0.05 and win_rate >= 0.55:
        return "elite"
    if avg_clv >= 0.02 and win_rate >= 0.50:
        return "sharp"
    if avg_clv >= 0.0 and win_rate >= 0.45:
        return "moderate"
    return "noise"


def fetch_trades(address, limit=500):
    try:
        r = requests.get(
            f"{DATA_API}/trades",
            params={"maker": address, "limit": limit},
            timeout=15,
        )
        if r.ok:
            return r.json() or []
    except Exception:
        pass
    try:
        r = requests.get(
            f"{CLOB_URL}/trades",
            params={"maker": address, "limit": limit},
            timeout=15,
        )
        if r.ok:
            return r.json() or []
    except Exception:
        pass
    return []


def fetch_positions(address):
    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": address, "sizeThreshold": 0, "limit": 500},
            timeout=15,
        )
        if r.ok:
            return r.json() or []
    except Exception:
        pass
    return []


def score_wallet(address, existing_label=None):
    trades = fetch_trades(address)
    if not trades or len(trades) < 3:
        return None

    positions = fetch_positions(address)
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

        clv = compute_clv(price, closing_price, side) if closing_price else None
        amount_usd = round(price * size, 2)

        bet = {
            "market_title": title[:500],
            "category": category,
            "side": side,
            "price": price,
            "size": size,
            "amount_usd": amount_usd,
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
    total_pnl = sum(
        (1 - b["price"]) * b["size"] if b["won"] else -b["price"] * b["size"]
        for b in resolved if b["won"] is not None
    )
    realized_roi = total_pnl / max(total_wagered, 1) if total_wagered else 0
    total_pos_pnl = sum(p.get("cashPnl", 0) for p in position_pnl.values())
    current_roi = (total_pnl + total_pos_pnl) / max(total_wagered, 1)

    cal_data = [(b["price"], b["won"]) for b in resolved if b["won"] is not None and b["price"] > 0]
    calibration = compute_calibration(cal_data)
    avg_edge = avg_clv * 0.7 + (win_rate - 0.5) * 0.3
    sharpe = realized_roi / max(0.01, calibration) if calibration > 0 else 0
    kelly = max(0, (win_rate * (1 + avg_clv) - 1) / max(avg_clv, 0.01))
    tier = assign_tier(avg_clv, win_rate, len(bets))

    # Resolve username
    username = existing_label
    if not username or username.startswith(("elite_", "sharp_", "moderate_", "noise_")):
        username = resolve_username(address)

    label = username or f"{tier}_{address[:6]}"

    now = datetime.now(timezone.utc).isoformat()

    # Save wallet
    sb_upsert("wallets", {
        "address": address,
        "label": label,
        "total_bets": len(bets),
        "total_volume": round(total_wagered, 2),
        "is_tracked": tier in ("elite", "sharp"),
        "updated_at": now,
    })

    # Save scores
    sb_upsert("wallet_scores", {
        "address": address,
        "total_bets": len(bets),
        "win_rate": round(win_rate, 4),
        "clv": round(avg_clv, 4),
        "roi": round(realized_roi, 4),
        "current_roi": round(current_roi, 4),
        "calibration": round(calibration, 4),
        "avg_edge": round(avg_edge, 4),
        "kelly_fraction": round(kelly, 4),
        "sharpe_ratio": round(sharpe, 4),
        "tier": tier,
        "updated_at": now,
    })

    # Save category scores
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
        sb_upsert("wallet_category_scores", {
            "address": address,
            "category": cat,
            "total_bets": len(cat_bets),
            "win_rate": round(cat_wr, 4),
            "clv": round(cat_avg_clv, 4),
            "roi": round(cat_roi, 4),
            "updated_at": now,
        })

    return {
        "address": address,
        "label": label,
        "tier": tier,
        "total_bets": len(bets),
        "clv": round(avg_clv, 4),
        "win_rate": round(win_rate, 4),
        "roi": round(realized_roi, 4),
        "current_roi": round(current_roi, 4),
    }


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        """Return list of wallet addresses to re-score."""
        wallets = sb_query("wallets", "select=address,label&order=created_at.desc&limit=500")
        self._json(200, {"wallets": wallets})

    def do_POST(self):
        """Score a single wallet."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json(400, {"error": "Invalid JSON"})
            return

        address = body.get("address", "").strip().lower()
        if not address or not address.startswith("0x"):
            self._json(400, {"error": "Missing or invalid address"})
            return

        existing_label = body.get("label")

        try:
            result = score_wallet(address, existing_label=existing_label)
            if result:
                self._json(200, {"ok": True, "result": result})
            else:
                self._json(200, {"ok": False, "error": "Insufficient trade data"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})
