"""
Vercel Python serverless function — rescore job queue API.

POST /api/rescore            -> create a rescore job (returns job ID)
GET  /api/rescore             -> return latest job status
GET  /api/rescore?id=123      -> return specific job status

No scoring logic here — Railway's scheduler processes jobs in background.
"""
import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def sb_query(table, params=""):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{params}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        timeout=10,
    )
    return r.json() if r.ok else []


def sb_insert(table, data):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=SB_HEADERS,
        json=data,
        timeout=10,
    )
    if r.ok:
        try:
            return r.json()
        except Exception:
            return data
    return None


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
        """Return job status."""
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        job_id = qs.get("id", [None])[0]

        if job_id:
            # Specific job
            jobs = sb_query("rescore_jobs", f"id=eq.{job_id}&limit=1")
        else:
            # Latest job
            jobs = sb_query("rescore_jobs", "order=created_at.desc&limit=1")

        if jobs:
            job = jobs[0] if isinstance(jobs, list) else jobs
            self._json(200, {"ok": True, "job": job})
        else:
            self._json(200, {"ok": True, "job": None})

    def do_POST(self):
        """Create a new rescore job."""
        # Count wallets to score
        wallets = sb_query("wallets", "select=address&limit=500")
        total = len(wallets) if isinstance(wallets, list) else 0

        if total == 0:
            self._json(200, {"ok": False, "error": "No wallets found"})
            return

        # Check if there's already a pending/running job
        active = sb_query("rescore_jobs", "status=in.(pending,running)&limit=1")
        if active and isinstance(active, list) and len(active) > 0:
            self._json(200, {"ok": True, "job": active[0], "message": "Job already queued"})
            return

        # Create new job
        result = sb_insert("rescore_jobs", {
            "status": "pending",
            "total_wallets": total,
            "scored": 0,
            "failed": 0,
        })

        if result:
            job = result[0] if isinstance(result, list) else result
            self._json(200, {"ok": True, "job": job})
        else:
            self._json(500, {"ok": False, "error": "Failed to create job"})
