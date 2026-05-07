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
from app.services.turnstile_solver import solve_turnstile, invalidate_cache
from app.services.turnstile_solver import solve_turnstile, invalidate_cache
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


async def _fetch_event_meta_uncached(session: aiohttp.ClientSession, slug: str, bearer: str) -> dict[str, Any]:
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


async def fetch_event_meta(session: aiohttp.ClientSession, slug: str, bearer: str) -> dict[str, Any]:
    """V13: Cached wrapper around the raw event-meta fetch.

    When N accounts target the same event in parallel, only one upstream
    call hits Webook for 30 seconds. The cached payload is read-only meta
    (event_id, is_seated, dates, ticket_categories) — NO per-account or
    bearer-specific state. Cache key intentionally omits bearer.
    """
    from app.services.perf_cache import event_meta_cache

    async def _do_fetch():
        return await _fetch_event_meta_uncached(session, slug, bearer)

    try:
        return await event_meta_cache.get_or_fetch(f"meta:{slug}", _do_fetch)
    except Exception:
        # Fail-open: never let cache layer break a booking.
        return await _do_fetch()


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


async def force_purge_cart(session: aiohttp.ClientSession, parent_event_id: str,
                            bearer: str, max_iterations: int = 5) -> dict:
    """v9 NUCLEAR cart cleanup: iteratively delete items until cart is empty.

    Webook's /cart/clear endpoint sometimes leaves stale items behind. This
    function GET-then-DELETE-then-GET loops until item_quantity reaches 0
    (or max_iterations is hit). Critical because checkout fails with
    'sold out' when cart has stale items from previous bookings (which used
    to be misclassified as account_limit_reached in v8 and earlier).
    """
    state = {"iterations": 0, "final_quantity": -1, "items_deleted": 0}
    for i in range(max_iterations):
        state["iterations"] += 1
        # First, try the bulk clear endpoints
        for url in [
            f"{WEBOOK_API}/cart/clear?lang=en&parent_event_id={parent_event_id}",
            f"{WEBOOK_API}/cart/clear-cart?lang=en&parent_event_id={parent_event_id}",
        ]:
            try:
                async with session.post(url, headers=build_headers(bearer),
                                         timeout=aiohttp.ClientTimeout(total=8)):
                    pass
            except Exception:
                pass

        # Read current cart state
        try:
            async with session.get(
                f"{WEBOOK_API}/cart?lang=en&parent_event_id={parent_event_id}",
                headers=build_headers(bearer),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                d = await r.json(content_type=None)
        except Exception:
            d = {}
        cart = (d or {}).get("data") or {}
        qty = cart.get("item_quantity", 0) or 0
        items = cart.get("cart_items") or []
        cart_id = cart.get("_id", "") or ""
        state["final_quantity"] = qty

        if qty == 0 and not items:
            return state  # cart is truly empty

        # Delete each item individually via best-effort multiple endpoint shapes
        for it in items:
            item_id = it.get("_id") or ""
            if not item_id:
                continue
            for url, method in [
                (f"{WEBOOK_API}/cart/items/{item_id}?lang=en", "DELETE"),
                (f"{WEBOOK_API}/cart/cart-items/{item_id}?lang=en", "DELETE"),
                (f"{WEBOOK_API}/cart/remove-item?lang=en&item_id={item_id}", "POST"),
            ]:
                try:
                    async with session.request(
                        method, url, headers=build_headers(bearer),
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as r:
                        if r.status in (200, 204):
                            state["items_deleted"] += 1
                            break
                except Exception:
                    pass

        # Also try whole-cart delete by ID
        if cart_id:
            for url, method in [
                (f"{WEBOOK_API}/cart/{cart_id}?lang=en", "DELETE"),
                (f"{WEBOOK_API}/carts/{cart_id}?lang=en", "DELETE"),
            ]:
                try:
                    async with session.request(
                        method, url, headers=build_headers(bearer),
                        timeout=aiohttp.ClientTimeout(total=6),
                    ):
                        pass
                except Exception:
                    pass

    return state


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
    turnstile_token: str = "",
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
    if turnstile_token:
        # v9: pass turnstile to checkout for events that require it
        body["turnstile"] = turnstile_token
    if seat_payload:
        clean = {k: v for k, v in seat_payload.items()
                  if v not in (None, "", [], {})}
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

    # ── Step 1: get a hold-token from webook (auto Turnstile bypass) ──
    webook_hold_token = ""
    if event_id:
        ht, ht_meta = await get_hold_token_from_webook(
            slug=slug, event_id=event_id, bearer=bearer,
            turnstile=turnstile_token,
            auto_solve_turnstile=True,
        )
        if ht_meta.get("turnstile_solved"):
            logs.append("🛡️ Turnstile auto-solved")
        if ht_meta.get("turnstile_required"):
            meta["turnstile_required"] = True
            logs.append("⚠️ webook hold-token still requires Turnstile after bypass")
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

    # ── Step 3: fetch chart with internal retry + Turnstile bypass on failure ──
    page_url = f"{WEBOOK_ORIGIN}/ar/events/{slug}"
    chart_turnstile_token = ""
    chart_attempts = 3
    async with SeatsioClient(
        event_key=event_key,
        workspace_key=workspace_key,
        chart_key=chart_key,
        provider=provider,
        hold_token=webook_hold_token,
    ) as client:
        for chart_try in range(1, chart_attempts + 1):
            if rendering_info is None:
                # Inject turnstile into chart-fetch headers when retrying
                if chart_try > 1 and not chart_turnstile_token:
                    try:
                        chart_turnstile_token = await solve_turnstile(
                            page_url, force_refresh=(chart_try > 2),
                        )
                        if chart_turnstile_token:
                            client.set_turnstile(chart_turnstile_token)
                            logs.append(f"🛡️ chart-layer Turnstile applied (try {chart_try})")
                    except Exception as e:
                        logs.append(f"⚠️ chart-layer turnstile solve err: {str(e)[:80]}")
                rendering_info = await client.rendering_info()
            if statuses is None:
                statuses = await client.object_statuses()

            if rendering_info and (rendering_info.get("objects") or []):
                break  # success
            # Else: chart fetch failed → reset and retry with bypass
            logs.append(f"⚠️ chart fetch attempt {chart_try}/{chart_attempts} returned empty")
            rendering_info = None
            statuses = None
            invalidate_cache(page_url)
            await __import__("asyncio").sleep(1.0 * chart_try)

        meta["rendering_info"] = rendering_info
        meta["statuses"] = statuses

        # If after all retries the chart is still empty → transient (NOT full)
        if not rendering_info or not (rendering_info.get("objects") or []):
            meta["chart_unreachable"] = True
            logs.append("⚠️ seats.io chart unreachable after retries")
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
    account_email: str = "",
    account_user_id: str = "",
    account_password: str = "",
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
        result["error"] = "no_bearer"
        result["fatal"] = True
        return result

    # v9 CRITICAL: Per-account ABSOLUTE ISOLATION.
    # Each call gets its OWN ClientSession + CookieJar so cookies/state from
    # one account NEVER bleed into another.
    cookie_jar = aiohttp.CookieJar(unsafe=False)
    connector = aiohttp.TCPConnector(
        limit=8, ttl_dns_cache=300, force_close=False,
        enable_cleanup_closed=True,
    )
    async with aiohttp.ClientSession(
        cookie_jar=cookie_jar,
        connector=connector,
        trust_env=False,
    ) as session:
        result["logs"].append(
            f"🔒 isolated session created (jar_id={id(cookie_jar)})"
        )
        meta = await fetch_event_meta(session, slug, bearer)
        if not meta.get("event_id"):
            result["error"] = "transient:event_meta_unreachable"
            result["chart_unreachable"] = True
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
                    # v8: Use SHORT TECHNICAL CODES, not user-facing strings.
                    # The orchestrator (book_one) is the SOLE authority for
                    # deciding whether to retry, hard-fail or watch. The bot
                    # presentation layer (handlers.py) is the SOLE authority
                    # for what the user actually sees. Never write user-facing
                    # Arabic prose here — it bypasses the retry pipeline.
                    if result["turnstile_required"]:
                        result["error"] = "transient:turnstile_required"
                    elif result["queued"]:
                        pos = seat_meta.get("queue_position") or "?"
                        result["error"] = f"transient:queued:{pos}"
                    elif result["chart_full"]:
                        result["error"] = "chart_full"
                    elif result["chart_unreachable"]:
                        result["error"] = "transient:chart_unreachable"
                    else:
                        result["error"] = f"no_contiguous_run:{quantity}"
                    result["seat_info"] = {
                        "event_key": manifest.get("event_key") or "",
                        "chart_key": manifest.get("chart_key") or "",
                        "workspace_key": manifest.get("workspace_key") or "",
                        "category": manifest.get("category") or "",
                    }
                    return result
            else:
                result["logs"].append("⚠️ no SeatCloud event key found — fallback only")

        # v9 NUCLEAR cart purge: iteratively delete every cart item until
        # webook reports item_quantity=0. Without this, stale items from
        # previous bookings cause 'sold out' on checkout (misclassified as
        # account_limit_reached in v8 and earlier).
        purge = await force_purge_cart(session, event_id, bearer)
        result["logs"].append(
            f"🧹 nuclear purge: iter={purge.get('iterations')} "
            f"deleted={purge.get('items_deleted')} final_qty={purge.get('final_quantity')}"
        )
        if purge.get("final_quantity", 0) > 0:
            result["chart_unreachable"] = True
            result["error"] = f"transient:cart_polluted:final_qty={purge['final_quantity']}"
            return result

        ok, cart_data = await add_to_cart(
            session,
            ticket_id=ticket_id,
            quantity=quantity,
            parent_event_id=event_id,
            time_slot_id=time_slot_id,
            bearer=bearer,
            seat_payload=seat_payload,
        )
        # v9: VERIFY post-add cart quantity — if mismatch, purge and retry
        if ok and isinstance(cart_data, dict):
            real_qty = cart_data.get("item_quantity", quantity)
            if real_qty != quantity:
                result["logs"].append(
                    f"⚠️ cart_qty_mismatch wanted={quantity} got={real_qty} — forcing re-purge"
                )
                purge2 = await force_purge_cart(session, event_id, bearer, max_iterations=8)
                result["logs"].append(
                    f"🧹 re-purge: iter={purge2.get('iterations')} "
                    f"deleted={purge2.get('items_deleted')} final_qty={purge2.get('final_quantity')}"
                )
                if purge2.get("final_quantity", 0) > 0:
                    result["chart_unreachable"] = True
                    result["error"] = (
                        f"transient:cart_polluted_after_add:"
                        f"final_qty={purge2['final_quantity']}"
                    )
                    return result
                ok, cart_data = await add_to_cart(
                    session, ticket_id=ticket_id, quantity=quantity,
                    parent_event_id=event_id, time_slot_id=time_slot_id,
                    bearer=bearer, seat_payload=seat_payload,
                )
                if ok and isinstance(cart_data, dict):
                    real_qty = cart_data.get("item_quantity", quantity)
                    result["logs"].append(f"🔄 re-add cart_qty={real_qty}")
        if not ok:
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
                result["error"] = f"account_limit_reached:{limit_msg[:120]}"
            else:
                # Detect transient errors at cart layer (Cloudflare/network)
                msg_lower = str(msg).lower()
                if any(h in msg_lower for h in ("cloudflare", "403", "502", "503", "504", "timeout", "timed out")):
                    result["chart_unreachable"] = True
                    result["error"] = f"transient:cart_blocked:{msg[:80]}"
                else:
                    result["error"] = f"add_to_cart_failed:{msg[:200]}"
            return result
        result["logs"].append(f"🛒 cart ok ({cart_data.get('item_quantity', quantity)} tickets)")

        # v9: TRIPLE-RETRY checkout with fresh Turnstile + cart re-purge
        # between each attempt. Webook sometimes rejects the FIRST checkout
        # with 'sold out' (race / hold_token consumed) but accepts the
        # second one with a new Turnstile token. This is NOT account_limit.
        ok, co_data = False, {}
        for co_attempt in range(1, 4):
            ts_for_co = ""
            if co_attempt > 1:
                try:
                    page_url = f"{WEBOOK_ORIGIN}/ar/events/{slug}"
                    ts_for_co = await solve_turnstile(
                        page_url, force_refresh=(co_attempt > 2),
                    )
                    if ts_for_co:
                        result["logs"].append(
                            f"🛡️ checkout-layer Turnstile applied (try {co_attempt})"
                        )
                except Exception as e:
                    result["logs"].append(f"⚠️ checkout-turnstile err: {str(e)[:80]}")

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
                turnstile_token=ts_for_co,
            )
            if ok:
                break
            raw = co_data if isinstance(co_data, dict) else {}
            msg_l = (raw.get("message") or raw.get("error") or "").lower()
            if "sold out" in msg_l or "already sold" in msg_l:
                result["logs"].append(
                    f"⚠️ checkout race try {co_attempt}/3 — retrying with fresh tokens"
                )
                await __import__("asyncio").sleep(1.5 * co_attempt)
                await clear_cart(session, event_id, bearer)
                ok2, _cd2 = await add_to_cart(
                    session, ticket_id=ticket_id, quantity=quantity,
                    parent_event_id=event_id, time_slot_id=time_slot_id,
                    bearer=bearer, seat_payload=seat_payload,
                )
                if not ok2:
                    break
                continue
            break

        if not ok:
            raw_body = co_data if isinstance(co_data, dict) else {}
            msg = (raw_body.get("message") or raw_body.get("error") or str(co_data))[:350]
            msg_lower = str(msg).lower()

            # v10: Detect Cloudflare WAF block on /checkout endpoint.
            # Signals: raw_body has key 'raw' with value '-' or empty (Cloudflare
            # served a non-JSON HTML challenge / 403 page), OR the message
            # explicitly mentions cloudflare/403/forbidden.
            cloudflare_block = (
                ("raw" in raw_body and raw_body.get("raw") in ("-", "", None))
                or "cloudflare" in msg_lower
                or "forbidden" in msg_lower
                or "403" in msg_lower
                or ("<html" in msg_lower and "cloudflare" in msg_lower)
                or ("raw" in raw_body and isinstance(raw_body.get("raw"), str)
                    and "<html" in raw_body["raw"].lower())
            )

            if cloudflare_block:
                # v10 BREAKTHROUGH: WAF blocked /checkout via aiohttp.
                # Open stealth Playwright browser, transplant cookies +
                # bearer + hold_token, replay /checkout from inside browser.
                # Real Chromium TLS/JA3 fingerprint defeats Cloudflare WAF.
                result["logs"].append(
                    "🚨 Cloudflare WAF detected on /checkout — invoking browser fallback (V10)"
                )
                try:
                    from app.services.booking_playwright import checkout_via_browser_fallback

                    # Snapshot ALL aiohttp cookies for transplant
                    cookie_dump: list[dict[str, Any]] = []
                    try:
                        for cookie in cookie_jar:
                            cookie_dump.append({
                                "name": cookie.key,
                                "value": cookie.value,
                                "domain": cookie.get("domain") or ".webook.com",
                                "path": cookie.get("path") or "/",
                            })
                    except Exception as e_dump:
                        result["logs"].append(f"⚠️ cookie dump err: {str(e_dump)[:80]}")

                    pw_res = await checkout_via_browser_fallback(
                        bearer=bearer,
                        user_id=account_user_id,
                        email=account_email,
                        slug=slug,
                        event_id=event_id,
                        ticket_id=ticket_id,
                        quantity=quantity,
                        time_slot_id=time_slot_id,
                        payment_method=payment_method,
                        seat_payload=seat_payload,
                        aiohttp_cookies=cookie_dump,
                        turnstile_token="",
                    )
                    result["logs"].extend(pw_res.get("logs", []))

                    if pw_res.get("ok") and pw_res.get("payment_url"):
                        # Build rich seat_objects for summarizer (same as success path)
                        if rendering_info_for_summary and seat_payload:
                            try:
                                from app.services.block_analyzer import _walk_objects
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
                        result["payment_url"] = pw_res["payment_url"]
                        result["order_id"] = pw_res.get("order_id", "")
                        result["payment_session_id"] = pw_res.get("payment_session_id", "")
                        result["logs"].append("✅ V10 Browser-Fallback succeeded — WAF bypassed")
                        return result
                    else:
                        # Browser fallback also failed — treat as transient
                        result["chart_unreachable"] = True
                        result["error"] = (
                            f"transient:cloudflare_blocked:browser_fallback_failed:"
                            f"{(pw_res.get('error') or '')[:120]}"
                        )
                        return result
                except Exception as e_pw:
                    result["logs"].append(f"⚠️ V10 browser fallback exception: {str(e_pw)[:120]}")
                    result["chart_unreachable"] = True
                    result["error"] = f"transient:cloudflare_blocked:exc:{str(e_pw)[:80]}"
                    return result

            # Non-Cloudflare classifications
            if "sold out" in msg_lower or "already sold" in msg_lower:
                result["chart_unreachable"] = True
                result["error"] = f"transient:checkout_race:{msg[:120]}"
                result["raw_response"] = str(co_data)[:500]
            elif "limit reached" in msg_lower:
                result["account_limit_reached"] = True
                result["error"] = f"account_limit_reached:{msg[:120]}"
            else:
                result["error"] = f"checkout_failed:{msg[:200]}"
            return result

        pay_url = co_data.get("redirect_url") or (co_data.get("response") or {}).get("redirect_url")
        if not pay_url:
            result["error"] = "checkout_no_redirect_url"
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
