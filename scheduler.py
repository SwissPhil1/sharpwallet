"""
Scheduler — runs the WebSocket monitor with periodic wallet refresh + batch rescoring.
Designed for Railway deployment.
"""
import asyncio
import os
import sys
import time
import threading
import signal
from datetime import datetime, timezone

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

RESCORE_INTERVAL_HOURS = int(os.environ.get("RESCORE_INTERVAL_HOURS", "168"))

# Verify required env vars
required = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"]
missing = [k for k in required if not os.environ.get(k)]
if missing:
    print(f"ERROR: Missing environment variables: {', '.join(missing)}")
    sys.exit(1)

running = True


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def signal_handler(sig, frame):
    global running
    log("Shutting down...")
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


async def run_leaderboard_batch():
    """Run batch scoring to discover and score new wallets."""
    log("Starting leaderboard batch scoring run...")
    try:
        from leaderboard_scraper import run_batch
        await run_batch()
        log("Batch scoring complete")
    except Exception as e:
        log(f"Batch scoring failed: {e}")


def periodic_refresh(interval=300):
    """Refresh the tracked wallet list every N seconds."""
    while running:
        try:
            from ws_monitor import load_tracked_wallets
            log("Refreshing wallet watchlist...")
            load_tracked_wallets()
        except Exception as e:
            log(f"Refresh error: {e}")
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)


async def main():
    log("=" * 60)
    log("SHARPWALLET SCHEDULER")
    log(f"Rescore interval: every {RESCORE_INTERVAL_HOURS}h")
    log("=" * 60)

    # Initial batch scoring
    await run_leaderboard_batch()

    # Start periodic wallet refresh in background
    refresh_thread = threading.Thread(target=periodic_refresh, args=(300,), daemon=True)
    refresh_thread.start()

    # Start WebSocket monitor
    log("Starting WebSocket trade monitor...")
    try:
        from ws_monitor import monitor_trades
        await monitor_trades()
    except Exception as e:
        log(f"WebSocket monitor error: {e}")
        log("Falling back to periodic rescoring only...")

        # If WS fails, just keep rescoring periodically
        run_count = 1
        while running:
            run_count += 1
            log(f"Sleeping {RESCORE_INTERVAL_HOURS}h until next rescore run...")
            await asyncio.sleep(RESCORE_INTERVAL_HOURS * 3600)
            log(f"Starting rescore run #{run_count}")
            await run_leaderboard_batch()


if __name__ == "__main__":
    asyncio.run(main())
