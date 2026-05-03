"""Inline keyboard builders. All callback_data strings are ≤ 64 bytes.

Long identifiers (slug, ObjectId) are stored via app.bot.tokens so the
callback_data carries only an 8-char opaque token.
"""
from __future__ import annotations

from typing import Any

from app.bot import tokens as tok


def main_menu() -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "🎫 الفعاليات الجارية", "callback_data": "events:0"}],
        [{"text": "🔗 إرسال رابط فعالية", "callback_data": "link:prompt"}],
        [{"text": "👥 إدارة الحسابات", "callback_data": "accounts:list"}],
        [{"text": "📋 حجوزاتي", "callback_data": "bookings:list"}],
        [{"text": "⚙️ الإعدادات", "callback_data": "settings:menu"}],
        [{"text": "ℹ️ تعليمات", "callback_data": "help:show"}],
    ]}


def events_keyboard(events: list[dict], page: int = 0,
                    page_size: int = 8) -> dict[str, Any]:
    start = page * page_size
    chunk = events[start:start + page_size]
    rows = []
    for e in chunk:
        t = tok.put({"slug": e["slug"]})
        rows.append([{"text": f"• {_truncate(e['title'] or e['slug'], 50)}",
                      "callback_data": f"evt:{t}"}])
    nav = []
    if page > 0:
        nav.append({"text": "◀️ السابق",
                    "callback_data": f"events:{page-1}"})
    if start + page_size < len(events):
        nav.append({"text": "التالي ▶️",
                    "callback_data": f"events:{page+1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "🔄 تحديث", "callback_data": "events:refresh"}])
    rows.append([{"text": "⬅️ القائمة الرئيسية", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def ticket_types_keyboard(event_slug: str,
                          tickets: list[dict]) -> dict[str, Any]:
    rows = []
    any_active = False
    for t in tickets:
        if t.get("status") != "active":
            continue
        any_active = True
        status = t.get("sale_status") or ""
        badge = ""
        if status == "ongoing":
            badge = " ✅"
        elif status == "not_yet":
            badge = " ⏳"
        elif status == "ended":
            badge = " ⛔"
        price = t.get("display_price") or 0
        ccy = _ccy(t.get("currency") or "SAR")
        price_lbl = f"{_fmt_price(price)} {ccy}" if price else "—"
        callback_tok = tok.put({"slug": event_slug, "ticket_id": t["id"]})
        label = f"{_truncate(t['title'], 30)} — {price_lbl}{badge}"
        rows.append([{"text": label, "callback_data": f"tck:{callback_tok}"}])

    if not any_active:
        rows.append([{"text": "⚠️ لا توجد تذاكر متاحة",
                      "callback_data": "menu"}])

    rows.append([{"text": "⬅️ رجوع للفعاليات",
                  "callback_data": "events:0"}])
    return {"inline_keyboard": rows}


def blocks_picker_keyboard(blocks: list[dict], session_token: str,
                            primary: str = "",
                            backups: list[str] | None = None,
                            mode: str = "primary",
                            webapp_url: str = "") -> dict[str, Any]:
    """Block picker for seats.io.

    blocks: [{"name": "S1", "free": 12, "total": 50}, ...]
            (free/total may be -1 when unknown via API)
    mode:   'primary'  → tap one block to set as PRIMARY
            'backup'   → tap blocks to add/remove as backup (toggle)
    webapp_url: optional Telegram WebApp URL for visual seat picking.
    """
    backups = backups or []
    rows = []
    for b in blocks[:30]:  # cap to keep callback area sane
        name = b.get("name", "")
        free = b.get("free", 0)
        total = b.get("total", 0)
        if free < 0 or total < 0:
            full = "⚪"  # unknown availability
            counts = ""
        else:
            full = "🔴" if free == 0 else ("🟢" if free > 5 else "🟡")
            counts = f" ({free}/{total})"
        marker = ""
        if name == primary:
            marker = " ⭐"
        elif name in backups:
            marker = f" #{backups.index(name) + 1}"
        label = f"{full} {name}{counts}{marker}"
        rows.append([{"text": label,
                      "callback_data": f"blk:{mode}:{session_token}:{_safe_block(name)}"}])
    rows.append([
        {"text": "⭐ وضع الرئيسي" if mode != "primary" else "✓ وضع الرئيسي",
         "callback_data": f"blk:setmode:{session_token}:primary"},
        {"text": "🔁 وضع الاحتياطي" if mode != "backup" else "✓ وضع الاحتياطي",
         "callback_data": f"blk:setmode:{session_token}:backup"},
    ])
    if webapp_url:
        rows.append([{
            "text": "🌐 الواجهة المرئية (Mini App)",
            "web_app": {"url": webapp_url},
        }])
    rows.append([{"text": "✅ تأكيد البلوكات والمتابعة",
                  "callback_data": f"blk:done:{session_token}"}])
    rows.append([{"text": "⬅️ رجوع", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def confirm_plan_keyboard(context_token: str) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "✅ تأكيد وبدء الحجز",
          "callback_data": f"go:{context_token}"}],
        [{"text": "❌ إلغاء", "callback_data": "menu"}],
    ]}


def accounts_keyboard(accounts: list[dict]) -> dict[str, Any]:
    rows = []
    for a in accounts:
        icon = {
            "ready": "✅", "refreshing": "🔄", "new": "🆕",
            "needs_relogin": "⚠️", "blocked": "🚫",
        }.get(a.get("status", ""), "❓")
        email = a.get("email", "—")
        label = a.get("label") or email.split("@")[0]
        rows.append([{"text": f"{icon} {label} — {_truncate(email, 25)}",
                      "callback_data": f"acc:{a['id']}"}])
    rows.append([{"text": "➕ إضافة حساب جديد", "callback_data": "acc:add"}])
    rows.append([{"text": "⬅️ رجوع", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def account_actions(account_id: str, status: str) -> dict[str, Any]:
    rows = []
    if status in ("new", "needs_relogin", "blocked"):
        rows.append([{"text": "🔐 تسجيل الدخول الآن",
                      "callback_data": f"acc:login:{account_id}"}])
    else:
        rows.append([{"text": "🔄 إعادة تسجيل الدخول",
                      "callback_data": f"acc:login:{account_id}"}])
    rows.append([{"text": "🗑️ حذف الحساب",
                  "callback_data": f"acc:del:{account_id}"}])
    rows.append([{"text": "⬅️ رجوع", "callback_data": "accounts:list"}])
    return {"inline_keyboard": rows}


def settings_keyboard(current_payment: str = "credit_card") -> dict[str, Any]:
    cc_mark = " ✓" if current_payment == "credit_card" else ""
    ap_mark = " ✓" if current_payment == "apple_pay" else ""
    return {"inline_keyboard": [
        [{"text": f"💳 بطاقة ائتمانية{cc_mark}",
          "callback_data": "settings:pay:credit_card"}],
        [{"text": f"🍎 Apple Pay{ap_mark}",
          "callback_data": "settings:pay:apple_pay"}],
        [{"text": "⬅️ رجوع", "callback_data": "menu"}],
    ]}


def back_to_menu() -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "⬅️ القائمة الرئيسية", "callback_data": "menu"}]
    ]}


def back_to_event(event_token: str) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "⬅️ رجوع", "callback_data": f"evt:{event_token}"}],
        [{"text": "🏠 القائمة", "callback_data": "menu"}],
    ]}


# ── helpers ─────────────────────────────────────────────────────────
def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _ccy(code: str) -> str:
    return {
        "SAR": "ر.س", "AED": "د.إ", "USD": "$", "EUR": "€",
        "KWD": "د.ك", "QAR": "ر.ق",
    }.get((code or "").upper(), code or "")


def _fmt_price(p: float) -> str:
    p = float(p or 0)
    if p == int(p):
        return str(int(p))
    return f"{p:.2f}"


def _safe_block(name: str) -> str:
    """Sanitize block name for callback_data (avoid colons/spaces)."""
    return (name or "").replace(":", "_").replace(" ", "_")[:20]
