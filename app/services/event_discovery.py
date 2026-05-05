"""
Webook event discovery + V12 Royal categorization.

V12 (Ultimate Royal):
  • DYNAMIC FILTERING: events with end_date_time in the past are dropped.
  • SOLD-OUT FILTERING: events whose every active ticket is sold out are
    flagged so the UI can hide them.
  • OFFICIAL CATEGORY MAPPING: maps the literal `category` value returned
    by Webook's own API (e.g. 'Sport Event', 'Music Event',
    'Theater Event', 'Entertainment Experience', 'Exhibition Event',
    'Conference Event') to one of the 5 royal sections that mirror the
    public webook.com top-nav.
  • KEYWORD FALLBACK: if the API field is empty, classify via bilingual
    (AR/EN) keyword scoring on title + sub_title + url.
  • NEWEST-FIRST SORTING: enriched results are sorted by start_date desc.
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
EXPERIENCE_LOC_RE = re.compile(
    r"<loc>(https?://webook\.com/[^<]*?/experiences/[a-z0-9\-]+)</loc>", re.I,
)
SLUG_IN_URL_RE = re.compile(r"/(?:events|experiences)/([a-z0-9\-]+)", re.I)
SKIP_SUFFIXES = ("/book", "/checkout", "/seats", "/event-info")


# ════════════════════════════════════════════════════════════════════════
# V12: Royal Category Catalogue — mirrors webook.com top-navigation.
# Five royal sections (the same five every webook.com user sees in the
# header). Each section carries:
#   • label_ar: Arabic display label used in the UI.
#   • emoji:    royal emoji used as the section dot.
#   • api_categories: exact strings emitted by Webook's API in the
#                     `category` field of /event-detail/{slug} (case-
#                     insensitive substring match).
#   • slug_hits:  url path tokens that appear when the API field is empty.
#   • kw_en / kw_ar: bilingual keyword fallback for keyword scoring.
# ════════════════════════════════════════════════════════════════════════
ROYAL_CATEGORIES: dict[str, dict[str, Any]] = {
    "sports": {
        "label_ar": "الرياضة والمباريات",
        "label_en": "Sports & Matches",
        "emoji": "⚽️",
        "api_categories": (
            "sport event", "sports event", "sport", "sports",
            "match", "league", "football", "soccer", "boxing",
            "mma", "ufc", "tennis", "f1", "formula", "racing",
            "fight", "wrestling",
        ),
        "slug_hits": (
            "spl-", "match-", "vs-", "-vs-", "-x-", "fc-",
            "boxing", "mma", "ufc", "tennis", "f1", "formula",
            "football", "soccer", "league", "kickoff",
        ),
        "kw_en": (
            "football", "soccer", "match", "league", "cup", "derby",
            "basketball", "tennis", "f1", "formula", "racing", "boxing",
            "fight", "mma", "ufc", "wrestling", "wwe", "olympic",
            "athletic", "sport", "esport", "tournament", "fifa",
            "club", "fc ", " fc", "vs", "x ", " x", "padel", "golf",
            "rally", "champions", "saff", "spl",
        ),
        "kw_ar": (
            "كرة", "مباراة", "دوري", "الهلال", "النصر", "الاتحاد",
            "الأهلي", "الشباب", "الفتح", "ملاكمة", "فورمولا", "سباق",
            "بطولة", "كأس", "السلة", "تنس", "رياض", "مصارعة",
            "نزال", "نزالات", "دربي",
        ),
    },
    "concerts": {
        "label_ar": "الموسيقى والحفلات",
        "label_en": "Music & Concerts",
        "emoji": "🎤",
        "api_categories": (
            "music event", "music", "concert", "concert event",
            "festival event", "festival", "live music",
            "jalsat", "jalsah", "musical event",
        ),
        "slug_hits": (
            "concert", "music", "festival", "jalsat", "jalsah",
            "tour", "live-", "-live", "fan-meet", "kpop", "k-pop",
            "edm", "dj-",
        ),
        "kw_en": (
            "concert", "live", "tour", "festival", "music", "dj",
            "singer", "band", "rap", "rock", "pop", "hip hop",
            "rnb", "jazz", "classical", "symphony", "orchestra",
            "fan meet", "kpop", "k-pop", "edm", "techno",
            "house party", "jalsat", "jalsah", "night",
        ),
        "kw_ar": (
            "حفل", "حفلة", "موسيق", "أغني", "مهرجان", "مغني",
            "مغنية", "فرقة", "غناء", "سهرة", "جلسة", "جلسات",
            "ليلة", "مطرب", "مطربة", "صوت",
        ),
    },
    "theater": {
        "label_ar": "المسرح والفنون",
        "label_en": "Theater & Performing Arts",
        "emoji": "🎭",
        "api_categories": (
            "theater event", "theatre event", "theater", "theatre",
            "performing arts", "drama event", "drama", "show event",
            "comedy event", "stand-up", "stand up", "musical theatre",
            "ballet", "opera",
        ),
        "slug_hits": (
            "theater", "theatre", "play-", "drama", "comedy",
            "stand-up", "musical-", "ballet", "opera", "show-",
            "circus",
        ),
        "kw_en": (
            "theater", "theatre", "play", "drama", "comedy",
            "stand up", "stand-up", "musical", "ballet", "opera",
            "show", "circus", "magic", "illusion", "cirque",
            "broadway", "puppet", "performance", "monologue",
        ),
        "kw_ar": (
            "مسرح", "مسرحية", "كوميد", "ستاند اب", "ستاند آب",
            "أوبرا", "باليه", "سيرك", "دراما", "ساخر", "تمثيل",
            "عرض حي", "العرض الحي",
        ),
    },
    "experiences": {
        "label_ar": "الترفيه والتجارب",
        "label_en": "Entertainment & Experiences",
        "emoji": "🎡",
        "api_categories": (
            "entertainment experience", "experience event",
            "experience", "tourism", "tourist", "amusement",
            "theme park", "attraction", "kids event",
            "family event", "edutainment", "boulevard",
            "riyadh season", "season event",
        ),
        "slug_hits": (
            "experience", "tour-", "tourism", "theme-park",
            "amusement", "boulevard", "riyadh-season",
            "season-", "kids-", "family-", "cruise",
        ),
        "kw_en": (
            "experience", "entertainment", "tour", "tourism",
            "amusement", "theme park", "attraction", "park",
            "kids", "family", "boulevard", "riyadh season",
            "winter wonderland", "edutainment", "zoo",
            "aquarium", "cruise", "boat", "yacht",
        ),
        "kw_ar": (
            "تجربة", "تجارب", "ترفيه", "سياح", "مدينة ملاهي",
            "ملاهي", "متنزه", "أطفال", "عائلة", "بوليفارد",
            "موسم الرياض", "حديقة", "كروز", "يخت",
        ),
    },
    "exhibitions": {
        "label_ar": "المعارض والمتاحف",
        "label_en": "Exhibitions & Museums",
        "emoji": "🖼",
        "api_categories": (
            "exhibition event", "exhibition", "exhibitions",
            "conference event", "conference", "summit",
            "forum event", "forum", "expo", "trade show",
            "museum", "art exhibition", "workshop event",
            "workshop", "cultural event", "culture event",
        ),
        "slug_hits": (
            "exhibition", "expo", "conference", "forum",
            "summit", "museum", "workshop", "moc", "biennale",
            "art-", "culture-",
        ),
        "kw_en": (
            "exhibition", "expo", "conference", "summit",
            "forum", "trade show", "museum", "art exhibition",
            "biennale", "workshop", "culture", "cultural",
            "heritage", "gallery",
        ),
        "kw_ar": (
            "معرض", "معارض", "متحف", "متاحف", "مؤتمر", "مؤتمرات",
            "منتدى", "قمة", "ورشة", "ورش", "ثقاف", "تراث",
            "بينالي", "صالون فني", "جاليري",
        ),
    },
}

# Default fallback for anything we can't classify
DEFAULT_CATEGORY = "experiences"

# Royal category keys in display order (mirrors webook.com top-nav).
ROYAL_CATEGORY_ORDER = ("sports", "concerts", "theater",
                         "experiences", "exhibitions")


def _haystack(*parts: str) -> str:
    return " ".join(p for p in parts if p).lower()


def classify_event(title: str, sub_title: str = "",
                   webook_category: str = "",
                   url: str = "") -> str:
    """Map an event to a royal category key.

    Priority chain (highest first):
      1. Webook's own API `category` field (literal substring match).
      2. URL/slug token match (sitemap path tokens).
      3. Bilingual keyword scoring on title + sub_title.
    Falls back to 'experiences' (the safest, broadest section).
    """
    cat_lower = (webook_category or "").lower().strip()
    url_lower = (url or "").lower()
    text = _haystack(title, sub_title)

    # ── 1) Direct API category match — highest confidence ─────────────
    if cat_lower:
        for key in ROYAL_CATEGORY_ORDER:
            for needle in ROYAL_CATEGORIES[key]["api_categories"]:
                if needle in cat_lower:
                    return key

    # ── 2) URL slug hit ───────────────────────────────────────────────
    if url_lower:
        for key in ROYAL_CATEGORY_ORDER:
            for needle in ROYAL_CATEGORIES[key]["slug_hits"]:
                if needle in url_lower:
                    return key

    # ── 3) Keyword scoring ────────────────────────────────────────────
    if text.strip():
        scores = {key: 0 for key in ROYAL_CATEGORY_ORDER}
        for key in ROYAL_CATEGORY_ORDER:
            meta = ROYAL_CATEGORIES[key]
            for kw in meta["kw_en"] + meta["kw_ar"]:
                if kw and kw in text:
                    scores[key] += 2 if len(kw) >= 5 else 1
        best_key, best_score = max(scores.items(), key=lambda kv: kv[1])
        if best_score > 0:
            return best_key

    return DEFAULT_CATEGORY


# ════════════════════════════════════════════════════════════════════════
# Sold-out / availability detection
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
    """Return True only when the event hasn't ended yet."""
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
    try:
        if start_ts:
            start_n = float(start_ts)
            if start_n < now - 6 * 3600:
                return False
            return True
    except Exception:
        pass
    return True


# ════════════════════════════════════════════════════════════════════════
# Sitemap discovery
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
    """Returns {slug: canonical_url}. Newest sitemaps scanned first.

    V12 also pulls /experiences/* sitemaps so the experiences/exhibitions
    sections see fresh content too.
    """
    async with aiohttp.ClientSession() as s:
        idx_txt = await _fetch_text(s, SITEMAP_INDEX, timeout=10)
        if not idx_txt:
            log.warning("sitemap index unreachable — falling back to homepage")
            return await _fallback_homepage_scrape(s)

        ev_subs = re.findall(r"<loc>([^<]+sitemap_events[^<]+)</loc>",
                              idx_txt)
        ex_subs = re.findall(r"<loc>([^<]+sitemap_experiences[^<]+)</loc>",
                              idx_txt)

        def _sort_key(u: str) -> int:
            m = re.search(r"_(\d+)\.xml", u)
            return int(m.group(1)) if m else 0

        ev_subs = sorted(ev_subs, key=_sort_key, reverse=True)
        ex_subs = sorted(ex_subs, key=_sort_key, reverse=True)

        slug_to_url: dict[str, str] = {}
        for sm_url in ev_subs[:20] + ex_subs[:8]:
            txt = await _fetch_text(s, sm_url, timeout=15)
            if not txt:
                continue
            locs = list(reversed(
                EVENT_LOC_RE.findall(txt)
                + EXPERIENCE_LOC_RE.findall(txt)
            ))
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

    log.info(f"📡 sitemap discovered {len(slug_to_url)} slugs")
    return dict(list(slug_to_url.items())[:max_events])


async def _fallback_homepage_scrape(session: aiohttp.ClientSession
                                     ) -> dict[str, str]:
    found: dict[str, str] = {}
    for page in [f"{WEBOOK_ORIGIN}/en", f"{WEBOOK_ORIGIN}/en/explore",
                  f"{WEBOOK_ORIGIN}/ar"]:
        txt = await _fetch_text(session, page, timeout=15)
        if not txt:
            continue
        for href in re.findall(
                r'href="([^"]*/(?:events|experiences)/[a-z0-9\-]+)"',
                txt, re.I):
            full = href if href.startswith("http") else WEBOOK_ORIGIN + href
            slug = full.rstrip("/").rsplit("/", 1)[-1]
            if slug:
                found.setdefault(slug, full)
    return found


# ════════════════════════════════════════════════════════════════════════
# Enrichment (V12: aggressive filtering + royal classification)
# ════════════════════════════════════════════════════════════════════════
async def enrich_slug(slug: str, url: str = "") -> dict[str, Any] | None:
    """Fetch full API data for a slug and normalize it."""
    from app.services.webook_api import get_event_detail

    detail_task = asyncio.create_task(get_event_detail(slug))
    tix_task = asyncio.create_task(get_event_tickets(slug))
    detail = await detail_task
    tickets_data = await tix_task

    if not detail and not tickets_data:
        return None

    ev = detail or (tickets_data or {}).get("event") or {}
    tickets = (tickets_data or {}).get("tickets") or []

    # ── Hard filter 1: skip events that have ended ──
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

    # Webook category — V12 uses the literal `category` API field first.
    raw_category = (
        ev.get("category")              # ← Webook's own field (e.g. "Music Event")
        or ev.get("category_name")
        or ev.get("category_slug")
        or ""
    )
    if not raw_category:
        m = re.search(r"/([^/]+)/events/", url)
        if m:
            raw_category = m.group(1)

    title = ev.get("title") or ev.get("name") or slug
    sub_title = ev.get("sub_title") or ""

    # ── V12: Royal category classification (uses url too) ──
    royal_cat = classify_event(title, sub_title, raw_category, url)

    # ── Availability flag (used by storage filter) ──
    has_avail = event_has_available_tickets(tickets)

    return {
        "slug": slug,
        "title": title,
        "sub_title": sub_title,
        "url": url,
        "city": city,
        "category": raw_category,                  # webook's own
        "royal_category": royal_cat,               # V12 normalized
        "is_seated": bool(ev.get("is_seated")),
        "poster": (ev.get("poster") or ev.get("mobile_poster")
                   or ev.get("promo_poster") or ""),
        "start_date": start_ts,
        "end_date": end_ts,
        "venue": ev.get("venue_name") or ev.get("venue") or "",
        "tickets": tickets,
        "has_availability": has_avail,
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

    # V12: filter out sold-out events from the public list
    enriched = [e for e in enriched if e.get("has_availability")]

    # Sort newest first (start_date desc), fallback to 0
    enriched.sort(key=lambda e: e.get("start_date") or 0, reverse=True)
    return enriched
