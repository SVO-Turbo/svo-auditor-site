"""
Engine for Layers 1 (Technical Foundation) and 2 (On-Page SEO).
16 checks, 58 points total.

Run with: l1_l2_checks, html, soup = await run_l1_l2_checks(validated_url)
The returned html and soup are reused by engine_l3_l4 to avoid re-parsing.
"""
import asyncio
import os
import re
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup


# ─── Shared data structure ─────────────────────────────────────
@dataclass
class CheckResult:
    id: str
    name: str
    category: str
    weight: str
    points_earned: int
    points_possible: int
    status: str           # pass | fail | warn
    detail: str           # Tier 1 visible
    fix: str = ""         # Tier 2 — behind email gate
    verify: str = ""      # Tier 2 — behind email gate


# ─── HTTP fetch with redirect chain tracking ───────────────────
USER_AGENT = "SVO-Auditor/2.0 (+https://svo-turbo.github.io/svo-auditor-site/)"
FETCH_TIMEOUT = 15.0
MAX_REDIRECTS = 5
MAX_HTML_BYTES = 5 * 1024 * 1024  # 5 MB cap


async def _fetch_page(url):
    """
    Fetch the page. Returns dict with:
      - status_code, html, headers, ttfb_ms, redirect_chain, ssl_ok, ssl_error
    """
    result = {
        "status_code": None,
        "html": "",
        "headers": {},
        "ttfb_ms": None,
        "redirect_chain": [],
        "ssl_ok": True,
        "ssl_error": None,
        "final_url": url,
        "error": None,
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=FETCH_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            start = time.monotonic()
            try:
                r = await client.get(url)
            except httpx.ConnectError as e:
                if "SSL" in str(e) or "ssl" in str(e).lower() or "certificate" in str(e).lower():
                    result["ssl_ok"] = False
                    result["ssl_error"] = str(e)
                    result["error"] = "ssl_failure"
                    return result
                raise

            elapsed_ms = (time.monotonic() - start) * 1000

            result["status_code"] = r.status_code
            result["headers"] = dict(r.headers)
            result["ttfb_ms"] = elapsed_ms
            result["final_url"] = str(r.url)
            result["redirect_chain"] = [str(h.url) for h in r.history] + [str(r.url)]

            # Cap HTML size
            content = r.content[:MAX_HTML_BYTES]
            # Decode with encoding fallback
            try:
                result["html"] = content.decode(r.encoding or "utf-8", errors="replace")
            except (LookupError, TypeError):
                result["html"] = content.decode("utf-8", errors="replace")

    except httpx.TimeoutException:
        result["error"] = "timeout"
    except httpx.RequestError as e:
        result["error"] = f"request_error: {type(e).__name__}"
    except Exception as e:
        result["error"] = f"unexpected: {type(e).__name__}"

    return result


# ─── PageSpeed Insights (optional) ────────────────────────────
async def _fetch_pagespeed(url):
    """
    Fetch LCP/CLS from Google PageSpeed Insights API.
    Returns None if API key missing or call fails — checks fall back to 'warn'.
    """
    api_key = os.environ.get("PAGESPEED_API_KEY")
    if not api_key:
        return None

    endpoint = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    params = {
        "url": url,
        "key": api_key,
        "strategy": "mobile",
        "category": ["performance"],
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(endpoint, params=params)
            if r.status_code != 200:
                return None
            data = r.json()
            audits = data.get("lighthouseResult", {}).get("audits", {})
            return {
                "lcp_ms": audits.get("largest-contentful-paint", {}).get("numericValue"),
                "cls": audits.get("cumulative-layout-shift", {}).get("numericValue"),
            }
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# LAYER 1 — TECHNICAL FOUNDATION (28 pts)
# ═══════════════════════════════════════════════════════════════

def _check_https_ssl(validated, page):
    """10 pts — HTTPS scheme + valid SSL certificate. HARD CAP if failed."""
    if validated.scheme != "https":
        return CheckResult(
            id="https_ssl", name="HTTPS / Valid SSL", category="technical", weight="critical",
            points_earned=0, points_possible=10, status="fail",
            detail="Site is served over HTTP. Score is capped at 40 until HTTPS is enabled.",
            fix="Provision a free SSL certificate via Let's Encrypt (certbot) or your hosting provider (Cloudflare, Vercel, Netlify all offer one-click HTTPS). Then enforce HTTPS by adding a 301 redirect from http:// to https:// at the server or CDN level.",
            verify="Run `curl -I https://yourdomain.com` and confirm a 200 OK response. Run `curl -I http://yourdomain.com` and confirm a 301 redirect to the https:// version.",
        )

    if not page.get("ssl_ok", True):
        return CheckResult(
            id="https_ssl", name="HTTPS / Valid SSL", category="technical", weight="critical",
            points_earned=0, points_possible=10, status="fail",
            detail=f"SSL certificate error: {page.get('ssl_error', 'unknown')[:120]}. Score is capped at 40.",
            fix="Your SSL certificate is invalid, expired, or misconfigured. Common causes: expired certificate, hostname mismatch, missing intermediate certificate, self-signed cert. Renew via Let's Encrypt or your CA, and verify the full certificate chain is served.",
            verify="Test at ssllabs.com/ssltest — you should get an A grade. Also verify with `openssl s_client -connect yourdomain.com:443 -servername yourdomain.com`.",
        )

    return CheckResult(
        id="https_ssl", name="HTTPS / Valid SSL", category="technical", weight="critical",
        points_earned=10, points_possible=10, status="pass",
        detail="Site is served over HTTPS with a valid certificate.",
    )


def _check_ttfb(page):
    """6 pts — Time to First Byte under 600ms."""
    ttfb = page.get("ttfb_ms")
    if ttfb is None:
        return CheckResult(
            id="ttfb", name="TTFB Under 600ms", category="technical", weight="high",
            points_earned=0, points_possible=6, status="fail",
            detail="Could not measure TTFB — page fetch failed.",
            fix="Page could not be fetched. Investigate connection errors, then re-run the audit.",
            verify="Run `curl -w '%{time_starttransfer}\\n' -o /dev/null -s https://yourdomain.com` — the value should be under 0.6 seconds.",
        )

    if ttfb < 600:
        return CheckResult(
            id="ttfb", name="TTFB Under 600ms", category="technical", weight="high",
            points_earned=6, points_possible=6, status="pass",
            detail=f"TTFB measured at {int(ttfb)}ms.",
        )

    earned = 3 if ttfb < 1200 else 0
    status = "warn" if ttfb < 1200 else "fail"
    return CheckResult(
        id="ttfb", name="TTFB Under 600ms", category="technical", weight="high",
        points_earned=earned, points_possible=6, status=status,
        detail=f"TTFB measured at {int(ttfb)}ms. Target is under 600ms.",
        fix="Slow TTFB usually means slow origin server response. Common fixes: (1) Enable CDN caching (Cloudflare, Fastly, Vercel Edge). (2) Add server-side caching (Redis, Memcached). (3) Optimize slow database queries. (4) Upgrade hosting tier if you're on a shared/low-end plan.",
        verify="Re-test with `curl -w '%{time_starttransfer}\\n' -o /dev/null -s https://yourdomain.com` from multiple locations using webpagetest.org. Target: under 600ms at the 75th percentile.",
    )


def _check_lcp(pagespeed):
    """5 pts — Largest Contentful Paint under 2.5s (from PageSpeed API)."""
    if pagespeed is None:
        return CheckResult(
            id="lcp", name="LCP Under 2.5s", category="technical", weight="high",
            points_earned=0, points_possible=5, status="warn",
            detail="PageSpeed Insights API not configured — LCP could not be measured.",
            fix="Configure the PAGESPEED_API_KEY environment variable to enable Core Web Vitals checks. Get a free key at console.cloud.google.com → enable PageSpeed Insights API.",
            verify="Run a new audit after configuring the key — this check will switch to pass/fail.",
        )

    lcp = pagespeed.get("lcp_ms")
    if lcp is None:
        return CheckResult(
            id="lcp", name="LCP Under 2.5s", category="technical", weight="high",
            points_earned=0, points_possible=5, status="warn",
            detail="LCP data not returned by PageSpeed API.",
            fix="The page may be too slow to load or blocking the PageSpeed API. Check pagespeed.web.dev manually.",
            verify="Run the page through pagespeed.web.dev directly — confirm Lighthouse can complete the analysis.",
        )

    if lcp < 2500:
        return CheckResult(
            id="lcp", name="LCP Under 2.5s", category="technical", weight="high",
            points_earned=5, points_possible=5, status="pass",
            detail=f"LCP measured at {int(lcp)}ms (target: under 2500ms).",
        )

    earned = 2 if lcp < 4000 else 0
    status = "warn" if lcp < 4000 else "fail"
    return CheckResult(
        id="lcp", name="LCP Under 2.5s", category="technical", weight="high",
        points_earned=earned, points_possible=5, status=status,
        detail=f"LCP measured at {int(lcp)}ms (target: under 2500ms).",
        fix="LCP is dominated by the largest visible element loading. Common fixes: (1) Preload the LCP image with `<link rel='preload' as='image' href='hero.jpg'>`. (2) Serve the LCP image in modern format (WebP, AVIF) with proper width/height attributes. (3) Defer non-critical CSS. (4) Eliminate render-blocking JavaScript. (5) Use a CDN for the LCP asset.",
        verify="Re-test at pagespeed.web.dev — LCP should drop below 2.5 seconds on mobile.",
    )


def _check_cls(pagespeed):
    """3 pts — Cumulative Layout Shift under 0.1 (from PageSpeed API)."""
    if pagespeed is None:
        return CheckResult(
            id="cls", name="CLS Under 0.1", category="technical", weight="med",
            points_earned=0, points_possible=3, status="warn",
            detail="PageSpeed Insights API not configured — CLS could not be measured.",
            fix="Configure the PAGESPEED_API_KEY environment variable to enable Core Web Vitals checks.",
            verify="Re-run the audit after configuring the key.",
        )

    cls = pagespeed.get("cls")
    if cls is None:
        return CheckResult(
            id="cls", name="CLS Under 0.1", category="technical", weight="med",
            points_earned=0, points_possible=3, status="warn",
            detail="CLS data not returned by PageSpeed API.",
            fix="Check pagespeed.web.dev manually.",
            verify="Confirm CLS appears in the Lighthouse report at pagespeed.web.dev.",
        )

    if cls < 0.1:
        return CheckResult(
            id="cls", name="CLS Under 0.1", category="technical", weight="med",
            points_earned=3, points_possible=3, status="pass",
            detail=f"CLS measured at {cls:.3f} (target: under 0.1).",
        )

    earned = 1 if cls < 0.25 else 0
    status = "warn" if cls < 0.25 else "fail"
    return CheckResult(
        id="cls", name="CLS Under 0.1", category="technical", weight="med",
        points_earned=earned, points_possible=3, status=status,
        detail=f"CLS measured at {cls:.3f} (target: under 0.1).",
        fix="Layout shift comes from elements moving as the page loads. Fix by: (1) Adding explicit width/height attributes to every <img> and <video>. (2) Reserving space for ads, embeds, and iframes with CSS aspect-ratio or fixed dimensions. (3) Avoiding inserting content above existing content. (4) Using transform animations instead of properties that trigger layout (top, left, width, height).",
        verify="Re-test at pagespeed.web.dev — CLS should drop below 0.1.",
    )


def _check_viewport(soup):
    """2 pts — Mobile viewport meta tag present."""
    tag = soup.find("meta", attrs={"name": "viewport"})
    if tag and tag.get("content"):
        content = tag["content"].lower()
        if "width=device-width" in content:
            return CheckResult(
                id="viewport", name="Mobile Viewport", category="technical", weight="med",
                points_earned=2, points_possible=2, status="pass",
                detail=f"Viewport meta tag found: {tag['content']}",
            )

    return CheckResult(
        id="viewport", name="Mobile Viewport", category="technical", weight="med",
        points_earned=0, points_possible=2, status="fail",
        detail="No mobile viewport meta tag found. Page will not render correctly on mobile devices.",
        fix='Add this inside your <head> section: <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        verify="View page source — confirm the meta tag is present. Test on mobile.dev → Mobile-Friendly Test.",
    )


def _check_redirect_chain(page):
    """1 pt — No more than 1 redirect hop."""
    chain = page.get("redirect_chain", [])
    hops = len(chain) - 1 if chain else 0

    if hops <= 1:
        return CheckResult(
            id="redirect_chain", name="No Redirect Chains", category="technical", weight="low",
            points_earned=1, points_possible=1, status="pass",
            detail=f"Page reached in {hops} redirect{'s' if hops != 1 else ''}.",
        )

    return CheckResult(
        id="redirect_chain", name="No Redirect Chains", category="technical", weight="low",
        points_earned=0, points_possible=1, status="fail",
        detail=f"Page reached after {hops} redirect hops. Long redirect chains slow page load and waste crawl budget.",
        fix=f"Redirect chain detected: {' → '.join(chain[:4])}{'...' if len(chain) > 4 else ''}. Update internal links to point directly to the final URL. At your server/CDN level, replace multi-step redirects with a single direct 301.",
        verify="Run `curl -ILs https://yourdomain.com | grep -i 'HTTP/\\|location'` — you should see at most one 301/302 before the final 200.",
    )


def _check_mixed_content(html, validated):
    """1 pt — No mixed content (HTTP resources on HTTPS page)."""
    if validated.scheme != "https":
        return CheckResult(
            id="mixed_content", name="No Mixed Content", category="technical", weight="low",
            points_earned=0, points_possible=1, status="fail",
            detail="Cannot evaluate mixed content — site is HTTP. Resolve HTTPS first.",
            fix="See the HTTPS/SSL fix above.",
            verify="After enabling HTTPS, re-run this audit.",
        )

    # Find http:// URLs in src= or href= attributes
    http_resources = re.findall(
        r'(?:src|href)\s*=\s*["\']http://[^"\']+',
        html,
        re.IGNORECASE,
    )

    if not http_resources:
        return CheckResult(
            id="mixed_content", name="No Mixed Content", category="technical", weight="low",
            points_earned=1, points_possible=1, status="pass",
            detail="No HTTP resources detected on this HTTPS page.",
        )

    return CheckResult(
        id="mixed_content", name="No Mixed Content", category="technical", weight="low",
        points_earned=0, points_possible=1, status="fail",
        detail=f"Found {len(http_resources)} HTTP resource(s) loaded on this HTTPS page. Browsers block these as mixed content.",
        fix="Replace every http:// URL with https:// (or use protocol-relative //example.com/image.jpg). Sample issues: " + "; ".join(http_resources[:3]),
        verify="View page in browser DevTools → Console — there should be no 'Mixed Content' warnings.",
    )


# ═══════════════════════════════════════════════════════════════
# LAYER 2 — ON-PAGE SEO (30 pts)
# ═══════════════════════════════════════════════════════════════

def _check_title(soup):
    """6 pts — Page title present, 50-60 chars."""
    title_tag = soup.find("title")
    if not title_tag or not title_tag.text.strip():
        return CheckResult(
            id="title", name="Page Title (50-60 chars)", category="on-page", weight="critical",
            points_earned=0, points_possible=6, status="fail",
            detail="No <title> tag found, or title is empty.",
            fix="Add a descriptive <title> tag inside <head>: <title>Primary Keyword — Brand Name</title>. Target 50-60 characters. Include your primary keyword near the start.",
            verify="View page source — confirm the <title> tag exists with descriptive content. Google's SERP will show the first ~580px.",
        )

    title = title_tag.text.strip()
    length = len(title)

    if 50 <= length <= 60:
        return CheckResult(
            id="title", name="Page Title (50-60 chars)", category="on-page", weight="critical",
            points_earned=6, points_possible=6, status="pass",
            detail=f"Title '{title[:80]}...' is {length} characters." if len(title) > 80 else f"Title '{title}' is {length} characters.",
        )

    if 30 <= length < 50 or 60 < length <= 70:
        return CheckResult(
            id="title", name="Page Title (50-60 chars)", category="on-page", weight="critical",
            points_earned=3, points_possible=6, status="warn",
            detail=f"Title is {length} characters (optimal: 50-60). Current: '{title[:80]}{'...' if len(title) > 80 else ''}'",
            fix=f"Current title is {length} chars. {'Tighten it' if length > 60 else 'Expand it'} to land in 50-60. Keep the primary keyword in the first 30 chars.",
            verify="View source. Use seo.tools/serp-preview to see how it renders in Google's results.",
        )

    return CheckResult(
        id="title", name="Page Title (50-60 chars)", category="on-page", weight="critical",
        points_earned=0, points_possible=6, status="fail",
        detail=f"Title is {length} characters (target: 50-60). Current: '{title[:80]}{'...' if len(title) > 80 else ''}'",
        fix=f"Rewrite the title to land between 50 and 60 characters. {'Currently too long — trim filler words and brand suffix.' if length > 70 else 'Currently too short — add context, location, or modifier.'}",
        verify="Preview at seo.tools/serp-preview.",
    )


def _check_meta_description(soup):
    """5 pts — Meta description 120-158 chars."""
    tag = soup.find("meta", attrs={"name": "description"})
    if not tag or not tag.get("content", "").strip():
        return CheckResult(
            id="meta_description", name="Meta Description (120-158 chars)", category="on-page", weight="high",
            points_earned=0, points_possible=5, status="fail",
            detail="No meta description tag found, or content is empty.",
            fix='Add inside <head>: <meta name="description" content="A specific, compelling 120-158 character summary of this page. Include your primary keyword and a call-to-action.">',
            verify="View source — confirm the meta description tag is present and populated.",
        )

    content = tag["content"].strip()
    length = len(content)

    if 120 <= length <= 158:
        return CheckResult(
            id="meta_description", name="Meta Description (120-158 chars)", category="on-page", weight="high",
            points_earned=5, points_possible=5, status="pass",
            detail=f"Meta description is {length} characters.",
        )

    if 80 <= length < 120 or 158 < length <= 200:
        return CheckResult(
            id="meta_description", name="Meta Description (120-158 chars)", category="on-page", weight="high",
            points_earned=2, points_possible=5, status="warn",
            detail=f"Meta description is {length} characters (optimal: 120-158).",
            fix=f"{'Trim it down' if length > 158 else 'Expand with more detail'} to land in 120-158 characters. Lead with the value proposition.",
            verify="Re-check character count after editing.",
        )

    return CheckResult(
        id="meta_description", name="Meta Description (120-158 chars)", category="on-page", weight="high",
        points_earned=0, points_possible=5, status="fail",
        detail=f"Meta description is {length} characters (target: 120-158).",
        fix="Rewrite to land between 120 and 158 characters. Include primary keyword and a clear call-to-action.",
        verify="Use ahrefs.com/serp-checker to preview how it appears in Google.",
    )


def _check_headings(soup):
    """4 pts — Single H1 and at least one H2."""
    h1s = soup.find_all("h1")
    h2s = soup.find_all("h2")
    h1_count = len(h1s)
    h2_count = len(h2s)

    if h1_count == 1 and h2_count >= 1:
        return CheckResult(
            id="headings", name="Heading Structure (1 H1 + H2s)", category="on-page", weight="high",
            points_earned=4, points_possible=4, status="pass",
            detail=f"Found 1 H1 and {h2_count} H2 tag(s).",
        )

    if h1_count == 1 and h2_count == 0:
        return CheckResult(
            id="headings", name="Heading Structure (1 H1 + H2s)", category="on-page", weight="high",
            points_earned=2, points_possible=4, status="warn",
            detail="Single H1 found, but no H2 tags. Page lacks content hierarchy.",
            fix="Add at least 2-3 H2 tags to break the page into logical sections. H2s help both readers and search engines understand content structure.",
            verify="View source — confirm at least one <h2> tag exists between the H1 and the page footer.",
        )

    issues = []
    if h1_count == 0:
        issues.append("No H1 tag found")
    elif h1_count > 1:
        issues.append(f"{h1_count} H1 tags found (should be exactly 1)")
    if h2_count == 0:
        issues.append("No H2 tags found")

    return CheckResult(
        id="headings", name="Heading Structure (1 H1 + H2s)", category="on-page", weight="high",
        points_earned=0, points_possible=4, status="fail",
        detail=". ".join(issues) + ".",
        fix="Each page needs exactly one <h1> (the main page heading) and at least one <h2> for section structure. If you have multiple H1s, demote all but the most important to H2.",
        verify="View source and count: there should be exactly 1 <h1> tag and 1+ <h2> tags.",
    )


def _check_canonical(soup, validated):
    """4 pts — Canonical URL present and self-referencing."""
    tag = soup.find("link", rel="canonical")
    if not tag or not tag.get("href"):
        return CheckResult(
            id="canonical", name="Canonical URL", category="on-page", weight="high",
            points_earned=0, points_possible=4, status="fail",
            detail="No canonical link tag found.",
            fix=f'Add inside <head>: <link rel="canonical" href="{validated.url}" />. The canonical should be the absolute URL of this page.',
            verify="View source — confirm <link rel='canonical' href='...'> is present.",
        )

    canonical = tag["href"].strip()
    # Self-referencing check (allow trailing slash diff and scheme variance)
    page_url_normalized = validated.url.rstrip("/").lower()
    canonical_normalized = canonical.rstrip("/").lower()

    if canonical_normalized == page_url_normalized:
        return CheckResult(
            id="canonical", name="Canonical URL", category="on-page", weight="high",
            points_earned=4, points_possible=4, status="pass",
            detail=f"Canonical points to this page: {canonical}",
        )

    return CheckResult(
        id="canonical", name="Canonical URL", category="on-page", weight="high",
        points_earned=2, points_possible=4, status="warn",
        detail=f"Canonical points to a different URL: {canonical}. This may be intentional if this page is a duplicate of another, but otherwise indicates a misconfiguration.",
        fix=f'If this page is the original, update the canonical to: <link rel="canonical" href="{validated.url}" />. If this page is a duplicate, the current canonical is correct.',
        verify="View source. If the canonical points elsewhere, confirm that other page is the intended primary version.",
    )


def _check_alt_text(soup):
    """3 pts — All non-decorative images have alt text."""
    images = soup.find_all("img")
    if not images:
        return CheckResult(
            id="alt_text", name="Image Alt Text", category="on-page", weight="med",
            points_earned=3, points_possible=3, status="pass",
            detail="No images on page — alt text not applicable.",
        )

    missing = [img for img in images if not img.get("alt") and img.get("alt") != ""]
    # alt="" is intentional (decorative). Only flag images with NO alt attribute at all.

    if not missing:
        return CheckResult(
            id="alt_text", name="Image Alt Text", category="on-page", weight="med",
            points_earned=3, points_possible=3, status="pass",
            detail=f"All {len(images)} image(s) have alt attributes.",
        )

    pct_missing = (len(missing) / len(images)) * 100
    earned = 1 if pct_missing < 25 else 0
    status = "warn" if pct_missing < 25 else "fail"

    sample_srcs = [img.get("src", "")[:60] for img in missing[:3]]

    return CheckResult(
        id="alt_text", name="Image Alt Text", category="on-page", weight="med",
        points_earned=earned, points_possible=3, status=status,
        detail=f"{len(missing)} of {len(images)} images ({pct_missing:.0f}%) are missing alt attributes.",
        fix=f"Add alt attributes to every <img> tag. Use descriptive alt text for content images (e.g., alt='Red running shoe on white background'). Use alt='' (empty) for decorative images. Sample missing: {sample_srcs}",
        verify="Run `grep -c '<img' page.html` vs `grep -c 'alt=' page.html` — counts should match.",
    )


def _check_internal_links(soup, validated):
    """2 pts — At least 2 internal links."""
    links = soup.find_all("a", href=True)
    internal = 0
    for a in links:
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        if href.startswith("/") and not href.startswith("//"):
            internal += 1
        elif validated.hostname in href:
            internal += 1

    if internal >= 2:
        return CheckResult(
            id="internal_links", name="Internal Links (min 2)", category="on-page", weight="low",
            points_earned=2, points_possible=2, status="pass",
            detail=f"Found {internal} internal link(s).",
        )

    return CheckResult(
        id="internal_links", name="Internal Links (min 2)", category="on-page", weight="low",
        points_earned=0, points_possible=2, status="fail",
        detail=f"Only {internal} internal link(s) found. Pages with weak internal linking get less PageRank flow.",
        fix="Add at least 2 internal links pointing to other pages on the same domain. Link to related blog posts, service pages, or your homepage from the body content.",
        verify="View source — count <a href> tags pointing to the same domain. Should be 2+.",
    )


def _check_word_count(soup):
    """2 pts — At least 300 words of body content."""
    # Remove script and style content
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    words = len(text.split())

    if words >= 300:
        return CheckResult(
            id="word_count", name="Word Count (300+)", category="on-page", weight="low",
            points_earned=2, points_possible=2, status="pass",
            detail=f"Page contains approximately {words} words of body content.",
        )

    return CheckResult(
        id="word_count", name="Word Count (300+)", category="on-page", weight="low",
        points_earned=0, points_possible=2, status="fail",
        detail=f"Page contains only {words} words. Thin content typically underperforms in search rankings.",
        fix="Expand the page content to at least 300 words. Add: a detailed value proposition, FAQ section, customer testimonials, feature explanations, or related information. Don't pad with filler — every paragraph should add value.",
        verify="Use a word counter tool on your final page copy — should exceed 300 words of substantive content.",
    )


def _check_lang_attr(soup):
    """1 pt — HTML lang attribute present."""
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        return CheckResult(
            id="lang_attr", name="HTML Lang Attribute", category="on-page", weight="low",
            points_earned=1, points_possible=1, status="pass",
            detail=f"<html> tag has lang='{html_tag['lang']}'.",
        )

    return CheckResult(
        id="lang_attr", name="HTML Lang Attribute", category="on-page", weight="low",
        points_earned=0, points_possible=1, status="fail",
        detail="No lang attribute on the <html> tag. Screen readers and search engines cannot reliably detect the page language.",
        fix='Update your <html> tag to include a lang attribute: <html lang="en"> (or your appropriate ISO 639-1 language code).',
        verify="View source — confirm <html> opening tag includes lang='xx'.",
    )


# ─── Orchestrator ───────────────────────────────────────────
async def run_l1_l2_checks(validated):
    """
    Run all Layer 1 and Layer 2 checks.
    Returns (list_of_check_results, html_string, beautifulsoup_object).
    The html and soup are returned so engine_l3_l4 can reuse them.
    """
    # Fetch page and PageSpeed data in parallel
    page_task = asyncio.create_task(_fetch_page(validated.url))
    pagespeed_task = asyncio.create_task(_fetch_pagespeed(validated.url))

    page = await page_task
    pagespeed = await pagespeed_task

    html = page.get("html", "")
    soup = BeautifulSoup(html, "lxml") if html else BeautifulSoup("", "lxml")

    checks = [
        # Layer 1 — Technical (28 pts)
        _check_https_ssl(validated, page),
        _check_ttfb(page),
        _check_lcp(pagespeed),
        _check_cls(pagespeed),
        _check_viewport(soup),
        _check_redirect_chain(page),
        _check_mixed_content(html, validated),

        # Layer 2 — On-Page (30 pts) — 9 checks:
        # title(6) + meta_desc(5) + headings(4) + canonical(4) + alt_text(3)
        # + internal_links(2) + word_count(2) + lang(1) + open_graph(3) = 30
        _check_title(soup),
        _check_meta_description(soup),
        _check_headings(soup),
        _check_canonical(soup, validated),
        _check_alt_text(soup),
        _check_internal_links(soup, validated),
        _check_word_count(soup),
        _check_lang_attr(soup),
    ]

    checks.append(_check_open_graph(soup))

    return checks, html, soup


def _check_open_graph(soup):
    """3 pts — Open Graph tags (og:title, og:description, og:image)."""
    og_title = soup.find("meta", attrs={"property": "og:title"})
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    og_image = soup.find("meta", attrs={"property": "og:image"})

    present = sum(1 for t in [og_title, og_desc, og_image] if t and t.get("content"))

    if present == 3:
        return CheckResult(
            id="open_graph", name="Open Graph Tags", category="on-page", weight="med",
            points_earned=3, points_possible=3, status="pass",
            detail="All three core Open Graph tags present (og:title, og:description, og:image).",
        )

    if present >= 1:
        return CheckResult(
            id="open_graph", name="Open Graph Tags", category="on-page", weight="med",
            points_earned=1, points_possible=3, status="warn",
            detail=f"Only {present} of 3 core Open Graph tags found. Social shares will render incompletely.",
            fix='Add the missing tags inside <head>: <meta property="og:title" content="...">, <meta property="og:description" content="...">, <meta property="og:image" content="https://yourdomain.com/og-image.jpg">. The og:image should be at least 1200x630 pixels.',
            verify="Test at opengraph.dev — paste your URL and confirm the preview renders correctly.",
        )

    return CheckResult(
        id="open_graph", name="Open Graph Tags", category="on-page", weight="med",
        points_earned=0, points_possible=3, status="fail",
        detail="No Open Graph meta tags found. Links shared on Facebook, LinkedIn, and Slack will not show a rich preview.",
        fix='Add to <head>: <meta property="og:title" content="Page Title">, <meta property="og:description" content="Page summary">, <meta property="og:image" content="https://yourdomain.com/og-image.jpg">, <meta property="og:url" content="https://yourdomain.com/this-page">, <meta property="og:type" content="website">.',
        verify="Test at opengraph.dev or use Facebook's Sharing Debugger.",
    )
