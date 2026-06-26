#!/usr/bin/env python3
"""
schema_audit.py  —  detection-only module

All JSON-LD extraction, type detection, duplicate analysis, page-type
classification, and coverage reporting lives here. No I/O, no HTTP.
Imported by app.py.

Detection logic (extraction, recursive types, duplicates) is reviewed and
stable — do not change without explicit review.
"""

import json
import logging
import re
from collections import Counter
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Business-type baseline  (site-level expectations, evaluated once per audit)
# ---------------------------------------------------------------------------

BUSINESS_TYPE_BASELINE: dict[str, dict[str, str]] = {
    "ecommerce": {
        "Organization": "relevant",
        "WebSite":      "relevant",
    },
    "local_business": {
        "Organization": "relevant",
        "WebSite":      "relevant",
        "LocalBusiness": "relevant",
    },
    "blog_media": {
        "Organization": "relevant",
        "WebSite":      "relevant",
    },
    "professional_services": {
        "Organization": "relevant",
        "WebSite":      "relevant",
    },
    "restaurant": {
        "Organization": "relevant",
        "WebSite":      "relevant",
        "LocalBusiness": "relevant",
        "Restaurant":    "relevant",
    },
    "other": {
        "Organization": "helpful",
        "WebSite":      "helpful",
    },
}

# ---------------------------------------------------------------------------
# Per-page-type expected schema sets  (page-specific only, no site-wide types)
# Priority levels: "relevant" and "helpful" — suggestions, not requirements.
# ---------------------------------------------------------------------------

PAGE_TYPE_SCHEMAS: dict[str, dict[str, str]] = {
    "article": {
        "Article":        "relevant",
        "BreadcrumbList": "relevant",
        "Person":         "helpful",
        "ImageObject":    "helpful",
    },
    "product": {
        "Product":         "relevant",
        "Offer":           "relevant",
        "AggregateRating": "relevant",
        "Review":          "relevant",
        "BreadcrumbList":  "helpful",
    },
    "location": {
        "LocalBusiness":             "relevant",
        "Place":                     "relevant",
        "GeoCoordinates":            "relevant",
        "OpeningHoursSpecification": "relevant",
        "Review":                    "helpful",
        "AggregateRating":           "helpful",
    },
    "service": {
        "Service":         "relevant",
        "AggregateRating": "helpful",
        "Review":          "helpful",
        "FAQPage":         "helpful",
        "BreadcrumbList":  "helpful",
    },
    "homepage": {
        "BreadcrumbList": "helpful",
        "SearchAction":   "helpful",
    },
    "contact": {
        "ContactPoint":  "relevant",
        "PostalAddress": "helpful",
    },
    "about": {
        "AboutPage": "helpful",
    },
    "menu": {
        "Menu":       "relevant",
        "Restaurant": "relevant",
    },
    "booking": {
        "Reservation": "helpful",
    },
    "faq": {
        "FAQPage": "relevant",
    },
    "general": {
        "BreadcrumbList": "helpful",
        "WebPage":        "helpful",
    },
}

# ---------------------------------------------------------------------------
# Page-type URL pattern hints  (English + Spanish, case-insensitive)
# ---------------------------------------------------------------------------

_URL_HINTS: list[tuple[str, re.Pattern]] = [
    ("article",  re.compile(r"/(blog|news|article|knowledge|guide|post|noticias|articulo)(/|$)", re.I)),
    ("product",  re.compile(r"/(product|shop|item|store|producto|productos|tienda)(/|$)", re.I)),
    ("location", re.compile(r"/(location|branch|sede|ubicacion|local)(/|$)", re.I)),
    ("service",  re.compile(r"/(services?|servicios?)(/|$)", re.I)),
    ("contact",  re.compile(r"/(contact|contact-us|contacto|contactanos|contactenos)(/|$)", re.I)),
    ("about",    re.compile(r"/(about|about-us|quienes-somos|sobre-nosotros|nosotros)(/|$)", re.I)),
    ("menu",     re.compile(r"/(menu|menú|carta)(/|$)", re.I)),
    ("booking",  re.compile(r"/(book|book-online|booking|reservations?|reservas?|reservar)(/|$)", re.I)),
    ("faq",      re.compile(r"/(faqs?|preguntas|preguntas-frecuentes)(/|$)", re.I)),
]

# ---------------------------------------------------------------------------
# Supporting (structural/plumbing) schema types
# These are nested helpers rather than meaningful top-level schemas.
# Exception: if a type appears in PAGE_TYPE_SCHEMAS for the page's classified
# type, it is promoted to primary — it matters to the user for that page type.
# ---------------------------------------------------------------------------

SUPPORTING_TYPES: set[str] = {
    "EntryPoint",
    "PropertyValueSpecification",
    "ListItem",
    "ReadAction",
    "CommentAction",
    "SearchAction",
    "Action",
    "ImageObject",
    "ContactPoint",
    "PostalAddress",
    "GeoCoordinates",
    "QuantitativeValue",
    "MonetaryAmount",
    "OpeningHoursSpecification",
}

# Schema types that signal each page type  (checked first, higher confidence)
_SCHEMA_SIGNALS: list[tuple[str, set[str]]] = [
    ("article",  {"Article", "BlogPosting", "NewsArticle"}),
    ("product",  {"Product", "Offer"}),
    ("location", {"LocalBusiness", "Place", "Restaurant"}),
    ("service",  {"Service"}),
]

# ---------------------------------------------------------------------------
# JSON-LD extraction  (unchanged — reviewed and stable)
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
# Type detection helpers  (unchanged — reviewed and stable)
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
# Duplicate detection  (unchanged — top-level only, intentional)
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
# Page-type classifier
# ---------------------------------------------------------------------------

def classify_page(url: str, found_types: set[str]) -> str:
    """
    Classify a page into a page type using schema types first (most reliable),
    then URL path hints, then homepage detection, then fallback to general.
    """
    for page_type, signals in _SCHEMA_SIGNALS:
        if found_types & signals:
            return page_type

    path = unquote(urlparse(url).path).rstrip("/")
    if not path:
        return "homepage"

    for page_type, pattern in _URL_HINTS:
        if pattern.search(path):
            return page_type

    return "general"


# ---------------------------------------------------------------------------
# Per-page coverage report  (page-specific expectations only)
# ---------------------------------------------------------------------------

def coverage_report(schemas: list[dict], url: str) -> dict:
    """
    Classify the page by type, then compare recursively-found schema types
    against page-type-specific expectations only (not site-wide baseline).
    """
    found_types = all_types_recursive(schemas)
    page_type   = classify_page(url, found_types)
    expected    = PAGE_TYPE_SCHEMAS.get(page_type, PAGE_TYPE_SCHEMAS["general"])

    present: list[str] = sorted(t for t in expected if t in found_types)
    suggested: dict[str, list[str]] = {"relevant": [], "helpful": []}
    for t, priority in expected.items():
        if t not in found_types:
            suggested[priority].append(t)

    return {
        "page_type":          page_type,
        "present_expected":   present,
        "suggested_relevant": sorted(suggested["relevant"]),
        "suggested_helpful":  sorted(suggested["helpful"]),
    }


# ---------------------------------------------------------------------------
# Site-level report  (runs once per audit, not per page)
# ---------------------------------------------------------------------------

def site_level_report(all_page_types: list[set[str]], business_type: str) -> dict:
    """
    Compare the union of all schema types found across the entire site against
    the business-type baseline. A site-wide schema (e.g. Organization) only
    needs to exist on ONE page to count as present.
    """
    baseline    = BUSINESS_TYPE_BASELINE.get(business_type, BUSINESS_TYPE_BASELINE["other"])
    site_union  = set().union(*all_page_types) if all_page_types else set()

    present: list[str] = sorted(t for t in baseline if t in site_union)
    missing: dict[str, list[str]] = {"relevant": [], "helpful": []}
    for t, priority in baseline.items():
        if t not in site_union:
            missing[priority].append(t)

    return {
        "business_type":               business_type,
        "site_schemas_present":        present,
        "site_schemas_missing_relevant": sorted(missing["relevant"]),
        "site_schemas_missing_helpful":  sorted(missing["helpful"]),
    }


# ---------------------------------------------------------------------------
# Per-page audit
# ---------------------------------------------------------------------------

def audit_page(raw_blocks: list[str], url: str) -> dict:
    """
    Given raw JSON-LD text blocks and the page URL, return the full structured
    audit result (excluding url/status/notes, which the caller fills in).

    Page-type classification drives per-page coverage suggestions.
    Site-wide baseline is handled separately by site_level_report().
    """
    schemas, parse_errors = extract_jsonld_from_blocks(raw_blocks)
    found = all_types_recursive(schemas)

    log.debug("  Recursive @types: %s", sorted(found))

    cov = coverage_report(schemas, url)
    page_type = cov["page_type"]

    expected_for_page = set(PAGE_TYPE_SCHEMAS.get(page_type, PAGE_TYPE_SCHEMAS["general"]))

    primary    = sorted(t for t in found if t not in SUPPORTING_TYPES or t in expected_for_page)
    supporting = sorted(t for t in found if t in SUPPORTING_TYPES and t not in expected_for_page)

    return {
        "page_type":           page_type,
        "schema_blocks":       len(raw_blocks),
        "schema_types_count":  len(found),
        "schema_types_found":  sorted(found),
        "primary_types":       primary,
        "primary_types_count": len(primary),
        "supporting_types":    supporting,
        "duplicate_types":     duplicate_types(schemas),
        "coverage": {
            "present_expected":   cov["present_expected"],
            "suggested_relevant": cov["suggested_relevant"],
            "suggested_helpful":  cov["suggested_helpful"],
        },
        "parse_errors": parse_errors,
    }
