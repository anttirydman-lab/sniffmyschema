#!/usr/bin/env python3
"""
app.py  —  FastAPI schema-audit API

Run locally:
    uvicorn app:app --reload --port 8000

Test:
    curl -s -X POST http://localhost:8000/audit \
      -H "Content-Type: application/json" \
      -d '{"sitemap_url":"https://example.com/sitemap.xml","business_type":"local_business","max_pages":10}' \
      | python3 -m json.tool
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from lxml import etree
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeout,
)
from pydantic import BaseModel, HttpUrl, field_validator

from schema_audit import BUSINESS_TYPE_SCHEMAS, audit_page

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PAGE_TIMEOUT      = 20_000   # ms — Playwright hard timeout per page
JSONLD_WAIT_MS    = 3_000    # ms — bounded wait for JSON-LD tags to appear
REQUEST_DELAY     = 1.0      # seconds — politeness delay between page loads
CRAWL_CONCURRENCY = 3        # simultaneous Playwright pages
SITEMAP_MAX_PAGES    = 500   # hard cap on total page URLs collected
SITEMAP_MAX_FETCHES  = 50    # hard cap on total sitemap files fetched (index recursion)
HTTP_TIMEOUT      = 15.0     # seconds — sitemap fetch timeout

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Schema Audit API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

BusinessType = Literal["ecommerce", "local_business", "blog_media", "professional_services", "other"]


class AuditRequest(BaseModel):
    sitemap_url: HttpUrl
    business_type: BusinessType = "other"
    max_pages: int = 25

    @field_validator("max_pages")
    @classmethod
    def cap_max_pages(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_pages must be at least 1")
        return min(v, SITEMAP_MAX_PAGES)


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------

SITEMAP_NS  = "http://www.sitemaps.org/schemas/sitemap/0.9"
INDEX_TAG   = f"{{{SITEMAP_NS}}}sitemap"
URL_TAG     = f"{{{SITEMAP_NS}}}url"
LOC_TAG     = f"{{{SITEMAP_NS}}}loc"


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    """Fetch URL text, raising HTTPException on failure."""
    try:
        r = await client.get(url, follow_redirects=True, timeout=HTTP_TIMEOUT)
    except httpx.TimeoutException:
        raise HTTPException(422, f"Timeout fetching sitemap: {url}")
    except httpx.RequestError as exc:
        raise HTTPException(422, f"Network error fetching sitemap: {exc}")
    if r.status_code == 404:
        raise HTTPException(422, f"Sitemap not found (404): {url}")
    if r.status_code != 200:
        raise HTTPException(422, f"Sitemap returned HTTP {r.status_code}: {url}")
    return r.text


def _parse_sitemap_xml(text: str, source_url: str) -> tuple[list[str], list[str]]:
    """
    Parse a sitemap XML string.
    Returns (page_urls, child_sitemap_urls).
    Raises HTTPException on malformed XML.
    """
    try:
        root = etree.fromstring(text.encode())
    except etree.XMLSyntaxError as exc:
        raise HTTPException(422, f"Malformed sitemap XML at {source_url}: {exc}")

    # Strip namespace for comparison (some sitemaps omit namespace)
    tag = root.tag.lower()
    is_index = "sitemapindex" in tag

    pages: list[str]    = []
    children: list[str] = []

    if is_index:
        for node in root.iter(INDEX_TAG):
            loc = node.find(LOC_TAG)
            if loc is not None and loc.text:
                children.append(loc.text.strip())
        # Also try without namespace
        if not children:
            for node in root.iter("sitemap"):
                loc = node.find("loc")
                if loc is None:
                    loc = node.find(LOC_TAG)
                if loc is not None and loc.text:
                    children.append(loc.text.strip())
    else:
        for node in root.iter(URL_TAG):
            loc = node.find(LOC_TAG)
            if loc is not None and loc.text:
                pages.append(loc.text.strip())
        # Also try without namespace
        if not pages:
            for node in root.iter("url"):
                loc = node.find("loc")
                if loc is None:
                    loc = node.find(LOC_TAG)
                if loc is not None and loc.text:
                    pages.append(loc.text.strip())

    return pages, children


async def _resolve_sitemap_url(client: httpx.AsyncClient, raw_url: str) -> str:
    """
    Given any URL, return a confirmed sitemap URL.
    If raw_url already looks like a sitemap, return it.
    Otherwise try /sitemap.xml and robots.txt fallbacks.
    Raises HTTPException if none found.
    """
    path = urlparse(raw_url).path.lower()
    if path.endswith(".xml") or "sitemap" in path:
        return raw_url  # Caller-supplied URL looks like a sitemap — use it

    base = f"{urlparse(raw_url).scheme}://{urlparse(raw_url).netloc}"

    # Try /sitemap.xml
    candidate = urljoin(base, "/sitemap.xml")
    try:
        r = await client.get(candidate, follow_redirects=True, timeout=HTTP_TIMEOUT)
        if r.status_code == 200 and ("<urlset" in r.text or "<sitemapindex" in r.text):
            log.info("Found sitemap via /sitemap.xml fallback: %s", candidate)
            return candidate
    except httpx.RequestError:
        pass

    # Try robots.txt
    robots_url = urljoin(base, "/robots.txt")
    try:
        r = await client.get(robots_url, follow_redirects=True, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            for line in r.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    sitemap = line.split(":", 1)[1].strip()
                    log.info("Found sitemap via robots.txt: %s", sitemap)
                    return sitemap
    except httpx.RequestError:
        pass

    raise HTTPException(
        422,
        f"Could not locate a sitemap for {raw_url}. "
        "Provide a direct sitemap URL or ensure /sitemap.xml or robots.txt Sitemap directive exists.",
    )


async def fetch_urls_from_sitemap(sitemap_url: str, max_pages: int) -> list[str]:
    """
    Fetch and recursively parse a sitemap (or sitemap index), returning up to
    max_pages page URLs.
    """
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        sitemap_url = await _resolve_sitemap_url(client, sitemap_url)

        all_pages: list[str]   = []
        queue: list[str]       = [sitemap_url]
        visited: set[str]      = set()

        while queue and len(all_pages) < max_pages and len(visited) < SITEMAP_MAX_FETCHES:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            log.info("Fetching sitemap: %s", url)
            text = await _fetch_text(client, url)
            pages, children = _parse_sitemap_xml(text, url)

            remaining = max_pages - len(all_pages)
            all_pages.extend(pages[:remaining])
            # Only enqueue children if we haven't hit the sitemap fetch cap
            if len(visited) < SITEMAP_MAX_FETCHES:
                queue.extend(children)

        if not all_pages:
            raise HTTPException(422, f"Sitemap at {sitemap_url} contains no page URLs.")

        log.info("Collected %d URLs from sitemap (cap=%d)", len(all_pages), max_pages)
        return all_pages[:max_pages]


# ---------------------------------------------------------------------------
# Async Playwright crawler
# ---------------------------------------------------------------------------

async def _crawl_page(sem: asyncio.Semaphore, browser_context, url: str, business_type: str) -> dict:
    """Crawl a single page and return the structured page result."""
    async with sem:
        page = await browser_context.new_page()
        status = "ok"
        notes  = ""
        raw_blocks: list[str] = []

        try:
            await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector(
                    'script[type="application/ld+json"]',
                    timeout=JSONLD_WAIT_MS,
                )
            except PlaywrightTimeout:
                pass  # No JSON-LD appeared — proceed with empty result

            raw_blocks = await page.evaluate("""
                () => Array.from(
                    document.querySelectorAll('script[type="application/ld+json"]')
                ).map(s => s.textContent)
            """)
            log.info("  OK  %s  (%d blocks)", url, len(raw_blocks))

        except PlaywrightTimeout:
            status = "timeout"
            notes  = "Page timed out"
            log.warning("  TIMEOUT  %s", url)
        except Exception as exc:
            status = "error"
            notes  = str(exc)[:200]
            log.warning("  ERROR  %s  %s", url, notes)
        finally:
            await page.close()

        if status == "ok":
            result = audit_page(raw_blocks, business_type)
        else:
            result = {
                "schema_blocks":      0,
                "schema_types_count": 0,
                "schema_types_found": [],
                "duplicate_types":    [],
                "coverage": {
                    "expected_present":     [],
                    "missing_critical":     [],
                    "missing_important":    [],
                    "missing_nice_to_have": [],
                },
                "parse_errors": [],
            }

        await asyncio.sleep(REQUEST_DELAY)

        return {
            "url":    url,
            "status": status,
            "notes":  notes,
            **result,
        }


async def crawl_pages(urls: list[str], business_type: str) -> list[dict]:
    """Crawl all URLs with bounded concurrency, returning page result dicts."""
    sem = asyncio.Semaphore(CRAWL_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        try:
            tasks = [_crawl_page(sem, context, url, business_type) for url in urls]
            # return_exceptions=True: one bad page never aborts the whole audit.
            # Unexpected exceptions are caught below and converted to error rows.
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await context.close()
            await browser.close()

    results: list[dict] = []
    for url, outcome in zip(urls, raw_results):
        if isinstance(outcome, Exception):
            log.error("Unexpected error crawling %s: %s", url, outcome)
            results.append({
                "url": url, "status": "error", "notes": str(outcome)[:200],
                "schema_blocks": 0, "schema_types_count": 0,
                "schema_types_found": [], "duplicate_types": [],
                "coverage": {
                    "expected_present": [], "missing_critical": [],
                    "missing_important": [], "missing_nice_to_have": [],
                },
                "parse_errors": [],
            })
        else:
            results.append(outcome)

    return results


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/audit")
async def audit(req: AuditRequest) -> dict:
    sitemap_url   = str(req.sitemap_url)
    business_type = req.business_type
    max_pages     = req.max_pages

    log.info("=== Audit request: sitemap=%s type=%s max=%d ===", sitemap_url, business_type, max_pages)

    urls = await fetch_urls_from_sitemap(sitemap_url, max_pages)
    pages = await crawl_pages(urls, business_type)

    # Summary
    ok_pages       = [p for p in pages if p["status"] == "ok"]
    pages_error    = len([p for p in pages if p["status"] in ("timeout", "error")])
    # "no schema" = no usable types found (blocks may exist but all failed to parse)
    pages_no_schema = len([p for p in ok_pages if p["schema_types_count"] == 0])
    pages_missing_critical = len([
        p for p in ok_pages if p["coverage"]["missing_critical"]
    ])

    return {
        "sitemap_url":   sitemap_url,
        "business_type": business_type,
        "audited_at":    datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_pages":             len(pages),
            "pages_ok":                len(ok_pages),
            "pages_error":             pages_error,
            "pages_no_schema":         pages_no_schema,
            "pages_missing_critical":  pages_missing_critical,
        },
        "pages": pages,
    }
