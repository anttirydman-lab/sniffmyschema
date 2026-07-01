"""
job_store.py  —  Redis-backed job state, fully isolated from the rest of the app.

ALL Redis access lives here. Nothing outside this module imports redis or calls
Redis directly. Every public function takes and returns plain Python dicts/values
so this module can be replaced with a Postgres implementation by rewriting only
this file.

IMPORTANT — Railway Redis persistence:
  The Railway Redis service MUST have persistence enabled (RDB or AOF) so that
  job records survive container restarts. Without it a paid audit in progress is
  lost on any restart. Enable persistence in the Railway Redis service settings.

Replacing this module with Postgres later:
  Each job record maps directly to one table row. The field names used here
  (job_id, status, created_at, updated_at, request, progress, result, error)
  are intentionally chosen to be clean SQL column names. To migrate:
  1. Write a new job_store.py using asyncpg or SQLAlchemy async.
  2. Map each function to its SQL equivalent (INSERT, SELECT, UPDATE).
  3. Drop this file. No other file needs to change.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REDIS_URL   = os.environ.get("REDIS_URL", "redis://localhost:6379")
JOB_TTL     = 86_400           # seconds — expire completed jobs after 24h
KEY_PREFIX  = "job:"

# Write progress to Redis every N increments rather than on every single page.
# Keeps Redis write rate proportional to crawl size.
_PROGRESS_BATCH = 5

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_client: aioredis.Redis | None = None

# In-process increment buffer: counts increments between Redis flushes.
# Per-job, keyed by job_id. Cleared on set_job_complete / set_job_failed.
# Note: this is process-local. A restart resets the buffer but the last
# flushed value in Redis is still correct for display purposes.
_increment_buffer: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

async def _redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _client


def _key(job_id: str) -> str:
    return f"{KEY_PREFIX}{job_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _read(job_id: str) -> dict | None:
    r = await _redis()
    raw = await r.get(_key(job_id))
    return json.loads(raw) if raw else None


async def _write(record: dict, ttl: int = JOB_TTL) -> None:
    r = await _redis()
    await r.set(_key(record["job_id"]), json.dumps(record), ex=ttl)


# ---------------------------------------------------------------------------
# Public API  (storage-agnostic signatures — plain dicts/values only)
# ---------------------------------------------------------------------------

async def create_job(job_id: str, request_data: dict) -> None:
    """
    Create a new job record with status "pending".
    request_data must contain: sitemap_url, business_type, max_pages.
    """
    record: dict[str, Any] = {
        "job_id":     job_id,
        "status":     "pending",
        "created_at": _now(),
        "updated_at": _now(),
        "request":    request_data,
        "progress":   {"pages_done": 0, "pages_total": 0},
        "result":     None,
        "error":      None,
    }
    await _write(record)
    log.info("Job created: %s", job_id)


async def get_job(job_id: str) -> dict | None:
    """
    Return the full job record, or None if the job_id is unknown.
    Merges the in-process increment buffer into progress.pages_done so the
    caller always sees the most up-to-date count without an extra Redis write.
    """
    record = await _read(job_id)
    if record is None:
        return None
    # Reflect any buffered increments not yet flushed to Redis
    buffered = _increment_buffer.get(job_id, 0)
    if buffered:
        stored = record["progress"]["pages_done"]
        record["progress"]["pages_done"] = stored + buffered
    return record


async def update_job_progress(job_id: str, pages_done: int, pages_total: int) -> None:
    """
    Set the authoritative page counts and transition status to "running".
    Called once when the URL list is known (before crawling starts).
    """
    record = await _read(job_id)
    if record is None:
        log.warning("update_job_progress: unknown job %s", job_id)
        return
    record["status"]     = "running"
    record["updated_at"] = _now()
    record["progress"]   = {"pages_done": pages_done, "pages_total": pages_total}
    await _write(record)


async def increment_progress(job_id: str) -> None:
    """
    Increment the completed-page counter. Writes to Redis every
    _PROGRESS_BATCH increments to avoid hammering the store on large audits.
    """
    _increment_buffer[job_id] = _increment_buffer.get(job_id, 0) + 1
    if _increment_buffer[job_id] % _PROGRESS_BATCH != 0:
        return  # not yet at a flush boundary

    record = await _read(job_id)
    if record is None:
        return
    # Flush the full buffer to Redis and reset it
    total_done = record["progress"]["pages_done"] + _increment_buffer[job_id]
    _increment_buffer[job_id] = 0
    record["progress"]["pages_done"] = total_done
    record["updated_at"] = _now()
    await _write(record)


async def set_job_complete(job_id: str, result: dict) -> None:
    """
    Mark the job complete and store the full audit result.
    Flushes any remaining increment buffer so pages_done == pages_total.
    """
    record = await _read(job_id)
    if record is None:
        log.error("set_job_complete: unknown job %s", job_id)
        return
    # Flush remaining buffer
    remaining = _increment_buffer.pop(job_id, 0)
    pages_total = record["progress"]["pages_total"]
    record.update({
        "status":     "complete",
        "updated_at": _now(),
        "progress":   {
            "pages_done":  record["progress"]["pages_done"] + remaining,
            "pages_total": pages_total,
        },
        "result": result,
        "error":  None,
    })
    await _write(record)
    log.info("Job complete: %s", job_id)


async def set_job_failed(job_id: str, error: str) -> None:
    """
    Mark the job failed with an error message.
    Safe to call even if the record is missing (e.g. Redis restart mid-job).
    """
    _increment_buffer.pop(job_id, None)
    record = await _read(job_id)
    if record is None:
        log.error("set_job_failed: unknown job %s (error: %s)", job_id, error)
        return
    record.update({
        "status":     "failed",
        "updated_at": _now(),
        "error":      error,
    })
    await _write(record)
    log.warning("Job failed: %s — %s", job_id, error)
