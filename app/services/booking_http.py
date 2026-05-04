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
from app.services.seatsio_client import (
    SeatsioClient, get_hold_token_from_webook,
)
from app.services.seatsio_runtime import ensure_event_warm, get_snapshot
from app.services.block_analyzer import (
    extract_blocks, find_seats_with_fallback, chart_is_sold_out,
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
    """Extract everything we need to drive seats.io for this event:
      - event_key, chart_key, workspace_key (from webook's `seats_io` blob)
      - seats_provider                       ('seats_planner' | 'seatsio')
      - category (= seats_io_category for the chosen ticket type)
      - event_id (= webook's _id, needed for hold-token endpoint)
    """
    raw_tickets = await fetch_raw_ticket_details(session, slug, bearer)
    raw_ticket = _find_ticket_blob(raw_tickets, ticket_id)
    raw_event = ((raw_tickets or {}).get("data") or {}).get("event") or {}
    meta_raw = (event_meta or {}).get("raw") or {}

    # Prefer the structured `seats_io` blob (webook returns it for both
    # seatsio and seats_planner events)
    seats_io_blob = (
        meta_raw.get("seats_io")
        or raw_event.get("seats_io")
        or {}
    )
    if not isinstance(seats_io_blob, dict):
        seats_io_blob = {}

    event_key = (
        seats_io_blob.get("event_key")
        or _deep_find_first(raw_ticket, SEATED_EVENT_KEY_CANDIDATES - {"chart_key", "chartKey"})
        or _deep_find_first(raw_event, SEATED_EVENT_KEY_CANDIDATES - {"chart_key", "chartKey"})
        or _deep_find_first(meta_raw, SEATED_EVENT_KEY_CANDIDATES - {"chart_key", "chartKey"})
        or ""
    )
    chart_key = (
        seats_io_blob.get("chart_key")
        or _deep_find_first(meta_raw, {"chart_key", "chartKey"})
        or _deep_find_first(raw_event, {"chart_key", "chartKey"})
        or ""
    )
    workspace_key = (
        seats_io_blob.get("workspace_key")
        or _deep_find_first(meta_raw, {"workspace_key", "workspaceKey"})
        or ""
    )
    seats_provider = (
        meta_raw.get("seats_provider")
        or raw_event.get("seats_provider")
        or ""
    )
    event_id = meta_raw.get("_id") or raw_event.get("_id") or ""

    category = (
        (ticket_meta or {}).get("seats_io_category")
        or raw_ticket.get("seats_io_category")
        or raw_ticket.get("seatcloud_category")
        or raw_ticket.get("category")
        or ""
    )
    return {
        "event_key": str(event_key or "").strip(),
        "chart_key": str(chart_key or "").strip(),
        "workspace_key": str(workspace_key or "").strip(),
        "seats_provider": str(seats_provider or "").strip(),
        "event_id": str(event_id or "").strip(),
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
        # Same v7 fix as create_checkout: drop empty strings + alias dupes.
        clean = {k: v for k, v in seat_payload.items()
                  if v not in (None, "", [], {})}
        clean.pop("holdToken", None)
        clean.pop("seat_hold_token", None)
        body.update(clean)

    status, data = await _post(session, f"{WEBOOK_API}/cart/add-to-cart?lang=en", bearer, body)
    if status == 200 and isinstance(data, dict) and data.get("status") == "success":
        return True, data.get("data") or {}
    # v7: surface webook's structured 'errors' dict (e.g. ticket-limit) to
    # the caller so book_one can classify it correctly.
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
        # CRITICAL v7 fix: filter empty strings/lists/dicts. The field
        # seats_io_category="" specifically triggers Cloudflare WAF 403
        # on /checkout. Also de-duplicate hold_token aliases — webook
        # only needs `hold_token` (snake_case); the camelCase aliases
        # (`holdToken`, `seat_hold_token`) are redundant and increase
        # the body fingerprint risk.
        clean = {k: v for k, v in seat_payload.items()
                  if v not in (None, "", [], {})}
        # Drop redundant aliases
        clean.pop("holdToken", None)
        clean.pop("seat_hold_token", None)
        body.update(clean)

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
    event_id: str = "",
    primary_block: str = "",
    backup_blocks: Optional[list[str]] = None,
    turnstile_token: str = "",
) -> tuple[Optional[dict[str, Any]], list[str], dict[str, Any]]:
    """Reserve seats via the Hydra engine.

    Returns: (seat_payload | None, logs, meta)
        meta keys:
          - 'event_key', 'workspace_key', 'chart_key'
          - 'block_used'
          - 'rendering_info', 'statuses'
          - 'chart_full'      → True ONLY if chart data was retrieved AND
                                 every block reports 0 free capacity.
                                 (Caller routes to drop-watcher when True.)
          - 'chart_unreachable'→ True when seats.io APIs failed entirely.
                                 (Caller should NOT engage drop-watcher; the
                                 booking should error with a transient msg.)
          - 'turnstile_required' → True when webook hold-token requires
                                    a Cloudflare Turnstile token.
          - 'queued', 'queue_position' → webook queue state when present.
    """
    logs: list[str] = []
    event_key = (manifest.get("event_key") or "").strip()
    workspace_key = (manifest.get("workspace_key") or "").strip()
    chart_key = (manifest.get("chart_key") or "").strip()
    provider = (manifest.get("seats_provider") or "").strip()

    meta: dict[str, Any] = {
        "event_key": event_key,
        "workspace_key": workspace_key,
        "chart_key": chart_key,
        "block_used": "",
        "chart_full": False,
        "chart_unreachable": False,
        "turnstile_required": False,
        "queued": False,
    }
    if not event_key:
        meta["chart_unreachable"] = True
        logs.append("⚠️ manifest has no event_key")
        return None, logs, meta

    backup_blocks = backup_blocks or []
    legacy_targets = target_blocks()

    # ── Step 1: get a hold-token from webook (preferred for seats_planner) ──
    webook_hold_token = ""
    if event_id:
        ht, ht_meta = await get_hold_token_from_webook(
            slug=slug, event_id=event_id, bearer=bearer,
            turnstile=turnstile_token,
        )
        if ht_meta.get("turnstile_required"):
            meta["turnstile_required"] = True
            logs.append("⚠️ webook hold-token requires Turnstile")
        if ht_meta.get("queued"):
            meta["queued"] = True
            meta["queue_position"] = ht_meta.get("waiting_number")
            logs.append(f"⏳ in queue at position {ht_meta.get('waiting_number')}")
        if ht:
            webook_hold_token = ht
            logs.append(f"🔑 hold-token from webook: …{ht[-8:]}")

    # ── Step 2: try cached snapshot first (fastest path) ──
    await ensure_event_warm(event_key)
    snapshot = get_snapshot(event_key)
    rendering_info = (snapshot or {}).get("rendering_info") if snapshot else None
    statuses = (snapshot or {}).get("statuses") if snapshot else None

    # ── Step 3: open client with all keys + hold-token ──
    async with SeatsioClient(
        event_key=event_key,
        workspace_key=workspace_key,
        chart_key=chart_key,
        provider=provider,
        hold_token=webook_hold_token,
    ) as client:
        if rendering_info is None:
            rendering_info = await client.rendering_info()
        if statuses is None:
            statuses = await client.object_statuses()

        meta["rendering_info"] = rendering_info
        meta["statuses"] = statuses

        # No chart data at all? → transient error, NOT chart-full
        if not rendering_info or not (rendering_info.get("objects") or []):
            meta["chart_unreachable"] = True
            logs.append("⚠️ seats.io returned no chart data — transient/network error")
            return None, logs, meta

        # ── Step 4: pick seats ──
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
            # Distinguish 'truly sold out' from 'no contiguous run for this qty'
            if chart_is_sold_out(rendering_info, statuses):
                meta["chart_full"] = True
                logs.append("🚫 chart is genuinely sold out")
            else:
                logs.append(f"🔍 no contiguous run of {quantity} found in "
                            f"primary/backup/neighbors")
            return None, logs, meta

        meta["block_used"] = used_block

        # ── Step 5: pre-hold via legacy adapter (best-effort) ──
        # For seats_planner generalAdmission, the actual seat assignment
        # happens server-side at checkout — so a 'failed pre-hold' here is
        # NOT fatal. We log it and let webook do its thing.
        used_token = webook_hold_token
        try:
            if not used_token:
                used_token = await client.init_hold_token()
            if rendering_info.get("_provider") != "seats_planner":
                # Only legacy charts support the actions/hold endpoint
                hold_result = await client.hold_objects(
                    seat_ids, ticket_type=manifest.get("category") or "",
                )
                errors = hold_result.get("errors") if isinstance(hold_result, dict) else None
                if errors:
                    logs.append(f"⚠️ legacy hold reported: {str(errors)[:100]}")
        except Exception as e:
            logs.append(f"⚠️ pre-hold soft-fail: {str(e)[:120]} (continuing)")

        if used_token:
            logs.append(f"🪑 selected {len(seat_ids)} seats in block={used_block}")
            return {
                "selected_seats": seat_ids,
                "selected_seat_labels": seat_ids,
                "hold_token": used_token,
                "seat_hold_token": used_token,
                "holdToken": used_token,
                "seats_io_category": manifest.get("category") or "",
            }, logs, meta

        # No hold-token available (webook didn't issue one and legacy POST failed)
        meta["chart_unreachable"] = True
        logs.append("⚠️ could not obtain hold-token — cannot proceed")
        return None, logs, meta


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
        # Fine-grained failure signals (used by orchestrator):
        "chart_full": False,         # chart genuinely sold out (drop-watcher)
        "chart_unreachable": False,  # transient seats.io failure (NO drop-watcher)
        "turnstile_required": False, # webook needs Cloudflare Turnstile token
        "queued": False,             # webook queue active
        # Legacy compat (kept so external callers don't break):
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
                    event_id=event_id,
                    primary_block=primary_block,
                    backup_blocks=backup_blocks,
                )
                result["logs"].extend(seat_logs)
                rendering_info_for_summary = seat_meta.get("rendering_info")
                statuses_for_summary = seat_meta.get("statuses")
                result["block_used"] = seat_meta.get("block_used", "")
                result["chart_full"] = bool(seat_meta.get("chart_full"))
                result["chart_unreachable"] = bool(seat_meta.get("chart_unreachable"))
                result["turnstile_required"] = bool(seat_meta.get("turnstile_required"))
                result["queued"] = bool(seat_meta.get("queued"))
                # legacy compat — only when truly full (NOT on transient errors)
                result["no_seats_anywhere"] = result["chart_full"]

                if seat_payload:
                    result["seat_info"] = {
                        "seats": seat_payload.get("selected_seats") or [],
                        "hold_token": seat_payload.get("hold_token") or "",
                        "category": manifest.get("category") or "",
                        "event_key": manifest.get("event_key") or "",
                        "chart_key": manifest.get("chart_key") or "",
                        "workspace_key": manifest.get("workspace_key") or "",
                        "block": result["block_used"],
                    }
                else:
                    if result["turnstile_required"]:
                        result["error"] = (
                            "هذه الفعالية تتطلب تحقق Cloudflare Turnstile. افتح الفعالية "
                            "في المتصفح لإصدار رمز التحقق أولاً، ثم أعد المحاولة."
                        )
                    elif result["queued"]:
                        pos = seat_meta.get("queue_position") or "?"
                        result["error"] = (
                            f"الفعالية في طابور الانتظار (رقمك: {pos}). البوت سيعيد المحاولة تلقائياً."
                        )
                    elif result["chart_full"]:
                        result["error"] = "الخريطة ممتلئة بالكامل — يمكنك تفعيل وضع الترقّب"
                    elif result["chart_unreachable"]:
                        result["error"] = "تعذّر جلب بيانات خريطة المقاعد (خطأ شبكي، أعد المحاولة)."
                    else:
                        result["error"] = (
                            f"تعذّر إيجاد {quantity} مقعداً متجاورًا في البلوكات المختارة أو المجاورة."
                        )
                    result["seat_info"] = {
                        "event_key": manifest.get("event_key") or "",
                        "chart_key": manifest.get("chart_key") or "",
                        "workspace_key": manifest.get("workspace_key") or "",
                        "category": manifest.get("category") or "",
                    }
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
            # v7: detect 'ticket limit reached' (per-account subscription cap).
            # webook returns it as data.errors[<id>].message containing 'limit reached'
            errors_blob = (cart_data or {}).get("data", {}).get("errors") if isinstance(cart_data, dict) else None
            if not errors_blob and isinstance(cart_data, dict):
                errors_blob = cart_data.get("errors")
            limit_msg = ""
            if isinstance(errors_blob, dict):
                for v in errors_blob.values():
                    if isinstance(v, dict) and "limit reached" in (v.get("message") or "").lower():
                        limit_msg = v.get("message") or ""
                        break
            msg = limit_msg or (cart_data.get("message") or cart_data.get("error") or str(cart_data))[:300]
            if limit_msg:
                result["account_limit_reached"] = True
                result["error"] = (
                    f"تم بلوغ الحد المسموح للحساب: {limit_msg}. "
                    "جرّب حساباً آخر."
                )
            else:
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
            raw_body = co_data if isinstance(co_data, dict) else {}
            msg = (raw_body.get("message") or raw_body.get("error") or str(co_data))[:350]
            # v7: 'sold out' from webook for a seat we just got cart_ok for is
            # almost always due to per-account ticket limit (not real sell-out)
            msg_lower = str(msg).lower()
            if "sold out" in msg_lower or "already sold" in msg_lower:
                result["account_limit_reached"] = True
                result["error"] = (
                    "تم رفض الحجز من Webook (غالباً تجاوزت حد التذاكر للحساب أو "
                    "المقعد المحدد لم يعد متاحاً). جرّب حساباً آخر."
                )
            elif "limit reached" in msg_lower:
                result["account_limit_reached"] = True
                result["error"] = f"تم بلوغ حد التذاكر للحساب: {msg}"
            elif "raw" in raw_body and raw_body.get("raw") == "-":
                # Cloudflare 403 — transient, will be retried by orchestrator
                result["chart_unreachable"] = True
                result["error"] = "حُجبت من Cloudflare (سيُعاد المحاولة تلقائياً)."
            else:
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
