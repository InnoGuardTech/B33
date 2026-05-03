"""
Telegram WebApp Mini Picker.

Robust goals:
- Render live blocks from seats_planner server-side.
- Tolerate multiple geometry shapes (points / polygon / polygons / coordinates).
- Always provide a textual selector even if the SVG is imperfect.
- Never hard-block selection purely because our sale-state inference may be stale.
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
    try:
        from app.bot.handlers import _PICKER_SESSIONS
        return _PICKER_SESSIONS.get(session_token)
    except Exception:
        return None


def _set_session_selection(session_token: str, payload: dict) -> bool:
    try:
        from app.bot.handlers import _PICKER_SESSIONS
        sess = _PICKER_SESSIONS.get(session_token)
        if not sess:
            return False
        primary = (payload.get("primary") or "").strip()
        backups = [str(b).strip() for b in (payload.get("backups") or []) if str(b).strip()]
        seats = [str(s) for s in (payload.get("seats") or []) if s]
        if not primary and backups:
            primary = backups.pop(0)
        if primary:
            sess["primary"] = primary
        seen = set([sess.get("primary")] if sess.get("primary") else [])
        sess["backups"] = []
        for b in backups:
            if not b or b in seen:
                continue
            seen.add(b)
            sess["backups"].append(b)
        if seats:
            sess["preselected_seats"] = seats
        sess["webapp_completed"] = True
        return True
    except Exception as e:
        log.warning(f"set_session_selection error: {e}")
        return False


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _extract_polygon(area: dict[str, Any]) -> list[dict[str, float]]:
    geom = area.get("geometry") or {}
    candidates: list[Any] = [
        geom.get("points"),
        geom.get("coordinates"),
        area.get("points"),
    ]
    poly = geom.get("polygon")
    if isinstance(poly, dict):
        candidates.append(poly.get("points") or poly.get("coordinates"))
    elif isinstance(poly, list):
        candidates.append(poly)
    polys = geom.get("polygons")
    if isinstance(polys, list):
        candidates.extend(polys)

    for cand in candidates:
        pts: list[dict[str, float]] = []
        if isinstance(cand, dict):
            cand = cand.get("points") or cand.get("coordinates") or []
        if not isinstance(cand, list):
            continue
        for p in cand:
            if isinstance(p, dict):
                if "x" in p or "y" in p:
                    pts.append({"x": _num(p.get("x")), "y": _num(p.get("y"))})
                elif "lng" in p or "lat" in p:
                    pts.append({"x": _num(p.get("lng")), "y": _num(p.get("lat"))})
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                pts.append({"x": _num(p[0]), "y": _num(p[1])})
        uniq = {(round(p["x"], 3), round(p["y"], 3)) for p in pts}
        if len(pts) >= 3 and len(uniq) >= 3:
            return pts
    return []


def _center_from_polygon(points: list[dict[str, float]]) -> dict[str, float]:
    if not points:
        return {}
    xs = [p.get("x", 0.0) for p in points]
    ys = [p.get("y", 0.0) for p in points]
    return {"x": sum(xs) / len(xs), "y": sum(ys) / len(ys)}


def _merge_live_with_cached(live_blocks: list[dict[str, Any]], cached_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {str((b.get("name") or "")).strip(): dict(b) for b in live_blocks if (b.get("name") or "").strip()}
    out: list[dict[str, Any]] = []
    used: set[str] = set()

    for cb in cached_blocks or []:
        name = str((cb.get("name") or "")).strip()
        if not name:
            continue
        lb = by_name.get(name, {})
        used.add(name)
        out.append({
            "id": lb.get("id") or cb.get("id") or name,
            "name": name,
            "category": lb.get("category") or cb.get("category") or "",
            "category_key": lb.get("category_key") or cb.get("category_key"),
            "color": lb.get("color") or cb.get("color") or "",
            "capacity": lb.get("capacity") if lb.get("capacity") not in (None, "") else cb.get("total", 0),
            "free": cb.get("free", lb.get("free", -1)),
            "total": cb.get("total", lb.get("total", -1)),
            "polygon": lb.get("polygon") or [],
            "center": lb.get("center") or {},
            "is_for_sale": lb.get("is_for_sale", True),
            "status": lb.get("status") or "",
        })

    for name, lb in by_name.items():
        if name in used:
            continue
        out.append(lb)

    out.sort(key=lambda x: (str(x.get("category") or ""), str(x.get("name") or "")))
    return out


@router.get("/picker/{session_token}", response_class=HTMLResponse)
async def picker_page(session_token: str) -> HTMLResponse:
    sess = _get_session(session_token)
    if not sess:
        return HTMLResponse(_error_page("انتهت جلسة الاختيار. أعد المحاولة من البوت."), status_code=410)
    return HTMLResponse(_picker_page_html(session_token, sess))


@router.get("/picker/{session_token}/data")
async def picker_data(session_token: str) -> JSONResponse:
    sess = _get_session(session_token)
    if not sess:
        raise HTTPException(410, "session expired")

    workspace_key = sess.get("workspace_key") or ""
    event_key = sess.get("event_key") or ""
    chart_key = sess.get("chart_key") or ""
    blocks_meta = sess.get("blocks_meta") or []

    payload: dict[str, Any] = {
        "workspace_key": workspace_key,
        "event_key": event_key,
        "chart_key": chart_key,
        "slug": sess.get("slug") or "",
        "blocks": [],
        "categories": [],
        "venue_name": "",
        "viewport": {"width": 800, "height": 800},
    }

    if not (workspace_key and chart_key and event_key):
        payload["blocks"] = _merge_live_with_cached([], blocks_meta)
        return JSONResponse(payload)

    try:
        from app.core.storage import list_accounts
        from app.services.booking_http import fetch_event_meta
        from app.services.seatsio_client import SeatsioClient, get_hold_token_from_webook
        import aiohttp

        hold_token = ""
        try:
            accs = list_accounts(status="ready")
            bearer = (accs[0].get("access_token") if accs else "") or ""
            if bearer:
                async with aiohttp.ClientSession() as s:
                    em = await fetch_event_meta(s, sess.get("slug") or "", bearer)
                event_id = em.get("event_id") or ""
                if event_id:
                    hold_token, _meta = await get_hold_token_from_webook(
                        slug=sess.get("slug") or "",
                        event_id=event_id,
                        bearer=bearer,
                    )
                    hold_token = hold_token or ""
        except Exception as e:
            log.debug(f"picker hold-token soft-fail: {e}")

        async with SeatsioClient(
            event_key=event_key,
            workspace_key=workspace_key,
            chart_key=chart_key,
            provider=sess.get("seats_provider") or "",
            hold_token=hold_token,
        ) as client:
            event_data = await client.fetch_event()
            map_data = await client.fetch_map()
            statuses = await client.fetch_item_statuses()

        if not map_data:
            payload["blocks"] = _merge_live_with_cached([], blocks_meta)
            return JSONResponse(payload)

        sold_specs: set[int] = set()
        for sp in (event_data.get("specifications") or []):
            if not isinstance(sp, dict):
                continue
            k = sp.get("id") if "id" in sp else sp.get("key")
            try:
                sold_specs.add(int(k))
            except Exception:
                pass
            payload["categories"].append({
                "key": k,
                "label": sp.get("name") or sp.get("label") or "",
                "color": sp.get("color") or "",
            })

        levels = map_data.get("levels") or [{}]
        first_level = levels[0] if isinstance(levels, list) and levels else {}
        payload["viewport"] = {
            "width": int(first_level.get("width") or 800),
            "height": int(first_level.get("height") or 800),
        }
        payload["venue_name"] = map_data.get("name") or ""

        live_blocks: list[dict[str, Any]] = []
        for area in (map_data.get("content") or {}).get("areas") or []:
            if not isinstance(area, dict) or area.get("published") is False:
                continue
            spec = area.get("specification") or {}
            spec_key = spec.get("key") if "key" in spec else spec.get("id")
            try:
                spec_key_i = int(spec_key) if spec_key is not None else None
            except Exception:
                spec_key_i = None
            polygon = _extract_polygon(area)
            geom = area.get("geometry") or {}
            center = geom.get("center") or _center_from_polygon(polygon)
            occ = area.get("occupancy") or {}
            area_id = str(area.get("id") or area.get("name") or "")
            lbl = area.get("label")
            name = area.get("name") or (lbl.get("label") if isinstance(lbl, dict) else area_id) or area_id
            is_for_sale = (spec_key_i in sold_specs) if sold_specs else True
            status = statuses.get(area_id) or statuses.get(name) or ""
            cap = int(occ.get("capacity") or 0)
            live_blocks.append({
                "id": area_id,
                "name": name,
                "category": spec.get("label") or spec.get("name") or "",
                "category_key": spec_key,
                "color": spec.get("color") or "",
                "capacity": cap,
                "free": cap if is_for_sale and not status else -1,
                "total": cap,
                "polygon": polygon,
                "center": center,
                "is_for_sale": is_for_sale,
                "status": status,
            })

        payload["blocks"] = _merge_live_with_cached(live_blocks, blocks_meta)
        return JSONResponse(payload)
    except Exception as e:
        log.exception(f"picker_data error: {e}")
        payload["blocks"] = _merge_live_with_cached([], blocks_meta)
        payload["error"] = str(e)[:200]
        return JSONResponse(payload)


@router.post("/picker/{session_token}/selection")
async def picker_selection(session_token: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")
    if not _set_session_selection(session_token, body or {}):
        raise HTTPException(404, "session not found")
    return JSONResponse({"ok": True})


_BASE_CSS = """
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; padding: 0; height: 100%; overscroll-behavior: contain;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Tahoma, sans-serif; }
body { background: var(--tg-theme-bg-color, #0f172a); color: var(--tg-theme-text-color, #e5e7eb);
  display: flex; flex-direction: column; min-height: 100vh; }
.topbar { padding: 12px 16px; background: rgba(255,255,255,.04); border-bottom: 1px solid rgba(255,255,255,.06); }
.topbar h1 { margin: 0; font-size: 15px; font-weight: 700; }
.topbar .sub { font-size: 11px; opacity: .7; margin-top: 4px; }
.legend { padding: 8px 16px; font-size: 12px; opacity: .9; border-bottom: 1px solid rgba(255,255,255,.05); }
.legend b { color: #fbbf24; }
#chart-wrap { flex: 0 0 44vh; position: relative; overflow: hidden; background: rgba(0,0,0,.2); }
#chart-svg { width: 100%; height: 100%; user-select: none; touch-action: manipulation; }
#status, #error { padding: 18px; text-align: center; font-size: 13px; opacity: .8; }
#error { color: #fecaca; }
.block-poly { stroke: rgba(255,255,255,.25); stroke-width: .8; cursor: pointer; transition: opacity .15s, stroke-width .15s; }
.block-poly.primary { stroke: #fbbf24; stroke-width: 2.5; }
.block-poly.backup { stroke: #38bdf8; stroke-width: 2; }
.block-poly.unavailable { opacity: .28; }
.block-label { font-size: 9px; pointer-events: none; fill: #fff; font-weight: 700; text-anchor: middle; }
#selection-list { padding: 0 16px 100px; overflow-y: auto; flex: 1; }
.block-row { display:flex; align-items:center; justify-content:space-between; gap:8px;
  padding:12px 14px; margin:8px 0; background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.08); border-radius:12px; cursor:pointer; }
.block-row.primary { border-color: rgba(251,191,36,.5); background: rgba(251,191,36,.08); }
.block-row.backup { border-color: rgba(56,189,248,.4); background: rgba(56,189,248,.06); }
.block-row .name { font-weight:700; font-size:14px; }
.block-row .meta { font-size:11px; opacity:.7; margin-top:3px; }
.tag { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; margin-left:6px; }
.tag.star { background: rgba(251,191,36,.22); color: #fbbf24; }
.tag.bk { background: rgba(56,189,248,.22); color: #38bdf8; }
.bottom { background: var(--tg-theme-bg-color, #111827); border-top: 1px solid rgba(255,255,255,.08);
  padding: 12px 16px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.summary { flex:1; font-size:12px; line-height:1.55; min-width:0; }
.summary .row { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.btn { padding: 11px 18px; border-radius: 10px; border:0; font-weight:700; cursor:pointer; font-size:14px; }
.btn.primary { background: var(--tg-theme-button-color, #2563eb); color: var(--tg-theme-button-text-color, #fff); }
.btn.primary:disabled { opacity: .45; cursor:not-allowed; }
@media (min-width: 720px) { body { max-width: 720px; margin: 0 auto; } }
"""


def _picker_page_html(session_token: str, sess: dict) -> str:
    slug = sess.get("slug") or ""
    return f"""<!doctype html>
<html lang="ar" dir="rtl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<title>Webook Picker</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>{_BASE_CSS}</style>
</head><body>
<div class="topbar">
  <h1>🗺️ اختيار البلوكات المرئي</h1>
  <div class="sub">{html.escape(slug)[:100]}</div>
</div>
<div class="legend">
  اضغط على أي بلوك لتحديده. أول اختيار يصبح <b>الرئيسي</b>، والبقية تصبح <span style="color:#38bdf8">احتياطية</span>.
  إن كانت الخريطة غير دقيقة في هذه الفعالية، استخدم القائمة النصية أسفلها.
</div>
<div id="chart-wrap">
  <div id="status">🔄 جارٍ تحميل الخريطة…</div>
  <svg id="chart-svg" style="display:none" xmlns="http://www.w3.org/2000/svg"></svg>
  <div id="error" style="display:none"></div>
</div>
<div id="selection-list"></div>
<div class="bottom">
  <div class="summary">
    <div class="row">⭐ الرئيسي: <b id="sum-primary">—</b></div>
    <div class="row">🔁 الاحتياطية: <b id="sum-backups">—</b></div>
  </div>
  <button id="btn-confirm" class="btn primary" disabled>تأكيد ➜</button>
</div>
<script>
const SESSION_TOKEN = {json.dumps(session_token)};
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {{ try {{ tg.expand(); tg.ready(); }} catch (e) {{}} }}
const STATE = {{ primary: '', backups: [], blocks: [] }};
function $(id) {{ return document.getElementById(id); }}
function colorFromCss(v) {{ return v || '#475569'; }}
function quantile(arr, q) {{
  if (!arr.length) return 0;
  const s = [...arr].sort((a,b) => a-b);
  const pos = Math.max(0, Math.min(s.length - 1, Math.floor((s.length - 1) * q)));
  return s[pos];
}}
function updateSummary() {{
  $('sum-primary').textContent = STATE.primary || '—';
  $('sum-backups').textContent = STATE.backups.length ? STATE.backups.join(' → ') : '—';
  $('btn-confirm').disabled = !STATE.primary && !STATE.backups.length;
}}
function tap(blockName) {{
  if (!blockName) return;
  if (STATE.primary === blockName) {{
    STATE.primary = '';
  }} else if (STATE.backups.includes(blockName)) {{
    STATE.backups = STATE.backups.filter(x => x !== blockName);
  }} else if (!STATE.primary) {{
    STATE.primary = blockName;
  }} else {{
    STATE.backups.push(blockName);
  }}
  if (!STATE.primary && STATE.backups.length) {{
    STATE.primary = STATE.backups.shift();
  }}
  redraw();
  updateSummary();
}}
function send() {{
  if (!STATE.primary && STATE.backups.length) STATE.primary = STATE.backups.shift();
  updateSummary();
  const payload = {{ primary: STATE.primary, backups: STATE.backups, seats: [] }};
  if (!payload.primary) return;
  if (tg && typeof tg.sendData === 'function') {{
    try {{ tg.sendData(JSON.stringify(payload)); try {{ tg.close(); }} catch (e) {{}} return; }}
    catch (e) {{ console.warn('sendData failed', e); }}
  }}
  fetch('/picker/' + SESSION_TOKEN + '/selection', {{
    method: 'POST', headers: {{'content-type':'application/json'}}, body: JSON.stringify(payload)
  }}).then(r => r.json()).then(() => {{
    document.body.innerHTML = '<div style="padding:32px;text-align:center;color:#86efac;font-size:14px">✅ تم إرسال اختيارك. يمكنك الرجوع للبوت الآن.</div>';
  }}).catch(() => alert('تعذّر الإرسال. أعد المحاولة من البوت.'));
}}
function showError(msg) {{
  $('status').style.display = 'none';
  $('chart-svg').style.display = 'none';
  $('error').style.display = 'block';
  $('error').textContent = msg;
}}
function renderSvg(data) {{
  const svg = $('chart-svg');
  const valid = STATE.blocks.filter(b => Array.isArray(b.polygon) && b.polygon.length >= 3);
  if (valid.length < 2) {{
    svg.style.display = 'none';
    $('status').style.display = 'none';
    return;
  }}
  const xs = [], ys = [];
  for (const b of valid) for (const p of (b.polygon || [])) {{
    if (Number.isFinite(p.x) && Number.isFinite(p.y)) {{ xs.push(p.x); ys.push(p.y); }}
  }}
  let minX = quantile(xs, 0.02), maxX = quantile(xs, 0.98);
  let minY = quantile(ys, 0.02), maxY = quantile(ys, 0.98);
  const vp = data.viewport || {{width:800, height:800}};
  if (!(maxX > minX) || !(maxY > minY)) {{ minX = 0; minY = 0; maxX = vp.width; maxY = vp.height; }}
  const pad = Math.max(20, Math.round(Math.max(maxX-minX, maxY-minY) * 0.04));
  svg.setAttribute('viewBox', `${{minX-pad}} ${{minY-pad}} ${{(maxX-minX)+pad*2}} ${{(maxY-minY)+pad*2}}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const ns = 'http://www.w3.org/2000/svg';
  for (const b of valid) {{
    const poly = document.createElementNS(ns, 'polygon');
    poly.setAttribute('points', b.polygon.map(p => `${{p.x}},${{p.y}}`).join(' '));
    poly.setAttribute('class', 'block-poly' +
      (STATE.primary === b.name ? ' primary' : '') +
      (STATE.backups.includes(b.name) ? ' backup' : '') +
      (b.is_for_sale === false ? ' unavailable' : ''));
    poly.setAttribute('fill', colorFromCss(b.color));
    poly.setAttribute('fill-opacity', b.is_for_sale === false ? '0.24' : '0.58');
    poly.addEventListener('click', () => tap(b.name));
    poly.addEventListener('touchend', ev => {{ ev.preventDefault(); tap(b.name); }}, {{passive:false}});
    svg.appendChild(poly);
    if (b.center && Number.isFinite(b.center.x) && Number.isFinite(b.center.y)) {{
      const t = document.createElementNS(ns, 'text');
      t.setAttribute('x', b.center.x); t.setAttribute('y', b.center.y);
      t.setAttribute('class', 'block-label'); t.setAttribute('dominant-baseline', 'middle');
      t.textContent = b.name; svg.appendChild(t);
    }}
  }}
  $('status').style.display = 'none';
  $('chart-svg').style.display = 'block';
}}
function renderSelectionList() {{
  const list = $('selection-list');
  list.innerHTML = '';
  const groups = {{}};
  for (const b of STATE.blocks) {{
    const c = b.category || 'Other';
    (groups[c] = groups[c] || []).push(b);
  }}
  for (const cat of Object.keys(groups)) {{
    const h = document.createElement('div');
    h.style.cssText = 'padding:14px 4px 4px;font-weight:700;font-size:13px;opacity:.85';
    h.textContent = cat; list.appendChild(h);
    for (const b of groups[cat]) {{
      const row = document.createElement('div');
      const isPrim = STATE.primary === b.name;
      const isBack = STATE.backups.includes(b.name);
      row.className = 'block-row' + (isPrim ? ' primary' : (isBack ? ' backup' : ''));
      let badge = '';
      if (isPrim) badge = '<span class="tag star">⭐ رئيسي</span>';
      else if (isBack) badge = '<span class="tag bk">#' + (STATE.backups.indexOf(b.name)+1) + '</span>';
      let counts = '';
      if (typeof b.capacity === 'number' && b.capacity > 0) counts = ' · سعة ' + b.capacity;
      else if (typeof b.free === 'number' && b.free >= 0) counts = ' · ' + b.free + '/' + b.total;
      row.innerHTML = '<div><div class="name">' + (b.name || '') + badge + '</div>' +
        '<div class="meta">' + (b.category || '') + counts + (b.is_for_sale === false ? ' · قد يكون غير ظاهر للبيع حالياً' : '') + '</div></div>';
      row.addEventListener('click', () => tap(b.name));
      list.appendChild(row);
    }}
  }}
}}
function redraw() {{
  if ($('chart-svg').style.display !== 'none') renderSvg({{viewport:{{width:800,height:800}}}});
  renderSelectionList();
}}
async function loadData() {{
  try {{
    const r = await fetch('/picker/' + SESSION_TOKEN + '/data');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    STATE.blocks = data.blocks || [];
    if (!STATE.blocks.length) {{ showError('لا توجد بلوكات قابلة للعرض لهذه الفعالية.'); return; }}
    renderSvg(data);
    renderSelectionList();
    updateSummary();
    if ($('chart-svg').style.display === 'none') $('status').textContent = 'ℹ️ تم تفعيل القائمة النصية لأن العرض المرئي غير واضح لهذه الخريطة.';
  }} catch (e) {{
    console.error(e);
    showError('تعذّر تحميل بيانات الخريطة.');
  }}
}}
document.addEventListener('DOMContentLoaded', () => {{
  $('btn-confirm').addEventListener('click', send);
  updateSummary();
  loadData();
}});
</script>
</body></html>"""


def _error_page(msg: str) -> str:
    return f"""<!doctype html><html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Webook Picker</title>
<style>body{{margin:0;background:#0f172a;color:#e5e7eb;font-family:-apple-system,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center}}.card{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:28px;max-width:340px;line-height:1.7}}</style></head>
<body><div class="card">⚠️<br>{html.escape(msg)}</div></body></html>"""
