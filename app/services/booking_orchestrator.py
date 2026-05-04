"""
Parallel booking across multiple accounts — Hybrid Fast-Lane Engine v5.

v5 changes:
  • FAST-LANE: replaces blocking asyncio.gather with as_completed.
    Whichever account succeeds first calls fast_callback(result) IMMEDIATELY,
    so the user gets the PayTabs URL without waiting for slow accounts.
  • SILENT DROP-WATCHER: every failed account (not just chart_full) is
    automatically converted to a drop_watcher unless the failure is fatal
    (e.g. invalid bearer / event missing). The watcher will keep listening
    for released seats and pounce instantly.
  • TURNSTILE auto-bypass is wired through booking_http (no user prompt).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from app.core.storage import (
    add_booking, get_account, mark_account_used, add_drop_watcher,
)
from app.core.config import default_payment_method
from app.services import auth_service
from app.services.booking_http import book_ticket_http
from app.services.booking_playwright import book_via_browser
from app.services.distributor import Assignment

log = logging.getLogger("booking")

BookingProgressCB = Callable[[str], Awaitable[None]]
FastLaneCB = Callable[[dict], Awaitable[None]]


# ── Decide whether a failed result should be silently converted to a watcher ──
def _should_watch(res: dict) -> bool:
    """Return True if this failed booking should automatically transition to
    a drop-watcher instead of being reported as a hard failure to the user.

    Rules:
      • chart_full        → YES (canonical case)
      • chart_unreachable → YES (transient seats.io error; keep watching)
      • turnstile_required→ YES (turnstile mode persists; the watcher's next
                                  attempt will auto-solve it)
      • queued            → YES (user is in webook queue; watcher retries)
      • any other failure WHERE we still have an event_key → YES
        (we'd rather be watching than silently dead)
      • no event_key at all → NO (nothing to watch)
    """
    if res.get("ok"):
        return False
    seat_info = res.get("seat_info") or {}
    event_key = seat_info.get("event_key", "") or res.get("event_key", "")
    if not event_key:
        return False
    return True


async def _convert_to_watcher(
    *, chat_id: str, account_id: str, event_slug: str,
    event_key: str, ticket_id: str, quantity: int,
    primary_block: str, backup_blocks: list[str],
) -> bool:
    blocks_pref = ([primary_block] if primary_block else []) + list(backup_blocks)
    try:
        add_drop_watcher(
            chat_id=str(chat_id),
            account_id=account_id,
            event_slug=event_slug,
            event_key=event_key,
            ticket_type_id=ticket_id,
            quantity=quantity,
            blocks_pref=blocks_pref,
        )
        return True
    except Exception as e:
        log.warning(f"add_drop_watcher failed: {e}")
        return False


async def book_one(
    assignment: Assignment,
    *,
    event_slug: str,
    event_title: str,
    ticket_id: str,
    ticket_title: str,
    ticket_price: float,
    currency: str,
    chat_id: str,
    notifier=None,
    progress: Optional[BookingProgressCB] = None,
    ticket_meta: Optional[dict] = None,
    primary_block: str = "",
    backup_blocks: Optional[list[str]] = None,
    payment_method: str = "",
) -> dict:
    backup_blocks = backup_blocks or []
    payment_method = payment_method or default_payment_method()

    acc = get_account(assignment.account_id)
    if not acc:
        return {"ok": False, "account_id": assignment.account_id,
                "error": "الحساب غير موجود", "fatal": True}

    label = acc.get("label") or acc.get("email")

    async def _p(txt: str):
        if progress:
            try:
                await progress(txt)
            except Exception:
                pass

    bearer = await auth_service.get_valid_bearer(
        assignment.account_id, notifier=notifier, auto_relogin=True,
    )
    if not bearer:
        return {
            "ok": False, "account_id": assignment.account_id, "label": label,
            "error": "لا يوجد توكن JWT صالح؛ أعد تسجيل الدخول.",
            "fatal": True,
        }

    await _p(f"⚡ <code>{label}</code> — HTTP-direct ({assignment.quantity} تذاكر)")
    res = await book_ticket_http(
        bearer=bearer,
        slug=event_slug,
        ticket_id=ticket_id,
        quantity=assignment.quantity,
        payment_method=payment_method,
        ticket_meta=ticket_meta,
        primary_block=primary_block,
        backup_blocks=backup_blocks,
    )

    # ── Fall back to browser ONLY for "soft" failures (no chart issues) ──
    soft_chart_failure = (res.get("chart_full") or res.get("chart_unreachable")
                          or res.get("turnstile_required") or res.get("queued"))
    if not res.get("ok") and not soft_chart_failure:
        first_err = (res.get("error") or "")[:220]
        await _p(f"🔁 <code>{label}</code> — HTTP فشل ({first_err[:90]}) — استخدام المتصفح")
        try:
            pw = await book_via_browser(
                email=acc["email"], password=acc["password"],
                event_slug=event_slug, ticket_id=ticket_id,
                quantity=assignment.quantity,
                access_token=bearer, user_id=acc.get("user_id") or "",
            )
            if pw.get("ok"):
                res = {
                    "ok": True,
                    "payment_url": pw.get("payment_url"),
                    "seat_info": pw.get("seat_info") or {},
                    "seat_objects": pw.get("seat_objects") or [],
                    "order_id": "", "block_used": "",
                    "logs": (res.get("logs") or []) + (pw.get("logs") or []),
                }
        except Exception as e:
            log.debug(f"browser fallback err: {e}")

    if res.get("ok"):
        pay_url = res.get("payment_url", "")
        seat_info = res.get("seat_info", {}) or {}

        db_id = add_booking(
            chat_id=chat_id, event_slug=event_slug, event_title=event_title,
            ticket_type=ticket_title, account_id=assignment.account_id,
            quantity=assignment.quantity, seat_info=seat_info,
            payment_url=pay_url,
            total_amount=ticket_price * assignment.quantity,
            currency=currency, status="pending",
        )
        mark_account_used(assignment.account_id)

        return {
            "ok": True,
            "account_id": assignment.account_id, "label": label,
            "booking_id": db_id, "payment_url": pay_url,
            "order_id": res.get("order_id", ""),
            "quantity": assignment.quantity,
            "seat_info": seat_info,
            "seat_objects": res.get("seat_objects", []),
            "block_used": res.get("block_used", ""),
            "logs": res.get("logs", []),
        }

    # ── Failure → silently convert to drop_watcher when possible ──
    seat_info = res.get("seat_info") or {}
    event_key = seat_info.get("event_key", "")

    failure_kind = (
        "chart_full" if res.get("chart_full")
        else "turnstile" if res.get("turnstile_required")
        else "queued" if res.get("queued")
        else "chart_unreachable" if res.get("chart_unreachable")
        else "no_seats"
    )

    if event_key and _should_watch(res):
        ok = await _convert_to_watcher(
            chat_id=chat_id, account_id=assignment.account_id,
            event_slug=event_slug, event_key=event_key,
            ticket_id=ticket_id, quantity=assignment.quantity,
            primary_block=primary_block, backup_blocks=backup_blocks,
        )
        if ok:
            await _p(f"👁️ <code>{label}</code> — وضع الترقّب فُعّل ({failure_kind})")
            return {
                "ok": False,
                "account_id": assignment.account_id, "label": label,
                "error": (res.get("error") or "بانتظار سقوط مقاعد")[:220],
                "drop_watcher_active": True,
                "failure_kind": failure_kind,
                "logs": res.get("logs", []),
            }

    return {
        "ok": False,
        "account_id": assignment.account_id, "label": label,
        "error": (res.get("error") or "فشل الحجز")[:320],
        "failure_kind": failure_kind,
        "logs": res.get("logs", []),
    }


# ════════════════════════════════════════════════════════════════════════
# FAST-LANE BOOKING ENGINE
# ════════════════════════════════════════════════════════════════════════
async def book_all_fast_lane(
    plan: list[Assignment],
    *,
    event_slug: str, event_title: str,
    ticket_id: str, ticket_title: str,
    ticket_price: float, currency: str,
    chat_id: str,
    notifier=None,
    progress: Optional[BookingProgressCB] = None,
    fast_callback: Optional[FastLaneCB] = None,
    concurrency: int = 6,
    ticket_meta: Optional[dict] = None,
    primary_block: str = "",
    backup_blocks: Optional[list[str]] = None,
    payment_method: str = "",
) -> list[dict]:
    """Event-driven booking. Each account runs concurrently. The first one
    to return ok=True triggers fast_callback() with its result IMMEDIATELY,
    without waiting for the rest. Slow/failed accounts continue in the
    background and either join later or fall into drop-watcher mode.

    Returns the full list of results (in completion order) when all accounts
    have terminated. fast_callback may also be invoked for subsequent
    successes after the first.
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict] = []
    notified = set()

    async def _runner(a: Assignment) -> dict:
        async with sem:
            try:
                r = await book_one(
                    a,
                    event_slug=event_slug, event_title=event_title,
                    ticket_id=ticket_id, ticket_title=ticket_title,
                    ticket_price=ticket_price, currency=currency,
                    chat_id=chat_id, notifier=notifier, progress=progress,
                    ticket_meta=ticket_meta,
                    primary_block=primary_block,
                    backup_blocks=backup_blocks or [],
                    payment_method=payment_method,
                )
            except Exception as e:
                log.exception(f"book_one crashed for {a.account_id}: {e}")
                r = {"ok": False, "account_id": a.account_id,
                     "error": f"خطأ: {str(e)[:200]}",
                     "failure_kind": "exception"}
            return r

    tasks = [asyncio.create_task(_runner(a), name=f"book:{a.account_id}")
             for a in plan]

    # as_completed → process winners as they finish
    for fut in asyncio.as_completed(tasks):
        try:
            r = await fut
        except Exception as e:
            log.exception(f"fast-lane task crashed: {e}")
            continue
        results.append(r)

        # Fast-lane: notify the user the moment a success comes in
        if r.get("ok") and fast_callback and r.get("account_id") not in notified:
            notified.add(r["account_id"])
            try:
                await fast_callback(r)
            except Exception as e:
                log.warning(f"fast_callback err: {e}")

    return results


# ── Backwards-compatible alias used by handlers.py ──
async def book_all(
    plan: list[Assignment],
    *,
    event_slug: str,
    event_title: str,
    ticket_id: str,
    ticket_title: str,
    ticket_price: float,
    currency: str,
    chat_id: str,
    notifier=None,
    progress: Optional[BookingProgressCB] = None,
    fast_callback: Optional[FastLaneCB] = None,
    concurrency: int = 6,
    ticket_meta: Optional[dict] = None,
    primary_block: str = "",
    backup_blocks: Optional[list[str]] = None,
    payment_method: str = "",
) -> list[dict]:
    """Backwards-compatible wrapper that now uses the Fast-Lane engine."""
    return await book_all_fast_lane(
        plan,
        event_slug=event_slug, event_title=event_title,
        ticket_id=ticket_id, ticket_title=ticket_title,
        ticket_price=ticket_price, currency=currency,
        chat_id=chat_id, notifier=notifier, progress=progress,
        fast_callback=fast_callback,
        concurrency=concurrency, ticket_meta=ticket_meta,
        primary_block=primary_block, backup_blocks=backup_blocks,
        payment_method=payment_method,
    )
