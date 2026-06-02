"""
Engine for Layers 3 (Structured Data) and 4 (AI Readiness).
12 checks, 42 points total.

Reuses the html string and BeautifulSoup object parsed by engine_l1_l2.
Some checks (robots.txt, llms.txt, IndexNow) require additional fetches
to non-HTML files at the domain root.
"""
import asyncio
import json
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup


# Import CheckResult from sibling engine module
from core.engine_l1_l2 import CheckResult, USER_AGENT, FETCH_TIMEOUT


# ─── Helper: fetch a sidecar file (robots.txt, llms.txt, sitemap.xml) ──
async def _fetch_text(client, url):
    """Fetch a text file. Returns (status_code, text) or (None, '') on failure."""
    try:
        r = await client.get(url)
        if r.status_code == 200:
            text = r.text[:200_000]  # 200KB cap on sidecar files
            return r.status_code, text
        return r.status_code, ""
    except Exception:
        return None, ""


# ═══════════════════════════════════════════════════════════════
# LAYER 3 — STRUCTURED DATA (22 pts)
# ═══════════════════════════════════════════════════════════════

def _parse_jsonld(soup):
    """Extract and parse all JSON-LD blocks. Returns list of parsed objects."""
    blocks = soup.find_all("script", type="application/ld+json")
    parsed = []
    for block in blocks:
        text = block.string or block.text
        if not text:
            continue
        text = text.strip()
        try:
            data = json.loads(text)
            # JSON-LD can be a single object or an array
            if isinstance(data, list):
                parsed.extend(data)
            else:
                parsed.append(data)
        except json.JSONDecodeError:
            continue
    return parsed


def _find_schema_type(jsonld_blocks, schema_type):
    """
    Return the first JSON-LD object matching @type=schema_type.
    Handles nested @graph arrays.
    """
    for block in jsonld_blocks:
        # Check direct type
        t = block.get("@type")
        if isinstance(t, str) and t == schema_type:
            return block
        if isinstance(t, list) and schema_type in t:
            return block
        # Check @graph
        graph = block.get("@graph", [])
        if isinstance(graph, list):
            for item in graph:
                if not isinstance(item, dict):
                    continue
                it = item.get("@type")
                if isinstance(it, str) and it == schema_type:
                    return item
                if isinstance(it, list) and schema_type in it:
                    return item
    return None


def _check_jsonld_present(jsonld_blocks):
    """4 pts — Any valid JSON-LD present."""
    if jsonld_blocks:
        return CheckResult(
            id="jsonld_present", name="JSON-LD Present", category="structured-data", weight="high",
            points_earned=4, points_possible=4, status="pass",
            detail=f"Found {len(jsonld_blocks)} valid JSON-LD block(s).",
        )

    return CheckResult(
        id="jsonld_present", name="JSON-LD Present", category="structured-data", weight="high",
        points_earned=0, points_possible=4, status="fail",
        detail="No JSON-LD structured data found on the page.",
        fix='Add at least one JSON-LD block inside <head> or before </body>. Start with Organization or WebSite schema: <script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Your Brand","url":"https://yourdomain.com"}</script>',
        verify="Test at search.google.com/test/rich-results — paste your URL and confirm structured data is detected.",
    )


def _check_localbusiness(jsonld_blocks):
    """6 pts — LocalBusiness entity with complete NAP (name, address, phone)."""
    lb = _find_schema_type(jsonld_blocks, "LocalBusiness")
    # Also check for subtypes (Restaurant, ProfessionalService, etc.)
    if not lb:
        local_subtypes = ["Restaurant", "Store", "ProfessionalService", "MedicalBusiness", "HomeAndConstructionBusiness", "AutomotiveBusiness", "FinancialService"]
        for subtype in local_subtypes:
            lb = _find_schema_type(jsonld_blocks, subtype)
            if lb:
                break

    if not lb:
        return CheckResult(
            id="localbusiness", name="LocalBusiness Schema", category="structured-data", weight="critical",
            points_earned=0, points_possible=6, status="fail",
            detail="No LocalBusiness schema found. Local search visibility requires this entity.",
            fix='Add a LocalBusiness JSON-LD block. Minimum NAP example: <script type="application/ld+json">{"@context":"https://schema.org","@type":"LocalBusiness","name":"Your Business Name","address":{"@type":"PostalAddress","streetAddress":"123 Main St","addressLocality":"City","addressRegion":"ST","postalCode":"12345","addressCountry":"US"},"telephone":"+1-555-555-5555","url":"https://yourdomain.com"}</script>',
            verify="Test at search.google.com/test/rich-results — confirm LocalBusiness is detected.",
        )

    # Check NAP completeness
    has_name = bool(lb.get("name"))
    has_phone = bool(lb.get("telephone"))
    address = lb.get("address")
    has_address = False
    if address and isinstance(address, dict):
        has_address = bool(address.get("streetAddress") and address.get("addressLocality"))

    if has_name and has_phone and has_address:
        return CheckResult(
            id="localbusiness", name="LocalBusiness Schema", category="structured-data", weight="critical",
            points_earned=6, points_possible=6, status="pass",
            detail="LocalBusiness schema present with complete NAP (name, address, phone).",
        )

    missing = []
    if not has_name: missing.append("name")
    if not has_address: missing.append("address (with streetAddress and addressLocality)")
    if not has_phone: missing.append("telephone")

    return CheckResult(
        id="localbusiness", name="LocalBusiness Schema", category="structured-data", weight="critical",
        points_earned=3, points_possible=6, status="warn",
        detail=f"LocalBusiness schema present but incomplete. Missing: {', '.join(missing)}.",
        fix=f"Add the missing fields to your LocalBusiness JSON-LD: {', '.join(missing)}. NAP consistency across your website, Google Business Profile, and citations is critical for local rankings.",
        verify="Re-test at search.google.com/test/rich-results — all NAP fields should populate.",
    )


def _check_service_product(jsonld_blocks):
    """4 pts — Service or Product schema present."""
    if _find_schema_type(jsonld_blocks, "Service") or _find_schema_type(jsonld_blocks, "Product"):
        return CheckResult(
            id="service_product", name="Service or Product Schema", category="structured-data", weight="high",
            points_earned=4, points_possible=4, status="pass",
            detail="Service or Product schema detected.",
        )

    return CheckResult(
        id="service_product", name="Service or Product Schema", category="structured-data", weight="high",
        points_earned=0, points_possible=4, status="fail",
        detail="No Service or Product schema found.",
        fix='If you offer services: add a Service schema block listing each main service. <script type="application/ld+json">{"@context":"https://schema.org","@type":"Service","name":"Your Service","provider":{"@type":"LocalBusiness","name":"Your Business"},"areaServed":"City Name","description":"Brief service description"}</script>. For products, use @type: Product with offers, price, availability.',
        verify="Test at search.google.com/test/rich-results — Service or Product should be detected.",
    )


def _check_aggregate_rating(jsonld_blocks):
    """3 pts — AggregateRating or Review schema present."""
    for block in jsonld_blocks:
        # Direct match
        if block.get("aggregateRating"):
            return CheckResult(
                id="aggregate_rating", name="Rating/Review Schema", category="structured-data", weight="med",
                points_earned=3, points_possible=3, status="pass",
                detail="AggregateRating schema detected.",
            )
        # Check nested @graph
        for item in block.get("@graph", []):
            if isinstance(item, dict) and item.get("aggregateRating"):
                return CheckResult(
                    id="aggregate_rating", name="Rating/Review Schema", category="structured-data", weight="med",
                    points_earned=3, points_possible=3, status="pass",
                    detail="AggregateRating schema detected.",
                )

    if _find_schema_type(jsonld_blocks, "Review"):
        return CheckResult(
            id="aggregate_rating", name="Rating/Review Schema", category="structured-data", weight="med",
            points_earned=3, points_possible=3, status="pass",
            detail="Review schema detected.",
        )

    return CheckResult(
        id="aggregate_rating", name="Rating/Review Schema", category="structured-data", weight="med",
        points_earned=0, points_possible=3, status="fail",
        detail="No AggregateRating or Review schema found. Star ratings won't appear in search results.",
        fix='Add aggregateRating to your LocalBusiness, Service, or Product schema: "aggregateRating": {"@type":"AggregateRating","ratingValue":"4.8","reviewCount":"127"}. Only mark up ratings that genuinely appear on the page.',
        verify="Test at search.google.com/test/rich-results — star ratings should appear in the rich result preview.",
    )


def _check_faqpage(jsonld_blocks):
    """3 pts — FAQPage schema present."""
    if _find_schema_type(jsonld_blocks, "FAQPage"):
        return CheckResult(
            id="faqpage", name="FAQPage Schema", category="structured-data", weight="med",
            points_earned=3, points_possible=3, status="pass",
            detail="FAQPage schema detected.",
        )

    return CheckResult(
        id="faqpage", name="FAQPage Schema", category="structured-data", weight="med",
        points_earned=0, points_possible=3, status="fail",
        detail="No FAQPage schema found. FAQ rich results provide significant SERP real estate.",
        fix='If your page has an FAQ section, add FAQPage schema: <script type="application/ld+json">{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"What is X?","acceptedAnswer":{"@type":"Answer","text":"X is..."}}]}</script>. Mark up only questions that genuinely appear in the visible page content.',
        verify="Test at search.google.com/test/rich-results — FAQ should be detected.",
    )


def _check_breadcrumb(jsonld_blocks):
    """2 pts — BreadcrumbList schema present."""
    if _find_schema_type(jsonld_blocks, "BreadcrumbList"):
        return CheckResult(
            id="breadcrumb", name="BreadcrumbList Schema", category="structured-data", weight="low",
            points_earned=2, points_possible=2, status="pass",
            detail="BreadcrumbList schema detected.",
        )

    return CheckResult(
        id="breadcrumb", name="BreadcrumbList Schema", category="structured-data", weight="low",
        points_earned=0, points_possible=2, status="fail",
        detail="No BreadcrumbList schema found. Search results will show the URL path instead of clean breadcrumbs.",
        fix='Add BreadcrumbList for any page below the root: <script type="application/ld+json">{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":1,"name":"Home","item":"https://yourdomain.com"},{"@type":"ListItem","position":2,"name":"Category","item":"https://yourdomain.com/category"}]}</script>',
        verify="Test at search.google.com/test/rich-results — breadcrumbs should appear in the preview.",
    )


# ═══════════════════════════════════════════════════════════════
# LAYER 4 — AI READINESS (20 pts)
# ═══════════════════════════════════════════════════════════════

AI_BOTS = ["GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "anthropic-ai", "CCBot"]


def _check_ai_bots_allowed(robots_text):
    """6 pts — AI crawlers not blocked in robots.txt."""
    if robots_text is None:
        return CheckResult(
            id="ai_bots_allowed", name="AI Crawlers Allowed", category="ai-readiness", weight="critical",
            points_earned=6, points_possible=6, status="pass",
            detail="No robots.txt found — all crawlers allowed by default.",
        )

    blocked = []
    # Parse robots.txt to find sections that disallow AI bots
    current_agents = []
    for line in robots_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        directive, value = line.split(":", 1)
        directive = directive.strip().lower()
        value = value.strip()

        if directive == "user-agent":
            current_agents = [value]
        elif directive == "disallow" and value == "/":
            for agent in current_agents:
                for bot in AI_BOTS:
                    if agent.lower() == bot.lower() or agent == "*":
                        if bot not in blocked:
                            blocked.append(bot)

    if not blocked:
        return CheckResult(
            id="ai_bots_allowed", name="AI Crawlers Allowed", category="ai-readiness", weight="critical",
            points_earned=6, points_possible=6, status="pass",
            detail="No AI crawlers blocked in robots.txt.",
        )

    # If wildcard blocks all, that's a different problem
    earned = 0 if len(blocked) >= 4 else 2
    return CheckResult(
        id="ai_bots_allowed", name="AI Crawlers Allowed", category="ai-readiness", weight="critical",
        points_earned=earned, points_possible=6, status="fail",
        detail=f"Blocked AI crawlers detected: {', '.join(blocked)}. Your content will not appear in AI search results.",
        fix=f"Remove the Disallow directives for these user-agents from your robots.txt: {', '.join(blocked)}. If you want to remain in AI results, the file should NOT contain 'User-agent: GPTBot\\nDisallow: /' or similar for {', '.join(AI_BOTS)}.",
        verify="View https://yourdomain.com/robots.txt — confirm no AI bot has a Disallow: / directive.",
    )


def _check_llms_txt(text_or_status):
    """
    7 pts combined (presence + validity).
    text_or_status: dict { status: int|None, text: str }
    """
    status = text_or_status.get("status")
    text = text_or_status.get("text", "")

    if status != 200 or not text:
        return CheckResult(
            id="llms_txt", name="llms.txt Present + Valid", category="ai-readiness", weight="critical",
            points_earned=0, points_possible=7, status="fail",
            detail="No llms.txt found at the domain root. AI agents cannot discover a curated content map.",
            fix="Create /llms.txt at your domain root following the spec at llmstxt.org. Minimum structure:\n\n# Your Site Name\n\n> Brief description of what your site is about.\n\n## Docs\n- [Page Title](https://yourdomain.com/page): Brief description\n\n## Optional\n- [Other Page](https://yourdomain.com/other): Description\n\nServe it as text/plain at https://yourdomain.com/llms.txt.",
            verify="curl https://yourdomain.com/llms.txt — should return 200 with markdown content starting with # heading.",
        )

    # Validate structure: needs H1, blockquote, and at least 2 sections
    has_h1 = bool(re.search(r"^#\s+\S", text, re.MULTILINE))
    has_blockquote = bool(re.search(r"^>\s+\S", text, re.MULTILINE))
    section_count = len(re.findall(r"^##\s+\S", text, re.MULTILINE))

    if has_h1 and has_blockquote and section_count >= 2:
        return CheckResult(
            id="llms_txt", name="llms.txt Present + Valid", category="ai-readiness", weight="critical",
            points_earned=7, points_possible=7, status="pass",
            detail=f"Valid llms.txt found with H1, description, and {section_count} sections.",
        )

    # Present but incomplete
    missing = []
    if not has_h1: missing.append("H1 heading")
    if not has_blockquote: missing.append("description blockquote")
    if section_count < 2: missing.append(f"sections (found {section_count}, need 2+)")

    return CheckResult(
        id="llms_txt", name="llms.txt Present + Valid", category="ai-readiness", weight="critical",
        points_earned=4, points_possible=7, status="warn",
        detail=f"llms.txt found but missing required elements: {', '.join(missing)}.",
        fix=f"Update /llms.txt to add: {', '.join(missing)}. Spec at llmstxt.org. Required: # H1 site title, > blockquote description, ## section headings with linked items.",
        verify="View https://yourdomain.com/llms.txt — confirm structure matches llmstxt.org spec.",
    )


def _check_indexnow(indexnow_status):
    """4 pts — IndexNow key file present."""
    if indexnow_status == 200:
        return CheckResult(
            id="indexnow", name="IndexNow Key File", category="ai-readiness", weight="high",
            points_earned=4, points_possible=4, status="pass",
            detail="IndexNow key file detected at /indexnow.txt or domain root.",
        )

    return CheckResult(
        id="indexnow", name="IndexNow Key File", category="ai-readiness", weight="high",
        points_earned=0, points_possible=4, status="fail",
        detail="No IndexNow key file found. Pages won't be instantly notified to Bing, Yandex, or Seznam.",
        fix="Generate an IndexNow key at indexnow.org. Save the key as a text file at your domain root (e.g., https://yourdomain.com/{key}.txt) containing only the key string. Then POST updated URLs to https://api.indexnow.org/indexnow whenever content changes.",
        verify="curl https://yourdomain.com/{your-key}.txt — should return 200 with the key string.",
    )


def _check_robots_txt(robots_status):
    """2 pts — robots.txt accessible."""
    if robots_status == 200:
        return CheckResult(
            id="robots_txt", name="robots.txt Accessible", category="ai-readiness", weight="low",
            points_earned=2, points_possible=2, status="pass",
            detail="robots.txt returns 200 OK.",
        )

    return CheckResult(
        id="robots_txt", name="robots.txt Accessible", category="ai-readiness", weight="low",
        points_earned=0, points_possible=2, status="fail",
        detail=f"robots.txt returned status {robots_status if robots_status else 'unreachable'}. Crawlers cannot find directives.",
        fix="Create a robots.txt file at your domain root. Minimum content for a fully crawlable site:\n\nUser-agent: *\nAllow: /\nSitemap: https://yourdomain.com/sitemap.xml\n\nServe it as text/plain at https://yourdomain.com/robots.txt.",
        verify="curl https://yourdomain.com/robots.txt — should return 200 with at least one User-agent line.",
    )


def _check_sitemap_declared(robots_text):
    """2 pts — Sitemap declared in robots.txt."""
    if not robots_text:
        return CheckResult(
            id="sitemap_declared", name="Sitemap in robots.txt", category="ai-readiness", weight="low",
            points_earned=0, points_possible=2, status="fail",
            detail="No robots.txt found — sitemap cannot be declared.",
            fix="Add a robots.txt first (see check above), then include a 'Sitemap:' directive.",
            verify="curl https://yourdomain.com/robots.txt | grep -i sitemap",
        )

    if re.search(r"^Sitemap:\s*https?://", robots_text, re.MULTILINE | re.IGNORECASE):
        return CheckResult(
            id="sitemap_declared", name="Sitemap in robots.txt", category="ai-readiness", weight="low",
            points_earned=2, points_possible=2, status="pass",
            detail="Sitemap directive found in robots.txt.",
        )

    return CheckResult(
        id="sitemap_declared", name="Sitemap in robots.txt", category="ai-readiness", weight="low",
        points_earned=0, points_possible=2, status="fail",
        detail="robots.txt exists but does not declare a sitemap URL.",
        fix="Add a Sitemap directive to your robots.txt: 'Sitemap: https://yourdomain.com/sitemap.xml'. Place it on its own line (any position in the file).",
        verify="curl https://yourdomain.com/robots.txt | grep -i sitemap — should show the Sitemap: line.",
    )


def _check_semantic_html(soup):
    """1 pt — At least 3 distinct semantic HTML5 tags."""
    semantic_tags = ["header", "nav", "main", "article", "section", "aside", "footer"]
    found = set()
    for tag in semantic_tags:
        if soup.find(tag):
            found.add(tag)

    if len(found) >= 3:
        return CheckResult(
            id="semantic_html", name="Semantic HTML Structure", category="ai-readiness", weight="low",
            points_earned=1, points_possible=1, status="pass",
            detail=f"Semantic tags detected: {', '.join(sorted(found))}.",
        )

    return CheckResult(
        id="semantic_html", name="Semantic HTML Structure", category="ai-readiness", weight="low",
        points_earned=0, points_possible=1, status="fail",
        detail=f"Only {len(found)} semantic HTML5 tag(s) used (target: 3+). Page relies heavily on <div> structure.",
        fix="Replace generic <div> containers with semantic equivalents where appropriate: <header> for site/page headers, <nav> for navigation, <main> for primary content, <article> for self-contained content, <section> for thematic groupings, <aside> for sidebars, <footer> for the page footer.",
        verify="View source — count distinct semantic tags. Should be at least 3 of: header, nav, main, article, section, aside, footer.",
    )


# ─── Orchestrator ───────────────────────────────────────────
async def run_l3_l4_checks(validated, html, soup):
    """
    Run all Layer 3 and Layer 4 checks.
    Fetches robots.txt, llms.txt, and probes for an IndexNow key file.
    """
    base = f"{validated.scheme}://{validated.hostname}"
    if (validated.scheme == "http" and validated.port != 80) or \
       (validated.scheme == "https" and validated.port != 443):
        base += f":{validated.port}"

    robots_url = base + "/robots.txt"
    llms_url = base + "/llms.txt"

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        # Fetch in parallel
        results = await asyncio.gather(
            _fetch_text(client, robots_url),
            _fetch_text(client, llms_url),
            _probe_indexnow(client, base),
            return_exceptions=True,
        )

    robots_result, llms_result, indexnow_status = results

    if isinstance(robots_result, Exception):
        robots_status, robots_text = None, ""
    else:
        robots_status, robots_text = robots_result

    if isinstance(llms_result, Exception):
        llms_status, llms_text = None, ""
    else:
        llms_status, llms_text = llms_result

    if isinstance(indexnow_status, Exception):
        indexnow_status = None

    # Parse JSON-LD once for all schema checks
    jsonld_blocks = _parse_jsonld(soup)

    checks = [
        # Layer 3 — Structured Data (22 pts)
        _check_localbusiness(jsonld_blocks),
        _check_jsonld_present(jsonld_blocks),
        _check_service_product(jsonld_blocks),
        _check_aggregate_rating(jsonld_blocks),
        _check_faqpage(jsonld_blocks),
        _check_breadcrumb(jsonld_blocks),

        # Layer 4 — AI Readiness (20 pts)
        _check_ai_bots_allowed(robots_text),
        _check_llms_txt({"status": llms_status, "text": llms_text}),
        _check_indexnow(indexnow_status),
        _check_robots_txt(robots_status),
        _check_sitemap_declared(robots_text),
        _check_semantic_html(soup),
    ]

    return checks


async def _probe_indexnow(client, base):
    """
    IndexNow key files have unpredictable names ({key}.txt at root).
    Probe common paths and look for a key file pattern.
    Returns 200 if found, None otherwise.
    """
    # Check root for any .txt file that looks like an IndexNow key
    # We can only really verify by checking standard locations
    candidates = ["/indexnow.txt"]

    for path in candidates:
        try:
            r = await client.head(base + path)
            if r.status_code == 200:
                return 200
        except Exception:
            continue

    return None
  
