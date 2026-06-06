# SVO-Turbo Search Visibility Auditor

Audit any public URL against 32 search-visibility checks and get a 0–100 score with exact fixes. Live at [svo-auditor-site.vercel.app](https://svo-auditor-site.vercel.app).

Enter a URL, get a score and a full pass/fail breakdown for free. Submit an email to unlock the fix directive for every failed check and download a PDF report.

## What it checks

32 weighted checks across four layers, scored out of 100:

- **Technical Foundation** (28 pts) — HTTPS/SSL, load speed (TTFB, LCP, CLS), mobile viewport, redirect chains, mixed content
- **On-Page SEO** (30 pts) — title, meta description, heading structure, canonical URL, image alt text, word count, internal links
- **Structured Data** (22 pts) — JSON-LD coverage including LocalBusiness, Service, FAQPage, BreadcrumbList, and review markup
- **AI Readiness** (20 pts) — AI crawler access (GPTBot, ClaudeBot, PerplexityBot, Google-Extended), llms.txt, IndexNow, sitemap, semantic HTML

A failed HTTPS/SSL check caps the total at 40 — an insecure origin cannot score higher regardless of the rest.

## How it works

The audit runs server-side: the URL is validated and rate-limited, the page is fetched and parsed once, all 32 checks run, and a report is produced. The free result shows your score, the four category scores, every check’s status, and a preview of the top fixes. Entering an email reveals the full fix-and-verify directives and enables PDF export.

## Built with

A single static HTML/JS front end, Python serverless functions on Vercel, Supabase for stored results, Upstash Redis for rate limiting, and a private GitHub repository as the report archive. No build step and no Node toolchain.