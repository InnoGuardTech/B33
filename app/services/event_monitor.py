"""
Background event monitor.

Single loop:
  • fetch_loop — sitemap/home discovery of new events on Webook.
    New events are upserted into the local cache and (after the bootstrap
    pass) trigger Telegram notifications.

The legacy speed-based 'sniper_loop' has been removed in v4. Booking is now
strictly user-initiated through the bot (link → blocks → confirm) and the
seat-drop watching is handled by app.services.drop_watcher (event-driven via
SeatCloud WebSocket — not by polling).
"""
from __future__ import annotations

import asyncio
import logging

from app.core.config import EVENT_POLL_INTERVAL
from app.core.storage import upsert_event
from app.services.event_discovery import enrich_all, fetch_event_slugs

log = logging.getLogger("monitor")
_BOOTSTRAPPED = False


async def fetch_loop(notifier=None) -> None:
    """Dual Monitoring System:
    1. Fast Scan (Every 2 min) - Check for specific tracked URLs and latest sitemaps.
    2. Full Scan (Every 30 min) - Deep sync of all event categories and statuses.
    """
    await asyncio.sleep(10)
    last_full_scan = 0.0
    while True:
        try:
            now = asyncio.get_event_loop().time()
            is_full = (now - last_full_scan) > 1800  # 30 minutes
            await _run_once(notifier, full_scan=is_full)
            if is_full:
                last_full_scan = now
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(f"fetch_loop error: {e}")
        # Dynamic polling: Faster check for new drops
        await asyncio.sleep(120 if not is_full else 300)


async def _run_once(notifier, full_scan: bool = False) -> None:
    global _BOOTSTRAPPED
    # Method 1: Automated Discovery
    slugs = await fetch_event_slugs(max_events=1000 if full_scan else 200)
    
    # Method 2: Tracked URLs (Implementation placeholder - can be extended to read from a DB table)
    # tracked_slugs = list_tracked_event_slugs()
    # slugs.update(tracked_slugs)

    if not slugs:
        return
    enriched = await enrich_all(slugs, concurrency=8 if full_scan else 4)
    from app.core.config import telegram_chat_id as _cid
    from app.bot import tokens as tok

    TELEGRAM_CHAT_ID = _cid()
    new_events = []
    for ev in enriched:
        is_new = upsert_event(ev["slug"], ev)
        if is_new:
            new_events.append(ev)

    if not _BOOTSTRAPPED:
        _BOOTSTRAPPED = True
        log.info(f"monitor bootstrap complete — cached {len(enriched)} events")
        return

    if not notifier or not TELEGRAM_CHAT_ID:
        return

    for ev in new_events[:5]:
        evt_tok = tok.put({"slug": ev["slug"]})
        rkb = {
            "inline_keyboard": [
                [{"text": "🎟️ فتح الفعالية", "callback_data": f"evt:{evt_tok}"}],
                [{"text": "📁 كل الفعاليات", "callback_data": "events:0"}],
            ]
        }
        txt = (
            f"🆕 <b>فعالية جديدة على Webook</b>\n\n"
            f"🎭 {ev.get('title') or ev.get('slug')}\n"
            f"🎟️ أنواع التذاكر: <b>{len(ev.get('tickets') or [])}</b>\n"
            f"🪑 محجوزة بمقاعد: <b>{'نعم' if ev.get('is_seated') else 'لا'}</b>\n\n"
            f"تم رصدها من أحدث فعاليات المنصة."
        )
        try:
            await notifier.send(TELEGRAM_CHAT_ID, txt, reply_markup=rkb)
        except Exception as e:
            log.debug(f"alert send failed: {e}")
