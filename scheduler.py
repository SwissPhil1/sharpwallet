"""
Scheduler — runs 3 background threads on Railway:
  1. Wallet refresh (reload tracked wallet list every 5 min)
  2. Job processor  (poll rescore_jobs every 30s, process pending jobs)
  3. Auto-rescore   (create rescore job every 4h for stale wallets)
Plus the WebSocket monitor in the main async loop.
"""
import asyncio
import os
import sys
import time
import threading
import signal
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))

RESCORE_INTERVAL_HOURS = int(os.environ.get("RESCORE_INTERVAL_HOURS", "4"))
JOB_POLL_INTERVAL = int(os.environ.get("JOB_POLL_INTERVAL", "30"))

# Normalize env vars — accept common aliases
if not os.environ.get("SUPABASE_SERVICE_KEY"):
    for alt in ["SUPABASE_KEY", "SUPABASE_ANON_KEY"]:
        if os.environ.get(alt):
            os.environ["SUPABASE_SERVICE_KEY"] = os.environ[alt]
            break

# Verify required env vars
required = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"]
missing = [k for k in required if not os.environ.get(k)]
if missing:
    print(f"ERROR: Missing environment variables: {', '.join(missing)}")
    print("Set these in Railway dashboard: Settings > Variables")
    print("Available env vars: " + ", ".join(sorted(k for k in os.environ if "SUPA" in k.upper() or "POLY" in k.upper())))
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


# ── Thread 1: Wallet refresh ──────────────────────────────

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


# ── Thread 2: Job processor ──────────────────────────────

def job_processor(interval=30):
    """Poll rescore_jobs table every N seconds and process pending jobs."""
    from scoring import sb_query, sb_upsert, score_wallet, save_to_supabase

    import requests
    SUPABASE_URL = os.environ["SUPABASE_URL"]
    SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
    patch_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    def update_job(job_id, data):
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/rescore_jobs?id=eq.{job_id}",
            headers=patch_headers,
            json=data,
            timeout=10,
        )

    log("Job processor started (polling every {}s)".format(interval))

    while running:
        try:
            # Check for pending jobs
            jobs = sb_query("rescore_jobs", "status=eq.pending&order=created_at.asc&limit=1")
            if jobs and isinstance(jobs, list) and len(jobs) > 0:
                job = jobs[0]
                job_id = job["id"]
                log(f"Processing rescore job #{job_id} ({job['total_wallets']} wallets)")

                # Mark as running
                update_job(job_id, {
                    "status": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                })

                # Fetch all wallets
                wallets = sb_query("wallets", "select=address,label&order=created_at.desc&limit=500")
                total = len(wallets) if isinstance(wallets, list) else 0
                scored = 0
                failed = 0

                for i, w in enumerate(wallets):
                    if not running:
                        break
                    addr = w.get("address", "")
                    label = w.get("label")

                    try:
                        result = score_wallet(addr, existing_label=label)
                        if result and result["total_bets"] >= 3:
                            save_to_supabase(result)
                            scored += 1
                            log(f"  [{scored}/{total}] {addr[:12]}... -> {result['tier'].upper()} CLV={result['clv']:+.4f}")
                        else:
                            failed += 1
                    except Exception as e:
                        log(f"  [{i+1}/{total}] {addr[:12]}... -> error: {e}")
                        failed += 1

                    # Update progress every 5 wallets
                    if (i + 1) % 5 == 0:
                        update_job(job_id, {"scored": scored, "failed": failed})

                    time.sleep(0.5)  # rate limit

                # Mark complete
                update_job(job_id, {
                    "status": "completed",
                    "scored": scored,
                    "failed": failed,
                    "total_wallets": total,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                })
                log(f"Job #{job_id} complete: {scored} scored, {failed} failed")

        except Exception as e:
            log(f"Job processor error: {e}")

        # Sleep with early exit
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)


# ── Thread 3: Auto-rescore ────────────────────────────────

def auto_rescore(interval_hours=4):
    """Create a rescore job every N hours for stale wallets."""
    from scoring import sb_query

    import requests
    SUPABASE_URL = os.environ["SUPABASE_URL"]
    SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
    insert_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    interval_secs = interval_hours * 3600
    log(f"Auto-rescore started (every {interval_hours}h)")

    while running:
        # Sleep first (let initial data load happen)
        for _ in range(interval_secs):
            if not running:
                break
            time.sleep(1)

        if not running:
            break

        try:
            # Check if there's already a pending/running job
            active = sb_query("rescore_jobs", "status=in.(pending,running)&limit=1")
            if active and isinstance(active, list) and len(active) > 0:
                log("Auto-rescore: job already queued, skipping")
                continue

            # Count wallets
            wallets = sb_query("wallets", "select=address&limit=500")
            total = len(wallets) if isinstance(wallets, list) else 0

            if total > 0:
                requests.post(
                    f"{SUPABASE_URL}/rest/v1/rescore_jobs",
                    headers=insert_headers,
                    json={"status": "pending", "total_wallets": total, "scored": 0, "failed": 0},
                    timeout=10,
                )
                log(f"Auto-rescore: created job for {total} wallets")
            else:
                log("Auto-rescore: no wallets to score")

        except Exception as e:
            log(f"Auto-rescore error: {e}")


# ── Main ──────────────────────────────────────────────────

async def main():
    log("=" * 60)
    log("SHARPWALLET SCHEDULER")
    log(f"Job poll interval: {JOB_POLL_INTERVAL}s")
    log(f"Auto-rescore interval: {RESCORE_INTERVAL_HOURS}h")
    log("=" * 60)

    # Start background threads
    refresh_thread = threading.Thread(
        target=periodic_refresh, args=(300,), daemon=True, name="wallet-refresh"
    )
    refresh_thread.start()
    log("Started wallet refresh thread (every 5m)")

    job_thread = threading.Thread(
        target=job_processor, args=(JOB_POLL_INTERVAL,), daemon=True, name="job-processor"
    )
    job_thread.start()
    log("Started job processor thread")

    rescore_thread = threading.Thread(
        target=auto_rescore, args=(RESCORE_INTERVAL_HOURS,), daemon=True, name="auto-rescore"
    )
    rescore_thread.start()
    log("Started auto-rescore thread")

    # Start WebSocket monitor in main async loop
    log("Starting WebSocket trade monitor...")
    try:
        from ws_monitor import monitor_trades
        await monitor_trades()
    except Exception as e:
        log(f"WebSocket monitor error: {e}")
        log("WebSocket failed — scheduler will continue with job processing only")

        # Keep running so job processor and auto-rescore can work
        while running:
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
