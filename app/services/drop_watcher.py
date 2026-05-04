"""
Drop Watcher — event-driven seat-release sniping.

When the user requested a quantity but the chart was full, we register
a watcher (drop_watchers table). A single background loop:

  1. Groups watchers by event_key  → opens ONE WebSocket per event_key
     (the "WS multiplexer" pattern — saves memory on Render Free).
  2. On `objectStatusChanged → status=free` events, attempts an immediate
     hold + booking for every watcher subscribed to that event_key.
  3. On success → marks watcher 'captured', sends Telegram alert with the
     payment URL; on failure → keeps watching.

Replaces the old SNIPER_POLL_INTERVAL polling loop (which polled every
2 seconds regardless of activity). This new design only consumes CPU
when seats actually change state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from app.core.config import (
    seatsio_drop_watcher_enabled,
    seatsio_drop_watcher_max_wait,
    default_payment_method,
)
from app.core.storage import (
    list_drop_watchers, set_drop_watcher_status, get_account, add_booking,
    mark_account_used,
)
from app.services.seatsio_client import SeatsioClient, SEATCLOUD_API
from app.services.seatsio_token_fetcher import ensure_tokens
from app.services.block_analyzer import (
    extract_blocks, find_seats_with_fallback,
)
from app.services.seat_summarizer import summarize_for_telegram

log = logging.getLogger("drop_watcher")

# event_key → asyncio.Task running the WS subscriber
_WS_TASKS: dict[str, asyncio.Task] = {}
# event_key → last known statuses (kept hot for immediate retry)
_LAST_STATUSES: dict[str, dict[str, str]] = {}


async def drop_watcher_loop(notifier=None) -> None:
    """Top-level supervisor. Every 15s, reconciles WS subscriptions
    against the active watchers in DB.

    v6 fix: also runs Pre-Watch Sanity Check on every fresh watcher —
    if seats are already free, the watcher is bypassed and the booking
    is fired through the fast-lane immediately.
    """
    if not seatsio_drop_watcher_enabled():
        log.info("drop watcher disabled by config")
        return
    await asyncio.sleep(8)
    while True:
        try:
            await _reconcile(notifier)
            # v6: opportunistic capture for any watcher whose seats are now free
            await _opportunistic_capture(notifier)
        except asyncio.CancelledError:
            for t in _WS_TASKS.values():
                t.cancel()
            raise
        except Exception as e:
            log.exception(f"drop_watcher reconcile error: {e}")
        await asyncio.sleep(15)


# ════════════════════════════════════════════════════════════════════════
# PRE-WATCH SANITY CHECK (v6)
# ════════════════════════════════════════════════════════════════════════
async def _opportunistic_capture(notifier) -> None:
    """For every active watcher, do a fast HTTP probe of the chart. If
    seats are already free for that watcher's preferences, fire the
    capture path immediately — do NOT wait for a WS drop event that may
    never come (seats were free all along).
    """
    watchers = list_drop_watchers(status="watching")
    if not watchers:
        return

    # Group by event_key to share one SeatsioClient probe per chart
    by_event: dict[str, list[dict]] = {}
    for w in watchers:
        ek = w.get("event_key") or ""
        if ek:
            by_event.setdefault(ek, []).append(w)

    for event_key, ws_list in by_event.items():
        try:
            async with SeatsioClient(event_key) as client:
                ri = await client.rendering_info()
                statuses = await client.object_statuses()
                if not ri or not (ri.get("objects") or []):
                    continue
                _LAST_STATUSES[event_key] = statuses

                for w in ws_list:
                    primary = (w.get("blocks_pref") or [None])[0] or ""
                    backups = (w.get("blocks_pref") or [])[1:]
                    qty = int(w.get("quantity") or 1)
                    seat_ids, used_block = find_seats_with_fallback(
                        ri, statuses,
                        primary_block=primary, backup_blocks=backups,
                        quantity=qty, expand_geometric=True,
                    )
                    if seat_ids:
                        log.info(
                            f"🎯 watcher#{w['id']} — seats already free, "
                            f"firing immediate capture (block={used_block})"
                        )
                        try:
                            await _capture_one(
                                client, w, ri, statuses, notifier,
                            )
                        except Exception as e:
                            log.debug(f"opportunistic capture err: {e}")
        except Exception as e:
            log.debug(f"opportunistic probe err for {event_key[:8]}: {e}")


async def _reconcile(notifier) -> None:
    watchers = list_drop_watchers(status="watching")
    if not watchers:
        # No active watchers — close any leftover WS
        for ek, t in list(_WS_TASKS.items()):
            t.cancel()
            _WS_TASKS.pop(ek, None)
        return

    # Auto-cancel stale watchers
    cutoff = time.time() - seatsio_drop_watcher_max_wait()
    fresh: list[dict] = []
    for w in watchers:
        if float(w.get("created_at") or 0) < cutoff:
            set_drop_watcher_status(w["id"], "expired")
            continue
        fresh.append(w)

    needed_keys = {w["event_key"] for w in fresh if w.get("event_key")}

    # Spawn missing WS subscribers
    for ek in needed_keys:
        if ek not in _WS_TASKS or _WS_TASKS[ek].done():
            _WS_TASKS[ek] = asyncio.create_task(
                _subscribe_event(ek, notifier),
                name=f"ws-drop:{ek[:8]}",
            )

    # Cancel WS for events that no longer have any watcher
    for ek in list(_WS_TASKS.keys()):
        if ek not in needed_keys:
            _WS_TASKS[ek].cancel()
            _WS_TASKS.pop(ek, None)


async def _subscribe_event(event_key: str, notifier) -> None:
    """Long-lived WS subscriber for one event. On any status change,
    triggers _try_capture for all watchers of that event."""
    backoff = 1.0
    while True:
        try:
            tokens = await ensure_tokens()
            ws_key = tokens.get("workspace_key") or ""
            if not ws_key:
                await asyncio.sleep(5)
                continue

            # Need a hold token for the WS handshake
            # Reuse existing hold token if available to avoid rate limits
            hold_token = ""
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(
                        f"{SEATCLOUD_API}/system/public/{ws_key}/hold-tokens",
                        json={}, timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        payload = await r.json(content_type=None)
                    hold_token = (payload or {}).get("holdToken") or ""
                except Exception as e:
                    log.debug(f"WS hold-token fetch failed: {e}")
                    if not hold_token:
                        await asyncio.sleep(10)
                        continue

                ws_url = (
                    f"wss://api.seatcloud.com/system/public/{ws_key}/events/"
                    f"{event_key}/changes/socket?holdToken={hold_token}"
                )
                async with session.ws_connect(ws_url, heartbeat=25) as ws:
                    log.info(f"🔌 WS connected → {event_key[:8]}")
                    backoff = 1.0
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in {aiohttp.WSMsgType.CLOSED,
                                            aiohttp.WSMsgType.ERROR}:
                                break
                            continue
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue
                        # Trigger capture attempt — only when something freed
                        if _is_status_free_event(data):
                            asyncio.create_task(
                                _try_capture(event_key, notifier),
                                name=f"capture:{event_key[:6]}",
                            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug(f"WS {event_key[:8]} reconnect: {e}")
            await asyncio.sleep(min(15.0, backoff))
            backoff = min(backoff * 1.5, 15.0)


def _is_status_free_event(msg: Any) -> bool:
    """Detect SeatCloud objectStatusChanged → free messages.

    The exact schema varies; we accept any of the common shapes.
    """
    if not isinstance(msg, dict):
        return False
    et = (msg.get("messageType") or msg.get("type") or "").lower()
    if "status" in et and "change" in et:
        new = (msg.get("newStatus") or msg.get("status") or "").lower()
        if new in {"free", "available", "not_booked"}:
            return True
    # Some payloads bundle multiple changes
    changes = msg.get("changes") or msg.get("statusChanges") or []
    if isinstance(changes, list):
        for c in changes:
            if isinstance(c, dict):
                new = (c.get("newStatus") or c.get("status") or "").lower()
                if new in {"free", "available", "not_booked"}:
                    return True
    return False


# ════════════════════════════════════════════════════════════════════════
# Capture
# ════════════════════════════════════════════════════════════════════════
_CAPTURE_LOCK: dict[str, asyncio.Lock] = {}


async def _try_capture(event_key: str, notifier) -> None:
    """Try to claim freshly-released seats for any watcher of this event_key."""
    lock = _CAPTURE_LOCK.setdefault(event_key, asyncio.Lock())
    if lock.locked():
        return  # avoid stampede on burst events
    async with lock:
        watchers = list_drop_watchers(status="watching", event_key=event_key)
        if not watchers:
            return

        # Refresh current statuses once for all watchers
        try:
            async with SeatsioClient(event_key) as client:
                statuses = await client.object_statuses()
                rendering_info = await client.rendering_info()
                _LAST_STATUSES[event_key] = statuses

                for w in watchers:
                    try:
                        await _capture_one(client, w, rendering_info, statuses,
                                            notifier)
                    except Exception as e:
                        log.debug(f"capture watcher#{w['id']} err: {e}")
        except Exception as e:
            log.debug(f"capture session err for {event_key[:8]}: {e}")


async def _capture_one(client: SeatsioClient, watcher: dict,
                        rendering_info: Any, statuses: dict[str, str],
                        notifier) -> None:
    primary = (watcher.get("blocks_pref") or [None])[0] or ""
    backups = (watcher.get("blocks_pref") or [])[1:]
    qty = int(watcher.get("quantity") or 1)

    seat_ids, used_block = find_seats_with_fallback(
        rendering_info, statuses,
        primary_block=primary,
        backup_blocks=backups,
        quantity=qty,
        expand_geometric=True,
    )
    if not seat_ids:
        return  # still nothing free → keep watching

    # Hold the seats immediately (best-effort — seats_planner doesn't need it)
    try:
        await client.init_hold_token()
        hold_result = await client.hold_objects(seat_ids)
        errors = hold_result.get("errors") if isinstance(hold_result, dict) else None
        if errors:
            log.debug(f"watcher#{watcher['id']} hold errors (non-fatal): {errors}")
    except Exception as e:
        log.debug(f"watcher#{watcher['id']} hold raise (non-fatal): {e}")

    # Now finalize the booking via the HTTP path (Turnstile auto-solved
    # internally when needed by booking_http → get_hold_token_from_webook).
    try:
        from app.services.booking_http import book_ticket_http
        from app.services import auth_service

        bearer = await auth_service.get_valid_bearer(
            watcher["account_id"], notifier=notifier, auto_relogin=True,
        )
        if not bearer:
            try:
                await client.release_objects(seat_ids)
            except Exception:
                pass
            return

        res = await book_ticket_http(
            bearer=bearer,
            slug=watcher["event_slug"],
            ticket_id=watcher["ticket_type_id"],
            quantity=qty,
            payment_method=default_payment_method(),
            preheld_seats=seat_ids,
            preheld_token=client.hold_token,
        )
    except Exception as e:
        log.warning(f"watcher#{watcher['id']} book raise: {e}")
        return

    if not res.get("ok"):
        try:
            await client.release_objects(seat_ids)
        except Exception:
            pass
        return

    # Success!
    set_drop_watcher_status(watcher["id"], "captured")
    mark_account_used(watcher["account_id"])
    acc = get_account(watcher["account_id"]) or {}
    label = acc.get("label") or acc.get("email") or watcher["account_id"]

    add_booking(
        chat_id=watcher["chat_id"],
        event_slug=watcher["event_slug"],
        event_title=watcher["event_slug"],
        ticket_type=watcher["ticket_type_id"],
        account_id=watcher["account_id"],
        quantity=qty,
        seat_info={"seats": seat_ids, "block": used_block,
                   "captured_via": "drop_watcher"},
        payment_url=res.get("payment_url", ""),
        status="pending",
    )

    if notifier:
        seats_summary = summarize_for_telegram(seat_ids)
        msg = (
            f"🎯 <b>تم اصطياد مقاعد ساقطة!</b>\n\n"
            f"👤 الحساب: <code>{label}</code>\n"
            f"📦 البلوك: <code>{used_block}</code>\n\n"
            f"{seats_summary}\n\n"
            f"💳 <a href=\"{res.get('payment_url','')}\">رابط الدفع</a>\n\n"
            f"⏰ أكمل الدفع خلال 5-10 دقائق وإلا تسقط المقاعد."
        )
        try:
            await notifier.send(str(watcher["chat_id"]), msg)
        except Exception:
            pass
