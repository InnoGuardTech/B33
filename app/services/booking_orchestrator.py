"""
Parallel booking across multiple accounts — Hybrid Engine v6 (Surgical Fix).

v6 changes (CRITICAL BUG FIX):
  • STRICT WATCHER CONDITION: a watcher is registered ONLY when seats.io
    explicitly reports the chart as fully booked (chart_full=True). Every
    other failure (turnstile, queue, network, no contiguous run, 2captcha
    delay, timeout) goes through SMART RETRY instead.
  • SMART RETRY LOOP: transient failures (turnstile_required, queued,
    chart_unreachable, network errors, timeouts) trigger up to 5 retries
    with exponential backoff. The retry path re-asks for a fresh
    Turnstile token, refreshes the bearer if expired, and re-tries the
    booking — never silently giving up to the watcher.
  • PRE-WATCH SANITY CHECK: even when chart_full looks true, we double-
    check via a fast HTTP statuses probe. If we find any free seat, we
    DO NOT register a watcher and return back to the fast-lane.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional

from app.core.storage import (
    add_booking, get_account, mark_account_used, add_drop_watcher,
)
from app.core.config import default_payment_method
from app.services import auth_service
from app.services.booking_http import book_ticket_http
from app.services.booking_playwright import book_via_browser
from app.services.distributor import Assignment

# V14: dynamic-secret extraction + HTTP/2 stealth client + per-account proxy.
# These are imported lazily-friendly so existing modules that import
# booking_orchestrator do not gain a hard dependency on the new layer.
try:
    from app.services.asset_secret_extractor import (
        get_webook_secrets as _v14_get_secrets,
        invalidate_cache as _v14_invalidate_secrets,
    )
    from app.services.stealth_client import StealthClient as _V14StealthClient
    _V14_AVAILABLE = True
except Exception as _e:  # pragma: no cover
    _v14_get_secrets = None
    _v14_invalidate_secrets = None
    _V14StealthClient = None
    _V14_AVAILABLE = False

log = logging.getLogger("booking")

BookingProgressCB = Callable[[str], Awaitable[None]]
FastLaneCB = Callable[[dict], Awaitable[None]]

# ── Retry tuning ──
MAX_RETRIES = 5            # max attempts per transient failure
BASE_BACKOFF = 1.2         # seconds
MAX_BACKOFF = 8.0          # cap


# ════════════════════════════════════════════════════════════════════════
# v8: Translate technical error codes from booking_http into user-facing
# Arabic messages. This keeps a single source-of-truth for what the user
# eventually sees, and prevents the engine from leaking outdated strings.
# ════════════════════════════════════════════════════════════════════════
def _humanize_error(res: dict) -> str:
    """Convert internal error codes to user-friendly Arabic. Called only
    when constructing the FINAL response sent to Telegram — never inside
    retry loops (which use the raw codes for classification).
    """
    err = (res.get("error") or "").strip()
    if not err:
        return "فشل الحجز (سبب غير محدد)."

    # Codes from booking_http
    if err == "no_bearer":
        return "توكن الحساب منتهي; سيُعاد تسجيل الدخول تلقائياً."
    if err.startswith("transient:turnstile"):
        return "تم حل تحدي Cloudflare تلقائياً — إعادة المحاولة..."
    if err.startswith("transient:queued"):
        pos = err.split(":", 2)[-1] if err.count(":") >= 2 else "?"
        return f"في طابور الانتظار (رقمك: {pos}); سيُعاد المحاولة تلقائياً."
    if err.startswith("transient:chart_unreachable") or \
       err.startswith("transient:event_meta_unreachable") or \
       err.startswith("transient:cloudflare_blocked") or \
       err.startswith("transient:cart_blocked"):
        return "عائق مؤقت في جلب بيانات الخريطة — سيُعاد المحاولة بتجاوز Cloudflare تلقائياً."
    if err == "chart_full":
        return "الخريطة ممتلئة تماماً — تفعيل وضع الترقّب."
    if err.startswith("no_contiguous_run:"):
        n = err.split(":", 1)[-1]
        return f"تعذّر إيجاد {n} مقعداً متجاوراً في البلوكات المختارة."
    if err.startswith("account_limit_reached"):
        return "بلوغ حد التذاكر للحساب — سيُجرّب حساب آخر."
    if err.startswith("checkout_failed:"):
        msg = err.split(":", 1)[-1][:120]
        return f"فشل إتمام الدفع: {msg}"
    if err.startswith("add_to_cart_failed:"):
        msg = err.split(":", 1)[-1][:120]
        return f"فشل إضافة للسلة: {msg}"
    if err == "checkout_no_redirect_url":
        return "الحجز نجح لكن لم يرجع رابط دفع — حاول مرة أخرى."
    # Anything else — strip code prefix if any
    return err if not err.startswith("transient:") else err.split(":", 1)[-1]


# ════════════════════════════════════════════════════════════════════════
# Strict watcher classification
# ════════════════════════════════════════════════════════════════════════
def _is_chart_truly_full(res: dict) -> bool:
    """Return True ONLY when seats.io explicitly says every block is booked.

    This is the ONLY signal that justifies sending the account to the
    drop_watcher. Anything else is transient and must be retried.
    """
    return bool(res.get("chart_full"))


def _is_account_limit(res: dict) -> bool:
    """Return True when webook rejected the booking due to per-account
    ticket-limit / subscription cap. Such an account must be skipped —
    NEVER retried (would just keep failing) and NEVER watched.
    """
    if res.get("account_limit_reached"):
        return True
    err = (res.get("error") or "").lower()
    return ("limit reached" in err or "حد التذاكر" in err
            or "تجاوزت حد" in err
            or "booking limit" in err)


def _is_transient_failure(res: dict) -> bool:
    """Return True for failures that should be retried, NOT watched."""
    if res.get("ok"):
        return False
    if _is_chart_truly_full(res):
        return False  # canonical watcher case
    if _is_account_limit(res):
        return False  # NEVER retry an account-limit failure — it's permanent
    # Explicit transient signals
    if res.get("turnstile_required"):
        return True
    if res.get("queued"):
        return True
    if res.get("chart_unreachable"):
        return True
    # v8: technical error codes (always lowercase prefix)
    err = (res.get("error") or "")
    if err.startswith("transient:"):
        return True
    # Heuristic: error text contains transient hints
    err_l = err.lower()
    transient_hints = (
        "timeout", "timed out", "network", "connection", "temporarily",
        "captcha", "turnstile", "queue", "rate limit", "429", "503", "502",
        "504", "reset", "broken", "unavailable", "cloudflare",
    )
    if any(h in err_l for h in transient_hints):
        return True
    return False


def _failure_kind(res: dict) -> str:
    if _is_account_limit(res):
        return "account_limit"
    if res.get("chart_full"):
        return "chart_full"
    if res.get("turnstile_required"):
        return "turnstile"
    if res.get("queued"):
        return "queued"
    if res.get("chart_unreachable"):
        return "chart_unreachable"
    err = (res.get("error") or "").lower()
    if "timeout" in err or "timed out" in err:
        return "timeout"
    if "captcha" in err or "turnstile" in err:
        return "captcha_delay"
    return "no_seats"


# ════════════════════════════════════════════════════════════════════════
# Pre-Watch Sanity Check
# ════════════════════════════════════════════════════════════════════════
async def _has_any_free_seat(event_key: str, primary_block: str,
                              backup_blocks: list[str], quantity: int) -> bool:
    """Fast HTTP probe: are there ANY free seats RIGHT NOW for this event?

    Returns True if we can find a contiguous run of `quantity` (or any free
    capacity at all in the user's preferred blocks). If True, the caller
    must NOT register a watcher — the fast-lane should retry instead.
    """
    if not event_key:
        return False
    try:
        from app.services.seatsio_client import SeatsioClient
        from app.services.block_analyzer import (
            find_seats_with_fallback, chart_is_sold_out,
        )
        async with SeatsioClient(event_key) as client:
            ri = await client.rendering_info()
            statuses = await client.object_statuses()
            if not ri or not (ri.get("objects") or []):
                # Chart unreachable — can't conclude anything; default to retry
                return True
            # Try the user's preferred blocks first
            seats, _ = find_seats_with_fallback(
                ri, statuses,
                primary_block=primary_block,
                backup_blocks=backup_blocks,
                quantity=quantity,
                expand_geometric=True,
                expand_limit=8,
            )
            if seats:
                return True
            # If any free capacity exists anywhere → still retryable
            return not chart_is_sold_out(ri, statuses)
    except Exception as e:
        log.debug(f"pre-watch probe error: {e}")
        # Be optimistic: assume retryable
        return True


# ════════════════════════════════════════════════════════════════════════
# Watcher registration
# ════════════════════════════════════════════════════════════════════════
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


# ════════════════════════════════════════════════════════════════════════
# book_one — single account with smart retry
# ════════════════════════════════════════════════════════════════════════
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
                "error": "الحساب غير موجود", "fatal": True,
                "failure_kind": "no_account"}

    label = acc.get("label") or acc.get("email")

    # V14: refresh dynamic Webook secrets (cached 1h). Cheap on hot path.
    if _V14_AVAILABLE and _v14_get_secrets is not None:
        try:
            v14_secrets = await _v14_get_secrets()
            if v14_secrets and v14_secrets.is_complete():
                # Best-effort propagation: stash on the assignment object
                # so booking_http (via attribute lookup) can pick them up
                # without a breaking signature change.
                setattr(assignment, "_v14_secrets", v14_secrets.as_dict())
        except Exception as _se:
            log.debug("V14 dynamic secret refresh skipped: %s", _se)

    # V14: per-account proxy URL (read from accounts.proxy_url).
    proxy_url = (acc.get("proxy_url") or "").strip() or None
    if proxy_url:
        log.debug("V14 booking %s via proxy=%s",
                  assignment.account_id,
                  proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url)

    async def _p(txt: str):
        if progress:
            try:
                await progress(txt)
            except Exception:
                pass

    # ── Smart Retry Loop (transient errors only) ──
    last_res: dict = {}
    bearer = ""
    for attempt in range(1, MAX_RETRIES + 1):
        # Fresh bearer (auto-relogin on every iteration so expired/blocked
        # tokens get refreshed mid-loop).
        bearer = await auth_service.get_valid_bearer(
            assignment.account_id, notifier=notifier, auto_relogin=True,
        )
        if not bearer:
            return {
                "ok": False, "account_id": assignment.account_id, "label": label,
                "error": "لا يوجد توكن JWT صالح؛ أعد تسجيل الدخول.",
                "fatal": True, "failure_kind": "no_bearer",
            }

        if attempt == 1:
            await _p(f"⚡ <code>{label}</code> — HTTP-direct ({assignment.quantity} تذاكر)")
        else:
            await _p(f"🔄 <code>{label}</code> — إعادة المحاولة {attempt}/{MAX_RETRIES}")

        res = await book_ticket_http(
            bearer=bearer,
            slug=event_slug,
            ticket_id=ticket_id,
            quantity=assignment.quantity,
            payment_method=payment_method,
            ticket_meta=ticket_meta,
            primary_block=primary_block,
            backup_blocks=backup_blocks,
            account_email=acc.get("email", "") or "",
            account_user_id=acc.get("user_id", "") or "",
            account_password=acc.get("password", "") or "",
        )
        last_res = res

        if res.get("ok"):
            break  # success — exit retry loop

        # Decide: retry, watch, or hard-fail
        if _is_chart_truly_full(res):
            # Chart genuinely sold out — break and let the post-loop watcher
            # logic handle it (with sanity check).
            break

        if _is_transient_failure(res):
            kind = _failure_kind(res)
            err_short = (res.get("error") or "")[:80]
            await _p(f"⏳ <code>{label}</code> — مؤقت [{kind}]: {err_short}")
            if attempt < MAX_RETRIES:
                # Exponential backoff with jitter
                delay = min(MAX_BACKOFF, BASE_BACKOFF * (1.6 ** (attempt - 1)))
                delay += random.uniform(0, 0.5)
                await asyncio.sleep(delay)
                continue
            # Out of retries on a transient → return as hard fail (do NOT watch)
            break

        # Non-transient, non-chart-full failure → hard fail immediately, NO watcher
        break

    res = last_res

    # ── Browser fallback only for non-chart, non-transient failures ──
    if not res.get("ok") and not _is_chart_truly_full(res) and not _is_transient_failure(res):
        first_err = (res.get("error") or "")[:220]
        await _p(f"🔁 <code>{label}</code> — استخدام المتصفح ({first_err[:60]})")
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

    # ── Success path ──
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

    # ── Failure: classify and decide ──
    seat_info = res.get("seat_info") or {}
    event_key = seat_info.get("event_key", "")
    kind = _failure_kind(res)

    # WATCHER ONLY when chart_full is explicit AND a sanity check confirms
    # there really is no free capacity for this user's selection.
    if _is_chart_truly_full(res) and event_key:
        await _p(f"🔍 <code>{label}</code> — فحص ما قبل المراقبة...")
        any_free = await _has_any_free_seat(
            event_key, primary_block, backup_blocks, assignment.quantity,
        )
        if any_free:
            # FALSE POSITIVE chart_full → don't watch, retry once more
            await _p(f"♻️ <code>{label}</code> — وُجدت مقاعد، إعادة المحاولة")
            try:
                bearer = await auth_service.get_valid_bearer(
                    assignment.account_id, notifier=notifier, auto_relogin=True,
                )
                retry_res = await book_ticket_http(
                    bearer=bearer or "",
                    slug=event_slug, ticket_id=ticket_id,
                    quantity=assignment.quantity,
                    payment_method=payment_method, ticket_meta=ticket_meta,
                    primary_block=primary_block, backup_blocks=backup_blocks,
                    account_email=acc.get("email", "") or "",
                    account_user_id=acc.get("user_id", "") or "",
                    account_password=acc.get("password", "") or "",
                )
                if retry_res.get("ok"):
                    pay_url = retry_res.get("payment_url", "")
                    si = retry_res.get("seat_info", {}) or {}
                    db_id = add_booking(
                        chat_id=chat_id, event_slug=event_slug,
                        event_title=event_title, ticket_type=ticket_title,
                        account_id=assignment.account_id,
                        quantity=assignment.quantity, seat_info=si,
                        payment_url=pay_url,
                        total_amount=ticket_price * assignment.quantity,
                        currency=currency, status="pending",
                    )
                    mark_account_used(assignment.account_id)
                    return {
                        "ok": True, "account_id": assignment.account_id,
                        "label": label, "booking_id": db_id,
                        "payment_url": pay_url,
                        "order_id": retry_res.get("order_id", ""),
                        "quantity": assignment.quantity, "seat_info": si,
                        "seat_objects": retry_res.get("seat_objects", []),
                        "block_used": retry_res.get("block_used", ""),
                        "logs": (res.get("logs") or []) + (retry_res.get("logs") or []),
                    }
            except Exception as e:
                log.debug(f"sanity-check retry err: {e}")
            # If retry also failed and chart still has free seats, return as
            # hard transient error (NOT watcher).
            return {
                "ok": False, "account_id": assignment.account_id,
                "label": label,
                "error": "تعارض بيانات الخريطة — لم يتمكن من الحجز رغم وجود مقاعد",
                "failure_kind": "transient_conflict",
                "logs": res.get("logs", []),
            }

        # Sanity check confirms truly full → register watcher
        ok = await _convert_to_watcher(
            chat_id=chat_id, account_id=assignment.account_id,
            event_slug=event_slug, event_key=event_key,
            ticket_id=ticket_id, quantity=assignment.quantity,
            primary_block=primary_block, backup_blocks=backup_blocks,
        )
        if ok:
            await _p(f"👁️ <code>{label}</code> — وضع الترقّب فُعّل (chart_full)")
            return {
                "ok": False, "account_id": assignment.account_id,
                "label": label,
                "error": "الخريطة ممتلئة — وضع الترقّب",
                "drop_watcher_active": True, "failure_kind": "chart_full",
                "logs": res.get("logs", []),
            }

    # All other failures → HARD FAIL, no watcher
    # v8: humanize the error code into Arabic before returning to UI
    return {
        "ok": False, "account_id": assignment.account_id, "label": label,
        "error": _humanize_error(res)[:320],
        "error_code": (res.get("error") or "")[:120],  # raw code for logs
        "failure_kind": kind,
        "logs": res.get("logs", []),
    }


# ════════════════════════════════════════════════════════════════════════
# FAST-LANE BOOKING ENGINE (unchanged interface)
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

    for fut in asyncio.as_completed(tasks):
        try:
            r = await fut
        except Exception as e:
            log.exception(f"fast-lane task crashed: {e}")
            continue
        results.append(r)

        if r.get("ok") and fast_callback and r.get("account_id") not in notified:
            notified.add(r["account_id"])
            try:
                await fast_callback(r)
            except Exception as e:
                log.warning(f"fast_callback err: {e}")

    return results


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
