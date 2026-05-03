"""Telegram update dispatcher — 100% button-driven, all Arabic UI.

v4 workflow:
  1. User sends an event link (or picks from list)
  2. Bot fetches seats.io rendering_info immediately and shows blocks
  3. User picks PRIMARY block, then BACKUP blocks (S1, S2, ...)
  4. User sends quantity → bot confirms → executes booking
  5. Booking algorithm: adjacency → backup blocks → geometric expansion →
     drop-watcher when chart fully booked
  6. Output uses smart seat summarization
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from urllib.parse import urlparse

from app.bot import keyboards as kb
from app.bot import state as fsm
from app.bot import tokens as tok
from app.bot.notifier import Notifier
from app.core.config import authorized_chat_ids, default_payment_method, PUBLIC_URL
from app.core.storage import (
    delete_account, get_account, list_accounts, list_bookings,
    list_recent_events, upsert_account, upsert_event, set_bot_setting,
    get_bot_setting,
)
from app.services import auth_service
from app.services.block_analyzer import extract_blocks
from app.services.booking_orchestrator import book_all
from app.services.distributor import describe_plan, distribute
from app.services.event_discovery import enrich_all, fetch_event_slugs
from app.services.seat_summarizer import summarize_for_telegram
from app.services.seatsio_client import SeatsioClient
from app.services.seatsio_runtime import ensure_event_warm
from app.services.webook_api import get_event_detail, get_event_tickets

log = logging.getLogger("handlers")

WELCOME = (
    "👋 <b>أهلاً بك في بوت Webook</b>\n\n"
    "أرسل رابط فعالية ↗️ أو اختر من قائمة الفعاليات.\n"
    "البوت يتعامل مباشرةً مع خرائط seats.io ويحجز مقاعد متجاورة بذكاء.\n\n"
    "اختر من القائمة:"
)

HELP = (
    "🆘 <b>طريقة الاستخدام</b>\n\n"
    "1️⃣ من <b>إدارة الحسابات</b> أضف حساباً أو أكثر\n"
    "2️⃣ اضغط <b>تسجيل الدخول</b> لكل حساب (مرة واحدة)\n"
    "3️⃣ أرسل <b>رابط الفعالية</b> أو اختر من القائمة\n"
    "4️⃣ اختر نوع التذكرة\n"
    "5️⃣ <b>اختر المربع الرئيسي + المربعات الاحتياطية</b>\n"
    "6️⃣ أرسل عدد التذاكر — البوت يحجز مقاعد متجاورة تلقائياً\n\n"
    "🎯 إذا الخريطة ممتلئة → يدخل وضع الترقّب لاصطياد المقاعد الساقطة.\n"
    "💳 طريقة الدفع الافتراضية: بطاقة ائتمانية (قابلة للتغيير من الإعدادات).\n"
    "💡 <i>التوكن صالح ~٧ أيام ويُجدَّد تلقائياً.</i>"
)


# ════════════════════════════════════════════════════════════════════════
# Entry
# ════════════════════════════════════════════════════════════════════════
async def dispatch(update: dict, notifier: Notifier) -> None:
    try:
        if "callback_query" in update:
            await _on_callback(update["callback_query"], notifier)
        elif "message" in update:
            await _on_message(update["message"], notifier)
    except Exception as e:
        log.exception(f"dispatch err: {e}")


def _authorized(chat_id: str) -> bool:
    ids = authorized_chat_ids()
    return not ids or str(chat_id) in ids


def _extract_slug_from_link(text: str) -> str | None:
    """Parse 'https://webook.com/ar/events/<slug>' or '<slug>' into slug."""
    text = text.strip()
    if not text:
        return None
    if text.startswith("http"):
        try:
            p = urlparse(text)
            parts = [x for x in p.path.split("/") if x]
            # patterns: /ar/events/<slug>  or /en/events/<slug>/book
            for i, seg in enumerate(parts):
                if seg == "events" and i + 1 < len(parts):
                    return parts[i + 1]
        except Exception:
            return None
    # treat raw text as slug only if it looks like one (lowercase/digits/hyphens)
    if re.fullmatch(r"[a-z0-9][a-z0-9\-]{2,}", text):
        return text
    return None


# ════════════════════════════════════════════════════════════════════════
# Messages
# ════════════════════════════════════════════════════════════════════════
async def _on_message(msg: dict, notifier: Notifier) -> None:
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if not _authorized(chat_id):
        await notifier.send(chat_id, "🚫 غير مصرّح لك باستخدام هذا البوت.")
        return

    # Handle WebApp Mini Picker payload (sent via Telegram.WebApp.sendData)
    if "web_app_data" in msg:
        try:
            wa = msg["web_app_data"] or {}
            raw = wa.get("data") or ""
            import json as _json
            payload = _json.loads(raw) if raw else {}
        except Exception:
            payload = {}
        await _handle_webapp_selection(chat_id, payload, notifier)
        return

    st = fsm.get_state(chat_id)
    if st:
        if st.name == "waiting_email":
            if "@" not in text or "." not in text:
                await notifier.send(chat_id, "⚠️ يرجى إرسال بريد صالح.")
                return
            fsm.set_state(chat_id, "waiting_password", email=text)
            await notifier.send(
                chat_id,
                "✅ تم استلام البريد.\nأرسل الآن <b>كلمة المرور</b>:")
            return

        if st.name == "waiting_password":
            email = st.data.get("email", "")
            account_id = "acc_" + uuid.uuid4().hex[:8]
            upsert_account(account_id, email=email, password=text,
                           label=email.split("@")[0])
            fsm.clear_state(chat_id)
            await notifier.send(
                chat_id,
                f"✅ تمت إضافة الحساب (<code>{email}</code>).\n\n"
                f"اضغط على الحساب ثم <b>🔐 تسجيل الدخول</b> لتفعيله.",
                reply_markup=kb.accounts_keyboard(list_accounts()),
            )
            return

        if st.name == "waiting_event_link":
            slug = _extract_slug_from_link(text)
            fsm.clear_state(chat_id)
            if not slug:
                await notifier.send(
                    chat_id,
                    "⚠️ تعذّر استخراج رابط الفعالية. أرسل رابطاً مثل:\n"
                    "<code>https://webook.com/ar/events/some-slug</code>",
                    reply_markup=kb.back_to_menu())
                return
            await _show_event(chat_id, slug, notifier)
            return

        if st.name == "waiting_qty":
            ctx = st.data
            try:
                n = int(text.strip())
                if n <= 0:
                    raise ValueError
            except ValueError:
                await notifier.send(chat_id,
                                     "⚠️ أرسل عدداً صحيحاً موجباً فقط.")
                return
            fsm.clear_state(chat_id)
            await _show_plan(chat_id, ctx["slug"], ctx["ticket_id"],
                             ctx.get("session_token", ""), n, notifier)
            return

    # plain text could be an event link
    slug = _extract_slug_from_link(text)
    if slug:
        await _show_event(chat_id, slug, notifier)
        return

    # otherwise → main menu
    await notifier.send(chat_id, WELCOME, reply_markup=kb.main_menu())


# ════════════════════════════════════════════════════════════════════════
# Callbacks
# ════════════════════════════════════════════════════════════════════════
async def _on_callback(cq: dict, notifier: Notifier) -> None:
    chat_id = str(cq["message"]["chat"]["id"])
    msg_id = cq["message"]["message_id"]
    data = cq.get("data", "")

    if not _authorized(chat_id):
        await notifier.answer_cb(cq["id"], "🚫 غير مصرّح", show_alert=True)
        return

    await notifier.answer_cb(cq["id"])

    try:
        await _route(chat_id, msg_id, data, notifier)
    except Exception as e:
        log.exception(f"callback err: {e}")
        try:
            await notifier.send(chat_id, f"⚠️ خطأ: <code>{e}</code>",
                                reply_markup=kb.back_to_menu())
        except Exception:
            pass


async def _route(chat_id: str, msg_id: int, data: str,
                 notifier: Notifier) -> None:
    if data == "menu":
        await notifier.edit(chat_id, msg_id, WELCOME,
                            reply_markup=kb.main_menu()); return
    if data == "help:show":
        await notifier.edit(chat_id, msg_id, HELP,
                            reply_markup=kb.back_to_menu()); return

    # Direct link prompt
    if data == "link:prompt":
        fsm.set_state(chat_id, "waiting_event_link")
        await notifier.edit(
            chat_id, msg_id,
            "🔗 <b>أرسل رابط الفعالية</b>\n\n"
            "مثال:\n<code>https://webook.com/ar/events/event-slug</code>",
            reply_markup=kb.back_to_menu(),
        ); return

    # Settings
    if data == "settings:menu":
        current = get_bot_setting("DEFAULT_PAYMENT_METHOD",
                                  default_payment_method())
        await notifier.edit(
            chat_id, msg_id,
            f"⚙️ <b>الإعدادات العامة</b>\n\n"
            f"💳 <b>طريقة الدفع الحالية:</b> "
            f"{'بطاقة ائتمانية' if current=='credit_card' else 'Apple Pay'}\n\n"
            f"تنطبق على <b>جميع الحسابات</b> بشكل موحّد.",
            reply_markup=kb.settings_keyboard(current),
        ); return
    if data.startswith("settings:pay:"):
        method = data.split(":", 2)[2]
        if method not in {"credit_card", "apple_pay"}:
            method = "credit_card"
        set_bot_setting("DEFAULT_PAYMENT_METHOD", method, updated_by=chat_id)
        await notifier.edit(
            chat_id, msg_id,
            f"✅ تم ضبط طريقة الدفع → "
            f"{'💳 بطاقة ائتمانية' if method=='credit_card' else '🍎 Apple Pay'}",
            reply_markup=kb.settings_keyboard(method),
        ); return

    # Events list
    if data.startswith("events:"):
        arg = data.split(":", 1)[1]
        await _show_events(chat_id, msg_id, arg, notifier); return
    if data.startswith("evt:"):
        t = data.split(":", 1)[1]
        entry = tok.get(t)
        if not entry:
            await notifier.edit(chat_id, msg_id, "انتهت صلاحية هذا الرابط.",
                                reply_markup=kb.back_to_menu()); return
        await _show_event(chat_id, entry["slug"], notifier,
                          edit_msg_id=msg_id, event_token=t)
        return
    if data.startswith("tck:"):
        t = data.split(":", 1)[1]
        entry = tok.get(t)
        if not entry:
            await notifier.edit(chat_id, msg_id, "انتهت صلاحية هذا الرابط.",
                                reply_markup=kb.back_to_menu()); return
        await _start_block_picker(chat_id, msg_id,
                                   entry["slug"], entry["ticket_id"],
                                   notifier)
        return

    # Block picker actions
    if data.startswith("blk:"):
        await _route_blocks(chat_id, msg_id, data, notifier); return

    if data.startswith("go:"):
        t = data.split(":", 1)[1]
        entry = tok.get(t)
        if not entry:
            await notifier.edit(chat_id, msg_id, "انتهت صلاحية هذا الرابط.",
                                reply_markup=kb.back_to_menu()); return
        await _execute_booking(
            chat_id, msg_id,
            entry["slug"], entry["ticket_id"], entry["qty"],
            entry.get("primary_block", ""),
            entry.get("backup_blocks", []),
            notifier,
        )
        return

    # Accounts
    if data == "accounts:list":
        await notifier.edit(
            chat_id, msg_id, "👥 <b>حساباتك</b>",
            reply_markup=kb.accounts_keyboard(list_accounts())); return
    if data == "acc:add":
        fsm.set_state(chat_id, "waiting_email")
        await notifier.send(chat_id,
                            "📧 أرسل <b>البريد الإلكتروني</b> لحساب webook:")
        return
    if data.startswith("acc:login:"):
        acc_id = data.split(":", 2)[2]
        await _login_flow(chat_id, msg_id, acc_id, notifier); return
    if data.startswith("acc:del:"):
        acc_id = data.split(":", 2)[2]
        delete_account(acc_id)
        await notifier.edit(chat_id, msg_id, "🗑️ تم حذف الحساب.",
                            reply_markup=kb.accounts_keyboard(list_accounts()))
        return
    if data.startswith("acc:"):
        acc_id = data.split(":", 1)[1]
        await _show_account(chat_id, msg_id, acc_id, notifier); return

    # Bookings
    if data == "bookings:list":
        await _show_bookings(chat_id, notifier, edit_msg_id=msg_id); return


# ════════════════════════════════════════════════════════════════════════
# Block picker (NEW)
# ════════════════════════════════════════════════════════════════════════
# session_token → {slug, ticket_id, blocks_meta, primary, backups, mode}
_PICKER_SESSIONS: dict[str, dict] = {}


def _webapp_url_for_session(session_token: str) -> str:
    """Build the public URL for the WebApp Mini Picker for a given session.

    Returns empty string if PUBLIC_URL is not configured (WebApp button
    will be hidden in that case).
    """
    if not PUBLIC_URL:
        return ""
    sess = _PICKER_SESSIONS.get(session_token) or {}
    if not sess.get("event_key"):
        # Without event_key the visual chart cannot be rendered, so don't
        # advertise the WebApp button.
        return ""
    return f"{PUBLIC_URL.rstrip('/')}/picker/{session_token}"


async def _start_block_picker(chat_id: str, msg_id: int,
                               slug: str, ticket_id: str,
                               notifier: Notifier) -> None:
    """Step 2: fetch event_key + rendering_info → show blocks list.

    Strategy (graceful degradation):
      1. Try seats.io live REST (works for legacy seatcloud charts)
      2. If that fails (e.g. seats_planner provider behind Turnstile),
         derive blocks from the ticket categories returned by webook.
         Each ticket-type IS effectively a block on these new charts.
      3. Offer a 🌐 WebApp Mini Picker so the user can pick exact seats
         visually inside Telegram (uses the user's browser → bypasses
         Turnstile naturally).
    """
    await notifier.edit(
        chat_id, msg_id,
        "🔄 جارٍ جلب بيانات seats.io للفعالية...",
        reply_markup=None,
    )

    import aiohttp
    from app.services.booking_http import (
        fetch_event_meta, resolve_seated_manifest,
    )
    async with aiohttp.ClientSession() as session:
        meta = await fetch_event_meta(session, slug, "")
        if not meta.get("is_seated"):
            await notifier.edit(
                chat_id, msg_id,
                "ℹ️ هذه الفعالية بدون خريطة مقاعد — انتقل لإدخال العدد.",
                reply_markup=None,
            )
            await _ask_quantity_no_blocks(chat_id, msg_id, slug, ticket_id, notifier)
            return
        manifest = await resolve_seated_manifest(
            session, slug, ticket_id, "", event_meta=meta,
        )
        # Also pull tickets for category-fallback
        tickets_data = await get_event_tickets(slug)

    event_key = manifest.get("event_key") or ""
    raw_event = (meta.get("raw") or {})
    seats_io_blob = raw_event.get("seats_io") or {}
    seats_provider = raw_event.get("seats_provider") or ""

    # 1) Try live seats.io rendering_info
    rendering_info = None
    statuses = {}
    blocks_meta: list[dict] = []
    if event_key:
        try:
            await ensure_event_warm(event_key)
            async with SeatsioClient(event_key) as client:
                rendering_info = await client.rendering_info()
                statuses = await client.object_statuses()
            blocks_meta = extract_blocks(rendering_info, statuses)
        except Exception as e:
            log.debug(f"seats.io rendering_info failed: {e}")

    # 2) Fallback: derive blocks from ticket categories
    fallback_used = False
    if not blocks_meta:
        active_tickets = [
            t for t in (tickets_data.get("tickets") or [])
            if t.get("status") == "active" and t.get("sale_status") == "ongoing"
        ]
        if active_tickets:
            blocks_meta = []
            for t in active_tickets:
                cat = t.get("seats_io_category") or ""
                blocks_meta.append({
                    "name": t.get("title") or f"cat-{cat}",
                    "free": -1,           # unknown via API
                    "total": -1,
                    "category": str(cat),
                    "ticket_id": t.get("id"),
                })
            fallback_used = True

    if not blocks_meta:
        await notifier.edit(
            chat_id, msg_id,
            "⚠️ لم أتمكّن من قراءة بلوكات الخريطة.\n"
            "ربما الفعالية لم يبدأ بيعها بعد.",
            reply_markup=kb.back_to_menu(),
        ); return

    # Cache the seat map for reuse (only if real)
    if rendering_info and not fallback_used:
        try:
            from app.core.storage import save_seat_map
            save_seat_map(
                chart_key=event_key, event_key=event_key,
                rendering_info=rendering_info,
                blocks_meta=[{"name": b["name"], "free": b["free"],
                              "total": b["total"]} for b in blocks_meta],
            )
        except Exception:
            pass

    session_token = uuid.uuid4().hex[:10]
    _PICKER_SESSIONS[session_token] = {
        "chat_id": chat_id,
        "slug": slug,
        "ticket_id": ticket_id,
        "event_key": event_key,
        "workspace_key": seats_io_blob.get("workspace_key") or "",
        "chart_key": seats_io_blob.get("chart_key") or "",
        "seats_provider": seats_provider,
        "blocks_meta": blocks_meta,
        "primary": "",
        "backups": [],
        "mode": "primary",
        "fallback_used": fallback_used,
        "webapp_completed": False,
        "created_at": time.time(),
    }

    if fallback_used:
        free_line = "📊 البلوكات (من أنواع التذاكر):"
    else:
        free_total = sum(b.get("free", 0) for b in blocks_meta if b.get("free", 0) >= 0)
        free_line = f"🟢 مقاعد متاحة الآن: <b>{free_total}</b>"

    txt = (
        f"🗺️ <b>اختر البلوكات</b>\n\n"
        f"📦 إجمالي البلوكات: <b>{len(blocks_meta)}</b>\n"
        f"{free_line}\n"
        + ("⚠️ <i>الفعالية محمية بـ Turnstile — يستحسن استخدام «الواجهة المرئية» أدناه لاختيار مقاعد مرئية.</i>\n" if fallback_used else "")
        + f"\n1️⃣ اضغط على بلوك ليصبح <b>الرئيسي ⭐</b>\n"
        f"2️⃣ بدّل لـ «وضع الاحتياطي» وأضف S2, S3...\n"
        f"3️⃣ اضغط <b>تأكيد البلوكات</b> للمتابعة\n"
        f"   أو <b>🌐 الواجهة المرئية</b> لاختيار من الخريطة الفعلية"
    )
    await notifier.edit(
        chat_id, msg_id, txt,
        reply_markup=kb.blocks_picker_keyboard(
            blocks_meta, session_token, primary="", backups=[],
            mode="primary",
        webapp_url=_webapp_url_for_session(session_token)),
    )


async def _route_blocks(chat_id: str, msg_id: int, data: str,
                         notifier: Notifier) -> None:
    """Handles all blk:* callbacks."""
    parts = data.split(":")
    if len(parts) < 3:
        return
    op = parts[1]
    sess_tok = parts[2]
    sess = _PICKER_SESSIONS.get(sess_tok)
    if not sess:
        await notifier.edit(chat_id, msg_id,
                            "⚠️ انتهت جلسة اختيار البلوكات.",
                            reply_markup=kb.back_to_menu()); return

    blocks_meta = sess["blocks_meta"]

    if op == "setmode":
        sess["mode"] = parts[3] if len(parts) > 3 else "primary"
        await notifier.edit(
            chat_id, msg_id,
            _build_picker_caption(sess),
            reply_markup=kb.blocks_picker_keyboard(
            blocks_meta, sess_tok,
                primary=sess["primary"], backups=sess["backups"],
                mode=sess["mode"],
            webapp_url=_webapp_url_for_session(sess_tok)),
        ); return

    if op in ("primary", "backup"):
        block_name_safe = parts[3] if len(parts) > 3 else ""
        # match against actual block names (which may contain spaces)
        actual = next(
            (b["name"] for b in blocks_meta
             if b["name"].replace(":", "_").replace(" ", "_")[:20] == block_name_safe),
            None,
        )
        if not actual:
            return
        if op == "primary":
            sess["primary"] = actual
            # remove from backups if duplicate
            sess["backups"] = [b for b in sess["backups"] if b != actual]
        else:  # backup toggle
            if actual == sess["primary"]:
                # promoting/demoting handled implicitly
                return
            if actual in sess["backups"]:
                sess["backups"].remove(actual)
            else:
                sess["backups"].append(actual)
        await notifier.edit(
            chat_id, msg_id,
            _build_picker_caption(sess),
            reply_markup=kb.blocks_picker_keyboard(
            blocks_meta, sess_tok,
                primary=sess["primary"], backups=sess["backups"],
                mode=sess["mode"],
            webapp_url=_webapp_url_for_session(sess_tok)),
        ); return

    if op == "done":
        if not sess["primary"]:
            await notifier.edit(
                chat_id, msg_id,
                "⚠️ <b>اختر البلوك الرئيسي أولاً</b>\n\n"
                "تأكد أن أحد البلوكات معلّم بـ ⭐",
                reply_markup=kb.blocks_picker_keyboard(
            blocks_meta, sess_tok,
                    primary=sess["primary"], backups=sess["backups"],
                    mode=sess["mode"],
                webapp_url=_webapp_url_for_session(sess_tok)),
            ); return
        # → ask quantity
        await _ask_quantity(chat_id, msg_id, sess["slug"], sess["ticket_id"],
                             sess_tok, notifier)
        return


def _build_picker_caption(sess: dict) -> str:
    blocks_meta = sess["blocks_meta"]
    known_free = [b.get("free", -1) for b in blocks_meta if b.get("free", -1) >= 0]
    free_line = (f"🟢 إجمالي مقاعد متاحة: <b>{sum(known_free)}</b>"
                 if known_free else "🟡 التوفّر المباشر غير معروف لهذه الخريطة")
    primary = sess["primary"] or "—"
    backups_str = (" → ".join(sess["backups"])) if sess["backups"] else "—"
    mode_lbl = "🟢 الوضع الحالي: <b>اختيار الرئيسي ⭐</b>" \
        if sess["mode"] == "primary" else \
        "🔁 الوضع الحالي: <b>اختيار الاحتياطي</b>"
    return (
        f"🗺️ <b>اختر البلوكات</b>\n\n"
        f"⭐ الرئيسي: <code>{primary}</code>\n"
        f"🔁 الاحتياطية: <code>{backups_str}</code>\n"
        f"{free_line}\n\n"
        f"{mode_lbl}\n\n"
        f"بعد الانتهاء اضغط <b>✅ تأكيد البلوكات</b>"
    )


# ════════════════════════════════════════════════════════════════════════
# Screens
# ════════════════════════════════════════════════════════════════════════
async def _show_events(chat_id: str, msg_id: int, arg: str,
                       notifier: Notifier) -> None:
    if arg == "refresh":
        await notifier.edit(chat_id, msg_id, "🔄 جارٍ تحديث الفعاليات...",
                            reply_markup=None)
        slugs = await fetch_event_slugs(max_events=200)
        events = await enrich_all(slugs, concurrency=6)
        for e in events:
            upsert_event(e["slug"], e)
        page = 0
    else:
        try:
            page = int(arg)
        except ValueError:
            page = 0
        events = list_recent_events(limit=200)
        if not events:
            await notifier.edit(chat_id, msg_id,
                                "🔄 أول تحميل — جارٍ جلب الفعاليات...",
                                reply_markup=None)
            slugs = await fetch_event_slugs(max_events=200)
            events = await enrich_all(slugs, concurrency=6)
            for e in events:
                upsert_event(e["slug"], e)

    if not events:
        await notifier.edit(chat_id, msg_id,
                            "⚠️ لا توجد فعاليات متاحة الآن.",
                            reply_markup=kb.back_to_menu())
        return

    await notifier.edit(
        chat_id, msg_id,
        f"🎫 <b>الفعاليات المتاحة</b> ({len(events)})\n\n"
        f"اضغط فعالية لعرض تذاكرها:",
        reply_markup=kb.events_keyboard(events, page=page),
    )


async def _show_event(chat_id: str, slug: str, notifier: Notifier,
                      edit_msg_id: int | None = None,
                      event_token: str | None = None) -> None:
    detail_task = asyncio.create_task(get_event_detail(slug))
    tix_task = asyncio.create_task(get_event_tickets(slug))
    detail = await detail_task
    data = await tix_task

    if not detail and not data:
        t = "⚠️ تعذّر جلب بيانات الفعالية."
        if edit_msg_id:
            await notifier.edit(chat_id, edit_msg_id, t,
                                reply_markup=kb.back_to_menu())
        else:
            await notifier.send(chat_id, t, reply_markup=kb.back_to_menu())
        return

    title = (detail or {}).get("title") or (data or {}).get("event", {}).get("title") or slug
    sub = (detail or {}).get("sub_title") or ""
    desc_raw = (detail or {}).get("description") or ""
    desc = re.sub(r"<[^>]+>", " ", desc_raw)
    desc = re.sub(r"\s+", " ", desc).strip()[:300]

    tickets = (data or {}).get("tickets") or []
    active = [t for t in tickets if t.get("status") == "active"]

    txt = f"🎭 <b>{title}</b>\n"
    if sub:
        txt += f"{sub}\n"
    if desc:
        txt += f"\n{desc}\n"

    if active:
        txt += f"\n🎟️ أنواع التذاكر المتاحة: <b>{len(active)}</b>\n\n"
        txt += "اختر نوع التذكرة:"
        rkb = kb.ticket_types_keyboard(slug, tickets)
    else:
        txt += "\n⚠️ <i>لا توجد تذاكر متاحة حالياً عبر API.</i>\n"
        txt += ("قد تكون الفعالية تتطلب اشتراكاً، أو لم يُفتح بيعها بعد، "
                "أو تُباع بطريقة مختلفة (مثل seats.io). "
                "افتحها في المتصفح للتأكد:")
        rkb = {"inline_keyboard": [
            [{"text": "🌐 فتح الفعالية في المتصفح",
              "url": f"https://webook.com/ar/events/{slug}"}],
            [{"text": "⬅️ رجوع للفعاليات", "callback_data": "events:0"}],
            [{"text": "🏠 القائمة", "callback_data": "menu"}],
        ]}

    if edit_msg_id:
        await notifier.edit(chat_id, edit_msg_id, txt, reply_markup=rkb)
    else:
        await notifier.send(chat_id, txt, reply_markup=rkb)


async def _handle_webapp_selection(chat_id: str, payload: dict,
                                     notifier: Notifier) -> None:
    """Apply a WebApp Mini Picker selection to the most recent picker
    session for this chat.

    Telegram doesn't tell us which session token the WebApp came from
    (sendData carries only the JSON), so we use the most recent session
    that has no completed selection yet.
    """
    if not payload:
        await notifier.send(chat_id, "⚠️ لم أستلم بيانات اختيار صالحة من الواجهة.",
                              reply_markup=kb.back_to_menu())
        return

    primary = (payload.get("primary") or "").strip()
    backups_raw = payload.get("backups") or []
    backups = [str(b).strip() for b in backups_raw if str(b).strip()]
    seats = [s for s in (payload.get("seats") or []) if s]
    if not primary and backups:
        primary = backups.pop(0)

    if not primary and not seats:
        await notifier.send(chat_id,
            "⚠️ لم تختر أي بلوك أو مقعد. أعد المحاولة.",
            reply_markup=kb.back_to_menu())
        return

    # Find the most recent picker session for this chat
    target_token = None
    target_sess = None
    for tok_, s in reversed(list(_PICKER_SESSIONS.items())):
        if s.get("chat_id") == chat_id and not s.get("webapp_completed"):
            target_token = tok_
            target_sess = s
            break
    if not target_sess:
        # Fallback: take the latest one regardless
        if _PICKER_SESSIONS:
            target_token = list(_PICKER_SESSIONS.keys())[-1]
            target_sess = _PICKER_SESSIONS[target_token]

    if not target_sess:
        await notifier.send(chat_id,
            "⚠️ انتهت جلسة اختيار البلوكات. ابدأ من جديد.",
            reply_markup=kb.main_menu())
        return

    if primary:
        target_sess["primary"] = primary
    if backups:
        seen = set([target_sess["primary"]] if target_sess.get("primary") else [])
        target_sess["backups"] = []
        for b in backups:
            if b in seen:
                continue
            seen.add(b)
            target_sess["backups"].append(b)
    if seats:
        target_sess["preselected_seats"] = seats
    target_sess["webapp_completed"] = True

    primary_show = target_sess.get("primary") or "—"
    backups_show = " → ".join(target_sess.get("backups") or []) or "—"
    seat_count = len(target_sess.get("preselected_seats") or [])
    seat_line = f"\n🪑 مقاعد مختارة مسبقاً: <b>{seat_count}</b>" if seat_count else ""

    await notifier.send(
        chat_id,
        f"✅ <b>تم استلام اختيارك من الواجهة المرئية</b>\n\n"
        f"⭐ الرئيسي: <code>{primary_show}</code>\n"
        f"🔁 الاحتياطية: <code>{backups_show}</code>"
        f"{seat_line}\n\n"
        f"✨ أرسل الآن عدد التذاكر المطلوب لكل حساب.",
        reply_markup=kb.back_to_menu(),
    )
    fsm.set_state(chat_id, "waiting_qty",
                   slug=target_sess["slug"],
                   ticket_id=target_sess["ticket_id"],
                   session_token=target_token)


async def _ask_quantity(chat_id: str, msg_id: int, slug: str, ticket_id: str,
                         session_token: str, notifier: Notifier) -> None:
    """Step 3: after blocks picked, ask for quantity."""
    data = await get_event_tickets(slug)
    ticket = next(
        (t for t in (data.get("tickets") or []) if t["id"] == ticket_id),
        None,
    )
    if not ticket:
        await notifier.edit(chat_id, msg_id, "⚠️ لم أجد نوع التذكرة.",
                            reply_markup=kb.back_to_menu())
        return

    accounts = [a for a in list_accounts(status="ready")
                if a.get("access_token")]
    price = ticket.get("display_price") or 0
    ccy = kb._ccy(ticket.get("currency") or "SAR")
    price_str = f"{kb._fmt_price(price)} {ccy}" if price else "يظهر عند الحجز"

    max_cap = ticket["max_per_order"] * max(len(accounts), 1)
    min_q = ticket.get("min_per_order", 1)

    if len(accounts) == 0:
        await notifier.edit(
            chat_id, msg_id,
            f"🎫 <b>{ticket['title']}</b>\n💰 السعر: <b>{price_str}</b>\n\n"
            f"⚠️ لا يوجد لديك حسابات مُفعّلة بعد.\n"
            f"أضف حساباً من <b>إدارة الحسابات</b> أولاً.",
            reply_markup=kb.back_to_menu(),
        ); return

    fsm.set_state(chat_id, "waiting_qty",
                   slug=slug, ticket_id=ticket_id,
                   session_token=session_token)

    sess = _PICKER_SESSIONS.get(session_token, {})
    primary = sess.get("primary", "—")
    backups = " → ".join(sess.get("backups", [])) or "—"

    txt = (
        f"🎫 <b>{ticket['title']}</b>\n\n"
        f"💰 السعر: <b>{price_str}</b>\n"
        f"⭐ البلوك الرئيسي: <code>{primary}</code>\n"
        f"🔁 الاحتياطية: <code>{backups}</code>\n"
        f"👥 حسابات جاهزة: <b>{len(accounts)}</b>\n"
        f"📊 الحد الأقصى لكل حساب: <b>{ticket['max_per_order']}</b>\n"
        f"🧮 أقصى إجمالي يمكنك حجزه: <b>{max_cap}</b>\n"
        f"🔢 الحد الأدنى لكل حساب: <b>{min_q}</b>\n\n"
        f"✏️ <b>أرسل الآن عدد التذاكر المطلوب</b> كرسالة:"
    )
    await notifier.edit(chat_id, msg_id, txt,
                        reply_markup=kb.back_to_menu())


async def _ask_quantity_no_blocks(chat_id: str, msg_id: int, slug: str,
                                    ticket_id: str, notifier: Notifier) -> None:
    """Quantity prompt for non-seated events (no blocks needed)."""
    fsm.set_state(chat_id, "waiting_qty",
                   slug=slug, ticket_id=ticket_id, session_token="")
    await notifier.send(chat_id,
                         "✏️ أرسل عدد التذاكر المطلوب:",
                         reply_markup=kb.back_to_menu())


async def _show_plan(chat_id: str, slug: str, ticket_id: str,
                      session_token: str, qty: int,
                      notifier: Notifier) -> None:
    data = await get_event_tickets(slug)
    detail = await get_event_detail(slug)
    ticket = next(
        (t for t in (data.get("tickets") or []) if t["id"] == ticket_id),
        None,
    )
    if not ticket:
        await notifier.send(chat_id, "⚠️ نوع التذكرة غير موجود.",
                            reply_markup=kb.back_to_menu())
        return

    accounts = [a for a in list_accounts(status="ready")
                if a.get("access_token")]
    try:
        plan, meta = distribute(qty, accounts=accounts,
                                max_per_order=ticket["max_per_order"],
                                min_per_order=ticket["min_per_order"])
    except ValueError as e:
        await notifier.send(
            chat_id,
            f"⚠️ <b>لا يمكن توزيع {qty} تذاكر</b>\n\n"
            f"السبب: <code>{e}</code>\n\n"
            f"الحلول: قلّل العدد أو أضف حسابات.",
            reply_markup=kb.back_to_menu()); return

    sess = _PICKER_SESSIONS.get(session_token, {})
    primary = sess.get("primary", "")
    backups = sess.get("backups", [])

    price = ticket.get("display_price") or 0
    total_tickets = meta.get("total_tickets", qty)
    total_amount = price * total_tickets
    ccy = kb._ccy(ticket.get("currency") or "SAR")
    title = (detail or {}).get("title") or slug
    pay_method = get_bot_setting("DEFAULT_PAYMENT_METHOD",
                                  default_payment_method())
    pay_lbl = "💳 بطاقة ائتمانية" if pay_method == "credit_card" else "🍎 Apple Pay"

    context_tok = tok.put({
        "slug": slug, "ticket_id": ticket_id,
        "qty": qty,
        "per_account": meta["actual_per_account"],
        "primary_block": primary,
        "backup_blocks": backups,
    })

    blocks_line = ""
    if primary:
        blocks_line = (f"⭐ الرئيسي: <code>{primary}</code>\n"
                        f"🔁 الاحتياطية: <code>{' → '.join(backups) or '—'}</code>\n")

    txt = (
        f"📊 <b>خطة التوزيع</b>\n\n"
        f"🎭 {title}\n"
        f"🎫 {ticket['title']}\n"
        f"{blocks_line}"
        f"🔢 لكل حساب: <b>{meta['actual_per_account']}</b> تذكرة\n"
        f"👥 عدد الحسابات: <b>{meta['accounts_count']}</b>\n"
        f"🧮 الإجمالي المتوقع: <b>{total_tickets}</b> تذكرة\n"
        f"💰 المجموع التقريبي: <b>{kb._fmt_price(total_amount)} {ccy}</b>\n"
        f"💳 طريقة الدفع: <b>{pay_lbl}</b>\n\n"
        f"{describe_plan(plan, accounts, meta)}\n\n"
        f"هل أبدأ الحجز؟"
    )
    await notifier.send(chat_id, txt,
                        reply_markup=kb.confirm_plan_keyboard(context_tok))


async def _execute_booking(chat_id: str, msg_id: int,
                            slug: str, ticket_id: str, qty: int,
                            primary_block: str, backup_blocks: list[str],
                            notifier: Notifier) -> None:
    await notifier.edit(
        chat_id, msg_id,
        "⚡ <b>جارٍ الحجز...</b>\n\n🔄 التحضير...",
        reply_markup=None,
    )

    data = await get_event_tickets(slug)
    detail = await get_event_detail(slug)
    ticket = next(
        (t for t in (data.get("tickets") or []) if t["id"] == ticket_id),
        None,
    )
    if not ticket:
        await notifier.edit(chat_id, msg_id, "⚠️ نوع التذكرة غير موجود.",
                            reply_markup=kb.back_to_menu())
        return

    accounts = [a for a in list_accounts(status="ready")
                if a.get("access_token")]
    try:
        plan, meta = distribute(qty, accounts=accounts,
                                max_per_order=ticket["max_per_order"],
                                min_per_order=ticket["min_per_order"])
    except ValueError as e:
        await notifier.edit(chat_id, msg_id,
                            f"⚠️ تعذّر التوزيع: <code>{e}</code>",
                            reply_markup=kb.back_to_menu())
        return

    progress_lines: list[str] = []

    async def _progress(line: str):
        progress_lines.append(line)
        tail = "\n".join(progress_lines[-12:])
        try:
            await notifier.edit(chat_id, msg_id,
                                f"⚡ <b>جارٍ الحجز...</b>\n\n{tail}")
        except Exception:
            pass

    title = (detail or {}).get("title") or slug
    pay_method = get_bot_setting("DEFAULT_PAYMENT_METHOD",
                                  default_payment_method())

    results = await book_all(
        plan,
        event_slug=slug,
        event_title=title,
        ticket_id=ticket_id,
        ticket_title=ticket["title"],
        ticket_price=ticket.get("display_price") or 0,
        currency=ticket["currency"],
        chat_id=chat_id, notifier=notifier,
        progress=_progress,
        ticket_meta=ticket,
        primary_block=primary_block,
        backup_blocks=backup_blocks,
        payment_method=pay_method,
    )

    succ = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    watching = [r for r in fail if r.get("drop_watcher_active")]

    # Per-account summary message + a private DM to each successful account
    summary_lines = [
        "🎉 <b>انتهى الحجز</b>",
        f"🎭 {title}",
        f"🎫 {ticket['title']}",
        "",
        f"✅ نجاح: <b>{len(succ)}</b>   "
        f"❌ فشل: <b>{len(fail) - len(watching)}</b>   "
        f"👁️ ترقّب: <b>{len(watching)}</b>",
        "",
    ]

    for r in succ:
        seat_objects = r.get("seat_objects") or []
        if seat_objects:
            seats_summary = summarize_for_telegram(seat_objects)
        else:
            seats = (r.get("seat_info") or {}).get("seats") or []
            seats_summary = summarize_for_telegram(seats) if seats else "—"

        block_used = r.get("block_used") or (r.get("seat_info") or {}).get("block", "")
        block_line = f"\n📦 البلوك المستخدم: <code>{block_used}</code>" if block_used else ""

        summary_lines.append(
            f"✅ <code>{r['label']}</code> — {r['quantity']} تذكرة"
            f"{block_line}\n{seats_summary}\n"
            f"💳 <a href=\"{r['payment_url']}\">رابط الدفع</a>"
        )

    for r in fail:
        if r.get("drop_watcher_active"):
            continue  # already counted in 'watching'
        lbl = r.get('label') or r.get('account_id')
        err_msg = (r.get('error') or '')[:200]
        summary_lines.append(f"❌ <code>{lbl}</code>: {err_msg}")

    for r in watching:
        lbl = r.get('label') or r.get('account_id')
        summary_lines.append(
            f"👁️ <code>{lbl}</code>: في وضع الترقّب — "
            f"سيُحجز فور سقوط مقعد."
        )

    if succ:
        summary_lines.append("\n⏱️ <i>صلاحية روابط الدفع محدودة — سارع!</i>")

    keyboard_rows = []
    for r in succ:
        if r.get("payment_url"):
            keyboard_rows.append([
                {"text": f"💳 دفع {r['label']}", "url": r["payment_url"]}
            ])
    keyboard_rows.append([
        {"text": "🌐 فتح صفحة الحجز يدوياً",
         "url": f"https://webook.com/ar/events/{slug}/book"}
    ])
    keyboard_rows.append([{"text": "⬅️ القائمة", "callback_data": "menu"}])

    await notifier.edit(chat_id, msg_id, "\n".join(summary_lines),
                        reply_markup={"inline_keyboard": keyboard_rows})


async def _show_account(chat_id: str, msg_id: int, acc_id: str,
                        notifier: Notifier) -> None:
    acc = get_account(acc_id)
    if not acc:
        await notifier.edit(chat_id, msg_id, "الحساب غير موجود.",
                            reply_markup=kb.back_to_menu())
        return
    status_map = {
        "ready": "✅ جاهز",
        "refreshing": "🔄 جارٍ التحديث",
        "new": "🆕 جديد — يحتاج تسجيل دخول",
        "needs_relogin": "⚠️ يحتاج إعادة تسجيل دخول",
        "blocked": "🚫 محظور",
    }
    status = status_map.get(acc.get("status", "new"), acc.get("status", "new"))
    last = acc.get("last_used_at") or 0
    last_str = ("منذ " + _ago(last)) if last else "لم يُستخدم بعد"
    exp = acc.get("token_expires_at") or 0
    exp_str = ("ينتهي بعد " + _until(exp)) if exp > time.time() else "منتهٍ"
    err = acc.get("last_error")

    txt = (
        f"👤 <b>{acc.get('label')}</b>\n"
        f"📧 {acc.get('email')}\n"
        f"📊 الحالة: <b>{status}</b>\n"
        f"🔑 التوكن: {exp_str}\n"
        f"🕐 آخر استخدام: {last_str}\n"
        f"🎫 تذاكر محجوزة: <b>{acc.get('tickets_booked', 0)}</b>"
    )
    if err:
        txt += f"\n\n⚠️ <i>آخر خطأ:</i> <code>{err[:150]}</code>"
    await notifier.edit(
        chat_id, msg_id, txt,
        reply_markup=kb.account_actions(acc_id, acc.get("status", "new")))


async def _login_flow(chat_id: str, msg_id: int, acc_id: str,
                      notifier: Notifier) -> None:
    acc = get_account(acc_id)
    if not acc:
        await notifier.edit(chat_id, msg_id, "الحساب غير موجود.",
                            reply_markup=kb.back_to_menu())
        return
    await notifier.edit(
        chat_id, msg_id,
        f"🔐 <b>تسجيل الدخول</b>\n\n"
        f"👤 {acc['label']}\n"
        f"📧 {acc['email']}\n\n"
        f"⏳ جارٍ الاتصال بـ webook.com...\n"
        f"🤖 <i>يُحلّ reCAPTCHA تلقائياً.</i>",
        reply_markup=None,
    )
    res = await auth_service.login_account(acc_id, notifier)
    if res.get("ok"):
        user = res.get("user", {})
        exp_days = int((res["tokens"]["expires_at"] - time.time()) / 86400)
        await notifier.send(
            chat_id,
            f"✅ <b>تم الدخول بنجاح</b>\n\n"
            f"👤 <b>{user.get('name') or acc['label']}</b>\n"
            f"📧 {user.get('email', acc['email'])}\n"
            f"🔑 التوكن صالح لمدة: <b>{exp_days} يوم</b>\n\n"
            f"🎉 الحساب جاهز للحجز.",
            reply_markup=kb.accounts_keyboard(list_accounts()),
        )
    else:
        await notifier.send(
            chat_id,
            f"❌ <b>فشل تسجيل الدخول</b>\n\n"
            f"السبب: <code>{(res.get('error') or '')[:200]}</code>",
            reply_markup=kb.accounts_keyboard(list_accounts()),
        )


async def _show_bookings(chat_id: str, notifier: Notifier,
                         edit_msg_id: int | None = None) -> None:
    bks = list_bookings(chat_id=chat_id, limit=10)
    if not bks:
        txt = "📋 لا توجد حجوزات بعد."
    else:
        lines = ["📋 <b>حجوزاتك الأخيرة</b>\n"]
        for b in bks:
            seat = b.get("seat_info") or {}
            seats = seat.get("seats") or []
            block = seat.get("block") or ""
            extra = ""
            if seats:
                summary = summarize_for_telegram(seats)
                extra = f"\n  {summary}"
            elif block:
                extra = f"\n  📦 {block}"
            title = (b.get("event_title") or "—")[:40]
            lines.append(
                f"• <b>{title}</b>\n"
                f"  {b.get('ticket_type', '')} × {b.get('quantity')}{extra}\n"
                f"  💳 <a href=\"{b.get('payment_url', '')}\">رابط الدفع</a>"
            )
        txt = "\n".join(lines)
    rkb = kb.back_to_menu()
    if edit_msg_id:
        await notifier.edit(chat_id, edit_msg_id, txt, reply_markup=rkb)
    else:
        await notifier.send(chat_id, txt, reply_markup=rkb)


# ════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════
def _ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 60: return f"{d} ث"
    if d < 3600: return f"{d // 60} د"
    if d < 86400: return f"{d // 3600} س"
    return f"{d // 86400} ي"


def _until(ts: float) -> str:
    d = max(0, int(ts - time.time()))
    if d < 60: return f"{d} ث"
    if d < 3600: return f"{d // 60} د"
    if d < 86400: return f"{d // 3600} س"
    return f"{d // 86400} ي"


# ════════════════════════════════════════════════════════════════════════
# Long-poll fallback
# ════════════════════════════════════════════════════════════════════════
async def long_poll_loop(notifier: Notifier) -> None:
    while not notifier.token:
        log.info("🤖 waiting for TELEGRAM_BOT_TOKEN (set via /admin)…")
        await asyncio.sleep(15)
    try:
        await notifier.delete_webhook()
    except Exception:
        pass
    offset = None
    log.info("🤖 long-polling started")
    while True:
        if not notifier.token:
            await asyncio.sleep(15)
            continue
        try:
            data = await notifier.get_updates(offset=offset, timeout=25)
            if data and data.get("ok"):
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    asyncio.create_task(dispatch(upd, notifier))
        except Exception as e:
            log.warning(f"long-poll err: {e}")
            await asyncio.sleep(3)
