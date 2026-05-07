"""
V15 — PHASE 4: Checkout / Loot Relay.

Once a worker (PHASE 3) has secured a hold-token, this module immediately
POSTs to Webook's /checkout endpoint to finalise the order and obtain
the EXTERNAL Payment Gateway URL (PayTabs / Apple Pay / STC Pay / Mada).
That URL is then relayed to the user via Telegram so they can complete
payment in-app — no need for the bot to handle card data itself.

Webook checkout flow (reverse-engineered)
-----------------------------------------
  POST /api/v2/event-detail/<slug>/checkout?lang=en
    body: {
      "event_id":   "<event_id>",
      "hold_token": "<hold_token from PHASE 3>",
      "tickets":    [{"category_key": "...", "quantity": N, ...}],
      "payment":    "credit_card" | "apple_pay" | "stc_pay" | "mada",
      "lang":       "en"
    }
  → 200 { "data": { "redirect_url": "https://secure.paytabs.sa/payment/...",
                    "order_id": "...", "amount": ..., "currency": "SAR" } }

Sometimes Webook returns the payment URL under a different key:
  - data.payment_url
  - data.checkout_url
  - data.payment.url
  - data.payment.redirect
  - data.url

This module's `extract_payment_url()` handles all of them.

Public API
----------
    res = await create_checkout(
        slug=..., event_id=..., bearer=...,
        hold_token=..., tickets=[...], payment="credit_card",
    )
    if res.payment_url:
        await notifier.send(chat_id, format_telegram_alert(res))

Self-test
---------
    python -m app.services.checkout_handler
    Tests `extract_payment_url()` against ~10 dummy JSON shapes that
    cover every observed Webook response format.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("checkout_handler")


# ════════════════════════════════════════════════════════════════════════
# Data classes
# ════════════════════════════════════════════════════════════════════════
@dataclass
class CheckoutResult:
    """Outcome of a single /checkout POST."""
    status: int                          # HTTP status (-1 on transport err)
    payment_url: Optional[str]           # external gateway URL for the user
    order_id: Optional[str]              # Webook order reference
    amount: Optional[float]              # total amount
    currency: Optional[str]              # 'SAR' / 'USD' / …
    method: Optional[str]                # 'credit_card', 'apple_pay', …
    expires_at: Optional[int]            # unix ts when the gateway link dies
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.payment_url)


# ════════════════════════════════════════════════════════════════════════
# Payment-URL extractor (the regex/parsing part the user asked us to test)
# ════════════════════════════════════════════════════════════════════════
# Keys that Webook (or its payment gateway proxy) has been observed to use.
_URL_KEYS: tuple[str, ...] = (
    "redirect_url",
    "redirectUrl",
    "payment_url",
    "paymentUrl",
    "checkout_url",
    "checkoutUrl",
    "url",
    "redirect",
    "payment_link",
    "paymentLink",
    "gateway_url",
)

# Containers that may wrap the URL one level deeper.
_NESTED_KEYS: tuple[str, ...] = (
    "data", "payment", "result", "checkout",
    "transaction", "order", "response",
)

# Trusted gateway hosts — anything else is rejected to avoid open-redirects.
_TRUSTED_HOSTS_RE = re.compile(
    r"^https://(?:"
    r"[a-z0-9.\-]*\.paytabs\.(?:com|sa)"        # PayTabs SA / global
    r"|secure\.paytabs\.com"
    r"|secure\.paytabs\.sa"
    r"|payments?\.checkout\.com"                # Checkout.com
    r"|api\.checkout\.com"
    r"|hpp\.checkout\.com"
    r"|.*\.stcpay\.com\.sa"                     # STC Pay
    r"|.*\.mada\.com\.sa"                       # Mada
    r"|.*\.applepay\.com"                       # Apple Pay (rare)
    r"|.*\.tap\.company"                        # Tap Payments
    r"|.*\.moyasar\.com"                        # Moyasar
    r"|.*\.hyperpay\.com"                       # HyperPay
    r"|.*\.payfort\.com"                        # PayFort / Amazon Payment Services
    r"|.*\.applepayentrust\.com"
    r"|webook\.com|.*\.webook\.com"             # webook self-hosted gateway
    r")(?:[/:?#].*)?$",
    flags=re.IGNORECASE,
)


def extract_payment_url(payload: Any) -> Optional[str]:
    """Walk a /checkout JSON response and return the external payment URL.

    Tolerates the many shapes Webook serves (snake_case vs camelCase,
    flat vs nested under data/payment/result, etc.). Rejects URLs that
    don't point to a known payment gateway or webook itself, to avoid
    open-redirect vulnerabilities.
    """
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload if _looks_like_gateway_url(payload) else None
    if not isinstance(payload, dict):
        return None

    # 1. Direct hit at this level.
    for k in _URL_KEYS:
        v = payload.get(k)
        if isinstance(v, str) and _looks_like_gateway_url(v):
            return v

    # 2. Recurse into well-known nested containers.
    for k in _NESTED_KEYS:
        v = payload.get(k)
        sub = extract_payment_url(v)
        if sub:
            return sub

    # 3. Last-ditch: scan any string value for a URL pattern.
    for v in payload.values():
        if isinstance(v, str) and _looks_like_gateway_url(v):
            return v
    return None


def _looks_like_gateway_url(s: str) -> bool:
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not s.startswith("https://"):
        return False
    if " " in s or len(s) > 2048:
        return False
    return bool(_TRUSTED_HOSTS_RE.match(s))


# ════════════════════════════════════════════════════════════════════════
# Field extractors for the rest of the response
# ════════════════════════════════════════════════════════════════════════
def _walk_for(payload: Any, keys: tuple[str, ...]) -> Optional[Any]:
    if not isinstance(payload, dict):
        return None
    for k in keys:
        v = payload.get(k)
        if v not in (None, ""):
            return v
    for k in _NESTED_KEYS:
        v = payload.get(k)
        sub = _walk_for(v, keys)
        if sub is not None:
            return sub
    return None


def _to_float(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", ""))
        except Exception:
            return None
    return None


def _to_int(v: Any) -> Optional[int]:
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v))
        except Exception:
            return None
    return None


def parse_checkout_response(payload: Any) -> dict[str, Any]:
    """Reduce the messy /checkout JSON into a flat dict of useful fields."""
    return {
        "payment_url": extract_payment_url(payload),
        "order_id": _walk_for(payload, ("order_id", "orderId",
                                          "reference", "ref", "id")),
        "amount": _to_float(_walk_for(payload, ("amount", "total",
                                                  "grand_total", "value"))),
        "currency": _walk_for(payload, ("currency", "currency_code", "ccy")),
        "method": _walk_for(payload, ("payment_method", "method",
                                        "paymentMethod", "channel")),
        "expires_at": _to_int(_walk_for(payload, ("expires_at", "expiresAt",
                                                     "expiry"))),
    }


# ════════════════════════════════════════════════════════════════════════
# Live POST to /checkout (production path)
# ════════════════════════════════════════════════════════════════════════
async def create_checkout(
    *,
    slug: str,
    event_id: str,
    bearer: str,
    hold_token: str,
    tickets: list[dict[str, Any]],
    payment: str = "credit_card",
    proxy_url: Optional[str] = None,
    fingerprint_seed: Optional[str] = None,
    turnstile: str = "",
    lang: str = "en",
    timeout: float = 15.0,
) -> CheckoutResult:
    """POST /api/v2/event-detail/<slug>/checkout — returns CheckoutResult.

    The caller (booking_orchestrator) feeds in the hold-token won by the
    worker pool and the ticket selection. This function is intentionally
    transport-agnostic: it uses the V14.1 curl_cffi StealthClient so the
    request bypasses Cloudflare exactly like the worker pool does.
    """
    from app.services.stealth_client import StealthClient

    if not (slug and event_id and bearer and hold_token and tickets):
        return CheckoutResult(
            status=-1, payment_url=None, order_id=None, amount=None,
            currency=None, method=None, expires_at=None,
            error="missing required argument",
        )

    url = (
        "https://api.webook.com/api/v2/event-detail/"
        f"{slug}/checkout?lang={lang}"
    )
    body: dict[str, Any] = {
        "event_id":   event_id,
        "hold_token": hold_token,
        "tickets":    tickets,
        "payment":    payment,
        "lang":       lang,
    }
    if turnstile:
        body["turnstile"] = turnstile

    headers = {
        "authorization": f"Bearer {bearer}",
        "content-type": "application/json",
    }
    try:
        from app.core.config import webook_public_token
        tok = webook_public_token()
        if tok:
            headers["token"] = tok
    except Exception:
        pass

    t0 = time.perf_counter()
    try:
        async with StealthClient(
            proxy_url=proxy_url,
            fingerprint_seed=fingerprint_seed,
            timeout=timeout,
        ) as cli:
            r = await cli.request("POST", url, headers=headers, json=body)
            elapsed = (time.perf_counter() - t0) * 1000
            try:
                data = r.json()
            except Exception:
                data = {"raw": (r.text or "")[:500]}
            parsed = parse_checkout_response(data)
            return CheckoutResult(
                status=r.status_code,
                payment_url=parsed["payment_url"],
                order_id=str(parsed["order_id"]) if parsed["order_id"] else None,
                amount=parsed["amount"],
                currency=str(parsed["currency"]) if parsed["currency"] else None,
                method=str(parsed["method"]) if parsed["method"] else None,
                expires_at=parsed["expires_at"],
                elapsed_ms=elapsed,
                raw=data if isinstance(data, dict) else {"raw": str(data)[:500]},
            )
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        return CheckoutResult(
            status=-1, payment_url=None, order_id=None, amount=None,
            currency=None, method=None, expires_at=None,
            elapsed_ms=elapsed, error=f"{type(e).__name__}: {e}",
        )


# ════════════════════════════════════════════════════════════════════════
# Telegram alert formatter
# ════════════════════════════════════════════════════════════════════════
def format_telegram_alert(
    res: CheckoutResult,
    *,
    event_title: str = "",
    seat_label: str = "",
) -> str:
    """Format a Telegram HTML alert with the payment link & big ALERT vibe."""
    if not res.ok:
        return (
            "❌ <b>فشل إنشاء الدفع</b>\n"
            f"الحالة: <code>{res.status}</code>\n"
            f"الخطأ: <code>{res.error or 'unknown'}</code>"
        )
    lines = [
        "🎯 <b>تم تأمين المقعد — أكمل الدفع الآن!</b>",
        "═══════════════════════════════════",
    ]
    if event_title:
        lines.append(f"🎫 الفعالية: <b>{event_title}</b>")
    if seat_label:
        lines.append(f"💺 المقعد: <code>{seat_label}</code>")
    if res.amount and res.currency:
        lines.append(f"💰 المبلغ: <b>{res.amount:g} {res.currency}</b>")
    if res.method:
        lines.append(f"💳 طريقة الدفع: {res.method}")
    if res.order_id:
        lines.append(f"🔢 رقم الطلب: <code>{res.order_id}</code>")
    lines.extend([
        "═══════════════════════════════════",
        f'👉 <a href="{res.payment_url}">اضغط هنا لإكمال الدفع</a>',
        "⚠️ الرابط ينتهي خلال دقائق — أكمل الدفع فوراً!",
    ])
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
# Self-test (parsing fixtures)
# ════════════════════════════════════════════════════════════════════════
def _self_test_parsing() -> int:
    """Verify extract_payment_url + parse_checkout_response on dummy JSON."""
    cases: list[tuple[str, dict, Optional[str]]] = [
        # 1. Canonical webook shape
        ("flat snake",
         {"status": "success",
          "data": {"redirect_url": "https://secure.paytabs.sa/payment/abc",
                   "order_id": "WBK-123", "amount": 350, "currency": "SAR"}},
         "https://secure.paytabs.sa/payment/abc"),
        # 2. camelCase variant
        ("flat camel",
         {"data": {"paymentUrl": "https://secure.paytabs.sa/p/xyz"}},
         "https://secure.paytabs.sa/p/xyz"),
        # 3. Nested under .payment
        ("nested payment.url",
         {"data": {"payment": {"url": "https://payments.checkout.com/page/123"}}},
         "https://payments.checkout.com/page/123"),
        # 4. Nested under .payment.redirect
        ("nested payment.redirect",
         {"data": {"payment": {"redirect": "https://api.tap.company/pay/abc"}}},
         "https://api.tap.company/pay/abc"),
        # 5. Direct top-level URL string (rare gateway echo)
        ("top-level url",
         {"url": "https://hpp.checkout.com/sess/789"},
         "https://hpp.checkout.com/sess/789"),
        # 6. checkout_url variant
        ("checkout_url",
         {"data": {"checkout_url": "https://secure.paytabs.com/c/AAA"}},
         "https://secure.paytabs.com/c/AAA"),
        # 7. Untrusted URL must be rejected
        ("untrusted host",
         {"data": {"redirect_url": "https://evil.example.com/steal"}},
         None),
        # 8. Non-https rejected
        ("http rejected",
         {"data": {"redirect_url": "http://secure.paytabs.sa/p/abc"}},
         None),
        # 9. Empty / null
        ("empty", {}, None),
        ("null", {"data": None}, None),
        # 10. Webook self-hosted gateway
        ("webook gateway",
         {"data": {"url": "https://webook.com/gateway/pay/aaa-bbb"}},
         "https://webook.com/gateway/pay/aaa-bbb"),
        # 11. Order metadata extraction
        ("with order meta",
         {"data": {"redirect_url": "https://secure.paytabs.sa/p/aa",
                   "orderId": 99, "amount": "120.50", "currency": "SAR",
                   "payment_method": "credit_card",
                   "expires_at": 1736000000}},
         "https://secure.paytabs.sa/p/aa"),
    ]
    fails = 0
    for name, payload, expected in cases:
        got = extract_payment_url(payload)
        ok = got == expected
        mark = "✅" if ok else "❌"
        if not ok:
            fails += 1
            print(f"  {mark} {name:<25} → {got!r} (expected {expected!r})")
        else:
            disp = (got[:50] + "…") if got and len(got) > 50 else got
            print(f"  {mark} {name:<25} → {disp}")
    # Verify field extraction on the rich case
    case_rich = cases[-1][1]
    parsed = parse_checkout_response(case_rich)
    assert parsed["amount"] == 120.5, parsed
    assert parsed["currency"] == "SAR"
    assert parsed["method"] == "credit_card"
    assert parsed["order_id"] == 99
    assert parsed["expires_at"] == 1736000000
    print("  ✅ field extraction (amount/currency/order_id/expires_at) OK")
    # Verify Telegram formatter
    res = CheckoutResult(
        status=200,
        payment_url="https://secure.paytabs.sa/p/abc",
        order_id="WBK-1", amount=350, currency="SAR", method="credit_card",
        expires_at=None, elapsed_ms=120,
    )
    msg = format_telegram_alert(res, event_title="Test Event", seat_label="A1-12-5")
    assert "secure.paytabs.sa" in msg
    assert "Test Event" in msg
    assert "A1-12-5" in msg
    assert "350 SAR" in msg
    print("  ✅ format_telegram_alert produces correct HTML alert")
    print(f"\n{'🏆' if fails == 0 else '❌'} {len(cases) - fails}/{len(cases)} parsing tests passed.")
    return fails


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    print("🧪 Hydra V15 — checkout_handler self-test")
    print("=" * 70)
    rc = _self_test_parsing()
    sys.exit(0 if rc == 0 else 1)
