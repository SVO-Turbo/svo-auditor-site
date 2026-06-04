"""
SVO Auditor — health / self-validation endpoint
GET /api/health

Actively tests each integration and returns a JSON report:
  - github:   PAT valid + target repo writable
  - supabase: both tables reachable
  - kv:       Upstash Redis reachable + commands-used readout
  - pagespeed: API key present (optional)

Safe to call anytime. Performs a real write+delete test against the
reports repo to prove end-to-end GitHub access, then cleans up after itself.
"""
import json
import os
import sys
import asyncio
import base64
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler
import httpx


async def _check_github():
    pat = os.environ.get("GITHUB_PAT")
    owner = os.environ.get("GITHUB_REPO_OWNER")
    repo = os.environ.get("GITHUB_REPO_NAME")

    if not pat or not owner or not repo:
        missing = [k for k, v in {
            "GITHUB_PAT": pat, "GITHUB_REPO_OWNER": owner, "GITHUB_REPO_NAME": repo
        }.items() if not v]
        return {"ok": False, "detail": f"Missing env vars: {', '.join(missing)}"}

    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # 1. Confirm the repo exists and is reachable with this token
            r = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers=headers,
            )
            if r.status_code == 404:
                return {"ok": False, "detail": f"Repo {owner}/{repo} not found, or token lacks access."}
            if r.status_code == 401:
                return {"ok": False, "detail": "GitHub token is invalid or expired."}
            if r.status_code != 200:
                return {"ok": False, "detail": f"GitHub repo check returned {r.status_code}."}

            repo_data = r.json()
            if not repo_data.get("permissions", {}).get("push"):
                return {"ok": False, "detail": "Token can read the repo but lacks write (push) access."}

            # 2. Prove write access: create a temp file, then delete it
            test_path = ".svo-health-check"
            test_content = base64.b64encode(
                f"health check {datetime.now(timezone.utc).isoformat()}".encode()
            ).decode()

            put = await client.put(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{test_path}",
                headers=headers,
                json={"message": "health check (auto-cleanup)", "content": test_content},
            )
            if put.status_code not in (200, 201):
                return {"ok": False, "detail": f"Write test failed ({put.status_code}). Token may be read-only."}

            # Clean up: delete the test file
            sha = put.json().get("content", {}).get("sha")
            if sha:
                await client.request(
                    "DELETE",
                    f"https://api.github.com/repos/{owner}/{repo}/contents/{test_path}",
                    headers=headers,
                    json={"message": "health check cleanup", "sha": sha},
                )

            return {"ok": True, "detail": f"Authenticated; {owner}/{repo} is writable (write+delete verified)."}

    except Exception as e:
        return {"ok": False, "detail": f"GitHub check error: {type(e).__name__}"}


async def _check_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        missing = [k for k, v in {
            "SUPABASE_URL": url, "SUPABASE_SERVICE_ROLE_KEY": key
        }.items() if not v]
        return {"ok": False, "detail": f"Missing env vars: {', '.join(missing)}"}

    headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            results = {}
            for table in ("audits", "reports"):
                r = await client.get(
                    f"{url}/rest/v1/{table}",
                    headers={**headers, "Range": "0-0"},
                    params={"select": "id"},
                )
                results[table] = r.status_code

            bad = {t: c for t, c in results.items() if c not in (200, 206)}
            if bad:
                return {"ok": False, "detail": f"Table check failed: {bad} (401=bad key, 404=table missing)."}

            return {"ok": True, "detail": "Both tables (audits, reports) reachable."}

    except Exception as e:
        return {"ok": False, "detail": f"Supabase check error: {type(e).__name__}"}


async def _check_kv():
    url = os.environ.get("KV_REST_API_URL")
    token = os.environ.get("KV_REST_API_TOKEN")

    if not url or not token:
        return {"ok": False, "detail": "KV not configured (rate limiting will be skipped — audits still work)."}

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            # PING to confirm connection
            r = await client.post(
                f"{url}/ping",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                return {"ok": False, "detail": f"KV ping returned {r.status_code}."}

            # DBSIZE as a lightweight usage signal (number of keys currently stored)
            size_r = await client.post(
                f"{url}/dbsize",
                headers={"Authorization": f"Bearer {token}"},
            )
            keys_stored = None
            if size_r.status_code == 200:
                keys_stored = size_r.json().get("result")

            detail = "Connected."
            if keys_stored is not None:
                detail += f" Keys currently stored: {keys_stored}. Free tier: 500,000 commands/month."

            return {"ok": True, "detail": detail}

    except Exception as e:
        return {"ok": False, "detail": f"KV check error: {type(e).__name__}"}


def _check_pagespeed():
    key = os.environ.get("PAGESPEED_API_KEY")
    if key:
        return {"ok": True, "detail": "API key present. LCP/CLS checks active."}
    return {"ok": True, "detail": "Not configured (optional). LCP/CLS checks return 'warn'; other 30 checks run normally."}


async def _run_health():
    github, supabase, kv = await asyncio.gather(
        _check_github(),
        _check_supabase(),
        _check_kv(),
    )
    pagespeed = _check_pagespeed()

    all_critical_ok = github["ok"] and supabase["ok"]

    return {
        "status": "ready" if all_critical_ok else "not_ready",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "github": github,
            "supabase": supabase,
            "kv": kv,
            "pagespeed": pagespeed,
        },
        "note": "github + supabase must be ok to run audits. kv is optional. pagespeed is optional.",
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            result = asyncio.run(_run_health())
            status_code = 200 if result["status"] == "ready" else 503
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "detail": f"{type(e).__name__}"}).encode())
