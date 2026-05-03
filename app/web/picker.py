"""
Telegram WebApp Mini Picker — visual seats.io picker inside Telegram.

How it works:
  1. Bot creates a picker session (in-memory, see app.bot.handlers
     _PICKER_SESSIONS) and exposes a URL like
        https://<public-url>/picker/<session_token>
  2. The user taps the "🌐 الواجهة المرئية" button in the block-picker
     keyboard. Telegram opens the URL inside its WebApp container.
  3. The HTML page loads the official seats.io chart renderer
     (chart.seatcloud.com / cdn.seatsio.net) using the workspace_key +
     event_key we already extracted from webook.
  4. When the user picks blocks/seats, JS calls
        Telegram.WebApp.sendData(JSON.stringify({...}))
     which delivers the payload to the bot via a `web_app_data` Telegram
     update — handled in app/bot/handlers.py `_on_message`.
  5. Bot persists the selection on the picker session and proceeds with
     the normal quantity → confirm → book flow.

This bypasses Cloudflare Turnstile because the chart loads in the user's
own browser session (Telegram WebApp = real Chromium with full cookies).
"""
from __future__ import annotations

import html
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("picker")

router = APIRouter()


def _get_session(session_token: str) -> dict[str, Any] | None:
    """Read picker session from in-memory store. Imported lazily to avoid
    import cycles between web and bot layers."""
    try:
        from app.bot.handlers import _PICKER_SESSIONS
        return _PICKER_SESSIONS.get(session_token)
    except Exception:
        return None


def _set_session_selection(session_token: str, payload: dict) -> bool:
    """Apply a WebApp selection to the picker session."""
    try:
        from app.bot.handlers import _PICKER_SESSIONS
        sess = _PICKER_SESSIONS.get(session_token)
        if not sess:
            return False
        primary = (payload.get("primary") or "").strip()
        backups = [str(b).strip() for b in (payload.get("backups") or []) if str(b).strip()]
        seats   = [str(s) for s in (payload.get("seats") or []) if s]
        if primary:
            sess["primary"] = primary
        if backups:
            # de-duplicate, preserve order, exclude primary
            seen = set([primary]) if primary else set()
            ordered = []
            for b in backups:
                if b in seen:
                    continue
                seen.add(b)
                ordered.append(b)
            sess["backups"] = ordered
        if seats:
            sess["preselected_seats"] = seats
        sess["webapp_completed"] = True
        return True
    except Exception as e:
        log.warning(f"set_session_selection error: {e}")
        return False


@router.get("/picker/{session_token}", response_class=HTMLResponse)
async def picker_page(session_token: str) -> HTMLResponse:
    sess = _get_session(session_token)
    if not sess:
        return HTMLResponse(_error_page("انتهت جلسة الاختيار.\nأعد المحاولة من البوت."), status_code=410)

    workspace_key = sess.get("workspace_key") or ""
    event_key = sess.get("event_key") or ""
    chart_key = sess.get("chart_key") or ""
    provider = sess.get("seats_provider") or ""
    slug = sess.get("slug") or ""
    blocks_hint = sess.get("blocks_meta") or []

    # Defaults: legacy seatsio (most charts) — region 'eu' is what webook uses
    region = "eu"

    blocks_json = json.dumps(blocks_hint, ensure_ascii=False)

    # Two render strategies:
    #   • If workspace_key exists → embed the official seats.io renderer.
    #     This is the rich visual chart with row/seat granularity.
    #   • Otherwise → fall back to a textual block list (still useful: lets
    #     the user pick primary + backups when API maps are unavailable).
    if workspace_key and event_key:
        body = _render_seatsio_chart(session_token, workspace_key,
                                      event_key, chart_key, region,
                                      provider, slug, blocks_json)
    else:
        body = _render_textual_picker(session_token, slug, blocks_json)

    return HTMLResponse(body)


@router.post("/picker/{session_token}/selection")
async def picker_selection(session_token: str, request: Request):
    """Fallback endpoint: WebApp can POST here when sendData isn't available
    (e.g. tested in a regular browser tab). Bot handlers also pick up the
    same payload via Telegram's `web_app_data` update."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")
    ok = _set_session_selection(session_token, body or {})
    if not ok:
        raise HTTPException(404, "session not found")
    return JSONResponse({"ok": True})


# ════════════════════════════════════════════════════════════════════════
# HTML templates
# ════════════════════════════════════════════════════════════════════════
_BASE_CSS = """
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; padding: 0; height: 100%; overscroll-behavior: contain;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Tahoma, sans-serif; }
body { background: var(--tg-theme-bg-color, #0f172a);
       color: var(--tg-theme-text-color, #e5e7eb); }
.topbar { padding: 14px 16px; background: rgba(255,255,255,.04);
          border-bottom: 1px solid rgba(255,255,255,.06);
          display: flex; align-items: center; justify-content: space-between;
          flex-wrap: wrap; gap: 8px; }
.topbar h1 { margin: 0; font-size: 16px; font-weight: 600; }
.topbar .status { font-size: 12px; opacity: .7; }
.legend { font-size: 12px; padding: 8px 16px; opacity: .8;
          border-bottom: 1px solid rgba(255,255,255,.05); }
.legend b { color: #fbbf24; }
.legend .b { color: #38bdf8; }
.chart { width: 100%; height: calc(100vh - 200px); position: relative; }
#chart-container, #seats-cloud-chart {
  width: 100%; height: 100%; background: rgba(0,0,0,.25); border-radius: 0;
}
.bottom { position: fixed; bottom: 0; left: 0; right: 0;
          padding: 12px 16px; background: var(--tg-theme-bg-color, #111827);
          border-top: 1px solid rgba(255,255,255,.08);
          display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.summary { flex: 1; font-size: 13px; line-height: 1.5; min-width: 0; }
.summary .row { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.btn { padding: 11px 18px; border-radius: 10px; border: 0; font-weight: 700;
       cursor: pointer; font-size: 14px; }
.btn.primary { background: var(--tg-theme-button-color, #2563eb);
                color: var(--tg-theme-button-text-color, #fff); }
.btn.primary:disabled { opacity: .4; cursor: not-allowed; }
.btn.ghost { background: transparent; color: inherit; opacity: .7; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 999px;
       background: rgba(56,189,248,.15); color: #38bdf8; font-size: 11px;
       margin-left: 4px; }
.tag.star { background: rgba(251,191,36,.18); color: #fbbf24; }
.list { padding: 0 16px 110px; }
.block-item { display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; margin: 8px 0; background: rgba(255,255,255,.04);
  border: 1px solid rgba(255,255,255,.08); border-radius: 12px; }
.block-item .name { font-weight: 600; }
.block-item .pill { font-size: 11px; opacity: .65; }
.block-item button { padding: 6px 12px; border-radius: 8px; border: 0;
  background: rgba(56,189,248,.15); color: #38bdf8; font-weight: 600;
  font-size: 12px; cursor: pointer; }
.block-item.selected-primary { border-color: rgba(251,191,36,.5);
  background: rgba(251,191,36,.08); }
.block-item.selected-backup { border-color: rgba(56,189,248,.4);
  background: rgba(56,189,248,.06); }
.error { padding: 24px; font-size: 14px; line-height: 1.7;
         color: #fca5a5; text-align: center; }
.note { padding: 8px 16px; font-size: 12px; opacity: .65; line-height: 1.5; }
"""


_COMMON_JS = """
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { try { tg.expand(); tg.ready(); } catch(e){} }

const STATE = {
  primary: '',
  backups: [],
  seats: [],
};

function updateSummary() {
  const p = STATE.primary || '—';
  const b = STATE.backups.length ? STATE.backups.join(' → ') : '—';
  const s = STATE.seats.length ? `${STATE.seats.length} مقعد` : '';
  document.getElementById('sum-primary').textContent = p;
  document.getElementById('sum-backups').textContent = b;
  document.getElementById('sum-seats').textContent = s;
  document.getElementById('btn-confirm').disabled = !STATE.primary;
}

function send() {
  const payload = {
    primary: STATE.primary,
    backups: STATE.backups,
    seats: STATE.seats,
  };
  // Path A: Telegram WebApp sendData (preferred — closes the WebApp and
  // delivers the JSON via web_app_data update to the bot).
  if (tg && typeof tg.sendData === 'function') {
    try {
      tg.sendData(JSON.stringify(payload));
      return;
    } catch (e) { console.warn('sendData failed', e); }
  }
  // Path B: HTTP POST fallback (e.g. when opened in a regular browser).
  fetch(window.location.pathname + '/selection', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify(payload),
  }).then(r => r.json()).then(d => {
    document.body.innerHTML =
      '<div class="error" style="color:#86efac">' +
      '✅ تم إرسال اختيارك. يمكنك الرجوع للبوت الآن.</div>';
  }).catch(() => {
    alert('تعذّر الإرسال. أعد المحاولة من البوت.');
  });
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('btn-confirm').addEventListener('click', send);
});
"""


def _render_textual_picker(session_token: str, slug: str, blocks_json: str) -> str:
    """Fallback when seats.io chart can't be embedded (no workspace_key
    or unsupported provider). Shows a clean tappable block list."""
    return f"""<!doctype html>
<html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Webook Block Picker</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>{_BASE_CSS}</style>
</head><body>
<div class="topbar">
  <h1>🗺️ اختيار البلوكات</h1>
  <span class="status">{html.escape(slug)[:60]}</span>
</div>
<div class="legend">
  ⭐ <b>الرئيسي</b> · 🔁 <span class="b">احتياطي</span> ·
  اضغط مرة على بلوك لتعيينه رئيسياً، مرة أخرى لإضافته كاحتياطي،
  ثالثة لإلغائه.
</div>
<div class="list" id="list"></div>

<div class="bottom">
  <div class="summary">
    <div class="row">⭐ الرئيسي: <b id="sum-primary">—</b></div>
    <div class="row">🔁 الاحتياطية: <b id="sum-backups">—</b></div>
    <div class="row" id="sum-seats-row"><b id="sum-seats"></b></div>
  </div>
  <button id="btn-confirm" class="btn primary" disabled>تأكيد ➜</button>
</div>

<script>
const BLOCKS = {blocks_json};
{_COMMON_JS}

function render() {{
  const list = document.getElementById('list');
  list.innerHTML = '';
  BLOCKS.forEach(b => {{
    const div = document.createElement('div');
    div.className = 'block-item';
    if (b.name === STATE.primary) div.classList.add('selected-primary');
    else if (STATE.backups.includes(b.name)) div.classList.add('selected-backup');
    let badge = '';
    if (b.name === STATE.primary) badge = '<span class="tag star">⭐ رئيسي</span>';
    else if (STATE.backups.includes(b.name)) badge = '<span class="tag">#' + (STATE.backups.indexOf(b.name)+1) + '</span>';

    let counts = '';
    if (typeof b.free === 'number' && b.free >= 0) {{
      counts = ' · ' + b.free + '/' + b.total;
    }}
    div.innerHTML = '<div><div class="name">' + (b.name||'') + badge + '</div>' +
                    '<div class="pill">' + (b.category || '') + counts + '</div></div>' +
                    '<button>اضغط</button>';
    div.addEventListener('click', () => toggle(b.name));
    list.appendChild(div);
  }});
}}

function toggle(name) {{
  if (STATE.primary === name) {{
    STATE.primary = '';
    if (!STATE.backups.includes(name)) STATE.backups.push(name);
  }} else if (STATE.backups.includes(name)) {{
    STATE.backups = STATE.backups.filter(b => b !== name);
  }} else {{
    if (!STATE.primary) STATE.primary = name;
    else STATE.backups.push(name);
  }}
  updateSummary();
  render();
}}

render();
updateSummary();
</script>
</body></html>"""


def _render_seatsio_chart(session_token: str, workspace_key: str,
                          event_key: str, chart_key: str, region: str,
                          provider: str, slug: str, blocks_json: str) -> str:
    """Embed the official seats.io chart renderer for visual seat picking.

    Uses the legacy seats.io chart.js loader (region-based) which is the
    most-supported entry point.  Webook's seats_planner provider also
    speaks the SIO adapter via chart.seatcloud.com — we try seatsio first
    and fall back to seatcloud automatically on load error.
    """
    # The seats.io chart needs ticket-types categories list. We pass them
    # from blocks_meta so user only sees the relevant block this session.
    return f"""<!doctype html>
<html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Webook Visual Picker</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>{_BASE_CSS}</style>
</head><body>
<div class="topbar">
  <h1>🗺️ اختيار المقاعد المرئي</h1>
  <span class="status">{html.escape(slug)[:60]}</span>
</div>
<div class="legend">
  اضغط على المقاعد المتاحة لتحديدها · حدد البلوك الرئيسي أولاً ·
  ثم أضف بلوكات احتياطية حسب الحاجة.
</div>

<div class="chart">
  <div id="chart-container"></div>
  <div id="seats-cloud-chart" style="display:none"></div>
  <div id="loading" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:14px;opacity:.7">
    🔄 جارٍ تحميل خريطة المقاعد...
  </div>
</div>

<div class="bottom">
  <div class="summary">
    <div class="row">⭐ الرئيسي: <b id="sum-primary">—</b></div>
    <div class="row">🔁 الاحتياطية: <b id="sum-backups">—</b></div>
    <div class="row">🪑 المقاعد المختارة: <b id="sum-seats">—</b></div>
  </div>
  <button id="btn-confirm" class="btn primary" disabled>تأكيد ➜</button>
</div>

<script>
const WORKSPACE_KEY = {json.dumps(workspace_key)};
const EVENT_KEY     = {json.dumps(event_key)};
const CHART_KEY     = {json.dumps(chart_key)};
const REGION        = {json.dumps(region)};
const PROVIDER      = {json.dumps(provider)};
const FALLBACK_BLOCKS = {blocks_json};

{_COMMON_JS}

function setLoading(show) {{
  document.getElementById('loading').style.display = show ? 'flex' : 'none';
}}

function rebuildSummary() {{
  // primary block = section of the first selected seat (if any)
  if (STATE.seats.length > 0) {{
    const sectionsCount = {{}};
    STATE.seats.forEach(s => {{
      const sec = (s.section || s.labels?.section || '').toString();
      if (sec) sectionsCount[sec] = (sectionsCount[sec] || 0) + 1;
    }});
    const sortedSections = Object.entries(sectionsCount).sort((a,b)=>b[1]-a[1]).map(x=>x[0]);
    if (sortedSections.length) {{
      STATE.primary = sortedSections[0];
      STATE.backups = sortedSections.slice(1, 5);
    }}
  }}
  document.getElementById('sum-seats').textContent =
    STATE.seats.length ? STATE.seats.map(s => s.label || s.id).slice(0,8).join(', ') +
                          (STATE.seats.length > 8 ? '…' : '') : '—';
  updateSummary();
}}

function loadScript(src) {{
  return new Promise((resolve, reject) => {{
    const s = document.createElement('script');
    s.src = src; s.onload = resolve; s.onerror = reject;
    document.head.appendChild(s);
  }});
}}

async function tryRenderSeatsio() {{
  await loadScript('https://cdn-' + REGION + '.seatsio.net/chart.js');
  return new Promise((resolve) => {{
    try {{
      const chart = new seatsio.SeatingChart({{
        divId: 'chart-container',
        workspaceKey: WORKSPACE_KEY,
        event: EVENT_KEY,
        session: 'continue',
        showLegend: true,
        onChartRendered: () => {{ setLoading(false); resolve(true); }},
        onChartRenderingFailed: () => resolve(false),
        onObjectSelected: (obj) => {{
          STATE.seats.push({{
            id: obj.id, label: obj.label,
            section: obj.labels?.section, row: obj.labels?.parent,
            seat: obj.labels?.own, category: obj.category?.label,
          }});
          rebuildSummary();
        }},
        onObjectDeselected: (obj) => {{
          STATE.seats = STATE.seats.filter(s => s.id !== obj.id);
          rebuildSummary();
        }},
      }}).render();
    }} catch (e) {{
      console.error('seatsio init err', e); resolve(false);
    }}
  }});
}}

async function tryRenderSeatCloud() {{
  await loadScript('https://chart.seatcloud.com/v1.0/chart.js');
  return new Promise((resolve) => {{
    if (!window.seats || !window.seats.adapters || !window.seats.adapters.SIO) {{
      resolve(false); return;
    }}
    document.getElementById('seats-cloud-chart').style.display = 'block';
    document.getElementById('chart-container').style.display = 'none';
    try {{
      window.seats.adapters.SIO({{
        workspaceKey: WORKSPACE_KEY,
        event: EVENT_KEY,
        divId: 'seats-cloud-chart',
        onChartRendered: () => {{ setLoading(false); resolve(true); }},
        onObjectSelected: (obj) => {{
          STATE.seats.push({{
            id: obj.id, label: obj.label,
            section: obj.labels?.section, row: obj.labels?.parent,
            seat: obj.labels?.own,
          }});
          rebuildSummary();
        }},
        onObjectDeselected: (obj) => {{
          STATE.seats = STATE.seats.filter(s => s.id !== obj.id);
          rebuildSummary();
        }},
      }}).render();
    }} catch (e) {{
      console.error('seatcloud init err', e); resolve(false);
    }}
  }});
}}

function fallbackTextualList() {{
  setLoading(false);
  const cont = document.getElementById('chart-container');
  cont.style.overflowY = 'auto';
  cont.innerHTML = '<div style="padding:16px"><p style="opacity:.7;font-size:13px">' +
    'تعذّر تحميل الخريطة المرئية (الفعالية محمية). اختر البلوكات يدوياً:</p></div>';
  const list = document.createElement('div');
  list.style.padding = '0 16px';
  FALLBACK_BLOCKS.forEach(b => {{
    const div = document.createElement('div');
    div.className = 'block-item';
    div.innerHTML = '<div class="name">' + b.name + '</div><button>اختر</button>';
    div.onclick = () => {{
      if (STATE.primary === b.name) {{
        STATE.primary = '';
        STATE.backups.push(b.name);
      }} else if (STATE.backups.includes(b.name)) {{
        STATE.backups = STATE.backups.filter(x => x !== b.name);
      }} else if (!STATE.primary) {{
        STATE.primary = b.name;
      }} else {{
        STATE.backups.push(b.name);
      }}
      updateSummary();
      Array.from(list.children).forEach((el, i) => {{
        const n = FALLBACK_BLOCKS[i].name;
        el.classList.toggle('selected-primary', n === STATE.primary);
        el.classList.toggle('selected-backup', STATE.backups.includes(n));
      }});
    }};
    list.appendChild(div);
  }});
  cont.appendChild(list);
}}

(async () => {{
  // Always prefer seatsio chart.js (most stable). Fall back to seatcloud's
  // SIO adapter, then to a tappable block list.
  const ok1 = await tryRenderSeatsio().catch(() => false);
  if (ok1) return;
  const ok2 = await tryRenderSeatCloud().catch(() => false);
  if (ok2) return;
  fallbackTextualList();
}})();
</script>
</body></html>"""


def _error_page(msg: str) -> str:
    return f"""<!doctype html><html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Webook Picker</title>
<style>body{{margin:0;background:#0f172a;color:#e5e7eb;font-family:-apple-system,sans-serif;
min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center}}
.card{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
border-radius:14px;padding:28px;max-width:340px;line-height:1.7}}</style></head>
<body><div class="card">⚠️<br>{html.escape(msg).replace(chr(10),'<br>')}</div></body></html>"""
