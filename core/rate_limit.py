"""
Rate limiter backed by Vercel KV (Upstash Redis).

Limits per hashed-IP:
  - 3 audits per rolling hour
  - 10 audits per rolling day

If KV is not configured (env vars missing), rate limiting is skipped.
This keeps local development friction-free while still securing prod.
"""
import os
import time
import httpx


class RateLimitExceeded(Exception):
    """Raised when an IP exceeds the rate limit."""
    pass


HOURLY_LIMIT = 3
DAILY_LIMIT = 10
HOUR_SECONDS = 3600
DAY_SECONDS = 86400


def _kv_config():
    """Return (url, token) or (None, None) if KV is not configured."""
    url = os.environ.get("KV_REST_API_URL")
    token = os.environ.get("KV_REST_API_TOKEN")
    if not url or not token:
        return None, None
    return url, token


async def _kv_pipeline(commands):
    """
    Execute multiple Redis commands in a single HTTP round-trip.
    Vercel KV (Upstash) exposes a /pipeline endpoint that accepts an
    array of command arrays and returns an array of results.
    """
    url, token = _kv_config()
    if not url:
        return None

    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            r = await client.post(
                f"{url}/pipeline",
                headers={"Authorization": f"Bearer {token}"},
                json=commands,
            )
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            return None


async def check_rate_limit(ip_hash):
    """
    Sliding-window-ish rate limit using two counters per IP.

    Strategy: maintain two keys per IP — an hourly counter and a daily
    counter. Each gets INCR'd, and on first INCR we set EXPIRE so the
    counter auto-resets. If either exceeds its limit, raise.

    This is not a true sliding window — it's a fixed-window approximation
    that's good enough for marketing-grade rate limiting. A determined
    attacker could send 6 requests across a window boundary, which is
    fine for our threat model.
    """
    if not ip_hash:
        return  # Cannot rate-limit without an IP

    url, _ = _kv_config()
    if not url:
        return  # KV not configured — skip (dev mode)

    hour_key = f"rl:h:{ip_hash}"
    day_key = f"rl:d:{ip_hash}"

    # Pipeline: increment both counters, then read TTL on both
    results = await _kv_pipeline([
        ["INCR", hour_key],
        ["INCR", day_key],
        ["TTL", hour_key],
        ["TTL", day_key],
    ])

    if results is None:
        return  # KV unreachable — fail open rather than block legit users

    try:
        hour_count = int(results[0]["result"])
        day_count = int(results[1]["result"])
        hour_ttl = int(results[2]["result"])
        day_ttl = int(results[3]["result"])
    except (KeyError, ValueError, TypeError):
        return  # Malformed response — fail open

    # On first increment, the TTL is -1 (no expiry yet). Set it.
    expire_commands = []
    if hour_ttl < 0:
        expire_commands.append(["EXPIRE", hour_key, HOUR_SECONDS])
    if day_ttl < 0:
        expire_commands.append(["EXPIRE", day_key, DAY_SECONDS])
    if expire_commands:
        await _kv_pipeline(expire_commands)

    if hour_count > HOURLY_LIMIT:
        raise RateLimitExceeded(
            f"Rate limit reached: {HOURLY_LIMIT} audits per hour. Please try again later."
        )

    if day_count > DAILY_LIMIT:
        raise RateLimitExceeded(
            f"Rate limit reached: {DAILY_LIMIT} audits per day. Please try again tomorrow."
        )
