"""
SeatCloud / Seats.io client — v2 (seats_planner provider).

Discovery (via browser sniffing of webook.com /book pages):

  • Webook now uses two providers behind the same `seats_io` blob:
      - 'seatsio'        → legacy chart with /system/public/{ws}/...
      - 'seats_planner'  → new chart with /api/v2/{ws}/...   ← MOST EVENTS

  • For seats_planner the actual endpoints are:
      GET  /api/v2/{ws}/event/{event_key}
            → event meta + specifications (= ticket categories)
      GET  /api/v2/{ws}/event/{event_key}/items?hold_token=X
            → seat statuses (booked/free) — 204 when there are no held items
      GET  /api/v2/{ws}/map/{chart_key}/data
            → full chart data: levels[], content.areas[], content.images[]
      POST /api/v1/team/{ws}/hold-tokens/public/generate
            → can also obtain a hold-token (we prefer webook's hold-token
              endpoint though, see WebookHoldTokenSource)

  • The hold-token MUST come from webook.com first when the chart is
    behind the queue/turnstile gate:
      POST /api/v2/event-detail/{slug}/hold-token   (Bearer required)
            body: {"event_id": "...", "lang": "en"[, "turnstile": "..."]}

This module exposes a unified `SeatsioClient` that:
  • Works for both providers transparently.
  • Returns rendering_info shaped like seats.io's classic API so the rest
    of the codebase (block_analyzer, summarizer, …) keeps working.
  • Falls back gracefully when the network/endpoint fails — never claims
    "chart full" on connectivity errors.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp  # retained ONLY for WebSocket message-type enums

from app.core.config import WEBOOK_API, WEBOOK_ORIGIN
from app.services.login_robust import resolve_public_token
from app.services.seatsio_token_fetcher import ensure_tokens
from app.services.stealth_client import StealthClient

log = logging.getLogger("seatsio_client")

# Modern (seats_planner) base
SEATCLOUD_API = "https://api.seatcloud.com"
# Legacy (seatsio) base — kept as fallback
SEATSIO_LEGACY_API = "https://api.seatcloud.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


# ════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════
def _trace_id() -> str:
    return f"{int(time.time()*1000)}-{int(time.time()*1e6) % 100000:05d}"


def _chart_headers(hold_token: str = "", turnstile: str = "") -> dict[str, str]:
    h = {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate",
        "user-agent": DEFAULT_UA,
        "origin": "https://chart.seatcloud.com",
        "referer": "https://chart.seatcloud.com/",
        "accept-language": "ar-SA,ar;q=0.9,en;q=0.8",
        "sec-ch-ua": '"Not:A-Brand";v="99", "Chromium";v="128"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    }
    if hold_token:
        h["x-hold-token"] = hold_token
    if turnstile:
        # Some webook deployments accept the turnstile token at chart-level too
        h["cf-turnstile-response"] = turnstile
        h["x-turnstile-token"] = turnstile
    return h


def _legacy_headers(hold_token: str = "") -> dict[str, str]:
    h = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "user-agent": DEFAULT_UA,
        "origin": WEBOOK_ORIGIN,
        "referer": f"{WEBOOK_ORIGIN}/",
        "accept-language": "ar-SA,ar;q=0.9,en;q=0.8",
        "x-client-tool": "chart-renderer",
    }
    if hold_token:
        h["x-hold-token"] = hold_token
    return h


async def _read_json(resp) -> Any:
    """Read possibly-gzipped JSON from EITHER aiohttp.ClientResponse
    OR a curl_cffi response. Both expose .read() / .text / .json()."""
    # curl_cffi path: the response body is already decoded.
    if hasattr(resp, "json") and not hasattr(resp, "_payload"):
        try:
            return resp.json()
        except Exception:
            try:
                return {"raw": (resp.text or "")[:500]}
            except Exception:
                return None
    # aiohttp path (legacy)
    raw = await resp.read()
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    txt = raw.decode("utf-8", errors="replace")
    if not txt.strip():
        return None
    try:
        return json.loads(txt)
    except Exception:
        return {"raw": txt[:500]}


# ════════════════════════════════════════════════════════════════════════
# Webook hold-token source (preferred for seats_planner events)
# ════════════════════════════════════════════════════════════════════════
async def _post_hold_token_once(
    *, slug: str, event_id: str, bearer: str,
    turnstile: str, time_slot_id: str,
) -> tuple[Optional[str], dict]:
    """Single attempt at /hold-token without auto-solving turnstile."""
    meta: dict[str, Any] = {"queued": False, "turnstile_required": False,
                             "http_status": 0}
    url = f"{WEBOOK_API}/event-detail/{slug}/hold-token?lang=en"
    body: dict[str, Any] = {"event_id": event_id, "lang": "en"}
    if turnstile:
        body["turnstile"] = turnstile
    if time_slot_id:
        body["time_slot_id"] = time_slot_id

    headers = {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
        "user-agent": DEFAULT_UA,
        "origin": WEBOOK_ORIGIN,
        "referer": f"{WEBOOK_ORIGIN}/",
        "authorization": f"Bearer {bearer}",
    }
    # V15-final: builtin-fallback aware public token
    try:
        from app.core.config import webook_public_token
        headers["token"] = resolve_public_token(webook_public_token() or "")
    except Exception:
        headers["token"] = resolve_public_token("")

    try:
        # V15-final: stealth POST via curl_cffi (matches Chrome JA3+H2)
        async with StealthClient(timeout=15.0) as cli:
            r = await cli.request("POST", url, headers=headers, json=body)
            meta["http_status"] = r.status_code
            try:
                d = r.json() or {}
            except Exception:
                try:
                    d = {"raw": (r.text or "")[:500]}
                except Exception:
                    d = {}

        if isinstance(d, dict) and d.get("errors", {}).get("turnstile"):
            meta["turnstile_required"] = True
            meta["errors"] = d.get("errors")
            return None, meta
        if not isinstance(d, dict):
            return None, meta

        q = d.get("_queue") or {}
        if q.get("queued"):
            meta["queued"] = True
            meta["waiting_number"] = q.get("waiting_number")
            meta["total_in_queue"] = q.get("total_in_queue")

        token = (d.get("data") or {}).get("token") or d.get("token")
        if isinstance(token, str) and token:
            return token, meta
        for key in ("hold_token", "holdToken", "seat_hold_token"):
            v = d.get(key) or (d.get("data") or {}).get(key)
            if isinstance(v, str) and v:
                return v, meta
        return None, meta
    except Exception as e:
        log.debug(f"webook hold-token error: {e}")
        return None, meta


async def get_hold_token_from_webook(
    *,
    slug: str,
    event_id: str,
    bearer: str,
    turnstile: str = "",
    time_slot_id: str = "",
    auto_solve_turnstile: bool = True,
) -> tuple[Optional[str], dict]:
    """Request a hold-token from webook with automatic Turnstile bypass.

    On 422 errors.turnstile response, automatically solves Turnstile via
    2Captcha (or Playwright fallback) and retries up to 2 times. The
    user is never prompted to open a browser.

    Returns (token, meta) where meta carries:
      - 'queued': bool, 'waiting_number': int, 'total_in_queue': int
      - 'turnstile_required': bool, 'turnstile_solved': bool
      - 'errors': dict (validation errors)
      - 'http_status': int
    """
    if not bearer or not slug or not event_id:
        return None, {"queued": False, "turnstile_required": False,
                       "http_status": 0}

    # First attempt — with whatever turnstile token we already have (may be empty)
    token, meta = await _post_hold_token_once(
        slug=slug, event_id=event_id, bearer=bearer,
        turnstile=turnstile, time_slot_id=time_slot_id,
    )
    if token:
        return token, meta

    # If turnstile is required and we're allowed to auto-solve, try up to 2 times
    if not (meta.get("turnstile_required") and auto_solve_turnstile):
        return None, meta

    try:
        from app.services.turnstile_solver import solve_turnstile, invalidate_cache
    except Exception as e:
        log.warning(f"turnstile_solver unavailable: {e}")
        return None, meta

    page_url = f"{WEBOOK_ORIGIN}/ar/sa/bur/sports-event/events/{slug}"
    for attempt in (1, 2):
        log.info(f"🛡️  auto-solving Turnstile (attempt {attempt}/2) for {slug}")
        ts_token = await solve_turnstile(
            page_url, force_refresh=(attempt == 2),
        )
        if not ts_token:
            log.warning("turnstile solver returned empty token")
            continue
        meta["turnstile_solved"] = True
        token, retry_meta = await _post_hold_token_once(
            slug=slug, event_id=event_id, bearer=bearer,
            turnstile=ts_token, time_slot_id=time_slot_id,
        )
        # Merge retry meta forward
        meta["http_status"] = retry_meta.get("http_status", meta["http_status"])
        if retry_meta.get("queued"):
            meta["queued"] = True
            meta["waiting_number"] = retry_meta.get("waiting_number")
            meta["total_in_queue"] = retry_meta.get("total_in_queue")
        if token:
            meta["turnstile_required"] = False
            log.info(f"✅ hold-token acquired after Turnstile bypass")
            return token, meta
        if not retry_meta.get("turnstile_required"):
            # Non-turnstile error — abort retry loop
            return None, retry_meta
        # Else: token rejected, force-refresh and try once more
        invalidate_cache(page_url)

    return None, meta


# ════════════════════════════════════════════════════════════════════════
# Map normalisation: seats_planner → seats.io rendering_info shape
# ════════════════════════════════════════════════════════════════════════
def _norm_specifications(event_data: dict, map_data: dict) -> list[dict]:
    """Merge category list from event + map (event has more accurate names)."""
    out: list[dict] = []
    seen_keys: set = set()

    for sp in (event_data.get("specifications") or []):
        if isinstance(sp, dict):
            key = sp.get("id") if "id" in sp else sp.get("key")
            label = sp.get("name") or sp.get("label") or ""
            color = sp.get("color") or ""
            if key is not None:
                seen_keys.add(int(key))
                out.append({"key": int(key), "label": str(label), "color": color})

    for sp in (map_data.get("specifications") or []):
        if isinstance(sp, dict):
            key = sp.get("key") if "key" in sp else sp.get("id")
            if key is None or int(key) in seen_keys:
                continue
            label = sp.get("name") or sp.get("label") or ""
            color = sp.get("color") or ""
            seen_keys.add(int(key))
            out.append({"key": int(key), "label": str(label), "color": color})
    return out


def _norm_objects_from_areas(map_data: dict, statuses: dict[str, str],
                              event_data: dict | None = None) -> list[dict]:
    """Convert seats_planner area list → seats.io-style objects[].

    Each generalAdmission area becomes a synthetic 'block' object with:
      • id        = area.id
      • label     = area.name
      • category  = specification.label (e.g. 'CAT 3 - N')
      • categoryKey = specification.key
      • capacity  = occupancy.capacity
      • status    = looked up in statuses[area.id]
      • center.x/y = geometry.center.x/y (for geometric expansion)

    NOTE on availability:
      The `occupancy.availableForSale` flag in the map is a DESIGN-TIME
      default — the live state comes from the event's spec list and the
      /items endpoint. We expose `is_for_sale` based on whether the area's
      specification key appears in event.specifications (= ticket types
      currently being sold).
    """
    content = map_data.get("content") or {}
    areas = content.get("areas") or []

    # Build set of currently-sold spec keys from event.specifications
    sold_spec_keys: set[int] = set()
    if isinstance(event_data, dict):
        for sp in (event_data.get("specifications") or []):
            if isinstance(sp, dict):
                k = sp.get("id") if "id" in sp else sp.get("key")
                if k is not None:
                    try:
                        sold_spec_keys.add(int(k))
                    except (TypeError, ValueError):
                        pass

    out: list[dict] = []
    for a in areas:
        if not isinstance(a, dict):
            continue
        if a.get("published") is False:
            continue
        spec = a.get("specification") or {}
        spec_key = spec.get("key") if "key" in spec else spec.get("id")
        spec_label = spec.get("label") or spec.get("name") or ""
        occ = a.get("occupancy") or {}
        geom = a.get("geometry") or {}
        center = geom.get("center") or {}

        area_id = str(a.get("id") or a.get("name") or "")
        # Robust name extraction (a['label'] may be dict or missing)
        lbl = a.get("label")
        if isinstance(lbl, dict):
            name = a.get("name") or lbl.get("label") or area_id
        else:
            name = a.get("name") or area_id

        # Live status (if any)
        status_live = statuses.get(area_id) or statuses.get(name) or ""

        # is_for_sale: spec key must be in the event's currently-sold list
        try:
            spec_key_int = int(spec_key) if spec_key is not None else None
        except (TypeError, ValueError):
            spec_key_int = None
        is_for_sale = (spec_key_int in sold_spec_keys) if sold_spec_keys else True

        out.append({
            "id": area_id,
            "objectId": area_id,
            "label": name,
            "displayedLabel": name,
            "category": str(spec_label),
            "categoryKey": spec_key,
            "ticketType": str(spec_label),
            "section": name,
            "capacity": occ.get("capacity") or 0,
            "minOccupancy": occ.get("minOccupancy") or 1,
            "isAvailableForSale": is_for_sale,    # ← derived from event.specs
            "x": center.get("x"),
            "y": center.get("y"),
            "center": center,
            "labels": {"section": name, "displayedLabel": name},
            "itemType": a.get("itemType") or "generalAdmission",
            "status_hint": status_live or ("free" if is_for_sale else "unavailable"),
        })
    return out


def normalize_seats_planner_to_rendering_info(
    event_data: dict, map_data: dict, statuses: dict[str, str]
) -> dict:
    """Produce a rendering_info-like dict the rest of the bot expects."""
    objs = _norm_objects_from_areas(map_data, statuses, event_data=event_data)
    return {
        "objects": objs,
        "categories": _norm_specifications(event_data, map_data),
        "_provider": "seats_planner",
        "_chart_key": map_data.get("key"),
        "_event_key": event_data.get("key"),
        "_venue_name": map_data.get("name"),
    }


# ════════════════════════════════════════════════════════════════════════
# Status normalisation
# ════════════════════════════════════════════════════════════════════════
def _coerce_statuses(payload: Any) -> dict[str, str]:
    """seats_planner /event/{ek}/items returns 204 when nothing held; or a
    dict {area_id: 'booked'/'free'} or list of {id, status}."""
    if not payload:
        return {}
    if isinstance(payload, dict):
        # direct mapping
        if all(isinstance(v, str) for v in payload.values()):
            return {str(k): str(v) for k, v in payload.items()}
        # nested under .items / .objects / .statuses
        for key in ("items", "objects", "statuses", "data"):
            v = payload.get(key)
            if isinstance(v, dict):
                return {str(k): str(vv) for k, vv in v.items()}
            if isinstance(v, list):
                out = {}
                for it in v:
                    if isinstance(it, dict):
                        kk = it.get("id") or it.get("objectId") or it.get("label")
                        ss = it.get("status") or it.get("state") or it.get("value")
                        if kk and ss:
                            out[str(kk)] = str(ss)
                return out
    if isinstance(payload, list):
        out = {}
        for it in payload:
            if isinstance(it, dict):
                kk = it.get("id") or it.get("objectId") or it.get("label")
                ss = it.get("status") or it.get("state") or it.get("value")
                if kk and ss:
                    out[str(kk)] = str(ss)
        return out
    return {}


# ════════════════════════════════════════════════════════════════════════
# Legacy seat selection (kept for backwards compat with the booking flow)
# ════════════════════════════════════════════════════════════════════════
def _seat_no(v: Any) -> Optional[int]:
    if v is None:
        return None
    s = str(v)
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def _deep_find_value(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            found = _deep_find_value(v, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_value(v, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def _extract_objects(rendering_info: Any) -> list[dict[str, Any]]:
    if isinstance(rendering_info, dict):
        for key in ("objects", "items", "selectableObjects", "renderableObjects"):
            val = rendering_info.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
        for v in rendering_info.values():
            got = _extract_objects(v)
            if got:
                return got
    elif isinstance(rendering_info, list):
        if rendering_info and isinstance(rendering_info[0], dict):
            if any((("id" in x or "objectId" in x) for x in rendering_info if isinstance(x, dict))):
                return rendering_info
        for v in rendering_info:
            got = _extract_objects(v)
            if got:
                return got
    return []


def pick_adjacent_from_snapshot(
    rendering_info: Any,
    statuses: dict[str, str],
    quantity: int,
    target_blocks: Optional[list[str]] = None,
) -> list[str]:
    """Legacy adjacent picker (still used by smoke_test + drop_watcher)."""
    target_blocks = [b.strip().lower() for b in (target_blocks or []) if b.strip()]
    objects = _extract_objects(rendering_info)
    free: list[dict[str, Any]] = []

    for obj in objects:
        oid = obj.get("id") or obj.get("objectId")
        labels = obj.get("labels") or {}
        label = (
            _deep_find_value(obj, {"displayedLabel", "label", "labelText"})
            or labels.get("displayedLabel") or oid
        )
        status = statuses.get(str(label)) or statuses.get(str(oid)) or "free"
        if str(status).lower() not in {"free", "available", "not_booked"}:
            continue
        category = obj.get("category") or obj.get("categoryKey") or obj.get("ticketType") or ""
        section = labels.get("section") or obj.get("section") or category or ""
        row = labels.get("parent") or obj.get("row") or ""
        seat = labels.get("own") or obj.get("seat") or obj.get("seatNumber") or ""
        section_norm = str(section).strip().lower()
        cat_norm = str(category).strip().lower()
        if target_blocks and section_norm not in target_blocks and cat_norm not in target_blocks:
            continue
        free.append({
            "id": str(oid or label),
            "label": str(label),
            "section": str(section),
            "row": str(row),
            "seat": str(seat),
            "seat_no": _seat_no(seat) or _seat_no(label),
        })

    if len(free) < quantity:
        return []

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in free:
        key = f"{item['section']}|{item['row']}"
        grouped.setdefault(key, []).append(item)

    for arr in grouped.values():
        arr.sort(key=lambda x: (x["seat_no"] is None, x["seat_no"] or 10**9, x["label"]))
        if len(arr) < quantity:
            continue
        for i in range(0, len(arr) - quantity + 1):
            window = arr[i:i + quantity]
            nums = [w["seat_no"] for w in window]
            if all(n is not None for n in nums) and all(nums[j] == nums[j - 1] + 1 for j in range(1, len(nums))):
                return [w["id"] for w in window]
        return [w["id"] for w in arr[:quantity]]

    return [w["id"] for w in free[:quantity]]


# ════════════════════════════════════════════════════════════════════════
# SeatsioClient
# ════════════════════════════════════════════════════════════════════════
class SeatsioClient:
    """Unified client.

    Usage:
        async with SeatsioClient(event_key,
                                  workspace_key=...,
                                  chart_key=...,
                                  provider="seats_planner",
                                  hold_token=...) as c:
            ri = await c.rendering_info()
            statuses = await c.object_statuses()
    """

    def __init__(self, event_key: str, *,
                 workspace_key: str = "",
                 chart_key: str = "",
                 provider: str = "",
                 hold_token: str = "",
                 turnstile_token: str = ""):
        self.event_key = event_key
        self.workspace_key = workspace_key or ""
        self.chart_key = chart_key or ""
        self.provider = (provider or "").lower()
        self.hold_token = hold_token or ""
        self.hold_token_expires = 0.0
        # v8: chart-layer Turnstile token — some webook deployments require
        # one even when fetching the chart map (not just the hold-token).
        self.turnstile_token = turnstile_token or ""
        # V15-final: session is now a StealthClient (curl_cffi).
        self.session: Optional[StealthClient] = None
        self._ws_task: Optional[asyncio.Task] = None
        # Cached normalised payload
        self._cached_event: dict = {}
        self._cached_map: dict = {}
        self._cached_ri: dict = {}

    def set_turnstile(self, token: str) -> None:
        """Inject a Turnstile token to use for subsequent chart requests."""
        self.turnstile_token = token or ""

    async def __aenter__(self):
        # Resolve workspace_key if missing — pull from the cached frontend tokens
        if not self.workspace_key:
            try:
                tokens = await ensure_tokens()
                self.workspace_key = tokens.get("workspace_key") or ""
            except Exception:
                pass
        # V15-final: stealth session per client instance (curl_cffi)
        self.session = StealthClient(
            fingerprint_seed=f"sio:{self.event_key[:16]}",
            timeout=20.0,
        )
        await self.session._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._ws_task:
            self._ws_task.cancel()
        if self.session:
            await self.session.close()

    # ── HTTP helpers (V15-final — curl_cffi) ──
    async def _get(self, url: str, *, headers: dict, timeout: int = 15) -> tuple[int, Any]:
        try:
            r = await self.session.request(
                "GET", url, headers=headers, timeout=float(timeout),
            )
            try:
                d = r.json()
            except Exception:
                try:
                    d = {"raw": (r.text or "")[:500]}
                except Exception:
                    d = None
            return r.status_code, d
        except Exception as e:
            log.debug(f"GET {url[:120]} → {e}")
            return 0, {"error": str(e)[:200]}

    async def _post(self, url: str, *, headers: dict, body: dict,
                    timeout: int = 15) -> tuple[int, Any]:
        try:
            r = await self.session.request(
                "POST", url, headers=headers, json=body,
                timeout=float(timeout),
            )
            try:
                d = r.json()
            except Exception:
                try:
                    d = {"raw": (r.text or "")[:500]}
                except Exception:
                    d = None
            return r.status_code, d
        except Exception as e:
            log.debug(f"POST {url[:120]} → {e}")
            return 0, {"error": str(e)[:200]}

    # ── Modern API (seats_planner) ──
    async def fetch_event(self) -> dict:
        if not self.workspace_key:
            return {}
        url = (f"{SEATCLOUD_API}/api/v2/{self.workspace_key}/event/"
               f"{self.event_key}?trace_id={_trace_id()}&plain=true")
        st, d = await self._get(
            url,
            headers=_chart_headers(self.hold_token, self.turnstile_token),
        )
        if st == 200 and isinstance(d, dict):
            self._cached_event = d
            return d
        if st in (401, 403):
            log.debug(f"fetch_event blocked status={st} for {self.event_key[:8]}")
        return {}

    async def fetch_map(self) -> dict:
        if not self.workspace_key or not self.chart_key:
            return {}
        url = (f"{SEATCLOUD_API}/api/v2/{self.workspace_key}/map/"
               f"{self.chart_key}/data?trace_id={_trace_id()}&plain=true")
        st, d = await self._get(
            url,
            headers=_chart_headers(self.hold_token, self.turnstile_token),
            timeout=20,
        )
        if st == 200 and isinstance(d, dict):
            self._cached_map = d
            return d
        if st in (401, 403):
            log.debug(f"fetch_map blocked status={st} for {self.chart_key[:8]}")
        return {}

    async def fetch_item_statuses(self) -> dict[str, str]:
        if not self.workspace_key:
            return {}
        url = (f"{SEATCLOUD_API}/api/v2/{self.workspace_key}/event/"
               f"{self.event_key}/items?trace_id={_trace_id()}&plain=true")
        if self.hold_token:
            url += f"&hold_token={self.hold_token}"
        st, d = await self._get(
            url,
            headers=_chart_headers(self.hold_token, self.turnstile_token),
        )
        # 204 = empty, no items held → all free
        if st in (200, 204):
            return _coerce_statuses(d)
        return {}

    # ── Public API used by the rest of the bot (rendering_info shape) ──
    async def rendering_info(self) -> dict:
        """Returns rendering_info-like dict (objects[], categories[]).

        For seats_planner: synthesises from /event + /map.
        For legacy seatsio: tries the old /system/public path (kept best-effort).
        Returns {} on hard failure (the caller should NOT interpret an empty
        dict as 'chart full' — it means the API was unreachable).
        """
        # Modern path first (works for seats_planner AND many legacy events)
        if self.workspace_key and self.chart_key:
            ev = await self.fetch_event()
            mp = await self.fetch_map()
            if ev and mp:
                statuses = await self.fetch_item_statuses()
                ri = normalize_seats_planner_to_rendering_info(ev, mp, statuses)
                self._cached_ri = ri
                return ri
            # If we reached here, the v2 endpoint failed — fall through to legacy
            log.debug(f"v2 path failed for {self.event_key} (ev={bool(ev)} mp={bool(mp)})")

        # Legacy path — only attempt with workspace_key
        if not self.workspace_key:
            return {}
        url = (f"{SEATSIO_LEGACY_API}/system/public/{self.workspace_key}/"
               f"rendering-info?event_key={self.event_key}")
        st, d = await self._get(url, headers=_legacy_headers(self.hold_token))
        if st == 200 and isinstance(d, dict) and "raw" not in d:
            self._cached_ri = d
            return d
        return {}

    async def object_statuses(self) -> dict[str, str]:
        """Unified status getter — modern first, legacy fallback."""
        if self.workspace_key and self.chart_key:
            sts = await self.fetch_item_statuses()
            if sts is not None:
                return sts
        # Legacy
        if self.workspace_key:
            url = (f"{SEATSIO_LEGACY_API}/system/public/{self.workspace_key}/"
                   f"events/object-statuses?event_key={self.event_key}")
            st, d = await self._get(url, headers=_legacy_headers(self.hold_token))
            if st == 200:
                return _coerce_statuses(d if isinstance(d, dict) else {})
        return {}

    # ── Hold-token + book + release ──
    async def init_hold_token(self) -> str:
        """Init via legacy POST /hold-tokens (used when webook didn't issue one)."""
        if self.hold_token:
            return self.hold_token
        if not self.workspace_key:
            raise RuntimeError("workspace_key not set")
        # Modern team-scoped endpoint
        url = (f"{SEATCLOUD_API}/api/v1/team/{self.workspace_key}/"
               f"hold-tokens/public/generate?trace_id={_trace_id()}")
        st, d = await self._post(url, headers=_chart_headers(), body={})
        token = ""
        if isinstance(d, dict):
            token = (d.get("data") or {}).get("token") or d.get("token") or d.get("holdToken") or ""
        if not token:
            # Legacy fallback
            url2 = f"{SEATSIO_LEGACY_API}/system/public/{self.workspace_key}/hold-tokens"
            st, d = await self._post(url2, headers=_legacy_headers(), body={})
            if isinstance(d, dict):
                token = d.get("holdToken") or d.get("token") or ""
        if not token:
            raise RuntimeError(f"could not obtain hold-token: status={st}")
        self.hold_token = token
        self.hold_token_expires = time.time() + 900
        return token

    async def hold_objects(self, object_ids: list[str], ticket_type: str = "") -> Any:
        """Best-effort hold via the legacy adapter — most webook charts still
        accept this path. For seats_planner the actual booking goes through
        webook's /add-to-cart with selected_seats, so this is only used as a
        defensive pre-hold step."""
        if not self.hold_token:
            await self.init_hold_token()
        if not self.workspace_key:
            return {"errors": ["no workspace_key"]}
        body = {
            "holdToken": self.hold_token,
            "objects": [
                ({"objectId": oid, "ticketType": ticket_type} if ticket_type
                 else {"objectId": oid})
                for oid in object_ids
            ],
        }
        url = (f"{SEATSIO_LEGACY_API}/adapter/sio/system/public/"
               f"{self.workspace_key}/events/{self.event_key}/actions/hold")
        st, d = await self._post(url, headers=_legacy_headers(self.hold_token), body=body)
        return d if isinstance(d, dict) else {"raw": d, "status": st}

    async def release_objects(self, object_ids: list[str]) -> Any:
        if not self.hold_token or not self.workspace_key:
            return {}
        body = {
            "holdToken": self.hold_token,
            "objects": [{"objectId": oid} for oid in object_ids],
        }
        url = (f"{SEATSIO_LEGACY_API}/adapter/sio/system/public/"
               f"{self.workspace_key}/events/{self.event_key}/actions/release")
        st, d = await self._post(url, headers=_legacy_headers(self.hold_token), body=body)
        return d if isinstance(d, dict) else {"raw": d, "status": st}

    async def pick_and_hold_adjacent(
        self,
        quantity: int,
        *,
        target_blocks: Optional[list[str]] = None,
        ticket_type: str = "",
        rendering_info: Any = None,
        statuses: Optional[dict[str, str]] = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Backwards-compat helper used by drop_watcher."""
        rendering = rendering_info if rendering_info is not None else await self.rendering_info()
        sts = statuses if statuses is not None else await self.object_statuses()
        object_ids = pick_adjacent_from_snapshot(
            rendering, sts, quantity, target_blocks=target_blocks,
        )
        if not object_ids:
            return [], {"rendering_info": rendering, "statuses": sts}
        try:
            await self.init_hold_token()
            await self.hold_objects(object_ids, ticket_type=ticket_type)
        except Exception as e:
            log.debug(f"pick_and_hold soft-fail: {e}")
        return object_ids, {"rendering_info": rendering, "statuses": sts}

    # ── WebSocket (drop watcher) ──
    async def watch_changes(self, on_change):
        """Subscribe to seat-status changes. seats_planner uses a different
        WS shape; we implement the legacy one here. drop_watcher will fall
        back to polling if WS is unsupported."""
        if not self.workspace_key:
            return
        if not self.hold_token:
            try:
                await self.init_hold_token()
            except Exception:
                return
        ws_url = (
            f"wss://api.seatcloud.com/system/public/{self.workspace_key}/events/"
            f"{self.event_key}/changes/socket?holdToken={self.hold_token}"
        )

        async def _runner():
            while True:
                try:
                    async with self.session.ws_connect(ws_url, heartbeat=25) as ws:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    await on_change(json.loads(msg.data))
                                except Exception:
                                    pass
                            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                                break
                except Exception as e:
                    log.debug(f"SeatCloud WS reconnect: {e}")
                    await asyncio.sleep(1.5)

        self._ws_task = asyncio.create_task(_runner())
