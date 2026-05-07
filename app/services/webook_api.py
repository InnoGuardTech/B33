"""
Webook.com REST API client — V15-final, curl_cffi (StealthClient).

Migrated from aiohttp to the V14.1 StealthClient so the TLS/JA3 + HTTP/2
fingerprint matches a real Chrome and Cloudflare's WAF stops returning
silent 403s on event-detail / ticket-details.

Validated against live webook.com traffic:
  • GET  /api/v2/event-detail/{slug}              → rich event metadata
  • GET  /api/v2/event-ticket-details/{slug}      → ticket categories & prices
  • GET  /api/v2/currencies                       → currency list
  • POST /api/v2/login                            → see login_robust.py

Booking endpoints: see booking_orchestrator.py / booking_http.py.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.core.config import WEBOOK_API, WEBOOK_LANG, WEBOOK_PUBLIC_TOKEN
from app.services.login_robust import resolve_public_token
from app.services.stealth_client import StealthClient

log = logging.getLogger("webook_api")

# V15-final: kept as a module-level constant so legacy callers that import
# `BASE_HEADERS` (e.g. event_discovery.py) keep working. The actual hot-path
# headers are built per-request by `_headers()` below, which honours the
# V15.1 builtin-fallback public token.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

BASE_HEADERS: dict[str, str] = {
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": DEFAULT_UA,
    "origin": "https://webook.com",
    "referer": "https://webook.com/",
}


def _resolved_token() -> str:
    """Resolve the public 'token' header value at call time.

    Honours WEBOOK_PUBLIC_TOKEN_BUILTIN_FALLBACK so /event-detail and
    /event-ticket-details unblock automatically when the env var is
    missing or holds the read-only 32-char token.
    """
    return resolve_public_token(WEBOOK_PUBLIC_TOKEN or "")


def _headers(bearer: Optional[str] = None,
             lang: Optional[str] = None) -> dict[str, str]:
    h = dict(BASE_HEADERS)
    h["token"] = _resolved_token()
    h["authorization"] = f"Bearer {bearer}" if bearer else "Bearer"
    h["accept-language"] = lang or WEBOOK_LANG
    h["sec-fetch-mode"] = "cors"
    h["sec-fetch-site"] = "same-site"
    return h


async def _json(method: str, url: str,
                *, bearer: Optional[str] = None,
                json_body: Optional[dict] = None,
                timeout: float = 15.0,
                lang: Optional[str] = None,
                proxy_url: Optional[str] = None,
                fingerprint_seed: Optional[str] = None,
                ) -> tuple[int, Any]:
    """Single-request helper — opens a fresh StealthClient per call.

    For high-throughput hot paths the caller should prefer creating one
    StealthClient and calling .request() repeatedly. This helper is for
    one-shot reads (event-detail, event-ticket-details, currencies, …).
    """
    try:
        async with StealthClient(
            proxy_url=proxy_url,
            fingerprint_seed=fingerprint_seed,
            timeout=timeout,
        ) as cli:
            r = await cli.request(
                method.upper(), url,
                headers=_headers(bearer, lang),
                json=json_body,
            )
            try:
                data = r.json()
            except Exception:
                try:
                    data = {"raw": (r.text or "")[:600]}
                except Exception:
                    data = {"raw": ""}
            return r.status_code, data
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
    status, data = await _json(
        "GET",
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
    """Returns:
        {"event": {...}, "tickets": [normalised dicts], "is_seated": bool}
    """
    status, data = await _json(
        "GET",
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
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_ticket(t: dict) -> dict:
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
