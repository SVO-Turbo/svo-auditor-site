"""
SVO Auditor — main audit endpoint
POST /api/audit { url: string }
Returns Tier 1 audit data (no fix directives).
"""
import json
import os
import sys
import asyncio
import hashlib
from datetime import datetime, timezone
from dataclasses import asdict

# Make the core/ package importable from within Vercel's function runtime.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler

from core.validator import validate_url, ValidationError
from core.rate_limit import check_rate_limit, RateLimitExceeded
from core.engine_l1_l2 import run_l1_l2_checks
from core.engine_l3_l4 import run_l3_l4_checks
from core.reporter import generate_markdown, push_to_github

import httpx


# ─── Category metadata ─────────────────────────────────────────
CATEGORY_LABELS = {
    "technical": "Technical Foundation",
    "on-page": "On-Page SEO",
    "structured-data": "Structured Data",
    "ai-readiness": "AI Readiness",
}

CATEGORY_MAX = {
    "technical": 28,
    "on-page": 30,
    "structured-data": 22,
    "ai-readiness": 20,
}


def _calculate_scores(checks):
    """Sum points by category and overall. Apply SSL hard cap if applicable."""
    by_category = {k: {"score": 0, "max": v, "label": CATEGORY_LABELS[k]} for k, v in CATEGORY_MAX.items()}

    total_score = 0
    total_max = 100
    ssl_failed = False

    for c in checks:
        cat = c.category
        if cat in by_category:
            by_category[cat]["score"] += c.points_earned
        total_score += c.points_earned

        if c.id == "https_ssl" and c.status == "fail":
            ssl_failed = True

    # Hard cap: HTTPS failure caps total at 40
    ssl_hard_cap_applied = False
    if ssl_failed and total_score > 40:
        total_score = 40
        ssl_hard_cap_applied = True

    for cat_data in by_category.values():
        cat_data["pct"] = round((cat_data["score"] / cat_data["max"]) * 100) if cat_data["max"] else 0

    return total_score, total_max, by_category, ssl_hard_cap_applied


def _build_fix_queue_preview(checks, limit=3):
    """Top N failed checks by points recoverable, for the Tier 1 preview."""
    problems = [c for c in checks if c.status != "pass"]
    problems.sort(key=lambda c: (c.points_possible - c.points_earned), reverse=True)
    return [
        {
            "name": c.name,
            "points_recoverable": c.points_possible - c.points_earned,
        }
        for c in problems[:limit]
    ]


def _serialize_tier1(check):
    """Strip fix/verify before sending to the client."""
    return {
        "id": check.id,
        "name": check.name,
        "category": check.category,
        "weight": check.weight,
        "points_earned": check.points_earned,
        "points_possible": check.points_possible,
        "status": check.status,
        "detail": check.detail,
        # NOTE: fix and verify are intentionally omitted.
    }


def _serialize_full(check):
    """Full check data — used for Supabase storage, not the response."""
    return asdict(check)


def _get_client_ip(headers):
    """Extract client IP from Vercel headers."""
    # Vercel forwards real client IP in x-forwarded-for (first entry)
    fwd = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return headers.get("x-real-ip") or headers.get("X-Real-IP") or "0.0.0.0"


async def _store_report_in_supabase(report_id, domain, score, full_checks):
    """Insert full check data into Supabase reports table for later Tier 2 unlock."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return  # Fail silently — audit still succeeds

    endpoint = f"{url}/rest/v1/reports"
    payload = {
        "id": report_id,
        "domain": domain,
        "score": score,
        "checks": full_checks,
        "unlocked": False,
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                endpoint,
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=payload,
            )
    except Exception:
        pass  # Don't fail the audit if storage fails


async def _run_audit(url, client_ip):
    """Main audit orchestration."""
    # 1. Validate URL (SSRF protection, DNS check, etc.)
    validated = validate_url(url)

    # 2. Rate limit check (hashed IP)
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()
    await check_rate_limit(ip_hash)

    # 3. Run checks in parallel where possible
    l1_l2_checks, html, soup = await run_l1_l2_checks(validated)
    l3_l4_checks = await run_l3_l4_checks(validated, html, soup)
    all_checks = l1_l2_checks + l3_l4_checks

    # 4. Calculate scores
    score, max_score, categories, ssl_cap = _calculate_scores(all_checks)

    # 5. Generate report ID + timestamp
    report_id = hashlib.sha256(
        f"{validated.url}{datetime.now(timezone.utc).isoformat()}".encode()
    ).hexdigest()
    # Reformat to UUID-like
    report_id = f"{report_id[:8]}-{report_id[8:12]}-{report_id[12:16]}-{report_id[16:20]}-{report_id[20:32]}"
    timestamp = datetime.now(timezone.utc).isoformat()

    # 6. Generate Markdown + push to GitHub (fail silently)
    full_checks_serialized = [_serialize_full(c) for c in all_checks]
    md = generate_markdown(
        domain=validated.hostname,
        url=validated.url,
        score=score,
        max_score=max_score,
        categories=categories,
        checks=all_checks,
        ssl_cap=ssl_cap,
        timestamp=timestamp,
    )
    await push_to_github(validated.hostname, timestamp, md)

    # 7. Store full check data for Tier 2 unlock
    await _store_report_in_supabase(report_id, validated.hostname, score, full_checks_serialized)

    # 8. Return Tier 1 response (no fix/verify)
    return {
        "report_id": report_id,
        "timestamp": timestamp,
        "url": validated.url,
        "domain": validated.hostname,
        "score": score,
        "max_score": max_score,
        "ssl_hard_cap_applied": ssl_cap,
        "categories": categories,
        "checks": [_serialize_tier1(c) for c in all_checks],
        "fix_queue_preview": _build_fix_queue_preview(all_checks),
    }


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 2048:
                return self._send_json(413, {"error": "Request too large."})

            raw = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "Invalid JSON."})

            url = (payload.get("url") or "").strip()
            if not url:
                return self._send_json(400, {"error": "A url is required."})

            client_ip = _get_client_ip(self.headers)
            result = asyncio.run(_run_audit(url, client_ip))
            self._send_json(200, result)

        except ValidationError as e:
            self._send_json(400, {"error": str(e)})
        except RateLimitExceeded as e:
            self._send_json(429, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": f"Audit failed: {type(e).__name__}"})
