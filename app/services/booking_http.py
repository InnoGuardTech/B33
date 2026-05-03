"""
Direct HTTP booking engine — fast path for Webook.

v4 enhancements:
  • per-event primary + backup block selection (was: global TARGET_BLOCKS)
  • geometric neighbor expansion when all chosen blocks are full
  • drop-watcher integration when chart is fully booked
  • preheld_seats path: skip discovery if drop_watcher already grabbed seats
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import aiohttp

from app.core.config import (
    WEBOOK_API,
    WEBOOK_ORIGIN,
    WEBOOK_PUBLIC_TOKEN,
    seatsio_enabled,
    target_blocks,
    default_payment_method,
)
from app.services.seatsio_client import SeatsioClient
from app.services.seatsio_runtime import ensure_event_warm, get_snapshot
from app.services.block_analyzer import (
    extract_blocks, find_seats_with_fallback,
)

log = logging.getLogger("booking_http")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

SEATED_EVENT_KEY_CANDIDATES = {
    "seats_io_event_key", "seatsio_event_key", "seatcloud_event_key",
    "event_key", "chart_key", "chartKey", "eventKey",
}


def build_headers(bearer: str, lang: str = "en") -> dict[str, str]:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": DEFAULT_UA,
        "accept-language": "ar-SA",
        "authorization": f"Bearer {bearer}" if bearer else "Bearer",
        "token": WEBOOK_PUBLIC_TOKEN,
        "origin": WEBOOK_ORIGIN,
        "referer": f"{WEBOOK_ORIGIN}/",
        "sec-ch-ua": '"Not:A-Brand";v="99", "Chromium";v="128"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    }


async def _get(session: aiohttp.ClientSession, url: str, bearer: str, timeout: int = 15) -> tuple[int, Any]:
    try:
        async with session.get(url, headers=build_headers(bearer), timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"raw": (await r.text())[:1200]}
            return r.status, data
    except Exception as e:
        return 0, {"error": str(e)[:200]}


async def _post(session: aiohttp.ClientSession, url: str, bearer: str, body: dict, timeout: int = 25) -> tuple[int, Any]:
    try:
        async with session.post(url, headers=build_headers(bearer), json=body, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"raw": (await r.text())[:1200]}
            return r.status, data
    except Exception as e:
        return 0, {"error": str(e)[:200]}


def _deep_find_first(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            found = _deep_find_first(v, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find_first(item, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def _find_ticket_blob(raw_payload: dict[str, Any], ticket_id: str) -> dict[str, Any]:
    event_ticket = ((raw_payload or {}).get("data") or {}).get("event_ticket") or []
    for item in event_ticket:
        if str(item.get("_id") or item.get("id")) == str(ticket_id):
            return item
    return {}


async def fetch_event_meta(session: aiohttp.ClientSession, slug: str, bearer: str) -> dict[str, Any]:
    url = f"{WEBOOK_API}/event-detail/{slug}?lang=en&visible_in=rs"
    status, data = await _get(session, url, bearer)
    if status != 200 or not isinstance(data, dict):
        return {}
    d = data.get("data") or {}
    return {
        "event_id": d.get("_id"),
        "title": d.get("title") or slug,
        "is_seated": bool(d.get("is_seated")),
        "booking_seats_without_map": bool(d.get("booking_seats_without_map")),
        "time_slot_dates": list(d.get("time_slots") or []),
        "is_experience": bool(d.get("is_experience")),
        "require_visa": bool(d.get("require_visa")),
        "raw": d,
    }


async def fetch_raw_ticket_details(session: aiohttp.ClientSession, slug: str, bearer: str = "") -> dict[str, Any]:
    status, data = await _get(
        session,
        f"{WEBOOK_API}/event-ticket-details/{slug}?lang=en&visible_in=rs&page=1",
        bearer,
    )
    return data if status == 200 and isinstance(data, dict) else {}


async def resolve_seated_manifest(
    session: aiohttp.ClientSession,
    slug: str,
    ticket_id: str,
    bearer: str = "",
    *,
    ticket_meta: Optional[dict[str, Any]] = None,
    event_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    raw_tickets = await fetch_raw_ticket_details(session, slug, bearer)
    raw_ticket = _find_ticket_blob(raw_tickets, ticket_id)
    raw_event = ((raw_tickets or {}).get("data") or {}).get("event") or {}
    meta_raw = (event_meta or {}).get("raw") or {}

    event_key = (
        _deep_find_first(raw_ticket, SEATED_EVENT_KEY_CANDIDATES)
        or _deep_find_first(raw_event, SEATED_EVENT_KEY_CANDIDATES)
        or _deep_find_first(meta_raw, SEATED_EVENT_KEY_CANDIDATES)
        or ""
    )
    category = (
        (ticket_meta or {}).get("seats_io_category")
        or raw_ticket.get("seats_io_category")
        or raw_ticket.get("seatcloud_category")
        or raw_ticket.get("category")
        or ""
    )
    return {
        "event_key": str(event_key or "").strip(),
        "category": str(category or "").strip(),
        "raw_ticket": raw_ticket,
        "raw_event": raw_event,
    }


async def prewarm_event_from_slug(slug: str, ticket_id: str = "") -> None:
    if not seatsio_enabled() or not slug:
        return
    async with aiohttp.ClientSession() as session:
        meta = await fetch_event_meta(session, slug, "")
        if not meta.get("is_seated"):
            return
        manifest = await resolve_seated_manifest(session, slug, ticket_id, "", event_meta=meta)
        if manifest.get("event_key"):
            await ensure_event_warm(manifest["event_key"])


async def fetch_timeslot_id(session: aiohttp.ClientSession, slug: str, date_str: str, ticket_id: str, bearer: str) -> Optional[str]:
    url = f"{WEBOOK_API}/event-detail/{slug}/timeslot-capacity?time_slot={date_str}&visible_in=rs&lang=en"
    status, data = await _get(session, url, bearer)
    if status != 200 or not isinstance(data, dict):
        return None
    slots = data.get("data") or []
    for s in slots:
        if s.get("is_soldout"):
            continue
        cap = s.get(ticket_id)
        if cap is None or cap == -1 or (isinstance(cap, (int, float)) and cap > 0):
            return s.get("_id")
    return slots[0].get("_id") if slots else None


async def add_to_cart(
    session: aiohttp.ClientSession,
    *,
    ticket_id: str,
    quantity: int,
    parent_event_id: str,
    time_slot_id: Optional[str],
    bearer: str,
    seat_payload: Optional[dict[str, Any]] = None,
) -> tuple[bool, Any]:
    body = {
        "ticket_id": ticket_id,
        "quantity": quantity,
        "type": "ticket",
        "parent_event_id": parent_event_id,
    }
    if time_slot_id:
        body["time_slot_id"] = time_slot_id
    if seat_payload:
        body.update({k: v for k, v in seat_payload.items() if v not in (None, "", [], {})})

    status, data = await _post(session, f"{WEBOOK_API}/cart/add-to-cart?lang=en", bearer, body)
    if status == 200 and isinstance(data, dict) and data.get("status") == "success":
        return True, data.get("data") or {}
    return False, data


async def clear_cart(session: aiohttp.ClientSession, parent_event_id: str, bearer: str) -> None:
    for url in [
        f"{WEBOOK_API}/cart/clear?lang=en&parent_event_id={parent_event_id}",
        f"{WEBOOK_API}/cart/clear-cart?lang=en&parent_event_id={parent_event_id}",
    ]:
        try:
            async with session.post(url, headers=build_headers(bearer), timeout=aiohttp.ClientTimeout(total=8)):
                pass
        except Exception:
            pass


async def create_checkout(
    session: aiohttp.ClientSession,
    *,
    slug: str,
    event_id: str,
    ticket_id: str,
    quantity: int,
    time_slot_id: Optional[str],
    bearer: str,
    payment_method: str = "credit_card",
    seat_payload: Optional[dict[str, Any]] = None,
) -> tuple[bool, dict]:
    body = {
        "event_id": event_id,
        "redirect": f"{WEBOOK_ORIGIN}/en/payment-success",
        "redirect_failed": f"{WEBOOK_ORIGIN}/en/payment-failed",
        "booking_source": "rs-web",
        "lang": "en",
        "payment_method": payment_method,
        "is_wallet": False,
        "saudi_redeem": None,
        "refund_guarantee": False,
        "perks": [],
        "merchandise": [],
        "addons": [],
        "vouchers": [],
        "tickets": [{"qty": quantity, "id": ticket_id}],
        "app_source": "rs",
    }
    if time_slot_id:
        body["time_slot_id"] = time_slot_id
    if seat_payload:
        body.update({k: v for k, v in seat_payload.items() if v not in (None, "", [], {})})

    status, data = await _post(session, f"{WEBOOK_API}/event-detail/{slug}/checkout?lang=en", bearer, body, timeout=30)
    if status == 200 and isinstance(data, dict) and data.get("status") == "success":
        return True, data.get("data") or {}
    return False, data or {}


async def _reserve_seated_inventory(
    *,
    slug: str,
    ticket_id: str,
    quantity: int,
    bearer: str,
    manifest: dict[str, Any],
    primary_block: str = "",
    backup_blocks: Optional[list[str]] = None,
) -> tuple[Optional[dict[str, Any]], list[str], dict[str, Any]]:
    """Reserve seats with the Hydra engine.

    Returns: (seat_payload | None, logs, meta)
        meta contains: 'block_used', 'rendering_info', 'statuses',
                       'event_key', 'no_seats_anywhere'
    """
    logs: list[str] = []
    meta: dict[str, Any] = {"event_key": manifest.get("event_key") or "",
                            "block_used": "",
                            "no_seats_anywhere": False}
    event_key = manifest.get("event_key") or ""
    if not event_key:
        return None, logs, meta

    backup_blocks = backup_blocks or []
    # Legacy ENV-level fallback (lower priority than user-picked blocks)
    legacy_targets = target_blocks()

    await ensure_event_warm(event_key)
    snapshot = get_snapshot(event_key)

    async with SeatsioClient(event_key) as client:
        # Try snapshot first (fastest)
        rendering_info = (snapshot or {}).get("rendering_info") if snapshot else None
        statuses = (snapshot or {}).get("statuses") if snapshot else None
        if rendering_info is None:
            rendering_info = await client.rendering_info()
        if statuses is None:
            statuses = await client.object_statuses()

        meta["rendering_info"] = rendering_info
        meta["statuses"] = statuses

        # Decide block preferences
        primary = primary_block or (legacy_targets[0] if legacy_targets else "")
        backups = backup_blocks or legacy_targets[1:]

        seat_ids, used_block = find_seats_with_fallback(
            rendering_info, statuses,
            primary_block=primary,
            backup_blocks=backups,
            quantity=quantity,
            expand_geometric=True,
            expand_limit=8,
        )

        if not seat_ids:
            # Detect whether the chart is genuinely full (drop-watcher case)
            blocks_meta = extract_blocks(rendering_info, statuses)
            total_free = sum(b.get("free", 0) for b in blocks_meta)
            meta["no_seats_anywhere"] = (total_free == 0)
            logs.append(f"🚫 no contiguous {quantity} seats available "
                        f"(total free in chart: {total_free})")
            return None, logs, meta

        meta["block_used"] = used_block
        try:
            await client.init_hold_token()
            hold_result = await client.hold_objects(
                seat_ids, ticket_type=manifest.get("category") or "",
            )
            errors = hold_result.get("errors") if isinstance(hold_result, dict) else None
            if errors:
                logs.append(f"hold errors on block={used_block}: {str(errors)[:100]}")
                return None, logs, meta
        except Exception as e:
            logs.append(f"hold raise on block={used_block}: {str(e)[:120]}")
            return None, logs, meta

        logs.append(f"🪑 held {len(seat_ids)} seats from block={used_block}")
        return {
            "selected_seats": seat_ids,
            "selected_seat_labels": seat_ids,
            "hold_token": client.hold_token,
            "seat_hold_token": client.hold_token,
            "holdToken": client.hold_token,
            "seats_io_category": manifest.get("category") or "",
        }, logs, meta


async def book_ticket_http(
    *,
    bearer: str,
    slug: str,
    ticket_id: str,
    quantity: int,
    payment_method: str = "",
    preferred_date: Optional[str] = None,
    ticket_meta: Optional[dict[str, Any]] = None,
    primary_block: str = "",
    backup_blocks: Optional[list[str]] = None,
    preheld_seats: Optional[list[str]] = None,
    preheld_token: str = "",
) -> dict[str, Any]:
    """Main HTTP booking entry point.

    New parameters:
      • primary_block, backup_blocks  → user's seat-picker preferences
      • preheld_seats, preheld_token  → if drop_watcher already held seats,
                                         skip the discovery + hold step
    """
    payment_method = payment_method or default_payment_method()
    backup_blocks = backup_blocks or []

    result: dict[str, Any] = {
        "ok": False,
        "payment_url": "",
        "order_id": "",
        "payment_session_id": "",
        "seat_info": {},
        "seat_objects": [],     # rich objects with category/block/row/seat for summarizer
        "block_used": "",
        "no_seats_anywhere": False,
        "logs": [],
        "error": "",
    }
    if not bearer:
        result["error"] = "لا يوجد توكن JWT صالح (يحتاج تسجيل دخول جديد)"
        return result

    async with aiohttp.ClientSession() as session:
        meta = await fetch_event_meta(session, slug, bearer)
        if not meta.get("event_id"):
            result["error"] = "تعذّر جلب بيانات الفعالية"
            return result
        event_id = meta["event_id"]
        result["logs"].append(f"📋 event_id={event_id[:8]} seated={meta['is_seated']}")

        time_slot_id = None
        dates = meta.get("time_slot_dates") or []
        if dates:
            pick = preferred_date if preferred_date in dates else dates[0]
            time_slot_id = await fetch_timeslot_id(session, slug, pick, ticket_id, bearer)
            if time_slot_id:
                result["logs"].append(f"⏰ time_slot={pick}")

        seat_payload: Optional[dict[str, Any]] = None
        rendering_info_for_summary = None
        statuses_for_summary = None

        if meta.get("is_seated") and not meta.get("booking_seats_without_map"):
            manifest = await resolve_seated_manifest(
                session, slug, ticket_id, bearer,
                ticket_meta=ticket_meta, event_meta=meta,
            )

            if preheld_seats and preheld_token:
                # Drop-watcher path: seats already held, just attach to cart/checkout
                seat_payload = {
                    "selected_seats": preheld_seats,
                    "selected_seat_labels": preheld_seats,
                    "hold_token": preheld_token,
                    "seat_hold_token": preheld_token,
                    "holdToken": preheld_token,
                    "seats_io_category": manifest.get("category") or "",
                }
                result["seat_info"] = {
                    "seats": preheld_seats,
                    "hold_token": preheld_token,
                    "category": manifest.get("category") or "",
                    "event_key": manifest.get("event_key") or "",
                }
                result["logs"].append(f"⚡ using {len(preheld_seats)} preheld seats")
            elif seatsio_enabled() and manifest.get("event_key"):
                seat_payload, seat_logs, seat_meta = await _reserve_seated_inventory(
                    slug=slug,
                    ticket_id=ticket_id,
                    quantity=quantity,
                    bearer=bearer,
                    manifest=manifest,
                    primary_block=primary_block,
                    backup_blocks=backup_blocks,
                )
                result["logs"].extend(seat_logs)
                rendering_info_for_summary = seat_meta.get("rendering_info")
                statuses_for_summary = seat_meta.get("statuses")
                result["block_used"] = seat_meta.get("block_used", "")
                result["no_seats_anywhere"] = bool(seat_meta.get("no_seats_anywhere"))

                if seat_payload:
                    result["seat_info"] = {
                        "seats": seat_payload.get("selected_seats") or [],
                        "hold_token": seat_payload.get("hold_token") or "",
                        "category": manifest.get("category") or "",
                        "event_key": manifest.get("event_key") or "",
                        "block": result["block_used"],
                    }
                else:
                    if result["no_seats_anywhere"]:
                        result["error"] = "الخريطة ممتلئة بالكامل — يمكنك تفعيل وضع الترقّب"
                        # caller will register a drop_watcher
                        result["seat_info"] = {
                            "event_key": manifest.get("event_key") or "",
                            "category": manifest.get("category") or "",
                        }
                    else:
                        result["error"] = "تعذّر إيجاد مقاعد متجاورة بالعدد المطلوب"
                    return result
            else:
                result["logs"].append("⚠️ no SeatCloud event key found — fallback only")

        await clear_cart(session, event_id, bearer)
        ok, cart_data = await add_to_cart(
            session,
            ticket_id=ticket_id,
            quantity=quantity,
            parent_event_id=event_id,
            time_slot_id=time_slot_id,
            bearer=bearer,
            seat_payload=seat_payload,
        )
        if not ok:
            msg = (cart_data.get("message") or cart_data.get("error") or str(cart_data))[:300]
            result["error"] = f"فشل add-to-cart: {msg}"
            return result
        result["logs"].append(f"🛒 cart ok ({cart_data.get('item_quantity', quantity)} tickets)")

        ok, co_data = await create_checkout(
            session,
            slug=slug,
            event_id=event_id,
            ticket_id=ticket_id,
            quantity=quantity,
            time_slot_id=time_slot_id,
            bearer=bearer,
            payment_method=payment_method,
            seat_payload=seat_payload,
        )
        if not ok:
            msg = (co_data.get("message") or co_data.get("error") or str(co_data))[:350]
            result["error"] = f"فشل checkout: {msg}"
            return result

        pay_url = co_data.get("redirect_url") or (co_data.get("response") or {}).get("redirect_url")
        if not pay_url:
            result["error"] = "checkout نجح لكن لم يرجع redirect_url"
            return result

        # Build rich seat_objects for the summarizer
        if rendering_info_for_summary and seat_payload:
            try:
                from app.services.block_analyzer import _walk_objects, _to_int as _to_int_helper
                wanted = set(seat_payload.get("selected_seats") or [])
                objs = _walk_objects(rendering_info_for_summary)
                rich = []
                for o in objs:
                    oid = str(o.get("id") or o.get("objectId") or "")
                    label = o.get("labels", {}).get("displayedLabel") or o.get("label") or oid
                    if oid in wanted or label in wanted:
                        rich.append(o)
                result["seat_objects"] = rich
            except Exception:
                pass

        result["ok"] = True
        result["payment_url"] = pay_url
        result["order_id"] = co_data.get("order_id", "")
        result["payment_session_id"] = co_data.get("payment_session_id", "")
        result["logs"].append("💳 PayTabs URL ready")
        return result
