"""
Entry point — FastAPI web server + Telegram bot + background monitors.

Runs on Render's free tier comfortably because:
  • single async process
  • Playwright is only used for login/captcha (then closed)
  • all other work is HTTP via aiohttp (low RAM)
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.bot.handlers import dispatch, long_poll_loop
from app.bot.notifier import Notifier
from app.core.config import (
    HOST, KEEP_ALIVE_ENABLED, LOG_LEVEL, PORT, PUBLIC_URL,
    telegram_bot_token,
)
from app.web.admin import router as admin_router, maybe_rebind_webhook
from app.core.logging_setup import setup_logging
from app.core.db import backend as db_backend, is_persistent as db_is_persistent
from app.core.storage import (
    list_accounts, list_bookings, list_recent_events,
)
from app.services.event_monitor import fetch_loop
from app.services.keep_alive import keep_alive_loop
from app.services.seatsio_runtime import stop_all as stop_seat_warmers

setup_logging()
log = logging.getLogger("main")


background_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 Webook Bot v4 starting…")

    # Defer ALL network-dependent startup to a background task so the
    # FastAPI HTTP port opens immediately. This avoids "No open ports
    # detected" on Render when Telegram or DB slow things down.
    async def _deferred_startup():
        await asyncio.sleep(2)
        notifier = Notifier()
        # Telegram webhook (best-effort)
        try:
            bot_tok = telegram_bot_token()
            if bot_tok and PUBLIC_URL:
                hook_url = f"{PUBLIC_URL.rstrip('/')}/telegram/webhook"
                ok = await notifier.set_webhook(hook_url)
                if ok:
                    log.info(f"✅ webhook set → {hook_url}")
                    background_tasks.append(asyncio.create_task(
                        fetch_loop(notifier), name="evt-fetch"))
                    # Drop watcher loop (event-driven, replaces sniper_loop)
                    try:
                        from app.services.drop_watcher import drop_watcher_loop
                        background_tasks.append(asyncio.create_task(
                            drop_watcher_loop(notifier), name="drop-watcher"))
                    except Exception as e:
                        log.warning(f"drop watcher unavailable: {e}")
                    if KEEP_ALIVE_ENABLED:
                        background_tasks.append(asyncio.create_task(
                            keep_alive_loop(), name="keep-alive"))
                    return
        except Exception as e:
            log.error(f"webhook set failed: {e}")

        # Fallback: long-poll
        background_tasks.append(asyncio.create_task(
            long_poll_loop(notifier), name="tg-poll"))
        background_tasks.append(asyncio.create_task(
            fetch_loop(notifier), name="evt-fetch"))
        try:
            from app.services.drop_watcher import drop_watcher_loop
            background_tasks.append(asyncio.create_task(
                drop_watcher_loop(notifier), name="drop-watcher"))
        except Exception as e:
            log.warning(f"drop watcher unavailable: {e}")
        if KEEP_ALIVE_ENABLED:
            background_tasks.append(asyncio.create_task(
                keep_alive_loop(), name="keep-alive"))

    async def _rebind_loop():
        while True:
            await asyncio.sleep(60)
            try:
                await maybe_rebind_webhook(PUBLIC_URL)
            except Exception:
                pass

    background_tasks.append(asyncio.create_task(_deferred_startup(),
                                                 name="deferred-startup"))
    background_tasks.append(asyncio.create_task(_rebind_loop(),
                                                 name="tg-rebind"))

    log.info("✅ startup complete (background tasks scheduled)")
    yield

    log.info("🛑 shutting down…")
    for t in background_tasks:
        t.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    try:
        await stop_seat_warmers()
    except Exception:
        pass


app = FastAPI(
    title="Webook Bot",
    version="4.0.0",
    description="Interactive Telegram bot for automated ticket booking with Hydra Seat Engine.",
    lifespan=lifespan,
)
app.include_router(admin_router)


# ── HTML dashboard ───────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
@app.head("/")
async def dashboard() -> HTMLResponse:
    accs = list_accounts()
    evs = list_recent_events(limit=5)
    bks = list_bookings(limit=5)
    ready = len([a for a in accs if a.get("status") == "ready"])
    return HTMLResponse(f"""
<!doctype html><html lang="ar" dir="rtl">
<head><meta charset="utf-8"><title>Webook Bot v4</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;font-family:-apple-system,'Segoe UI',Tahoma,sans-serif}}
body{{margin:0;background:linear-gradient(135deg,#0b1220,#1a2438);color:#e2e8f0;
     min-height:100vh}}
.wrap{{max-width:900px;margin:0 auto;padding:24px}}
.card{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
       border-radius:14px;padding:22px;margin-bottom:18px}}
h1{{margin:0 0 8px;font-size:26px}}
.badge{{display:inline-block;padding:4px 12px;border-radius:999px;font-size:12px;
       background:#10b981;color:#0b1020;font-weight:700;vertical-align:middle}}
.muted{{color:#94a3b8;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
      gap:12px;margin-top:12px}}
.stat{{background:rgba(15,23,42,.6);border-radius:10px;padding:14px;
       text-align:center}}
.stat b{{display:block;font-size:28px;color:#38bdf8;margin-bottom:4px}}
ul{{margin:8px 0;padding-right:18px}}
li{{margin:6px 0}}
code{{background:rgba(255,255,255,.08);padding:2px 7px;border-radius:5px;
      font-size:12px}}
a{{color:#60a5fa;text-decoration:none}}
a:hover{{text-decoration:underline}}
</style></head><body><div class="wrap">

<div class="card">
  <h1>🎯 Webook Bot <span class="badge">v4.0 Hydra</span></h1>
  <p class="muted">بوت حجز تذاكر تفاعلي عبر تيليجرام — محرك Hydra لخرائط seats.io</p>
  <div class="grid">
    <div class="stat"><b>{len(accs)}</b>حسابات</div>
    <div class="stat"><b>{ready}</b>جاهزة</div>
    <div class="stat"><b>{len(evs)}</b>فعاليات مُكتشفة</div>
    <div class="stat"><b>{len(bks)}</b>حجوزات</div>
  </div>
  <p style="margin-top:14px"><a href="/admin/" style="background:#8b5cf6;color:white;padding:8px 16px;border-radius:6px;text-decoration:none">⚙️ لوحة الإدارة</a></p>
</div>

<div class="card">
  <h3>💬 استخدم البوت مباشرةً عبر تيليجرام</h3>
  <p>أرسل رابط فعالية → يجلب البوت بيانات seats.io فوراً → تختار البلوك الرئيسي والاحتياطي → يحجز ويلخّص النتيجة بذكاء.</p>
</div>

<div class="card">
  <h3>🔗 Endpoints</h3>
  <ul>
    <li><code>GET /</code> — هذه الصفحة</li>
    <li><code>GET /health</code> — للفحص</li>
    <li><code>GET /ping</code> — لمنع النوم</li>
    <li><code>GET /stats</code> — إحصائيات JSON</li>
    <li><code>POST /telegram/webhook</code> — تحديثات تيليجرام</li>
  </ul>
</div>

<div class="card muted">
  Public URL: <code>{PUBLIC_URL or '—'}</code> ·
  Keep-alive: <code>{'enabled' if KEEP_ALIVE_ENABLED else 'disabled'}</code><br>
  Storage: <code>{db_backend()}</code> ·
  Persistent: <code>{'yes' if db_is_persistent() else 'no (ephemeral — data lost on restart)'}</code>
</div>

</div></body></html>
""")


@app.get("/health")
@app.head("/health")
async def health():
    accs = list_accounts()
    return {
        "status": "ok",
        "version": "4.0.0",
        "accounts_total": len(accs),
        "accounts_ready": sum(1 for a in accs if a.get("status") == "ready"),
        "events_cached": len(list_recent_events(limit=999)),
        "storage": db_backend(),
        "persistent": db_is_persistent(),
    }


@app.get("/ping")
@app.head("/ping")
async def ping():
    return {"pong": True}


@app.get("/stats")
async def stats():
    accs = list_accounts()
    return {
        "accounts_total": len(accs),
        "accounts_ready": sum(1 for a in accs if a.get("status") == "ready"),
        "accounts_breakdown": {
            s: sum(1 for a in accs if a.get("status") == s)
            for s in ["ready", "new", "refreshing", "needs_relogin", "blocked"]
        },
        "events_cached": len(list_recent_events(limit=999)),
        "bookings_total": len(list_bookings(limit=9999)),
        "public_url": PUBLIC_URL,
    }


# ── Telegram webhook ─────────────────────────────────────────────────────
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"},
                            status_code=400)
    notifier = Notifier()
    asyncio.create_task(dispatch(update, notifier))
    return {"ok": True}


# ── Entrypoint ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"binding on {HOST}:{PORT}")
    uvicorn.run(
        "main:app", host=HOST, port=PORT,
        log_level=LOG_LEVEL.lower(), reload=False,
    )
