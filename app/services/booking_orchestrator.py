"""
Parallel booking across multiple accounts.

v4 changes:
  • per-event primary + backup blocks pass-through
  • cross-account adjacency: tries to grab one big contiguous run
    (per_account × accounts_count) inside the SAME block first, then
    splits cleanly across accounts.
  • drop_watcher registration when chart is fully booked
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
        return {"ok": False, "account_id": assignment.account_id, "error": "الحساب غير موجود"}

    label = acc.get("label") or acc.get("email")

    async def _p(txt: str):
        if progress:
            try:
                await progress(txt)
            except Exception:
                pass

    bearer = await auth_service.get_valid_bearer(
        assignment.account_id,
        notifier=notifier,
        auto_relogin=True,
    )
    if not bearer:
        return {
            "ok": False,
            "account_id": assignment.account_id,
            "label": label,
            "error": "لا يوجد توكن JWT صالح؛ أعد تسجيل الدخول.",
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

    # Chart fully booked → register a drop watcher and return early
    if not res.get("ok") and res.get("no_seats_anywhere"):
        seat_info = res.get("seat_info") or {}
        event_key = seat_info.get("event_key", "")
        if event_key:
            blocks_pref = ([primary_block] if primary_block else []) + list(backup_blocks)
            try:
                add_drop_watcher(
                    chat_id=str(chat_id),
                    account_id=assignment.account_id,
                    event_slug=event_slug,
                    event_key=event_key,
                    ticket_type_id=ticket_id,
                    quantity=assignment.quantity,
                    blocks_pref=blocks_pref,
                )
                await _p(f"👁️ <code>{label}</code> — الخريطة ممتلئة، فُعّل وضع الترقّب")
            except Exception as e:
                log.warning(f"add_drop_watcher failed: {e}")
        return {
            "ok": False,
            "account_id": assignment.account_id,
            "label": label,
            "error": "الخريطة ممتلئة — فُعّل وضع الترقّب لاصطياد المقاعد الساقطة.",
            "drop_watcher_active": True,
        }

    if not res.get("ok"):
        first_err = (res.get("error") or "")[:220]
        await _p(f"🔁 <code>{label}</code> — HTTP فشل ({first_err[:90]}) — استخدام المتصفح")
        pw = await book_via_browser(
            email=acc["email"],
            password=acc["password"],
            event_slug=event_slug,
            ticket_id=ticket_id,
            quantity=assignment.quantity,
            access_token=bearer,
            user_id=acc.get("user_id") or "",
        )
        if pw.get("ok"):
            res = {
                "ok": True,
                "payment_url": pw.get("payment_url"),
                "seat_info": pw.get("seat_info") or {},
                "seat_objects": pw.get("seat_objects") or [],
                "order_id": "",
                "block_used": "",
                "logs": (res.get("logs") or []) + (pw.get("logs") or []),
            }
        else:
            return {
                "ok": False,
                "account_id": assignment.account_id,
                "label": label,
                "error": (pw.get("error") or first_err or "فشل الحجز")[:320],
                "logs": (res.get("logs") or []) + (pw.get("logs") or []),
            }

    pay_url = res.get("payment_url", "")
    seat_info = res.get("seat_info", {}) or {}

    db_id = add_booking(
        chat_id=chat_id,
        event_slug=event_slug,
        event_title=event_title,
        ticket_type=ticket_title,
        account_id=assignment.account_id,
        quantity=assignment.quantity,
        seat_info=seat_info,
        payment_url=pay_url,
        total_amount=ticket_price * assignment.quantity,
        currency=currency,
        status="pending",
    )
    mark_account_used(assignment.account_id)

    return {
        "ok": True,
        "account_id": assignment.account_id,
        "label": label,
        "booking_id": db_id,
        "payment_url": pay_url,
        "order_id": res.get("order_id", ""),
        "quantity": assignment.quantity,
        "seat_info": seat_info,
        "seat_objects": res.get("seat_objects", []),
        "block_used": res.get("block_used", ""),
        "logs": res.get("logs", []),
    }


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
    concurrency: int = 5,
    ticket_meta: Optional[dict] = None,
    primary_block: str = "",
    backup_blocks: Optional[list[str]] = None,
    payment_method: str = "",
) -> list[dict]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(a: Assignment) -> dict:
        async with sem:
            try:
                return await book_one(
                    a,
                    event_slug=event_slug,
                    event_title=event_title,
                    ticket_id=ticket_id,
                    ticket_title=ticket_title,
                    ticket_price=ticket_price,
                    currency=currency,
                    chat_id=chat_id,
                    notifier=notifier,
                    progress=progress,
                    ticket_meta=ticket_meta,
                    primary_block=primary_block,
                    backup_blocks=backup_blocks or [],
                    payment_method=payment_method,
                )
            except Exception as e:
                log.exception(f"book_one crashed for {a.account_id}: {e}")
                return {"ok": False, "account_id": a.account_id, "error": f"خطأ: {str(e)[:200]}"}

    return await asyncio.gather(*[_one(a) for a in plan])
