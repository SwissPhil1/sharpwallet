"""
WebSocket monitor for Polymarket sharp wallet activity.
Watches for trades by tracked wallets and creates alerts in Supabase.
Optionally sends Telegram notifications.
"""
import os
import sys
import json
import time
import asyncio
import signal
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

import requests
import websockets
from supabase import create_client
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_ANON_KEY"]
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Polymarket WebSocket endpoints
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_URL = "https://clob.polymarket.com"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── State ──
tracked_wallets = {}   # address -> {label, tier, ...}
market_cache = {}      # condition_id -> {slug, title, category}
running = True


def load_tracked_wallets():
    """Load wallets flagged for tracking from Supabase."""
    global tracked_wallets
    try:
        result = supabase.table("wallets").select("address,label").eq("is_tracked", True).execute()
        tracked_wallets = {w["address"]: w for w in result.data}

        # Also load their scores
        if tracked_wallets:
            scores = supabase.table("wallet_scores").select("address,tier,clv,win_rate,rank").execute()
            for s in scores.data:
                if s["address"] in tracked_wallets:
                    tracked_wallets[s["address"]].update(s)

        print(f"  Loaded {len(tracked_wallets)} tracked wallets")
        return tracked_wallets
    except Exception as e:
        print(f"  Error loading wallets: {e}")
        return {}


def get_market_info(condition_id):
    """Look up market metadata, with caching."""
    if condition_id in market_cache:
        return market_cache[condition_id]

    try:
        result = supabase.table("markets").select("slug,title,category").eq("condition_id", condition_id).limit(1).execute()
        if result.data:
            market_cache[condition_id] = result.data[0]
            return result.data[0]
    except Exception:
        pass

    # Fallback: fetch from Gamma API
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets",
                        params={"condition_id": condition_id}, timeout=10)
        if r.ok and r.json():
            m = r.json()[0]
            info = {
                "slug": m.get("slug", condition_id),
                "title": m.get("question", m.get("title", condition_id)),
                "category": categorize_market(m.get("question", "")),
            }
            market_cache[condition_id] = info
            return info
    except Exception:
        pass

    return {"slug": condition_id, "title": condition_id, "category": "other"}


def categorize_market(title):
    """Quick market categorization."""
    t = (title or "").lower()
    if any(w in t for w in ["trump", "biden", "election", "congress", "president", "democrat", "republican"]):
        return "politics"
    if any(w in t for w in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana"]):
        return "crypto"
    if any(w in t for w in ["nfl", "nba", "mlb", "soccer", "football", "ufc", "sports"]):
        return "sports"
    return "other"


def create_alert(wallet, trade, market_info):
    """Insert an alert into Supabase."""
    try:
        alert = {
            "address": wallet["address"],
            "wallet_label": wallet.get("label"),
            "wallet_tier": wallet.get("tier", "unknown"),
            "market_slug": market_info.get("slug", ""),
            "market_title": market_info.get("title", ""),
            "category": market_info.get("category", "other"),
            "outcome": trade.get("outcome", "Yes"),
            "side": trade.get("side", "BUY"),
            "price": float(trade.get("price", 0)),
            "size": float(trade.get("size", 0)),
            "amount_usd": round(float(trade.get("price", 0)) * float(trade.get("size", 0)), 2),
            "alert_type": "bet",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Check for arb gap (placeholder — would need Pinnacle API)
        # alert["pinnacle_price"] = ...
        # alert["arb_gap"] = ...

        supabase.table("alerts").insert(alert).execute()
        print(f"  ALERT: {wallet.get('label', wallet['address'][:10])} | "
              f"{trade.get('side')} {trade.get('outcome')} @ {trade.get('price')} | "
              f"{market_info.get('title', '')[:50]}")

        # Send Telegram if configured
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            send_telegram(alert)

        return alert
    except Exception as e:
        print(f"  Alert insert error: {e}")
        return None


def send_telegram(alert):
    """Send alert to Telegram."""
    tier_emoji = {"elite": "🟢", "sharp": "🔵", "moderate": "🟡"}.get(alert["wallet_tier"], "⚪")
    msg = (
        f"{tier_emoji} <b>{alert['wallet_tier'].upper()} WALLET</b>\n"
        f"<code>{alert['wallet_label'] or alert['address'][:12]}</code>\n\n"
        f"<b>{alert['side']}</b> {alert['outcome']} @ <b>{alert['price']:.1%}</b>\n"
        f"Size: ${alert.get('amount_usd', 0):,.0f}\n"
        f"Market: {alert['market_title'][:80]}\n"
        f"Category: {alert['category']}"
    )
    if alert.get("arb_gap") and abs(alert["arb_gap"]) > 0.02:
        msg += f"\n\n⚠️ ARB GAP: {alert['arb_gap']:.1%}"

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"  Telegram send error: {e}")


# ── WebSocket trade monitoring ──

async def monitor_trades():
    """Connect to Polymarket WebSocket and watch for tracked wallet trades."""
    global running

    print("\n" + "=" * 60)
    print("POLYMARKET WEBSOCKET MONITOR")
    print("=" * 60)

    if not tracked_wallets:
        load_tracked_wallets()
        if not tracked_wallets:
            print("\nNo tracked wallets found. Run seed_data.py first.")
            return

    print(f"\nMonitoring {len(tracked_wallets)} wallets...")
    print("Press Ctrl+C to stop\n")

    reconnect_delay = 2

    while running:
        try:
            # Subscribe to trade events
            async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10) as ws:
                print(f"  Connected to {WS_URL}")
                reconnect_delay = 2  # reset on success

                # Subscribe to all market trade channels
                # Polymarket WS expects asset subscription messages
                sub_msg = json.dumps({
                    "type": "subscribe",
                    "channel": "trades",
                })
                await ws.send(sub_msg)
                print("  Subscribed to trade feed")

                while running:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        data = json.loads(msg)

                        # Process trade messages
                        if isinstance(data, dict):
                            trades = data.get("data", [data])
                            if not isinstance(trades, list):
                                trades = [trades]

                            for trade in trades:
                                # Check if any trade involves a tracked wallet
                                maker = trade.get("maker_address", "")
                                taker = trade.get("taker_address", "")
                                owner = trade.get("owner", "")

                                for addr in [maker, taker, owner]:
                                    if addr and addr in tracked_wallets:
                                        wallet = tracked_wallets[addr]
                                        condition_id = trade.get("condition_id") or trade.get("asset_id", "")
                                        market_info = get_market_info(condition_id)
                                        create_alert(wallet, trade, market_info)

                    except asyncio.TimeoutError:
                        # No message in 60s — send ping to keep alive
                        try:
                            await ws.ping()
                        except Exception:
                            break
                    except websockets.ConnectionClosed:
                        print("  Connection closed, reconnecting...")
                        break

        except Exception as e:
            if not running:
                break
            print(f"  WebSocket error: {e}")
            print(f"  Reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

            # Refresh tracked wallets on reconnect
            load_tracked_wallets()


# ── Fallback: REST polling ──

async def poll_trades():
    """
    Poll the Polymarket data API for recent trades by tracked wallets.
    """
    print("\n" + "=" * 60)
    print("POLYMARKET TRADE POLLER (DATA API)")
    print("=" * 60)

    if not tracked_wallets:
        load_tracked_wallets()
        if not tracked_wallets:
            print("\nNo tracked wallets found. Run wallet_analyzer.py first.")
            return

    print(f"\nPolling trades for {len(tracked_wallets)} wallets...")
    print("Press Ctrl+C to stop\n")

    seen_trades = set()
    DATA_API = "https://data-api.polymarket.com"

    while running:
        for addr in list(tracked_wallets.keys()):
            if not running:
                break
            try:
                r = requests.get(
                    f"{DATA_API}/trades",
                    params={"user": addr, "limit": 20},
                    timeout=10,
                )
                if r.ok:
                    trades = r.json()
                    if not isinstance(trades, list):
                        trades = []

                    for trade in trades:
                        trade_id = trade.get("transactionHash", "") + trade.get("asset", "")
                        if trade_id and trade_id not in seen_trades:
                            seen_trades.add(trade_id)
                            market_info = {
                                "slug": trade.get("slug", ""),
                                "title": trade.get("title", ""),
                                "category": categorize_market(trade.get("title", "")),
                            }
                            wallet = tracked_wallets[addr]
                            create_alert(wallet, trade, market_info)

            except Exception as e:
                print(f"  Poll error for {addr[:10]}: {e}")

            await asyncio.sleep(0.5)

        # Cap seen_trades size
        if len(seen_trades) > 10000:
            seen_trades = set(list(seen_trades)[-5000:])

        # Wait between full cycles
        await asyncio.sleep(30)


# ── Entry point ──

def handle_signal(sig, frame):
    global running
    print("\n\nShutting down...")
    running = False

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "ws"

    if mode == "ws":
        print("Starting WebSocket monitor...")
        asyncio.run(monitor_trades())
    elif mode == "poll":
        print("Starting REST poller...")
        asyncio.run(poll_trades())
    elif mode == "test":
        print("Sending test Telegram alert...")
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
            return
        test_alert = {
            "address": "0x0000000000000000000000000000000000000000",
            "wallet_label": "TEST_WALLET",
            "wallet_tier": "elite",
            "market_title": "SharpWallet end-to-end test alert",
            "side": "BUY",
            "outcome": "Yes",
            "price": 0.65,
            "amount_usd": 100,
            "category": "test",
        }
        send_telegram(test_alert)
        print("Test alert sent — check Telegram @The_Sharpest_bot")
    else:
        print(f"Usage: python ws_monitor.py [ws|poll|test]")
        print(f"  ws   — WebSocket live feed (default)")
        print(f"  poll — REST API polling every 30s")
        print(f"  test — Send a test Telegram alert")


if __name__ == "__main__":
    main()
