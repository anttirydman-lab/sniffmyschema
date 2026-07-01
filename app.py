#!/usr/bin/env python3
"""
app.py  —  FastAPI schema-audit API (background-job architecture)

Run locally:
    # Start Redis first:  docker run -p 6379:6379 redis
    uvicorn app:app --reload --port 8000

Submit an audit:
    curl -s -X POST http://localhost:8000/audit \
      -H "Content-Type: application/json" \
      -d '{"sitemap_url":"https://example.com/sitemap.xml","business_type":"local_business","max_pages":10}'
    # -> {"job_id": "...", "status": "pending"}

Poll for results:
    curl -s http://localhost:8000/audit/<job_id> | python3 -m json.tool
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
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

import job_store
from schema_audit import audit_page, site_level_report

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

PAGE_TIMEOUT         = 20_000   # ms — Playwright hard timeout per page
JSONLD_WAIT_MS       = 3_000    # ms — bounded wait for JSON-LD tags to appear
REQUEST_DELAY        = 1.0      # seconds — politeness delay between page loads
CRAWL_CONCURRENCY    = 3        # simultaneous Playwright pages
SITEMAP_MAX_PAGES    = 500      # hard cap on total page URLs collected
SITEMAP_MAX_FETCHES  = 75       # hard cap on total sitemap files fetched
HTTP_TIMEOUT         = 15.0     # seconds — sitemap fetch timeout
MAX_AUDIT_SECONDS    = 1800     # seconds — max runtime before a job is auto-failed (30 min)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Schema Audit API", version="2.0.0")

# CORS is app-level and covers all routes, including GET /audit/{job_id}
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Strong references to background tasks prevent them from being garbage-collected
# before they complete. Tasks remove themselves via the done callback.
_background_tasks: set[asyncio.Task] = set()

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

BusinessType = Literal[
    "ecommerce", "local_business", "blog_media",
    "professional_services", "restaurant", "other"
]


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
# Sitemap parsing  (unchanged)
# ---------------------------------------------------------------------------

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
INDEX_TAG  = f"{{{SITEMAP_NS}}}sitemap"
URL_TAG    = f"{{{SITEMAP_NS}}}url"
LOC_TAG    = f"{{{SITEMAP_NS}}}loc"


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
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
    try:
        root = etree.fromstring(text.encode())
    except etree.XMLSyntaxError as exc:
        raise HTTPException(422, f"Malformed sitemap XML at {source_url}: {exc}")

    tag = root.tag.lower()
    is_index = "sitemapindex" in tag
    pages: list[str]    = []
    children: list[str] = []

    if is_index:
        for node in root.iter(INDEX_TAG):
            loc = node.find(LOC_TAG)
            if loc is not None and loc.text:
                children.append(loc.text.strip())
        if not children:
            for node in root.iter("sitemap"):
                loc = node.find("loc") or node.find(LOC_TAG)
                if loc is not None and loc.text:
                    children.append(loc.text.strip())
    else:
        for node in root.iter(URL_TAG):
            loc = node.find(LOC_TAG)
            if loc is not None and loc.text:
                pages.append(loc.text.strip())
        if not pages:
            for node in root.iter("url"):
                loc = node.find("loc") or node.find(LOC_TAG)
                if loc is not None and loc.text:
                    pages.append(loc.text.strip())

    return pages, children


async def _resolve_sitemap_url(client: httpx.AsyncClient, raw_url: str) -> str:
    path = urlparse(raw_url).path.lower()
    if path.endswith(".xml") or "sitemap" in path:
        return raw_url

    base = f"{urlparse(raw_url).scheme}://{urlparse(raw_url).netloc}"

    candidate = urljoin(base, "/sitemap.xml")
    try:
        r = await client.get(candidate, follow_redirects=True, timeout=HTTP_TIMEOUT)
        if r.status_code == 200 and ("<urlset" in r.text or "<sitemapindex" in r.text):
            log.info("Found sitemap via /sitemap.xml fallback: %s", candidate)
            return candidate
    except httpx.RequestError:
        pass

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
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        sitemap_url = await _resolve_sitemap_url(client, sitemap_url)
        all_pages: list[str] = []
        queue: list[str]     = [sitemap_url]
        visited: set[str]    = set()

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
            if len(visited) < SITEMAP_MAX_FETCHES:
                queue.extend(children)

        if not all_pages:
            raise HTTPException(422, f"Sitemap at {sitemap_url} contains no page URLs.")

        log.info("Collected %d URLs from sitemap (cap=%d)", len(all_pages), max_pages)
        return all_pages[:max_pages]


# ---------------------------------------------------------------------------
# Async Playwright crawler
# ---------------------------------------------------------------------------

def _empty_page_result() -> dict:
    return {
        "page_type":           "general",
        "schema_blocks":       0,
        "schema_types_count":  0,
        "schema_types_found":  [],
        "primary_types":       [],
        "primary_types_count": 0,
        "supporting_types":    [],
        "duplicate_types":     [],
        "coverage": {
            "present_expected":   [],
            "suggested_relevant": [],
            "suggested_helpful":  [],
        },
        "parse_errors": [],
    }


async def _crawl_page(
    sem: asyncio.Semaphore,
    browser_context,
    url: str,
    on_page_done: Callable[[], Awaitable[None]] | None = None,
) -> dict:
    """Crawl a single page and return the structured page result."""
    async with sem:
        status = "ok"
        notes  = ""
        raw_blocks: list[str] = []

        try:
            page = await browser_context.new_page()
        except Exception as exc:
            log.error("  BROWSER CLOSED  %s  %s", url, exc)
            if on_page_done:
                await on_page_done()
            return {
                "url": url, "status": "error",
                "notes": f"Browser unavailable: {str(exc)[:180]}",
                **_empty_page_result(),
            }

        try:
            await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector(
                    'script[type="application/ld+json"]',
                    timeout=JSONLD_WAIT_MS,
                )
            except PlaywrightTimeout:
                pass

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

        result = audit_page(raw_blocks, url) if status == "ok" else _empty_page_result()

        # Signal completion before the politeness delay so progress updates
        # are reflected promptly, not held up by the sleep.
        if on_page_done:
            await on_page_done()

        await asyncio.sleep(REQUEST_DELAY)

        return {"url": url, "status": status, "notes": notes, **result}


async def crawl_pages(
    urls: list[str],
    on_page_done: Callable[[], Awaitable[None]] | None = None,
) -> list[dict]:
    """Crawl all URLs with bounded concurrency, returning page result dicts."""
    sem = asyncio.Semaphore(CRAWL_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        )
        context = await browser.new_context(user_agent=USER_AGENT)
        try:
            tasks = [_crawl_page(sem, context, url, on_page_done) for url in urls]
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
                **_empty_page_result(),
            })
        else:
            results.append(outcome)

    return results


# ---------------------------------------------------------------------------
# Result builder  (shared between background task and any future sync path)
# ---------------------------------------------------------------------------

def _build_result(pages: list[dict], sitemap_url: str, business_type: str) -> dict:
    ok_pages = [p for p in pages if p["status"] == "ok"]
    pages_error = len([p for p in pages if p["status"] in ("timeout", "error")])
    pages_no_schema = len([p for p in ok_pages if p["schema_types_count"] == 0])
    pages_with_suggestions = len([
        p for p in ok_pages
        if p["coverage"]["suggested_relevant"] or p["coverage"]["suggested_helpful"]
    ])
    type_counts: dict[str, int] = {}
    for p in ok_pages:
        pt = p.get("page_type", "general")
        type_counts[pt] = type_counts.get(pt, 0) + 1

    all_page_types = [set(p["schema_types_found"]) for p in ok_pages]
    site_report = site_level_report(all_page_types, business_type)

    return {
        "sitemap_url":   sitemap_url,
        "business_type": business_type,
        "audited_at":    datetime.now(timezone.utc).isoformat(),
        "site_level":    site_report,
        "summary": {
            "total_pages":            len(pages),
            "pages_ok":               len(ok_pages),
            "pages_error":            pages_error,
            "pages_no_schema":        pages_no_schema,
            "pages_with_suggestions": pages_with_suggestions,
            "page_types_detected":    type_counts,
        },
        "pages": pages,
    }


# ---------------------------------------------------------------------------
# Background audit task
# ---------------------------------------------------------------------------

async def _audit_core(
    job_id: str,
    sitemap_url: str,
    business_type: str,
    max_pages: int,
) -> None:
    """Inner coroutine: the full audit pipeline, wrapped by _run_audit_job."""
    # Fetch URLs — if the sitemap is unreachable, fail the job cleanly.
    try:
        urls = await fetch_urls_from_sitemap(sitemap_url, max_pages)
    except HTTPException as exc:
        await job_store.set_job_failed(job_id, f"Sitemap error: {exc.detail}")
        return

    # Transition to running and record total page count
    await job_store.update_job_progress(job_id, pages_done=0, pages_total=len(urls))

    # Crawl — increment_progress is called per page inside job_store
    pages = await crawl_pages(
        urls,
        on_page_done=lambda: job_store.increment_progress(job_id),
    )

    result = _build_result(pages, sitemap_url, business_type)
    await job_store.set_job_complete(job_id, result)


async def _run_audit_job(
    job_id: str,
    sitemap_url: str,
    business_type: str,
    max_pages: int,
) -> None:
    """
    Background task wrapper. Enforces a max-runtime ceiling and ensures the
    job always resolves to either "complete" or "failed" — never stays "running".
    """
    log.info("Background audit started: %s", job_id)
    try:
        await asyncio.wait_for(
            _audit_core(job_id, sitemap_url, business_type, max_pages),
            timeout=MAX_AUDIT_SECONDS,
        )
    except asyncio.TimeoutError:
        msg = f"Audit exceeded {MAX_AUDIT_SECONDS}s time limit and was stopped."
        log.warning("Job timed out: %s", job_id)
        await job_store.set_job_failed(job_id, msg)
    except Exception as exc:
        log.exception("Unexpected error in audit job %s", job_id)
        await job_store.set_job_failed(job_id, str(exc)[:500])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/audit", status_code=202)
async def start_audit(req: AuditRequest) -> dict:
    """
    Submit an audit. Returns immediately with a job_id.
    The crawl runs in the background; poll GET /audit/{job_id} for progress.
    """
    job_id        = str(uuid.uuid4())
    sitemap_url   = str(req.sitemap_url)
    business_type = req.business_type
    max_pages     = req.max_pages

    log.info("Audit submitted: job=%s sitemap=%s type=%s max=%d",
             job_id, sitemap_url, business_type, max_pages)

    await job_store.create_job(job_id, {
        "sitemap_url":   sitemap_url,
        "business_type": business_type,
        "max_pages":     max_pages,
    })

    # Schedule the crawl as a background task. Keep a strong reference so it
    # isn't garbage-collected before it finishes.
    task = asyncio.create_task(
        _run_audit_job(job_id, sitemap_url, business_type, max_pages)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    # Response returns here, before the crawl begins
    return {"job_id": job_id, "status": "pending"}


@app.get("/audit/{job_id}")
async def get_audit(job_id: str) -> dict:
    """
    Poll job status.

    pending / running  →  {"job_id", "status", "progress": {"pages_done", "pages_total"}}
    complete           →  full audit result (sitemap_url, business_type, audited_at,
                           site_level, summary, pages) plus job_id and status
    failed             →  {"job_id", "status": "failed", "error": "..."}
    unknown job_id     →  HTTP 404
    """
    job = await job_store.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job not found: {job_id}")

    status = job["status"]

    if status in ("pending", "running"):
        return {
            "job_id":   job_id,
            "status":   status,
            "progress": job["progress"],
        }

    if status == "complete":
        return {
            "job_id": job_id,
            "status": "complete",
            **job["result"],
        }

    # failed
    return {
        "job_id": job_id,
        "status": "failed",
        "error":  job.get("error", "Unknown error"),
    }
