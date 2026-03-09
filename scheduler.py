“””
SharpWallet — Scheduler
The main entry point for Railway. Runs the leaderboard batch scorer
once on startup, then weekly thereafter. Keeps the process alive 24/7.

Railway runs this via: worker: python scheduler.py
“””

import asyncio
import os
from datetime import datetime, timezone

# How often to re-score the leaderboard (in hours)

RESCORE_INTERVAL_HOURS = int(os.environ.get(“RESCORE_INTERVAL_HOURS”, 168))  # 7 days

def log(msg: str):
ts = datetime.now(timezone.utc).strftime(”%Y-%m-%d %H:%M:%S UTC”)
print(f”[{ts}] {msg}”, flush=True)

async def run_leaderboard_batch():
“”“Import and run the batch scorer.”””
log(“🚀 Starting leaderboard batch scoring run…”)
try:
from leaderboard_scraper import run_batch
await run_batch()
log(“✅ Batch scoring complete”)
except Exception as e:
log(f”❌ Batch scoring failed: {e}”)
raise

async def main():
log(”=” * 50)
log(”  SharpWallet Scheduler Starting”)
log(f”  Rescore interval: every {RESCORE_INTERVAL_HOURS}h”)
log(”=” * 50)

```
run_count = 0

while True:
    run_count += 1
    log(f"📅 Starting run #{run_count}")

    try:
        await run_leaderboard_batch()
    except Exception as e:
        log(f"❌ Run #{run_count} failed: {e}")
        log("  Will retry next scheduled interval")

    next_run_hours = RESCORE_INTERVAL_HOURS
    log(f"💤 Sleeping {next_run_hours}h until next run...")
    log(f"   Next run at approximately: "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')} + {next_run_hours}h")

    await asyncio.sleep(next_run_hours * 3600)
```

if **name** == “**main**”:
asyncio.run(main())
