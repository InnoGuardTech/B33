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
    await asyncio.sleep(10)
    while True:
        try:
            await _run_once(notifier)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(f"fetch_loop error: {e}")
        await asyncio.sleep(EVENT_POLL_INTERVAL)


async def _run_once(notifier) -> None:
    global _BOOTSTRAPPED
    slugs = await fetch_event_slugs()
    if not slugs:
        return
    enriched = await enrich_all(slugs, concurrency=4)
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
