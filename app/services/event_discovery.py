"""
Discover Webook events primarily via the public sitemap (no Cloudflare).

Order of preference:
  1) sitemap_events_*.xml  → clean, no JS, no bot checks (~1300+ events)
  2) homepage HTML scrape  → fallback if sitemaps ever change
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp

from app.core.config import WEBOOK_ORIGIN
from app.services.webook_api import BASE_HEADERS as _H, get_event_tickets

log = logging.getLogger("discovery")

SITEMAP_INDEX = f"{WEBOOK_ORIGIN}/sitemap.xml"
EVENT_LOC_RE = re.compile(
    r"<loc>(https?://webook\.com/[^<]*?/events/[a-z0-9\-]+)</loc>", re.I,
)
SLUG_IN_URL_RE = re.compile(r"/events/([a-z0-9\-]+)", re.I)
# Keep only one URL per slug (prefer English + non-/book tail)
SKIP_SUFFIXES = ("/book", "/checkout", "/seats", "/event-info")


async def _fetch_text(session: aiohttp.ClientSession, url: str,
                       timeout: int = 15) -> str | None:
    try:
        async with session.get(
            url, headers={"user-agent": _H["user-agent"]},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status != 200:
                return None
            return await r.text()
    except Exception as e:
        log.debug(f"fetch {url}: {e}")
        return None


async def fetch_event_slugs(max_events: int = 400) -> dict[str, str]:
    """
    Returns {slug: canonical_url}.

    We scan the NEWEST sitemaps (highest numbered indices = most recent
    events) because webook appends new events to latest sitemaps.
    """
    async with aiohttp.ClientSession() as s:
        idx_txt = await _fetch_text(s, SITEMAP_INDEX, timeout=10)
        if not idx_txt:
            log.warning("sitemap index unreachable — falling back to homepage")
            return await _fallback_homepage_scrape(s)

        sub_urls = re.findall(r"<loc>([^<]+sitemap_events[^<]+)</loc>",
                               idx_txt)
        # Newest sitemap files first (higher _N.xml)
        sub_urls = sorted(
            sub_urls,
            key=lambda u: int(re.search(r"_(\d+)\.xml", u).group(1))
                         if re.search(r"_(\d+)\.xml", u) else 0,
            reverse=True,
        )

        slug_to_url: dict[str, str] = {}
        # Scan enough sitemaps to get a large candidate pool, so that after
        # filtering out ended events we still have plenty of fresh ones.
        for sm_url in sub_urls[:20]:
            txt = await _fetch_text(s, sm_url, timeout=15)
            if not txt:
                continue
            # Inside a single sitemap, later entries are usually newer —
            # reverse the list so we take them first.
            locs = list(reversed(EVENT_LOC_RE.findall(txt)))
            for loc in locs:
                if any(loc.endswith(suf) for suf in SKIP_SUFFIXES):
                    continue
                m = SLUG_IN_URL_RE.search(loc)
                if not m:
                    continue
                slug = m.group(1)
                existing = slug_to_url.get(slug)
                if existing and "/en/" in existing and "/ar/" in loc:
                    continue
                slug_to_url[slug] = loc
            if len(slug_to_url) >= max_events:
                break

    log.info(f"📡 sitemap discovered {len(slug_to_url)} event slugs")
    return dict(list(slug_to_url.items())[:max_events])


async def _fallback_homepage_scrape(session: aiohttp.ClientSession
                                     ) -> dict[str, str]:
    found: dict[str, str] = {}
    for page in [f"{WEBOOK_ORIGIN}/en", f"{WEBOOK_ORIGIN}/en/explore"]:
        txt = await _fetch_text(session, page, timeout=15)
        if not txt:
            continue
        for href in re.findall(r'href="([^"]*/events/[a-z0-9\-]+)"', txt,
                                 re.I):
            full = href if href.startswith("http") else WEBOOK_ORIGIN + href
            slug = full.rstrip("/").rsplit("/", 1)[-1]
            if slug:
                found.setdefault(slug, full)
    return found


async def enrich_slug(slug: str, url: str = "") -> dict[str, Any] | None:
    """Fetch full API data for a slug and normalize it.

    Strategy:
      • /event-detail  → authoritative Arabic title/description/metadata
      • /event-ticket-details  → ticket list (may be empty for non-active events)
    """
    from app.services.webook_api import get_event_detail

    # Run both in parallel to save ~200ms per event
    detail_task = asyncio.create_task(get_event_detail(slug))
    tix_task = asyncio.create_task(get_event_tickets(slug))
    detail = await detail_task
    tickets_data = await tix_task

    if not detail and not tickets_data:
        return None

    # event-detail is the richest source for Arabic title/description
    ev = detail or (tickets_data or {}).get("event") or {}
    tickets = (tickets_data or {}).get("tickets") or []

    # Skip events that ended > 1 day ago
    import time as _t
    end_ts = ev.get("end_date_time") or 0
    if end_ts and end_ts < _t.time() - 86400:
        return None

    # Also skip events that have no title at all (dead slugs)
    if not (ev.get("title") or ev.get("name")):
        # Check if it's a private event that only returns data on specific endpoints
        if not tickets_data:
            return None

    # Extract city (e.g. /en/SA/RUH/...) when present
    city = None
    m = re.search(r"/SA/([A-Z]{3})/", url)
    if m:
        city = m.group(1)

    # Category segment
    category = None
    # Dynamic category extraction from URL or detail
    category = ev.get("category_name") or ev.get("category_slug")
    if not category:
        m = re.search(r"/([^/]+)/events/", url)
        if m:
            category = m.group(1)

    title = ev.get("title") or ev.get("name") or slug
    return {
        "slug": slug,
        "title": title,
        "url": url,
        "city": city,
        "category": category,
        "is_seated": bool(ev.get("is_seated")),
        "poster": (ev.get("poster") or ev.get("mobile_poster")
                   or ev.get("promo_poster") or ""),
        "start_date": ev.get("start_date_time"),
        "tickets": tickets,
    }


async def enrich_all(slugs: dict[str, str], concurrency: int = 5
                     ) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)

    async def _one(slug, url):
        async with sem:
            try:
                return await enrich_slug(slug, url)
            except Exception as e:
                log.debug(f"enrich {slug} failed: {e}")
                return None

    results = await asyncio.gather(
        *[_one(s, u) for s, u in slugs.items()],
    )
    enriched = [r for r in results if r]

    # Sort newest first (start_date desc), fallback to 0
    enriched.sort(key=lambda e: e.get("start_date") or 0, reverse=True)
    return enriched
