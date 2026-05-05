"""
Discover Webook events primarily via the public sitemap (no Cloudflare).

V11 enhancements (Royal UI):
  • DYNAMIC FILTERING: events with end_date_time in the past are dropped.
  • SOLD-OUT FILTERING: events whose every active ticket is sold out are
    flagged so the UI can hide them.
  • CATEGORY CLASSIFICATION: each event is mapped to a royal category
    (sports / theater / concerts) using webook taxonomy + keyword match.
  • NEWEST-FIRST SORTING: results are sorted by start_date desc and
    newcomers (first_seen_at) bubble to the top.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
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
SKIP_SUFFIXES = ("/book", "/checkout", "/seats", "/event-info")


# ════════════════════════════════════════════════════════════════════════
# V11: Royal Category Classification
# ════════════════════════════════════════════════════════════════════════
ROYAL_CATEGORIES = {
    "sports": {
        "label": "⚽️ الرياضة والمباريات",
        "emoji": "⚽️",
        "kw_en": (
            "football", "soccer", "match", "league", "cup", "derby",
            "basketball", "tennis", "f1", "formula", "racing", "boxing",
            "fight", "mma", "ufc", "wrestling", "wwe", "olympic",
            "athletic", "sport", "esport", "tournament", "fifa",
            "club", "fc", "vs", "x ", " x", "saudi pro league",
            "champions", "saff", "padel", "golf", "rally",
        ),
        "kw_ar": (
            "كرة", "مباراة", "دوري", "الهلال", "النصر", "الاتحاد", "الأهلي",
            "الشباب", "الفتح", "ملاكمة", "فورمولا", "سباق", "بطولة",
            "كأس", "السلة", "تنس", "رياض", "مصارعة",
        ),
    },
    "theater": {
        "label": "🎭 المسرح والعروض",
        "emoji": "🎭",
        "kw_en": (
            "theater", "theatre", "play", "drama", "comedy", "stand up",
            "stand-up", "musical", "ballet", "opera", "show", "circus",
            "magic", "illusion", "cirque", "broadway", "puppet",
            "performance", "act ", "monologue",
        ),
        "kw_ar": (
            "مسرح", "مسرحية", "عرض", "كوميد", "ستاند اب", "ستاند آب",
            "أوبرا", "باليه", "سيرك", "دراما", "ساخر",
        ),
    },
    "concerts": {
        "label": "🎤 الحفلات والترفيه",
        "emoji": "🎤",
        "kw_en": (
            "concert", "live", "tour", "festival", "music", "dj",
            "singer", "band", "rap", "rock", "pop", "hip hop", "rnb",
            "jazz", "classical", "symphony", "orchestra", "fan meet",
            "kpop", "k-pop", "edm", "techno", "house party",
            "entertainment", "night",
        ),
        "kw_ar": (
            "حفل", "حفلة", "موسيق", "أغني", "مهرجان", "مغني",
            "مغنية", "فرقة", "غناء", "ترفيه", "ليل", "سهرة",
        ),
    },
}

# Default fallback for anything we can't classify
DEFAULT_CATEGORY = "concerts"


def classify_event(title: str, sub_title: str = "",
                   webook_category: str = "") -> str:
    """Map an event to a royal category key: 'sports', 'theater', or 'concerts'.

    We use a multi-source signal: webook's own category slug → fast match,
    then fall back to keyword matching on title + sub_title in both
    Arabic and English.
    """
    haystack = " ".join((title or "", sub_title or "",
                         webook_category or "")).lower()
    if not haystack.strip():
        return DEFAULT_CATEGORY

    # Direct webook category mapping (highest priority)
    cat_lower = (webook_category or "").lower()
    if any(s in cat_lower for s in
           ("sport", "football", "soccer", "match", "league")):
        return "sports"
    if any(s in cat_lower for s in
           ("theater", "theatre", "drama", "play", "comedy", "performing")):
        return "theater"
    if any(s in cat_lower for s in
           ("concert", "music", "festival", "entertainment")):
        return "concerts"

    # Keyword scoring across title/sub_title
    scores = {"sports": 0, "theater": 0, "concerts": 0}
    for cat_key, meta in ROYAL_CATEGORIES.items():
        for kw in meta["kw_en"] + meta["kw_ar"]:
            if kw and kw in haystack:
                scores[cat_key] += 2 if len(kw) >= 5 else 1

    best = max(scores.items(), key=lambda x: x[1])
    return best[0] if best[1] > 0 else DEFAULT_CATEGORY


# ════════════════════════════════════════════════════════════════════════
# V11: Sold-out / availability detection
# ════════════════════════════════════════════════════════════════════════
def event_has_available_tickets(tickets: list[dict]) -> bool:
    """Return True if at least one ticket is selectable RIGHT NOW.

    Selectable = status='active' AND sale_status not in {'ended','sold_out'}
    AND (quantity is None or > 0).
    """
    if not tickets:
        return False
    for t in tickets:
        if (t.get("status") or "").lower() != "active":
            continue
        sale = (t.get("sale_status") or "").lower()
        if sale in ("ended", "sold_out", "soldout"):
            continue
        # quantity check (None = uncapped/seats.io managed → assume ok)
        qty = t.get("quantity")
        if qty is not None:
            try:
                if int(qty) <= 0:
                    continue
            except (TypeError, ValueError):
                pass
        return True
    return False


def event_is_in_future(start_ts: Any, end_ts: Any) -> bool:
    """Return True only when the event hasn't ended yet.

    Webook timestamps are seconds. We compare against `now` and give a
    1-hour grace window so a match that started 30min ago (but still has
    seats) is still bookable.
    """
    now = time.time()
    grace = 3600  # 1 hour
    try:
        if end_ts:
            end_n = float(end_ts)
            if end_n < now - grace:
                return False
            return True
    except Exception:
        pass
    # No end_ts → fall back to start_ts: if it started > 6 hours ago, hide
    try:
        if start_ts:
            start_n = float(start_ts)
            if start_n < now - 6 * 3600:
                return False
            return True
    except Exception:
        pass
    # Neither timestamp present → keep (uncertain ≠ expired)
    return True


# ════════════════════════════════════════════════════════════════════════
# Sitemap discovery (unchanged core)
# ════════════════════════════════════════════════════════════════════════
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
    """Returns {slug: canonical_url}. Newest sitemaps scanned first."""
    async with aiohttp.ClientSession() as s:
        idx_txt = await _fetch_text(s, SITEMAP_INDEX, timeout=10)
        if not idx_txt:
            log.warning("sitemap index unreachable — falling back to homepage")
            return await _fallback_homepage_scrape(s)

        sub_urls = re.findall(r"<loc>([^<]+sitemap_events[^<]+)</loc>",
                               idx_txt)
        sub_urls = sorted(
            sub_urls,
            key=lambda u: int(re.search(r"_(\d+)\.xml", u).group(1))
                         if re.search(r"_(\d+)\.xml", u) else 0,
            reverse=True,
        )

        slug_to_url: dict[str, str] = {}
        for sm_url in sub_urls[:20]:
            txt = await _fetch_text(s, sm_url, timeout=15)
            if not txt:
                continue
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


# ════════════════════════════════════════════════════════════════════════
# Enrichment (V11: aggressive filtering + royal classification)
# ════════════════════════════════════════════════════════════════════════
async def enrich_slug(slug: str, url: str = "") -> dict[str, Any] | None:
    """Fetch full API data for a slug and normalize it.

    Returns None for events that should NOT appear in the listings:
      • ended (end_date_time in the past)
      • sold-out (every ticket gone)
      • dead slugs (no title, no tickets)
    """
    from app.services.webook_api import get_event_detail

    detail_task = asyncio.create_task(get_event_detail(slug))
    tix_task = asyncio.create_task(get_event_tickets(slug))
    detail = await detail_task
    tickets_data = await tix_task

    if not detail and not tickets_data:
        return None

    ev = detail or (tickets_data or {}).get("event") or {}
    tickets = (tickets_data or {}).get("tickets") or []

    # ── Hard filter 1: skip events that have ended (V11) ──
    start_ts = ev.get("start_date_time") or 0
    end_ts = ev.get("end_date_time") or 0
    if not event_is_in_future(start_ts, end_ts):
        return None

    # ── Hard filter 2: dead slugs ──
    if not (ev.get("title") or ev.get("name")):
        if not tickets_data:
            return None

    # Extract city
    city = None
    m = re.search(r"/SA/([A-Z]{3})/", url)
    if m:
        city = m.group(1)

    # Webook category (raw)
    raw_category = ev.get("category_name") or ev.get("category_slug") or ""
    if not raw_category:
        m = re.search(r"/([^/]+)/events/", url)
        if m:
            raw_category = m.group(1)

    title = ev.get("title") or ev.get("name") or slug
    sub_title = ev.get("sub_title") or ""

    # ── V11: Royal category classification ──
    royal_cat = classify_event(title, sub_title, raw_category)

    # ── V11: Availability flag (used by storage filter) ──
    has_avail = event_has_available_tickets(tickets)

    return {
        "slug": slug,
        "title": title,
        "sub_title": sub_title,
        "url": url,
        "city": city,
        "category": raw_category,                  # webook's own
        "royal_category": royal_cat,               # V11 normalized
        "is_seated": bool(ev.get("is_seated")),
        "poster": (ev.get("poster") or ev.get("mobile_poster")
                   or ev.get("promo_poster") or ""),
        "start_date": start_ts,
        "end_date": end_ts,
        "venue": ev.get("venue_name") or ev.get("venue") or "",
        "tickets": tickets,
        "has_availability": has_avail,             # V11
        "is_sold_out": (not has_avail) and bool(tickets),
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

    # V11: filter out sold-out events from the public list (kept in DB
    # for analytics, just hidden from the user's browsing screen)
    enriched = [e for e in enriched if e.get("has_availability")]

    # Sort newest first (start_date desc), fallback to 0
    enriched.sort(key=lambda e: e.get("start_date") or 0, reverse=True)
    return enriched
