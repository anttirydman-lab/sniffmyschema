#!/usr/bin/env python3
"""
schema_audit.py  —  detection-only module

All JSON-LD extraction, type detection, duplicate analysis, and coverage
reporting lives here. No I/O, no HTTP, no Sheets. Imported by app.py.

Detection logic is reviewed and stable — do not change without explicit review.
"""

import json
import logging
from collections import Counter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Business-type schema priority map
# ---------------------------------------------------------------------------

BUSINESS_TYPE_SCHEMAS: dict[str, dict[str, str]] = {
    "ecommerce": {
        "Product":         "critical",
        "Offer":           "critical",
        "Organization":    "critical",
        "AggregateOffer":  "important",
        "Review":          "important",
        "AggregateRating": "important",
        "BreadcrumbList":  "important",
        "WebSite":         "nice-to-have",
    },
    "local_business": {
        "LocalBusiness":             "critical",
        "Organization":              "critical",
        "Place":                     "important",
        "GeoCoordinates":            "important",
        "OpeningHoursSpecification": "important",
        "Review":                    "important",
        "AggregateRating":           "important",
        "BreadcrumbList":            "nice-to-have",
    },
    "blog_media": {
        "Article":        "critical",
        "BlogPosting":    "critical",
        "Organization":   "critical",
        "NewsArticle":    "important",
        "Person":         "important",
        "BreadcrumbList": "important",
        "WebSite":        "nice-to-have",
    },
    "professional_services": {
        "Organization":    "critical",
        "Service":         "critical",
        "LocalBusiness":   "important",
        "FAQPage":         "important",
        "Review":          "important",
        "AggregateRating": "important",
        "BreadcrumbList":  "nice-to-have",
    },
    "other": {
        "Organization":   "critical",
        "WebSite":        "important",
        "WebPage":        "important",
        "FAQPage":        "nice-to-have",
        "BreadcrumbList": "nice-to-have",
    },
}

# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------

def extract_jsonld_from_blocks(raw_blocks: list[str]) -> tuple[list[dict], list[str]]:
    """
    Parse raw JSON-LD text blocks from the DOM.
    Returns (schemas, parse_errors).
    """
    schemas: list[dict] = []
    errors: list[str]   = []

    for raw in raw_blocks:
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
            _ingest(data, schemas)
        except json.JSONDecodeError:
            try:
                data = json.loads(raw, strict=False)
                _ingest(data, schemas)
            except json.JSONDecodeError as exc:
                errors.append(str(exc))

    return schemas, errors


def _ingest(data: object, schemas: list[dict]) -> None:
    if isinstance(data, list):
        schemas.extend(data)
    elif isinstance(data, dict):
        if data.get("@type"):
            schemas.append(data)
        if "@graph" in data:
            schemas.extend(data["@graph"])


# ---------------------------------------------------------------------------
# Type detection helpers
# ---------------------------------------------------------------------------

def normalize_types(item: dict) -> set[str]:
    """Exact @type values of a single schema item — top-level only."""
    t = item.get("@type", "")
    if isinstance(t, list):
        return {str(v).strip() for v in t if v}
    raw = str(t).strip()
    return {raw} if raw else set()


def _collect_types_recursive(value: object, found: set[str], depth: int = 0) -> None:
    """
    Walk any JSON-LD value and collect every @type string at any depth.
    Capped at depth 10 to guard against pathological nesting / circular @id refs.
    """
    if depth > 10:
        return
    if isinstance(value, dict):
        t = value.get("@type")
        if t:
            if isinstance(t, list):
                for v in t:
                    s = str(v).strip()
                    if s:
                        found.add(s)
            else:
                s = str(t).strip()
                if s:
                    found.add(s)
        for v in value.values():
            _collect_types_recursive(v, found, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _collect_types_recursive(item, found, depth + 1)


def all_types_recursive(schemas: list[dict]) -> set[str]:
    """All @type strings found anywhere in the full nested schema tree."""
    found: set[str] = set()
    for s in schemas:
        _collect_types_recursive(s, found)
    return found


# ---------------------------------------------------------------------------
# Duplicate detection  (top-level only — intentional)
# A Review nested inside each of N Product items is normal nesting, not a
# duplicate declaration. Only repeated top-level schema items are flagged.
# ---------------------------------------------------------------------------

def duplicate_types(schemas: list[dict]) -> list[str]:
    """
    Return list of strings like "Organization (3)" for any top-level @type
    appearing more than once. Empty list if none.
    """
    counts: Counter = Counter()
    for s in schemas:
        for t in normalize_types(s):
            counts[t] += 1
    return [f"{t} ({c})" for t, c in sorted(counts.items()) if c > 1]


# ---------------------------------------------------------------------------
# Coverage report
# ---------------------------------------------------------------------------

def coverage_report(schemas: list[dict], business_type: str) -> dict[str, list[str]]:
    """
    Compare recursively-found types against the expected set for this business type.
    Returns four lists: expected_present, missing_critical, missing_important,
    missing_nice_to_have.
    """
    expected    = BUSINESS_TYPE_SCHEMAS.get(business_type, BUSINESS_TYPE_SCHEMAS["other"])
    found_types = all_types_recursive(schemas)

    present: list[str] = sorted(t for t in expected if t in found_types)
    missing: dict[str, list[str]] = {"critical": [], "important": [], "nice-to-have": []}
    for t, priority in expected.items():
        if t not in found_types:
            missing[priority].append(t)

    return {
        "expected_present":     present,
        "missing_critical":     sorted(missing["critical"]),
        "missing_important":    sorted(missing["important"]),
        "missing_nice_to_have": sorted(missing["nice-to-have"]),
    }


# ---------------------------------------------------------------------------
# Per-page audit  (called by app.py after Playwright extraction)
# ---------------------------------------------------------------------------

def audit_page(raw_blocks: list[str], business_type: str) -> dict:
    """
    Given raw JSON-LD text blocks from a page and a business type, return the
    full structured audit result for that page (excluding url/status/notes,
    which the caller fills in).
    """
    schemas, parse_errors = extract_jsonld_from_blocks(raw_blocks)
    found = all_types_recursive(schemas)

    log.debug("  Recursive @types: %s", sorted(found))

    return {
        "schema_blocks":      len(raw_blocks),
        "schema_types_count": len(found),
        "schema_types_found": sorted(found),
        "duplicate_types":    duplicate_types(schemas),
        "coverage":           coverage_report(schemas, business_type),
        "parse_errors":       parse_errors,
    }
