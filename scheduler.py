import asyncio
import os
from datetime import datetime, timezone

RESCORE_INTERVAL_HOURS = int(os.environ.get("RESCORE_INTERVAL_HOURS", "168"))

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print("[" + ts + "] " + msg, flush=True)

async def run_leaderboard_batch():
    log("Starting leaderboard batch scoring run...")
    try:
        from leaderboard_scraper import run_batch
        await run_batch()
        log("Batch scoring complete")
    except Exception as e:
        log("Batch scoring failed: " + str(e))
        raise

async def main():
    log("==================================================")
    log("SharpWallet Scheduler Starting")
    log("Rescore interval: every " + str(RESCORE_INTERVAL_HOURS) + "h")
    log("==================================================")

    run_count = 0

    while True:
        run_count += 1
        log("Starting run #" + str(run_count))

        try:
            await run_leaderboard_batch()
        except Exception as e:
            log("Run #" + str(run_count) + " failed: " + str(e))
            log("Will retry next scheduled interval")

        log("Sleeping " + str(RESCORE_INTERVAL_HOURS) + "h until next run...")
        await asyncio.sleep(RESCORE_INTERVAL_HOURS * 3600)

if __name__ == "__main__":
    asyncio.run(main())
