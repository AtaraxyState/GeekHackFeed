#!/usr/bin/env python3
"""Pull vendor storefronts into the same feed format as the geekhack scraper.

Every supported vendor runs Shopify, which serves its whole catalogue as JSON
at /products.json. That makes this far cheaper than the forum scraper: one
request per vendor, no HTML parsing, and no cover-image pipeline at all --
Shopify's CDN resizes on demand, so an 819 KB product shot is requested as a
32 KB thumbnail instead of downloaded and shrunk locally.

    python vendors.py --open
    python vendors.py --only omnitype,meletrix
    python vendors.py --include-all      # keep add-ons and gift cards too
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import os
import re
import sys
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone

import scrape

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_SIZE = 250
MAX_PAGES = 8
THUMB_SIZE = "_600x"
EXCERPT_CHARS = 220

# Singakbd is deliberately absent: its /products.json returns 401 while the
# rest of the storefront is public, and working around that is a decision for
# whoever runs this, not a default. Add it here if you make that call.
VENDORS = {
    "omnitype": ("Omnitype", "https://omnitype.com"),
    "meletrix": ("Meletrix", "https://meletrix.com"),
    "qwertykeys": ("Qwertykeys", "https://qwertykeys.com"),
    "modedesigns": ("Mode Designs", "https://modedesigns.com"),
    "matrixlab": ("Matrix Lab", "https://www.matrixlab.store"),
}


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


def fetch_products(fetcher, base):
    """Page through /products.json until Shopify runs out of products."""
    products = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{base}/products.json?limit={PAGE_SIZE}&page={page}"
        raw = fetcher.get(url)
        if not raw:
            break
        try:
            batch = json.loads(raw).get("products", [])
        except json.JSONDecodeError:
            scrape.warn(f"{base} returned something that is not JSON")
            break
        products.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return products


# --------------------------------------------------------------------------
# categorising
# --------------------------------------------------------------------------

# What the vendors call things, mapped onto the feed's own categories. Only
# Omnitype fills this in consistently; the rest fall through to the classifier.
PRODUCT_TYPE_MAP = {
    "keycap": "Keycaps",
    "keycaps": "Keycaps",
    "keyset": "Keycaps",
    "deskpad": "Deskmats",
    "deskmat": "Deskmats",
    "desk pad": "Deskmats",
    "mousepad": "Deskmats",
    "switch": "Switches",
    "switches": "Switches",
    "cable": "Cables",
    "cables": "Cables",
    "artisan": "Artisans",
    "artisans": "Artisans",
    "keyboard": "Keyboards",
    "keyboards": "Keyboard",
    "pre-build keyboard": "Keyboards",
    "prebuilt keyboard": "Keyboards",
    "keyboard parts": "Parts & Accessories",
    "ec keyboard parts": "Parts & Accessories",
    "kb_extra_part": "Parts & Accessories",
    "accessories": "Parts & Accessories",
    "accessory": "Parts & Accessories",
    "components": "Parts & Accessories",
    "component": "Parts & Accessories",
}

# A storefront names the board in every one of its spare parts -- "Neo60 Cu
# Weight", "ZOOM65 V3 Add On - External Weight" -- and the forum scraper's
# size rule (which exists so "Shy60" reads as a keyboard) then files the whole
# parts bin under Keyboards. On a catalogue the part noun wins.
PART_NOUN_RE = re.compile(
    r"\b(weights?|plates?|pcbs?|feet|foot|housings?|sockets?|badges?|screws?|"
    r"gaskets?|foams?|daughter\s?boards?|stabili[sz]ers?|stabs?|o-?rings?|"
    r"bumpers?|dampeners?|springs?|films?|tops?|bottoms?|bezels?|knobs?|"
    r"spare\s+parts?|extra\s+parts?|replacement)\b",
    re.I,
)
# ...unless the part noun is describing one of these, which are their own thing.
PART_OVERRIDE_RE = re.compile(
    r"\bkey\s?caps?\b|\bkey\s?sets?\b|\bdesk\s?(mat|pad)s?\b|\bswitch(es)?\b|"
    r"\bcables?\b|\bartisans?\b|\bkeyboards?\b",
    re.I,
)

NOISE_RE = re.compile(
    r"gift\s?card|e-?gift|\bdonation\b|\bdeposit\b|shipping\s+(fee|cost|upgrade)|"
    r"shipping\s?protection|\btax\b|\bcoupon\b|\bsample\s+pack\b|\btest\s+product\b|"
    r"price\s+difference|\badd[\s-]?on\b|^\s*\[cfg\]|\bconfigurator\b|"
    r"make\s?up\s+(the\s+)?difference",
    re.I,
)
# Deliberately not matching "configurator": Mode tags standalone products
# (Lotus Keycaps, 65% Plate, SixtyFive Weight) as configurator-component
# because they double as build options. Dropping those loses half a catalogue
# of real products. Configurator-only entries are caught by their titles.
NOISE_TAG_RE = re.compile(r"bogos-gift|^hidden$|do-not-", re.I)


def categorise(product):
    """Vendor's own label first, then the shared keyword classifier."""
    declared = (product.get("product_type") or "").strip().lower()
    mapped = PRODUCT_TYPE_MAP.get(declared)
    if mapped:
        return mapped

    title = product.get("title") or ""
    if PART_NOUN_RE.search(title) and not PART_OVERRIDE_RE.search(title):
        return "Parts & Accessories"

    body = strip_html(product.get("body_html"), 2500)
    # Tags are the vendor's other vocabulary; worth reading as title-strength.
    tags = " ".join(product.get("tags") or [])
    return scrape.classify(f"{title} {declared} {tags}", body)


IN_STOCK_TAG_RE = re.compile(r"\bin[\s_-]?stock\b|readytoship|ready\s+to\s+ship", re.I)
GB_TAG_RE = re.compile(r"group\s?buy|\bgb\b|pre-?order", re.I)
IC_TAG_RE = re.compile(r"interest\s?check|\bic\b|upcoming", re.I)


def stage_of(product):
    """Map stock and tags onto the same lifecycle labels the forum feed uses."""
    variants = product.get("variants") or []
    available = any(v.get("available") for v in variants)
    tags = " ".join(product.get("tags") or [])
    title = product.get("title") or ""
    haystack = f"{tags} {title}"

    if IC_TAG_RE.search(haystack):
        return "Interest Check"
    if GB_TAG_RE.search(haystack):
        return "Group Buy"
    if available:
        return "In Stock"
    if IN_STOCK_TAG_RE.search(haystack):
        return "Sold Out"
    return "Sold Out"


def is_noise(product):
    if NOISE_RE.search(product.get("title") or ""):
        return True
    return any(NOISE_TAG_RE.search(t) for t in (product.get("tags") or []))


# --------------------------------------------------------------------------
# shaping
# --------------------------------------------------------------------------


def strip_html(raw, limit=EXCERPT_CHARS):
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", htmlmod.unescape(text)).strip()[:limit]


def thumbnail(src):
    """Shopify resizes on request: name_600x.jpg is served instead of the 800 KB original."""
    if not src:
        return None
    path, _, query = src.partition("?")
    sized = re.sub(r"(\.[A-Za-z]{3,4})$", THUMB_SIZE + r"\1", path)
    return f"{sized}?{query}" if query else sized


def price_of(product):
    prices = []
    for variant in product.get("variants") or []:
        try:
            prices.append(float(variant.get("price")))
        except (TypeError, ValueError):
            continue
    return min(prices) if prices else None


def to_project(product, vendor_name, base):
    images = [i.get("src") for i in (product.get("images") or []) if i.get("src")]
    # Storefronts curate their own lead image, so no scoring needed here.
    cover = thumbnail(images[0]) if images else None
    price = price_of(product)
    variants = product.get("variants") or []

    return {
        "id": int(product.get("id", 0)),
        "title": product.get("title") or "",
        "name": product.get("title") or "",
        "author": vendor_name,
        "source": vendor_name,
        "url": f"{base}/products/{product.get('handle', '')}",
        "category": categorise(product),
        "stage": stage_of(product),
        "excerpt": strip_html(product.get("body_html")),
        "images": [cover] if cover else [],
        "local_image": None,
        "price": price,
        "currency": None,
        "available": any(v.get("available") for v in variants),
        "created": product.get("published_at"),
        "last_post": product.get("updated_at"),
        "replies": 0,
        "views": 0,
        "sticky": False,
    }


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------


def collect(only=None, include_all=False, delay=1.0):
    fetcher = scrape.Fetcher(delay=delay)
    chosen = {k: v for k, v in VENDORS.items() if not only or k in only}
    if only:
        unknown = set(only) - set(VENDORS)
        if unknown:
            scrape.warn(f"unknown vendor(s): {', '.join(sorted(unknown))}")

    projects = []
    for key, (name, base) in chosen.items():
        print(f"[vendor] {name}  {base}")
        products = fetch_products(fetcher, base)
        if not products:
            scrape.warn(f"{name}: nothing returned (the catalogue API may be closed)")
            continue
        kept = [p for p in products if include_all or not is_noise(p)]
        for product in kept:
            projects.append(to_project(product, name, base))
        print(f"  {len(kept)} kept of {len(products)}")
    return projects


def summarise(projects):
    by_source, by_category, by_stage = {}, {}, {}
    for project in projects:
        by_source[project["source"]] = by_source.get(project["source"], 0) + 1
        by_category[project["category"]] = by_category.get(project["category"], 0) + 1
        by_stage[project["stage"]] = by_stage.get(project["stage"], 0) + 1

    for label, counts in (("by vendor", by_source), ("by category", by_category),
                          ("by stage", by_stage)):
        print(f"\n{label}:")
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>4}  {name}")
    covered = sum(1 for p in projects if p["images"])
    print(f"\n{covered}/{len(projects)} have a product image")


def main():
    parser = argparse.ArgumentParser(description="Scrape vendor storefronts.")
    parser.add_argument("--only", help="comma-separated vendor keys: "
                                       + ",".join(VENDORS))
    parser.add_argument("--include-all", action="store_true",
                        help="keep gift cards, add-ons and configurator entries")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--out", default=None)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    only = [k.strip().lower() for k in args.only.split(",")] if args.only else None
    projects = collect(only=only, include_all=args.include_all, delay=args.delay)
    if not projects:
        print("nothing collected")
        return 1

    out_path = args.out or os.path.join(HERE, "feed-vendors.html")
    scrape.render(projects, "vendors", out_path)
    summarise(projects)
    print(f"\nwrote {out_path}")

    if args.open:
        webbrowser.open(f"file:///{out_path.replace(os.sep, '/')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
