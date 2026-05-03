"""
Password-protected admin web UI.

Features:
  • /admin/           — settings dashboard (key/value store)
  • /admin/db        — read-only database explorer
  • /admin/api/*     — JSON endpoints (authenticated)

Session: HMAC-signed cookie (7 days).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Optional

from fastapi import APIRouter, Cookie, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core import db as db_core
from app.core import settings as cfg_settings
from app.core.config import admin_password

log = logging.getLogger("admin")
router = APIRouter(prefix="/admin", tags=["admin"])

_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET") or secrets.token_hex(32)
_COOKIE = "wbk_admin"
_TTL = 7 * 24 * 3600

SENSITIVE = {
    "TELEGRAM_BOT_TOKEN", "WEBOOK_PUBLIC_TOKEN",
    "ADMIN_PASSWORD", "DATABASE_URL", "TURSO_AUTH_TOKEN",
    "ADMIN_SESSION_SECRET", "password", "access_token",
    "refresh_token",
}

KNOWN_SETTINGS = [
    ("TELEGRAM_BOT_TOKEN", "توكن بوت تيليجرام (من BotFather) — شكل: 1234:abc..."),
    ("TELEGRAM_CHAT_ID", "معرف تيليجرامك (من @userinfobot)"),
    ("AUTHORIZED_CHAT_IDS", "معرفات إضافية مسموح لها (فواصل)"),
    ("WEBOOK_PUBLIC_TOKEN", "توكن webook العام (محدد افتراضياً)"),
    ("ADMIN_PASSWORD", "كلمة مرور لوحة الإدارة"),
]


# ════════════════════════════════════════════════════════════════════════
# Session helpers
# ════════════════════════════════════════════════════════════════════════
def _sign(payload: str) -> str:
    mac = hmac.new(_SESSION_SECRET.encode(), payload.encode(),
                   hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def _verify(cookie_value: str) -> bool:
    try:
        body, mac = cookie_value.rsplit(".", 1)
        expected = hmac.new(_SESSION_SECRET.encode(), body.encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return False
        exp_str, _ = body.split(":", 1)
        return time.time() < float(exp_str)
    except Exception:
        return False


def _issue() -> str:
    return _sign(f"{time.time() + _TTL}:admin")


def _is_authed(cv: Optional[str]) -> bool:
    return bool(cv and _verify(cv))


def _guard(cv: Optional[str]):
    if not _is_authed(cv):
        raise HTTPException(303, headers={"Location": "/admin/"})


# ════════════════════════════════════════════════════════════════════════
# Shared CSS + topbar
# ════════════════════════════════════════════════════════════════════════
_CSS = """
*{box-sizing:border-box;font-family:-apple-system,'Segoe UI',Tahoma,sans-serif}
body{margin:0;background:linear-gradient(135deg,#0b1220,#1a2438);color:#e2e8f0;min-height:100vh}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:12px}
.topbar h1{margin:0;font-size:22px}
nav.tabs{display:flex;gap:4px;margin-bottom:16px;flex-wrap:wrap}
nav.tabs a{padding:8px 16px;border-radius:8px;background:rgba(255,255,255,.04);
          color:#cbd5e1;text-decoration:none;font-size:14px;border:1px solid rgba(255,255,255,.06)}
nav.tabs a:hover{background:rgba(255,255,255,.08)}
nav.tabs a.active{background:#38bdf8;color:#0b1020;font-weight:700;border-color:#38bdf8}
.card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
      border-radius:14px;padding:22px;margin-bottom:18px}
table{width:100%;border-collapse:collapse}
td,th{padding:10px 8px;border-top:1px solid rgba(255,255,255,.06);vertical-align:top;
      font-size:13px;text-align:right}
th{background:rgba(15,23,42,.6);text-align:right;font-size:12px;color:#94a3b8;
   text-transform:uppercase;letter-spacing:.5px}
td:first-child,th:first-child{width:auto}
.muted{color:#94a3b8;font-size:12px}
.val{background:rgba(15,23,42,.7);padding:6px 10px;border-radius:6px;font-size:12px;
     word-break:break-all;display:inline-block;max-width:100%;font-family:monospace}
.edit-form{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.edit-form input[type=text],.edit-form input[type=password]{flex:1;min-width:180px;padding:8px 10px;
     border-radius:6px;border:1px solid rgba(255,255,255,.12);
     background:rgba(15,23,42,.6);color:#e2e8f0;font-size:13px;font-family:monospace}
.edit-form button{padding:8px 14px;border-radius:6px;border:0;background:#10b981;
     color:#0b1020;font-weight:700;cursor:pointer;font-size:13px}
.edit-form button:hover{background:#059669}
.del,.secondary{color:#fca5a5;text-decoration:none;font-size:12px;padding:6px 10px;
      border-radius:6px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.2)}
.del:hover{background:rgba(239,68,68,.2)}
.logout{background:#ef4444;color:white;padding:8px 14px;border-radius:6px;text-decoration:none;font-size:13px}
.flash{background:rgba(16,185,129,.15);border:1px solid rgba(16,185,129,.3);
       color:#86efac;padding:10px 14px;border-radius:8px;font-size:13px;margin-bottom:14px}
.flash.err{background:rgba(239,68,68,.15);border-color:rgba(239,68,68,.3);color:#fca5a5}
a{color:#60a5fa}
.add-form{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.add-form input{flex:1;min-width:150px;padding:10px 12px;border-radius:6px;
      border:1px solid rgba(255,255,255,.12);background:rgba(15,23,42,.6);color:#e2e8f0;font-size:13px;font-family:monospace}
.add-form button{padding:10px 20px;border-radius:6px;border:0;background:#8b5cf6;
      color:white;font-weight:700;cursor:pointer}
.eye{cursor:pointer;background:transparent;border:0;color:#94a3b8;font-size:16px;padding:4px 6px;border-radius:4px}
.eye:hover{color:#e2e8f0;background:rgba(255,255,255,.05)}
.value-cell{display:flex;align-items:center;gap:6px}
.value-text{flex:1;font-family:monospace;font-size:12px;word-break:break-all}
.chip{display:inline-block;padding:3px 8px;border-radius:99px;font-size:11px;
      background:rgba(56,189,248,.15);color:#38bdf8;margin-inline-start:6px}
.chip.warn{background:rgba(245,158,11,.15);color:#fbbf24}
.chip.ok{background:rgba(16,185,129,.15);color:#86efac}
.tbl-scroll{overflow-x:auto}
.tbl-scroll table{min-width:720px}
.action-btn{display:inline-block;padding:6px 10px;border-radius:6px;background:#3b82f6;
       color:white;text-decoration:none;font-size:12px;border:0;cursor:pointer;margin-inline-start:4px}
.action-btn.danger{background:#ef4444}
.action-btn:hover{opacity:.9}
"""


def _page(title: str, body: str, current_tab: str = "settings", flash: str = "") -> HTMLResponse:
    tabs_html = ''.join([
        f'<a href="/admin/{href}" class="{"active" if current_tab==name else ""}">{label}</a>'
        for name, href, label in [
            ("settings", "", "⚙️ الإعدادات"),
            ("db", "db", "🗄️ قاعدة البيانات"),
            ("info", "info", "ℹ️ معلومات النظام"),
        ]
    ])
    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ''
    return HTMLResponse(f"""
<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_CSS}</style>
</head><body><div class="wrap">
<div class="topbar">
  <h1>⚙️ لوحة إدارة Webook Bot</h1>
  <form method="POST" action="/admin/logout" style="margin:0">
    <button class="logout" type="submit">خروج</button>
  </form>
</div>
<nav class="tabs">{tabs_html}</nav>
{flash_html}
{body}
<p style="text-align:center;margin-top:30px"><a href="/">← العودة للصفحة الرئيسية</a></p>
</div>
<script>
function togglePwd(btn, cellId) {{
  const cell = document.getElementById(cellId);
  if (!cell) return;
  const real = cell.getAttribute('data-real');
  if (cell.textContent.startsWith('••')) {{
    cell.textContent = real;
    btn.textContent = '🙈';
  }} else {{
    cell.textContent = '••••••••••';
    btn.textContent = '👁️';
  }}
}}
function copyVal(btn, cellId) {{
  const cell = document.getElementById(cellId);
  if (!cell) return;
  const text = cell.getAttribute('data-real') || cell.textContent;
  navigator.clipboard.writeText(text).then(() => {{
    const orig = btn.textContent;
    btn.textContent = '✓';
    setTimeout(() => btn.textContent = orig, 1200);
  }});
}}
</script>
</body></html>
""")


# ════════════════════════════════════════════════════════════════════════
# Login
# ════════════════════════════════════════════════════════════════════════
def _login_page(err: str = "") -> HTMLResponse:
    return HTMLResponse(f"""
<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>Admin Login — Webook Bot</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_CSS}
body{{display:flex;align-items:center;justify-content:center}}
.card{{max-width:400px;width:90%}}
input[type=password]{{width:100%;padding:12px 14px;border-radius:8px;
   border:1px solid rgba(255,255,255,.12);background:rgba(15,23,42,.6);color:#e2e8f0;font-size:15px}}
button.primary{{margin-top:20px;width:100%;padding:12px;border-radius:8px;border:0;
   background:#38bdf8;color:#0b1020;font-weight:700;font-size:15px;cursor:pointer}}
button.primary:hover{{background:#0ea5e9}}
</style></head><body><div class="card">
<h1 style="margin:0 0 20px">🔐 Webook Bot — Admin</h1>
{f'<div class="flash err">{html.escape(err)}</div>' if err else ''}
<form method="POST" action="/admin/login">
  <label style="display:block;font-size:13px;margin-bottom:8px;color:#94a3b8">كلمة المرور</label>
  <input type="password" name="password" autofocus required>
  <button type="submit" class="primary">دخول</button>
</form>
<p class="muted" style="margin-top:16px;text-align:center">الافتراضي: <code>webook-admin</code></p>
</div></body></html>
""")


@router.get("/", response_class=HTMLResponse)
async def admin_home(wbk_admin: Optional[str] = Cookie(default=None),
                      flash: Optional[str] = None):
    if not _is_authed(wbk_admin):
        return _login_page()
    return _settings_page(flash or "")


@router.post("/login")
async def admin_login(password: str = Form(...)):
    if password != admin_password():
        return _login_page("كلمة المرور غير صحيحة")
    resp = RedirectResponse(url="/admin/", status_code=303)
    resp.set_cookie(_COOKIE, _issue(), max_age=_TTL, httponly=True,
                    samesite="lax", secure=True)
    return resp


@router.post("/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin/", status_code=303)
    resp.delete_cookie(_COOKIE)
    return resp


# ════════════════════════════════════════════════════════════════════════
# Settings page
# ════════════════════════════════════════════════════════════════════════
def _render_value_cell(key: str, value: str, row_id: str) -> str:
    """Render a cell with eye+copy icons for sensitive keys."""
    if not value:
        return '<span class="muted"><em>غير مُعيّن</em></span>'
    is_sensitive = key in SENSITIVE
    display = "••••••••••" if is_sensitive else html.escape(value)
    real_esc = html.escape(value, quote=True).replace("'", "&#39;")
    icons = ""
    if is_sensitive:
        icons = f"""
<button class="eye" type="button" onclick="togglePwd(this, '{row_id}')">👁️</button>
<button class="eye" type="button" onclick="copyVal(this, '{row_id}')" title="نسخ">📋</button>
"""
    else:
        icons = f'<button class="eye" type="button" onclick="copyVal(this, \'{row_id}\')" title="نسخ">📋</button>'
    return f"""
<div class="value-cell">
  <code class="val value-text" id="{row_id}" data-real="{real_esc}">{display}</code>
  {icons}
</div>
"""


def _settings_page(flash: str = "") -> HTMLResponse:
    settings_data = cfg_settings.list_all()
    env_overrides = [k for k, _ in KNOWN_SETTINGS if os.environ.get(k)]

    shown_keys = set()
    rows_html = []
    for i, (key, desc) in enumerate(KNOWN_SETTINGS):
        shown_keys.add(key)
        val = settings_data.get(key, "")
        env_over = key in env_overrides
        env_badge = '<span class="chip warn">يستخدم env</span>' if env_over else ''
        cell = _render_value_cell(key, val, f"val_{i}")
        rows_html.append(f"""
<tr>
  <td>
    <strong>{html.escape(key)}</strong>{env_badge}
    <br><span class="muted">{html.escape(desc)}</span>
  </td>
  <td>{cell}</td>
  <td>
    <form method="POST" action="/admin/settings" class="edit-form">
      <input type="hidden" name="key" value="{html.escape(key)}">
      <input type="{'password' if key in SENSITIVE else 'text'}" name="value" placeholder="قيمة جديدة...">
      <button type="submit">حفظ</button>
    </form>
  </td>
</tr>
""")

    # Extra (custom) keys
    custom_rows = []
    for idx, (k, v) in enumerate(sorted(settings_data.items())):
        if k in shown_keys or k.startswith("_"):
            continue
        cell = _render_value_cell(k, v, f"cval_{idx}")
        custom_rows.append(f"""
<tr>
  <td><strong>{html.escape(k)}</strong></td>
  <td>{cell}</td>
  <td>
    <form method="POST" action="/admin/settings" class="edit-form">
      <input type="hidden" name="key" value="{html.escape(k)}">
      <input type="text" name="value" placeholder="قيمة جديدة...">
      <button type="submit">حفظ</button>
      <a href="/admin/settings/delete?key={html.escape(k)}" class="del" onclick="return confirm('حذف {html.escape(k)}؟')">حذف</a>
    </form>
  </td>
</tr>
""")

    custom_section = ""
    if custom_rows:
        custom_section = f"""
<div class="card">
  <h3>مفاتيح مخصصة</h3>
  <table>
    <thead><tr><th>المفتاح</th><th>القيمة</th><th>الإجراء</th></tr></thead>
    <tbody>{''.join(custom_rows)}</tbody>
  </table>
</div>
"""

    body = f"""
<div class="card">
  <h3 style="margin:0 0 8px">🔑 المتغيرات الرئيسية</h3>
  <p class="muted">اضغط 👁️ لعرض القيمة المخفية، 📋 لنسخها. التغيير يُطبَّق فوراً بدون إعادة تشغيل.
  المتغيرات المعلّمة <span class="chip warn">يستخدم env</span> تأتي من بيئة Render وتتجاوز أي تعديل هنا.</p>
  <div class="tbl-scroll"><table>
    <thead><tr><th>المفتاح</th><th>القيمة الحالية</th><th>تعديل</th></tr></thead>
    <tbody>{''.join(rows_html)}</tbody>
  </table></div>
</div>

{custom_section}

<div class="card">
  <h3>➕ إضافة مفتاح مخصص</h3>
  <form method="POST" action="/admin/settings" class="add-form">
    <input type="text" name="key" placeholder="KEY_NAME" required>
    <input type="text" name="value" placeholder="value" required>
    <button type="submit">إضافة</button>
  </form>
</div>

<div class="card">
  <h3>🚨 حالة البوت</h3>
  <p class="muted">بعد تعديل <code>TELEGRAM_BOT_TOKEN</code> أو <code>TELEGRAM_CHAT_ID</code>،
  سيعيد البوت ربط webhook خلال دقيقة تلقائياً. يمكنك تسريع ذلك بالضغط على الزر أدناه.</p>
  <form method="POST" action="/admin/rebind-webhook" style="margin-top:10px">
    <button class="action-btn" type="submit">🔄 إعادة ربط webhook تيليجرام الآن</button>
  </form>
</div>
"""
    return _page("إعدادات — Webook Bot", body, current_tab="settings", flash=flash)


@router.post("/settings")
async def admin_set(key: str = Form(...), value: str = Form(""),
                     wbk_admin: Optional[str] = Cookie(default=None)):
    if not _is_authed(wbk_admin):
        return RedirectResponse(url="/admin/", status_code=303)
    key = key.strip()
    if not key:
        return RedirectResponse(url="/admin/", status_code=303)
    try:
        # strip whitespace but keep inner spaces
        cfg_settings.set_value(key, value.strip())
        log.info(f"admin set {key} ({len(value)} chars)")
        # Verify it was actually saved
        saved = cfg_settings.get(key, None)
        if saved != value.strip():
            flash = f"⚠️ {key} — محفوظ لكن القيمة المقروءة مختلفة (قد يكون env يتجاوزها)"
        else:
            flash = f"✅ تم حفظ {key}"
    except Exception as e:
        log.exception(f"admin set failed: {e}")
        flash = f"❌ خطأ: {e}"
    return RedirectResponse(url=f"/admin/?flash={flash}", status_code=303)


@router.get("/settings/delete")
async def admin_delete(key: str,
                        wbk_admin: Optional[str] = Cookie(default=None)):
    if not _is_authed(wbk_admin):
        return RedirectResponse(url="/admin/", status_code=303)
    try:
        cfg_settings.delete(key)
        flash = f"🗑️ تم حذف {key}"
    except Exception as e:
        flash = f"❌ {e}"
    return RedirectResponse(url=f"/admin/?flash={flash}", status_code=303)


# ════════════════════════════════════════════════════════════════════════
# Database explorer
# ════════════════════════════════════════════════════════════════════════
@router.get("/db", response_class=HTMLResponse)
async def admin_db(wbk_admin: Optional[str] = Cookie(default=None),
                    table: Optional[str] = None,
                    limit: int = 100,
                    flash: Optional[str] = None):
    if not _is_authed(wbk_admin):
        return _login_page()

    tables = db_core.list_tables()
    selected = table if (table and table in tables) else (tables[0] if tables else "")
    cols, rows = (db_core.query_table(selected, limit=limit) if selected else ([], []))

    tables_nav = " · ".join([
        f'<a href="/admin/db?table={html.escape(t)}" '
        f'class="{"active" if t==selected else ""}" '
        f'style="padding:6px 12px;border-radius:6px;'
        f'{"background:#38bdf8;color:#0b1020;font-weight:700" if t==selected else "background:rgba(255,255,255,.06)"};'
        f'text-decoration:none;font-size:13px">{html.escape(t)}</a>'
        for t in tables
    ])

    # Render rows
    rows_html = []
    for ri, row in enumerate(rows):
        cells = []
        key_col = None
        key_val = None
        for ci, v in enumerate(row):
            col_name = cols[ci] if ci < len(cols) else f"c{ci}"
            if col_name in ("id", "slug", "key") and key_col is None:
                key_col = col_name
                key_val = v
            if v is None:
                disp = '<span class="muted">NULL</span>'
            else:
                s = str(v)
                truncated = len(s) > 120
                if truncated:
                    s = s[:120] + "…"
                # Mask sensitive columns
                if col_name in ("password", "access_token", "refresh_token"):
                    disp = f'<span class="muted">[{len(str(v))} chars]</span>'
                else:
                    disp = html.escape(s)
            cells.append(f'<td style="font-family:monospace;font-size:11px">{disp}</td>')

        # Action column (only tables we safely can delete from)
        action = ""
        if key_col is not None and selected in ("accounts", "events", "bookings", "event_blocks", "drop_watchers", "bot_settings", "settings"):
            action = f"""
<td style="white-space:nowrap">
  <form method="POST" action="/admin/db/delete" style="display:inline">
    <input type="hidden" name="table" value="{html.escape(selected)}">
    <input type="hidden" name="col" value="{html.escape(key_col)}">
    <input type="hidden" name="val" value="{html.escape(str(key_val))}">
    <button class="action-btn danger" type="submit"
     onclick="return confirm('حذف السجل {html.escape(str(key_val))[:30]}؟')">🗑️</button>
  </form>
</td>
"""
        else:
            action = "<td></td>"
        rows_html.append(f"<tr>{''.join(cells)}{action}</tr>")

    header_cells = ''.join(f'<th>{html.escape(c)}</th>' for c in cols) + '<th>إجراء</th>'

    body = f"""
<div class="card">
  <h3 style="margin:0 0 12px">🗄️ جداول قاعدة البيانات</h3>
  <p class="muted">التخزين الحالي: <code>{db_core.backend()}</code> ·
  عدد الجداول: <b>{len(tables)}</b></p>
  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:12px">{tables_nav}</div>
</div>

<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:12px">
    <h3 style="margin:0">📋 {html.escape(selected)} <span class="chip">{len(rows)} صف</span></h3>
    <form method="GET" action="/admin/db" style="display:flex;gap:6px">
      <input type="hidden" name="table" value="{html.escape(selected)}">
      <select name="limit" style="padding:6px;border-radius:6px;background:rgba(15,23,42,.6);color:#e2e8f0;border:1px solid rgba(255,255,255,.12)">
        {"".join(f'<option value="{n}" {"selected" if n==limit else ""}>{n}</option>' for n in [25,50,100,250,500])}
      </select>
      <button class="action-btn">تطبيق</button>
    </form>
  </div>
  <div class="tbl-scroll">
    <table>
      <thead><tr>{header_cells}</tr></thead>
      <tbody>{"".join(rows_html) if rows_html else f'<tr><td colspan="{len(cols)+1}" class="muted" style="text-align:center;padding:30px">لا توجد بيانات في هذا الجدول</td></tr>'}</tbody>
    </table>
  </div>
</div>
"""
    return _page(f"قاعدة البيانات — {selected}", body, current_tab="db", flash=flash or "")


@router.post("/db/delete")
async def admin_db_delete(table: str = Form(...), col: str = Form(...),
                           val: str = Form(...),
                           wbk_admin: Optional[str] = Cookie(default=None)):
    if not _is_authed(wbk_admin):
        return RedirectResponse(url="/admin/", status_code=303)
    # Try casting val to int if numeric column
    parsed: Any = val
    try:
        parsed = int(val)
    except Exception:
        pass
    ok = db_core.delete_row(table, col, parsed)
    flash = f"🗑️ تم حذف السجل" if ok else "❌ فشل الحذف"
    return RedirectResponse(url=f"/admin/db?table={table}&flash={flash}",
                             status_code=303)


# ════════════════════════════════════════════════════════════════════════
# System info
# ════════════════════════════════════════════════════════════════════════
@router.get("/info", response_class=HTMLResponse)
async def admin_info(wbk_admin: Optional[str] = Cookie(default=None)):
    if not _is_authed(wbk_admin):
        return _login_page()

    from app.core.config import telegram_bot_token, telegram_chat_id, PUBLIC_URL
    from app.core.db import backend, is_persistent

    tok = telegram_bot_token()
    tok_preview = (tok[:12] + "…" + tok[-4:]) if tok and len(tok) > 20 else (tok or "غير مضبوط")
    chat = telegram_chat_id()

    webhook_info = "—"
    try:
        from app.bot.notifier import Notifier
        if tok:
            n = Notifier(token=tok)
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(f"https://api.telegram.org/bot{tok}/getWebhookInfo",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
                    if data.get("ok"):
                        info = data.get("result", {})
                        url = info.get("url", "—")
                        pending = info.get("pending_update_count", 0)
                        last_err = info.get("last_error_message", "—")
                        webhook_info = f"""
<b>URL:</b> <code>{html.escape(url) or '— (غير مضبوط)'}</code><br>
<b>تحديثات معلّقة:</b> {pending}<br>
<b>آخر خطأ:</b> <code>{html.escape(str(last_err))[:120]}</code>
"""
    except Exception as e:
        webhook_info = f'<span class="muted">تعذّر الجلب: {html.escape(str(e))}</span>'

    # getMe info
    bot_info = "—"
    if tok:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(f"https://api.telegram.org/bot{tok}/getMe",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
                    if data.get("ok"):
                        u = data["result"]
                        bot_info = f'<b>@{html.escape(u.get("username","?"))}</b> — {html.escape(u.get("first_name",""))} (ID: {u.get("id")})'
                    else:
                        bot_info = f'<span style="color:#f87171">❌ {html.escape(str(data))[:100]}</span>'
        except Exception as e:
            bot_info = f'<span class="muted">تعذّر: {e}</span>'

    body = f"""
<div class="card">
  <h3 style="margin:0 0 12px">🤖 حالة البوت</h3>
  <table>
    <tr><td><b>Bot token</b></td><td><code>{html.escape(tok_preview)}</code></td></tr>
    <tr><td><b>Bot API check</b></td><td>{bot_info}</td></tr>
    <tr><td><b>Chat ID</b></td><td><code>{html.escape(chat or '—')}</code></td></tr>
    <tr><td><b>Webhook</b></td><td>{webhook_info}</td></tr>
  </table>
</div>

<div class="card">
  <h3 style="margin:0 0 12px">🖥️ النظام</h3>
  <table>
    <tr><td><b>Storage backend</b></td><td><code>{backend()}</code></td></tr>
    <tr><td><b>Persistent</b></td><td>{'✅ نعم' if is_persistent() else '⚠️ لا (مؤقت)'}</td></tr>
    <tr><td><b>Public URL</b></td><td><code>{html.escape(PUBLIC_URL or '—')}</code></td></tr>
  </table>
</div>

<div class="card">
  <h3>🔄 إجراءات سريعة</h3>
  <form method="POST" action="/admin/rebind-webhook" style="display:inline">
    <button class="action-btn" type="submit">🔁 إعادة ربط webhook</button>
  </form>
</div>
"""
    return _page("معلومات النظام", body, current_tab="info")


# ════════════════════════════════════════════════════════════════════════
# API endpoints
# ════════════════════════════════════════════════════════════════════════
@router.get("/api/settings")
async def admin_api_list(wbk_admin: Optional[str] = Cookie(default=None)):
    if not _is_authed(wbk_admin):
        raise HTTPException(401, "unauth")
    return JSONResponse(cfg_settings.list_all())


@router.post("/rebind-webhook")
async def admin_rebind(wbk_admin: Optional[str] = Cookie(default=None)):
    if not _is_authed(wbk_admin):
        return RedirectResponse(url="/admin/", status_code=303)
    try:
        # Force rebind by clearing fingerprint
        cfg_settings.delete("_LAST_WEBHOOK_TOKEN_FP")
        from app.core.config import PUBLIC_URL
        await maybe_rebind_webhook(PUBLIC_URL)
        flash = "✅ تم إعادة ربط webhook"
    except Exception as e:
        flash = f"❌ {e}"
    return RedirectResponse(url=f"/admin/info?flash={flash}", status_code=303)


# ════════════════════════════════════════════════════════════════════════
# Webhook rebind (runs as a startup background task)
# ════════════════════════════════════════════════════════════════════════
async def maybe_rebind_webhook(public_url: str) -> None:
    """If TELEGRAM_BOT_TOKEN changed, rebind the Telegram webhook."""
    try:
        from app.bot.notifier import Notifier
        from app.core.config import telegram_bot_token

        current = telegram_bot_token()
        if not current or not public_url:
            return
        last = cfg_settings.get("_LAST_WEBHOOK_TOKEN_FP", "")
        fp = current[:8] + "…" + current[-4:]
        if last == fp:
            return

        notifier = Notifier(token=current)
        hook_url = f"{public_url.rstrip('/')}/telegram/webhook"
        ok = await notifier.set_webhook(hook_url)
        if ok:
            cfg_settings.set_value("_LAST_WEBHOOK_TOKEN_FP", fp)
            log.info(f"🔁 webhook rebound for token {fp}")
        else:
            log.warning(f"webhook rebind failed for token {fp}")
    except Exception as e:
        log.debug(f"rebind err: {e}")
