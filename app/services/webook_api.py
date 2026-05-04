"""
Webook.com REST API client — HTTP only (no browser).

Validated against live webook.com traffic:
  • GET  /api/v2/event-detail/{slug}       -> rich event metadata (title, ...)
  • GET  /api/v2/event-ticket-details/{slug}  -> ticket categories & prices
  • GET  /api/v2/currencies                -> currency list
  • POST /api/v2/login                     -> {email,password,captcha,lang}

Booking endpoints: Webook's booking API is not publicly documented. The
non-seated flow relies on UI interactions to create a cart, and the seated
flow integrates with seats.io. Until we reverse-engineer the exact payload
shape, live bookings are executed through Playwright (see
booking_orchestrator.py).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import aiohttp

from app.core.config import WEBOOK_API, WEBOOK_LANG, WEBOOK_PUBLIC_TOKEN

log = logging.getLogger("webook_api")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": DEFAULT_UA,
    "origin": "https://webook.com",
    "referer": "https://webook.com/",
}


def _headers(bearer: Optional[str] = None,
             lang: Optional[str] = None) -> dict[str, str]:
    h = dict(BASE_HEADERS)
    h["token"] = WEBOOK_PUBLIC_TOKEN
    h["authorization"] = f"Bearer {bearer}" if bearer else "Bearer"
    h["accept-language"] = lang or WEBOOK_LANG
    return h


async def _json(session: aiohttp.ClientSession, method: str, url: str,
                *, bearer: Optional[str] = None,
                json_body: Optional[dict] = None,
                timeout: int = 15,
                lang: Optional[str] = None) -> tuple[int, Any]:
    try:
        async with session.request(
            method, url,
            headers=_headers(bearer, lang),
            json=json_body,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            text = await r.text()
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"raw": text[:600]}
            return r.status, data
    except Exception as e:
        log.debug(f"HTTP {method} {url} -> {e}")
        return 0, {"error": str(e)}


# ════════════════════════════════════════════════════════════════════════
# Public endpoints
# ════════════════════════════════════════════════════════════════════════
async def get_event_detail(slug: str,
                           lang: Optional[str] = None,
                           bearer: Optional[str] = None
                           ) -> Optional[dict[str, Any]]:
    async with aiohttp.ClientSession() as s:
        status, data = await _json(
            s, "GET",
            f"{WEBOOK_API}/event-detail/{slug}"
            f"?lang={lang or WEBOOK_LANG}&visible_in=rs",
            bearer=bearer,
            lang=lang,
        )
    if status == 200 and isinstance(data, dict):
        return data.get("data")
    return None


async def get_event_tickets(slug: str,
                            lang: Optional[str] = None,
                            bearer: Optional[str] = None) -> dict[str, Any]:
    """
    Returns:
      {"event": {...}, "tickets": [normalised dicts], "is_seated": bool}
    """
    async with aiohttp.ClientSession() as s:
        status, data = await _json(
            s, "GET",
            f"{WEBOOK_API}/event-ticket-details/{slug}"
            f"?lang={lang or WEBOOK_LANG}&visible_in=rs&page=1",
            bearer=bearer,
            lang=lang,
        )
    if status != 200 or not isinstance(data, dict):
        return {}

    payload = data.get("data") or {}
    event_meta = payload.get("event") or {}
    raw_tickets = payload.get("event_ticket") or []

    return {
        "event": event_meta,
        "tickets": [_normalize_ticket(t) for t in raw_tickets],
        "is_seated": bool(event_meta.get("is_seated")),
    }


# ════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════
def _f(v, default: float = 0.0) -> float:
    """Cast possibly-string numbers to float safely."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_ticket(t: dict) -> dict:
    """Normalise a raw webook ticket, exposing a meaningful display price.

    Webook quotes ``price`` as the NET amount (excl. VAT). The total
    charged to the user on webook.com is ``price + vat`` — that's what we
    expose as ``price_with_vat`` and ``display_price``.

    For subscription-gated tickets where ``price`` is zero, fall back to
    ``subscription_ticket_type.price`` (if present).
    """
    price_net = _f(t.get("price"))
    original_price = _f(t.get("original_price"))
    vat = _f(t.get("vat"))
    original_price_vat = _f(t.get("original_price_vat"))
    price_with_vat = price_net + vat
    original_with_vat = original_price + original_price_vat

    sub = t.get("subscription_ticket_type") or {}
    sub_price = 0.0
    if isinstance(sub, dict):
        sub_price = _f(sub.get("price"))

    display_price = next(
        (p for p in (price_with_vat, price_net,
                     original_with_vat, original_price, sub_price) if p > 0),
        0.0,
    )

    return {
        "id": t.get("_id"),
        "title": t.get("title") or "",
        "description": _strip_html(t.get("description") or ""),
        "price": price_net,
        "price_with_vat": price_with_vat,
        "original_price": original_price,
        "original_price_with_vat": original_with_vat,
        "vat": vat,
        "display_price": display_price,
        "currency": t.get("currency") or "SAR",
        "min_per_order": max(1, int(_f(t.get("min_per_order"), 1))),
        "max_per_order": max(1, int(_f(t.get("max_per_order"), 10))),
        "sale_status": t.get("sale_status"),
        "status": t.get("status"),
        "quantity": t.get("quantity"),
        "seats_io_category": t.get("seats_io_category") or "",
        "group_name": t.get("group_name") or "",
        "ticket_color": t.get("ticket_color") or "",
        "start_sale_date": t.get("start_sale_date"),
        "end_sale_date": t.get("end_sale_date"),
        "requires_subscription": (
            display_price == 0 and bool(sub)
        ),
    }


def _strip_html(s: str) -> str:
    import re
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&nbsp;|&#160;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:500]


def _find_paytabs_url(obj: Any) -> Optional[str]:
    """Recursively look for a secure-webook.paytabs.com URL anywhere in a
    JSON-serialisable structure."""
    import re
    pat = re.compile(r"https?://[^\s\"']*paytabs[^\s\"']+", re.I)
    try:
        m = pat.search(json.dumps(obj, ensure_ascii=False))
        return m.group(0) if m else None
    except Exception:
        return None
