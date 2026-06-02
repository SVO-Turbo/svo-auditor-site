"""
SVO Auditor — email capture + Tier 2 unlock endpoint
POST /api/capture { report_id: string, email: string }
Returns full check data including fix/verify directives.
"""
import json
import os
import sys
import re
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler
import httpx


# ─── Email validation ────────────────────────────────────────
# Pragmatic email regex — RFC 5322 in full is overkill for marketing capture.
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# Block disposable email services. List is short by design — covers the
# obvious ones without becoming a maintenance burden.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.info",
    "tempmail.com", "temp-mail.org", "10minutemail.com", "10minutemail.net",
    "throwaway.email", "yopmail.com", "yopmail.fr", "yopmail.net",
    "trashmail.com", "fakeinbox.com", "dispostable.com", "maildrop.cc",
    "getnada.com", "mintemail.com", "mohmal.com", "tempr.email",
    "tempmailaddress.com", "throwawaymail.com", "sharklasers.com",
    "grr.la", "spam4.me", "mailnesia.com", "spamgourmet.com",
    "mvrht.com", "incognitomail.org",
}

UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.IGNORECASE)


def _validate_email(email):
    """Return (is_valid, reason) tuple."""
    if not email or len(email) > 254:
        return False, "Email is required."
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return False, "Invalid email format."
    domain = email.split("@", 1)[1]
    if domain in DISPOSABLE_DOMAINS:
        return False, "Disposable email addresses are not accepted."
    return True, email


async def _fetch_report(report_id):
    """Get the full check payload from Supabase reports table."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return None

    endpoint = f"{url}/rest/v1/reports"
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(
            endpoint,
            params={"id": f"eq.{report_id}", "select": "id,domain,score,checks"},
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
            },
        )
        if r.status_code != 200:
            return None
        rows = r.json()
        return rows[0] if rows else None


async def _mark_unlocked(report_id, email):
    """Set unlocked=true and store email on the report row."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return

    endpoint = f"{url}/rest/v1/reports"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.patch(
                endpoint,
                params={"id": f"eq.{report_id}"},
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={"unlocked": True, "email": email},
            )
    except Exception:
        pass


async def _insert_lead(email, domain, score, report_id):
    """Insert a lead row into the audits table."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return

    endpoint = f"{url}/rest/v1/audits"
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
                json={
                    "email": email,
                    "domain": domain,
                    "score": score,
                    "report_id": report_id,
                },
            )
    except Exception:
        pass


async def _handle_capture(report_id, email):
    # Validate UUID format
    if not UUID_RE.match(report_id):
        raise ValueError("Invalid report ID format.")

    # Validate email
    ok, result = _validate_email(email)
    if not ok:
        raise ValueError(result)
    email = result

    # Fetch the full report
    report = await _fetch_report(report_id)
    if not report:
        raise LookupError("Report not found or expired.")

    # Run lead insert + unlock mark in parallel
    await asyncio.gather(
        _insert_lead(email, report["domain"], report["score"], report_id),
        _mark_unlocked(report_id, email),
    )

    # Return full checks (with fix + verify)
    return {
        "domain": report["domain"],
        "score": report["score"],
        "checks": report["checks"],
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
            if length > 1024:
                return self._send_json(413, {"error": "Request too large"})

            raw = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "Invalid JSON"})

            report_id = (payload.get("report_id") or "").strip()
            email = (payload.get("email") or "").strip()

            result = asyncio.run(_handle_capture(report_id, email))
            self._send_json(200, result)

        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except LookupError as e:
            self._send_json(404, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": f"Capture failed: {type(e).__name__}"})
