"""
V13 — Early-Warning Pre-Sale Probe.

Polls /event-ticket-details/{slug} every 10 seconds for newly discovered
slugs whose ticket sale has NOT yet opened, detects the 'not_yet' →
'ongoing' transition, and notifies Telegram the moment a pre-sale flips
to live.

Why:
  • Sitemap discovery surfaces a slug minutes-to-hours BEFORE the actual
    sale opens. Without this probe the bot sees the sale only at the next
    fetch_loop tick (120-300 s later) — an eternity in ticket-rush land.
  • One coroutine, one shared aiohttp session, polite cadence (10 s).
  • Targets are auto-discovered: any cached event with a ticket whose
    sale_status='not_yet' OR start_sale_date>now is added to the watch
    list, then removed once the sale flips or the slug ends.

Resource budget:
  • ~ 1 HTTP request per slug per 10 s.
  • Cap = 24 concurrent slugs (≈ 2.4 req/s sustained max).
  • Auto-stops watching a slug after `max_age_hours` regardless.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.bot import tokens as tok
from app.core.config import telegram_chat_id as _cid
from app.core.storage import (
    _ensure_event_v12_columns, list_recent_events, get_event,
)

log = logging.getLogger("pre_sale_probe")

PROBE_INTERVAL = 10.0          # seconds between polls
MAX_WATCHED_SLUGS = 24
MAX_AGE_HOURS = 72             # stop watching after 3 days
QUIET_BACKOFF = 60.0           # if nothing to watch, wait this long


# In-memory state — NOT persisted (probe is best-effort).
_state: dict[str, dict[str, Any]] = {}
# slug → {"first_seen": ts, "last_status": "not_yet"|"ongoing"|"unknown",
#         "notified": bool}


def _is_pre_sale(tickets: list[dict]) -> bool:
    """Return True if every active ticket is in 'not_yet' / future-sale state."""
    if not tickets:
        return False
    actives = [t for t in tickets if (t.get("status") or "").lower() == "active"]
    if not actives:
        return False
    for t in actives:
        sale = (t.get("sale_status") or "").lower()
        if sale == "ongoing":
            return False
    # All actives are not_yet / ended — treat as pre-sale only when at least
    # one says 'not_yet'.
    return any((t.get("sale_status") or "").lower() == "not_yet"
               for t in actives)


def _has_live_sale(tickets: list[dict]) -> bool:
    if not tickets:
        return False
    for t in tickets:
        if (t.get("status") or "").lower() != "active":
            continue
        if (t.get("sale_status") or "").lower() == "ongoing":
            return True
    return False


def _refresh_watch_list() -> None:
    """Scan storage for slugs that look like pre-sale; populate _state."""
    try:
        events = list_recent_events(limit=200, only_available=False,
                                     hide_ended=True)
    except Exception as e:
        log.debug(f"watchlist scan failed: {e}")
        return

    now = time.time()
    cutoff = now - MAX_AGE_HOURS * 3600
    # Drop expired
    for slug in list(_state.keys()):
        if _state[slug].get("first_seen", now) < cutoff:
            _state.pop(slug, None)

    for ev in events:
        slug = ev.get("slug")
        if not slug or slug in _state:
            continue
        # Inspect cached tickets payload (denormalized JSON)
        try:
            cached = get_event(slug) or {}
            tickets = cached.get("tickets") or []
        except Exception:
            tickets = []
        if _is_pre_sale(tickets):
            if len(_state) >= MAX_WATCHED_SLUGS:
                break
            _state[slug] = {
                "first_seen": now,
                "last_status": "not_yet",
                "notified": False,
            }


async def _probe_slug(slug: str, notifier) -> None:
    """Poll one slug; flip to 'ongoing' triggers a Telegram alert."""
    from app.services.webook_api import get_event_tickets

    try:
        data = await get_event_tickets(slug)
    except Exception as e:
        log.debug(f"probe {slug} fetch err: {e}")
        return

    tickets = (data or {}).get("tickets") or []
    if not tickets:
        return

    st = _state.get(slug)
    if not st:
        return

    if _has_live_sale(tickets) and not st.get("notified"):
        st["notified"] = True
        st["last_status"] = "ongoing"
        await _alert_sale_open(slug, data, notifier)
        # Once notified we stop polling this slug.
        _state.pop(slug, None)
    elif not _is_pre_sale(tickets):
        # No longer pre-sale (could be ended). Drop without alert.
        _state.pop(slug, None)


async def _alert_sale_open(slug: str, data: dict, notifier) -> None:
    """Send a luxurious 👑 Telegram alert when a pre-sale flips to live."""
    chat_id = _cid()
    if not chat_id or notifier is None:
        return
    ev = (data or {}).get("event") or {}
    title = ev.get("title") or ev.get("name") or slug
    venue = ev.get("venue_name") or ev.get("venue") or ""

    evt_tok = tok.put({"slug": slug})
    rkb = {"inline_keyboard": [
        [{"text": "⚡  احجز فوراً  ⚡", "callback_data": f"evt:{evt_tok}"}],
        [{"text": "👑 البوابة الملكية", "callback_data": "cats:menu"}],
    ]}
    txt = (
        "🚨  <b>تنبيه ملكي مبكر</b>  🚨\n"
        "══════════════════════════\n"
        f"💎 <b>{title}</b>\n"
    )
    if venue:
        txt += f"📍 {venue}\n"
    txt += (
        "══════════════════════════\n"
        "⚡ <b>تم فتح البيع للتو</b> — ثوانٍ معدودة قبل النفاد!\n"
        "👑 اضغط الزر أدناه للحجز الفوري."
    )
    try:
        await notifier.send(chat_id, txt, reply_markup=rkb)
        log.info(f"🚨 pre-sale alert dispatched for {slug}")
    except Exception as e:
        log.warning(f"pre-sale alert send failed: {e}")


async def pre_sale_probe_loop(notifier) -> None:
    """Background entry point — runs forever (cancelled at shutdown)."""
    _ensure_event_v12_columns()
    log.info("🔭 pre-sale probe started (interval=%.0fs, cap=%d)",
             PROBE_INTERVAL, MAX_WATCHED_SLUGS)

    # Bootstrap delay so the rest of startup settles first.
    await asyncio.sleep(20)

    while True:
        try:
            _refresh_watch_list()
            if not _state:
                await asyncio.sleep(QUIET_BACKOFF)
                continue

            # Probe in parallel but bounded.
            slugs = list(_state.keys())[:MAX_WATCHED_SLUGS]
            await asyncio.gather(
                *[_probe_slug(s, notifier) for s in slugs],
                return_exceptions=True,
            )
            await asyncio.sleep(PROBE_INTERVAL)
        except asyncio.CancelledError:
            log.info("🛑 pre-sale probe stopped")
            return
        except Exception as e:
            log.exception(f"pre-sale probe tick err: {e}")
            await asyncio.sleep(15)
