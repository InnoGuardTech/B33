"""
Playwright-based booking engine.

Improved strategy:
  1) Reuse the saved JWT access token whenever possible (fast path)
  2) Fall back to live login only if the booking page still redirects to /login
  3) Handle common checkout steps automatically:
       • quantity increment for normal tickets
       • best-available / adjacent seats heuristics for seated maps
       • payment method selection (Credit Card / Mada)
       • terms checkbox acceptance
  4) Return the PayTabs URL as soon as it appears in page / frames / network
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from app.core.config import (
    HEADLESS,
    WEBOOK_API,
    WEBOOK_ORIGIN,
    WEBOOK_PUBLIC_TOKEN,
    proxy_password,
    proxy_server,
    proxy_username,
    use_stealth_browser,
)

log = logging.getLogger("booking_pw")

WEBOOK_RECAPTCHA_SITE_KEY = "6LcvYHooAAAAAC-G46bpymJKtIwfDQpg9DsHPMpL"

_pw_err: Optional[Exception] = None
try:
    if use_stealth_browser():
        from patchright.async_api import async_playwright, TimeoutError as PWTimeout  # type: ignore
    else:
        raise ImportError("stealth disabled")
except Exception:
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout  # type: ignore
    except Exception as _e:  # pragma: no cover
        _pw_err = _e
        PWTimeout = Exception  # type: ignore


PRIMARY_ACTION_SELECTORS = [
    "button:has-text('Checkout')",
    "button:has-text('Continue')",
    "button:has-text('Proceed')",
    "button:has-text('Book Now')",
    "button:has-text('Pay')",
    "button:has-text('Confirm')",
    "button:has-text('Next')",
    "button:has-text('Go to Payment')",
    "button:has-text('Place Order')",
    "button:has-text('Complete')",
    "button:has-text('Agree')",
    "button:has-text('Accept')",
    "button:has-text('Continue to payment')",
    "button:has-text('Review')",
    # Arabic
    "button:has-text('المتابعة')",
    "button:has-text('متابعة')",
    "button:has-text('التالي')",
    "button:has-text('الدفع')",
    "button:has-text('ادفع')",
    "button:has-text('إتمام')",
    "button:has-text('تأكيد')",
    "button:has-text('احجز الآن')",
    "button:has-text('الذهاب للدفع')",
    "button:has-text('أوافق')",
    "button:has-text('موافق')",
    "button:has-text('قبول')",
    "a:has-text('Checkout')",
    "a:has-text('Pay')",
    "a:has-text('الدفع')",
]

BOOK_ENTRY_SELECTORS = [
    "button:has-text('Book tickets')",
    "button:has-text('Book Now')",
    "a:has-text('Book tickets')",
    "a:has-text('Book Now')",
    "button:has-text('احجز الآن')",
    "button:has-text('حجز التذاكر')",
    "a:has-text('احجز الآن')",
    "a:has-text('حجز التذاكر')",
]

PAYMENT_METHOD_TEXTS = [
    "Credit Card or Mada",
    "Credit Card",
    "Mada",
    "Card",
    "بطاقة ائتمانية أو مدى",
    "بطاقة ائتمانية",
    "مدى",
]

BEST_AVAILABLE_TEXTS = [
    "Best Available",
    "Auto Select",
    "Best Seats",
    "Select Best",
    "أفضل المقاعد",
    "أفضل المتاح",
    "اختيار تلقائي",
    "تحديد تلقائي",
]

TERMS_TEXT_HINTS = [
    "resell",
    "re-sale",
    "ticket on another platform",
    "غير نظامية",
    "إعادة بيع",
    "حظر الحساب",
    "إلغاء التذكرة",
]


async def book_via_browser(*, email: str, password: str,
                           event_slug: str, ticket_id: str,
                           quantity: int,
                           access_token: str = "",
                           user_id: str = "",
                           max_runtime: int = 120) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "payment_url": "",
        "seat_info": {},
        "final_url": "",
        "logs": [],
        "error": "",
    }
    if _pw_err is not None:
        result["error"] = f"Playwright unavailable: {_pw_err}"
        return result

    deadline = time.time() + max_runtime

    # V13: Use the Chromium singleton instead of launching a fresh browser.
    # Saves ~150MB RAM and ~3s of startup per booking. The singleton applies
    # low-RAM flags + UA/viewport/locale rotation automatically.
    from app.services.browser_pool import browser_context

    async with browser_context(label=email or "book") as ctx:
        if access_token:
            await _install_saved_auth(ctx, access_token, user_id, email)
            await ctx.set_extra_http_headers({
                "token": WEBOOK_PUBLIC_TOKEN,
                "authorization": f"Bearer {access_token}",
                "accept-language": "ar-SA",
                "origin": WEBOOK_ORIGIN,
                "referer": f"{WEBOOK_ORIGIN}/",
            })

        # V13: Inject stealth-extra HTTP headers consistent with the
        # randomized UA assigned by the browser_pool.
        try:
            await ctx.set_extra_http_headers({
                "accept-language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
                "origin": WEBOOK_ORIGIN,
                "referer": f"{WEBOOK_ORIGIN}/",
            })
        except Exception:
            pass

        page = await ctx.new_page()
        seen_paytabs: list[str] = []

        def _remember_url(u: str):
            if u and "paytabs" in u.lower() and u not in seen_paytabs:
                seen_paytabs.append(u)

        page.on("request", lambda r: _remember_url(r.url))
        page.on("response", lambda r: _remember_url(r.url))

        try:
            book_url = f"{WEBOOK_ORIGIN}/en/events/{event_slug}/book"
            public_url = f"{WEBOOK_ORIGIN}/en/events/{event_slug}"

            # Fast path: reuse saved token and skip reCAPTCHA/login entirely.
            if access_token:
                result["logs"].append("🔑 using saved token")
                await page.goto(book_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(4500)
            else:
                result["logs"].append("🔐 login required")

            # Fallback to interactive login only when webook still redirects us.
            if (not access_token) or "/login" in page.url:
                result["logs"].append("🔐 live login fallback")
                auth = await _login_with_page(page, email, password)
                if not auth.get("ok"):
                    raise RuntimeError(auth.get("error") or "login failed")
                access_token = auth.get("access_token") or access_token
                user_id = auth.get("user_id") or user_id
                result["logs"].append("✅ login ok")
                await _seed_auth_on_page(page, access_token, user_id, email)
                try:
                    await ctx.set_extra_http_headers({
                        "token": WEBOOK_PUBLIC_TOKEN,
                        "authorization": f"Bearer {access_token}",
                        "accept-language": "ar-SA",
                        "origin": WEBOOK_ORIGIN,
                        "referer": f"{WEBOOK_ORIGIN}/",
                    })
                except Exception:
                    pass
                await page.goto(book_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(5000)

            if "/login" in page.url:
                await page.goto(public_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3500)
                await _click_any_in_frames(page, BOOK_ENTRY_SELECTORS)
                await page.wait_for_timeout(5000)

            result["logs"].append(f"📄 page: {page.url}")

            # Quantity or seat selection.
            picked = await _increment_ticket(page, ticket_id, quantity, result)
            if not picked:
                picked = await _select_adjacent_seats(page, quantity, result)
            if not picked:
                # Last resort: try best-available button then continue.
                await _click_text_candidates_in_frames(page, BEST_AVAILABLE_TEXTS)
                await page.wait_for_timeout(1500)
                picked = await _select_adjacent_seats(page, quantity, result)
            if not picked:
                raise RuntimeError("تعذّر تحديد التذاكر/المقاعد تلقائياً")

            # Advance through checkout, selecting payment method + terms automatically.
            for _ in range(10):
                if time.time() > deadline:
                    break
                await _prepare_checkout(page, quantity, result)
                if await _extract_paytabs_url(page, seen_paytabs):
                    break
                clicked = await _click_any_in_frames(page, PRIMARY_ACTION_SELECTORS)
                await page.wait_for_timeout(2800 if clicked else 1200)
                if await _extract_paytabs_url(page, seen_paytabs):
                    break

            result["final_url"] = page.url
            pay_url = await _extract_paytabs_url(page, seen_paytabs)
            if pay_url:
                result["ok"] = True
                result["payment_url"] = pay_url
                result["seat_info"] = await _scrape_seat_info(page)
                result["logs"].append("💳 reached PayTabs")
            else:
                result["error"] = (
                    "لم نصل إلى PayTabs بعد. غالباً توجد خطوة إضافية خاصة بالفعالية "
                    "مثل خريطة المقاعد أو تأكيدات مخصصة."
                )
        except PWTimeout as e:
            result["error"] = f"timeout: {e}"
        except Exception as e:
            result["error"] = str(e)[:350]
        # V13: Browser singleton stays warm; only the context closes
        # automatically when the `async with browser_context(...)` block exits.

    return result


async def _install_saved_auth(ctx, access_token: str, user_id: str, email: str) -> None:
    user_data = {
        "_id": user_id or "",
        "email": email,
        "name": email.split("@")[0],
    }
    script = f"""
    (() => {{
      try {{
        const token = {json.dumps(access_token)};
        const user = {json.dumps(json.dumps(user_data))};
        localStorage.setItem('access_token', token);
        sessionStorage.setItem('access_token', token);
        localStorage.setItem('user_data', user);
        sessionStorage.setItem('user_data', user);
      }} catch (e) {{}}
    }})();
    """
    await ctx.add_init_script(script=script)


async def _seed_auth_on_page(page, access_token: str, user_id: str, email: str) -> None:
    user_data = {
        "_id": user_id or "",
        "email": email,
        "name": email.split("@")[0],
    }
    await page.evaluate(
        f"""() => {{
          try {{
            localStorage.setItem('access_token', {json.dumps(access_token)});
            sessionStorage.setItem('access_token', {json.dumps(access_token)});
            localStorage.setItem('user_data', {json.dumps(json.dumps(user_data))});
            sessionStorage.setItem('user_data', {json.dumps(json.dumps(user_data))});
          }} catch (e) {{}}
        }}"""
    )


async def _login_with_page(page, email: str, password: str) -> dict[str, Any]:
    await page.goto(f"{WEBOOK_ORIGIN}/en/login", wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)
    await _dismiss_cookies(page)

    try:
        await page.wait_for_function(
            "() => window.grecaptcha && typeof window.grecaptcha.execute === 'function'",
            timeout=45000,
        )
    except Exception:
        await page.evaluate(f"""
          () => new Promise((resolve) => {{
            if (window.grecaptcha && window.grecaptcha.execute) return resolve();
            const s = document.createElement('script');
            s.src = 'https://www.google.com/recaptcha/api.js?render={WEBOOK_RECAPTCHA_SITE_KEY}';
            s.onload = () => resolve();
            s.onerror = () => resolve();
            document.head.appendChild(s);
          }})
        """)
        await page.wait_for_function(
            "() => window.grecaptcha && typeof window.grecaptcha.execute === 'function'",
            timeout=25000,
        )

    captcha_token = await page.evaluate(f"""
      () => new Promise((resolve, reject) => {{
        grecaptcha.ready(() => {{
          grecaptcha.execute('{WEBOOK_RECAPTCHA_SITE_KEY}', {{action: 'login'}})
            .then(resolve).catch(reject);
        }});
      }})
    """)

    login_result = await page.evaluate(
        f"""async () => {{
          const r = await fetch('{WEBOOK_API}/login', {{
            method:'POST', credentials:'include',
            headers:{{
              'accept':'application/json',
              'content-type':'application/json',
              'token':'{WEBOOK_PUBLIC_TOKEN}',
              'authorization':'Bearer',
              'accept-language':'ar-SA',
            }},
            body: JSON.stringify({{
              email: {json.dumps(email)},
              password: {json.dumps(password)},
              captcha: {json.dumps(captcha_token)},
              lang: 'en',
            }})
          }});
          return {{ status: r.status, body: await r.text() }};
        }}"""
    )
    try:
        payload = json.loads(login_result.get("body") or "{}")
    except Exception:
        payload = {}
    if payload.get("status") != "success":
        err = payload.get("message") or payload.get("error") or str(login_result)
        return {"ok": False, "error": f"login failed: {err}"[:250]}

    data = payload.get("data") or {}
    return {
        "ok": True,
        "access_token": data.get("access_token") or "",
        "user_id": data.get("_id") or "",
        "user": data,
    }


async def _dismiss_cookies(page) -> None:
    for _ in range(10):
        for sel in (
            "button:has-text('Reject all')",
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "button:has-text('قبول')",
            "button:has-text('موافق')",
        ):
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(400)
                    return
            except Exception:
                pass
        await page.wait_for_timeout(300)


async def _prepare_checkout(page, quantity: int, result: dict[str, Any]) -> None:
    await _click_text_candidates_in_frames(page, BEST_AVAILABLE_TEXTS)
    await _choose_payment_method(page, result)
    await _accept_terms(page, result)


async def _choose_payment_method(page, result: dict[str, Any]) -> None:
    clicked = await _click_text_candidates_in_frames(page, PAYMENT_METHOD_TEXTS)
    if clicked:
        result["logs"].append("💳 payment method selected")


async def _accept_terms(page, result: dict[str, Any]) -> None:
    changed = False
    for target in [page, *page.frames]:
        try:
            ok = await target.evaluate(
                f"""(hints) => {{
                  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                  const hits = hints.map(norm);
                  const nodes = Array.from(document.querySelectorAll('label, div, span, p, li'));
                  for (const node of nodes) {{
                    const txt = norm(node.innerText || node.textContent || '');
                    if (!txt) continue;
                    if (!hits.some(h => txt.includes(h))) continue;
                    let box = null;
                    if (node.matches('label')) {{
                      const forId = node.getAttribute('for');
                      if (forId) box = document.getElementById(forId);
                      if (!box) box = node.querySelector('input[type="checkbox"], input[type="radio"]');
                    }}
                    if (!box) box = node.querySelector('input[type="checkbox"], input[type="radio"]');
                    if (!box && node.parentElement) box = node.parentElement.querySelector('input[type="checkbox"], input[type="radio"]');
                    if (box) {{
                      if (!box.checked) box.click();
                      return true;
                    }}
                    node.click();
                    return true;
                  }}
                  const checks = Array.from(document.querySelectorAll('input[type="checkbox"]')).filter(el => !el.checked);
                  if (checks.length === 1) {{ checks[0].click(); return true; }}
                  return false;
                }}""",
                TERMS_TEXT_HINTS,
            )
            changed = changed or bool(ok)
        except Exception:
            continue
    if changed:
        result["logs"].append("☑️ terms accepted")


async def _click_any_in_frames(page, selectors: list[str]) -> bool:
    if await _click_any(page, selectors):
        return True
    for fr in page.frames:
        try:
            if await _click_any(fr, selectors):
                return True
        except Exception:
            continue
    return False


async def _click_any(target, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            btn = await target.query_selector(sel)
            if btn:
                try:
                    vis = await btn.is_visible()
                except Exception:
                    vis = True
                enabled = True
                try:
                    enabled = await btn.is_enabled()
                except Exception:
                    pass
                if vis and enabled:
                    try:
                        await btn.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    await btn.click()
                    return True
        except Exception:
            continue
    return False


async def _click_text_candidates_in_frames(page, texts: list[str]) -> bool:
    for target in [page, *page.frames]:
        try:
            ok = await target.evaluate(
                """(texts) => {
                  const vis = el => !!(el && (el.offsetParent !== null || el.getClientRects().length));
                  const nodes = Array.from(document.querySelectorAll('button, a, label, div, span'));
                  for (const t of texts) {
                    const n = nodes.find(el => vis(el) && ((el.innerText || el.textContent || '').trim().toLowerCase().includes(t.toLowerCase())));
                    if (n) { n.click(); return true; }
                  }
                  return false;
                }""",
                texts,
            )
            if ok:
                return True
        except Exception:
            continue
    return False


async def _extract_paytabs_url(page, seen_paytabs: list[str]) -> str:
    if "paytabs" in (page.url or "").lower():
        return page.url
    for fr in page.frames:
        try:
            if "paytabs" in (fr.url or "").lower():
                return fr.url
        except Exception:
            pass
    if seen_paytabs:
        return seen_paytabs[-1]
    for target in [page, *page.frames]:
        try:
            link = await target.evaluate(
                """() => {
                  const a = Array.from(document.querySelectorAll('a[href*="paytabs"], iframe[src*="paytabs"]'))[0];
                  return a ? (a.href || a.src || '') : '';
                }"""
            )
            if link:
                return link
        except Exception:
            continue
    return ""


async def _increment_ticket(page, ticket_id: str, quantity: int,
                            result: dict[str, Any]) -> bool:
    js = f"""
      () => {{
        const rows = Array.from(document.querySelectorAll(
          '[data-ticket-id], [data-id], [data-testid*="ticket"]'));
        const row = rows.find(r =>
          (r.getAttribute('data-ticket-id') || r.getAttribute('data-id') || '')
            === {json.dumps(ticket_id)}
        );
        return row ? true : false;
      }}
    """
    try:
        has_match = await page.evaluate(js)
    except Exception:
        has_match = False

    async def _press_plus_in_row():
        return await page.evaluate(
            f"""
            () => {{
              const rows = Array.from(document.querySelectorAll(
                '[data-ticket-id], [data-id], [data-testid*="ticket"]'));
              const row = rows.find(r =>
                (r.getAttribute('data-ticket-id') || r.getAttribute('data-id') || '')
                  === {json.dumps(ticket_id)});
              if (!row) return false;
              const plus = row.querySelector(
                'button[aria-label*="increase" i], button[aria-label*="add" i],'
                + 'button[aria-label*="plus" i], button[data-testid*="increment" i]'
              );
              if (plus) {{ plus.click(); return true; }}
              const btns = Array.from(row.querySelectorAll('button'));
              const p = btns.find(b => ['+','＋'].includes((b.innerText||'').trim()));
              if (p) {{ p.click(); return true; }}
              return false;
            }}
            """
        )

    async def _press_first_plus():
        return await page.evaluate(
            """
            () => {
              const vis = Array.from(document.querySelectorAll('button'))
                .filter(b => b.offsetParent !== null || b.getClientRects().length);
              let plus = vis.find(b => ['+','＋'].includes((b.innerText||'').trim()));
              if (!plus) plus = vis.find(b => /(increase|increment|plus|add|زيادة|إضافة)/i
                .test((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '')));
              if (plus) {
                plus.scrollIntoView({block:'center'});
                plus.click();
                return true;
              }
              return false;
            }
            """
        )

    pressed = 0
    for _ in range(quantity):
        ok = False
        if has_match:
            try:
                ok = await _press_plus_in_row()
            except Exception:
                ok = False
        if not ok:
            try:
                ok = await _press_first_plus()
            except Exception:
                ok = False
        if not ok:
            break
        pressed += 1
        await page.wait_for_timeout(350)

    if pressed == 0:
        return False
    result["logs"].append(f"+{pressed} ✓")
    return True


async def _select_adjacent_seats(page, quantity: int, result: dict[str, Any]) -> bool:
    for target in [page, *page.frames]:
        try:
            picked = await target.evaluate(
                """(qty) => {
                  const vis = el => !!(el && (el.offsetParent !== null || el.getClientRects().length));
                  const bad = s => /(unavailable|booked|sold|reserved|occupied|disabled|blocked|locked|taken|غير متاح|محجوز|مباع|مغلق)/i.test(s || '');
                  const getText = el => [el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'), el.id, el.className]
                    .filter(Boolean).join(' ');
                  const candidates = Array.from(document.querySelectorAll(
                    '[data-testid*="seat"], [aria-label*="Seat"], [aria-label*="مقعد"], [title*="Seat"], [title*="مقعد"], [class*="seat"], [id*="seat"], svg [data-object-type="seat"], svg [data-testid*="seat"]'
                  )).filter(vis);
                  const items = [];
                  const click = el => {
                    try { el.scrollIntoView({block:'center'}); } catch(e) {}
                    try { el.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true,view:window})); } catch(e) {}
                    try { el.click && el.click(); } catch(e) {}
                  };
                  for (const el of candidates) {
                    const txt = getText(el);
                    if (!txt || bad(txt)) continue;
                    if (el.getAttribute('aria-disabled') === 'true') continue;
                    const section = el.getAttribute('data-section') || el.getAttribute('data-category') || (txt.match(/section\s*:?\s*([^,\-]+)/i)?.[1] || txt.match(/قسم\s*:?\s*([^,\-]+)/i)?.[1] || '');
                    const row = el.getAttribute('data-row') || (txt.match(/row\s*:?\s*([a-z0-9]+)/i)?.[1] || txt.match(/صف\s*:?\s*([a-z0-9]+)/i)?.[1] || '');
                    const noM = txt.match(/(?:seat|chair|مقعد|كرسي)\s*#?\s*([a-z0-9]+)/i) || txt.match(/\b(\d{1,3})\b/);
                    const seatNo = noM ? noM[1] : '';
                    items.push({el, section: section || '', row: row || '', seatNo, score: txt.length});
                  }
                  if (items.length < qty) return '';
                  const groups = new Map();
                  for (const it of items) {
                    const key = `${it.section}|${it.row}`;
                    if (!groups.has(key)) groups.set(key, []);
                    groups.get(key).push(it);
                  }
                  let chosen = null;
                  for (const arr of groups.values()) {
                    const numeric = arr.filter(x => /^\d+$/.test(String(x.seatNo))).sort((a,b) => Number(a.seatNo) - Number(b.seatNo));
                    if (numeric.length >= qty) {
                      for (let i = 0; i <= numeric.length - qty; i++) {
                        const slice = numeric.slice(i, i + qty);
                        let consec = true;
                        for (let j = 1; j < slice.length; j++) {
                          if (Number(slice[j].seatNo) !== Number(slice[j-1].seatNo) + 1) { consec = false; break; }
                        }
                        if (consec) { chosen = slice; break; }
                      }
                      if (chosen) break;
                    }
                    if (!chosen && arr.length >= qty) chosen = arr.slice(0, qty);
                    if (chosen) break;
                  }
                  if (!chosen && items.length >= qty) chosen = items.slice(0, qty);
                  if (!chosen || chosen.length < qty) return '';
                  chosen.forEach(x => click(x.el));
                  return chosen.map(x => x.seatNo || '?').join(',');
                }""",
                quantity,
            )
            if picked:
                result["logs"].append(f"🪑 seats: {picked}")
                await page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


async def _scrape_seat_info(page) -> dict[str, str]:
    for target in [page, *page.frames]:
        try:
            info = await target.evaluate(
                """() => {
                  const grab = (pats) => {
                    for (const p of pats) {
                      const el = document.querySelector(p);
                      if (el) {
                        const t = (el.innerText || el.textContent || '').trim();
                        if (t) return t;
                      }
                    }
                    return '';
                  };
                  return {
                    section: grab(['[data-testid*="section"]', '.section', '[class*="section"]', '[class*="category"]']),
                    row: grab(['[data-testid*="row"]', '[class*="row-number"]', '[class*="row"]']),
                    seat_number: grab(['[data-testid*="seat-number"]', '[class*="seat-number"]', '[class*="seat-label"]', '[data-testid*="seat"]']),
                  };
                }"""
            )
            if any(info.values()):
                return info
        except Exception:
            continue
    return {"section": "", "row": "", "seat_number": ""}


# ════════════════════════════════════════════════════════════════════════
# v10: Checkout-via-Browser Fallback
# ────────────────────────────────────────────────────────────────────────
# When aiohttp hits Cloudflare WAF (HTTP 403) on POST /checkout, we open a
# stealth Playwright browser, transplant the bearer token, hold_token,
# selected seats and ALL aiohttp cookies into the browser context, then
# replay the EXACT /checkout request from inside the page (so it carries
# real Chromium TLS fingerprint + JA3 + canvas/WebGL signals). Cloudflare
# WAF treats it as a legitimate browser session and lets it through,
# returning the PayTabs redirect URL.
# ════════════════════════════════════════════════════════════════════════
async def checkout_via_browser_fallback(
    *,
    bearer: str,
    user_id: str,
    email: str,
    slug: str,
    event_id: str,
    ticket_id: str,
    quantity: int,
    time_slot_id: Optional[str],
    payment_method: str,
    seat_payload: Optional[dict[str, Any]],
    aiohttp_cookies: Optional[list[dict[str, Any]]] = None,
    turnstile_token: str = "",
    max_runtime: int = 90,
) -> dict[str, Any]:
    """V10 WAF bypass: replay /checkout from inside a real Chromium browser.

    Returns dict with keys:
      ok: bool
      payment_url: str
      order_id: str
      payment_session_id: str
      logs: list[str]
      error: str
    """
    out: dict[str, Any] = {
        "ok": False, "payment_url": "", "order_id": "",
        "payment_session_id": "", "logs": [], "error": "",
    }
    if _pw_err is not None:
        out["error"] = f"Playwright unavailable: {_pw_err}"
        return out

    deadline = time.time() + max_runtime

    body: dict[str, Any] = {
        "event_id": event_id,
        "redirect": f"{WEBOOK_ORIGIN}/en/payment-success",
        "redirect_failed": f"{WEBOOK_ORIGIN}/en/payment-failed",
        "booking_source": "rs-web",
        "lang": "en",
        "payment_method": payment_method or "credit_card",
        "is_wallet": False,
        "saudi_redeem": None,
        "refund_guarantee": False,
        "perks": [],
        "merchandise": [],
        "addons": [],
        "vouchers": [],
        "tickets": [{"qty": quantity, "id": ticket_id}],
        "app_source": "rs",
    }
    if time_slot_id:
        body["time_slot_id"] = time_slot_id
    if turnstile_token:
        body["turnstile"] = turnstile_token
    if seat_payload:
        clean = {k: v for k, v in seat_payload.items()
                 if v not in (None, "", [], {})}
        clean.pop("holdToken", None)
        clean.pop("seat_hold_token", None)
        body.update(clean)

    # V13: Use the singleton + randomized fingerprint context.
    from app.services.browser_pool import browser_context

    async with browser_context(label=email or "checkout") as ctx:
        try:
            # 1) Inject saved auth into localStorage
            if bearer:
                await _install_saved_auth(ctx, bearer, user_id, email)

            # 2) Transplant aiohttp cookies into browser context
            if aiohttp_cookies:
                pw_cookies = []
                for c in aiohttp_cookies:
                    try:
                        name = c.get("name")
                        value = c.get("value")
                        if not name or value is None:
                            continue
                        domain = c.get("domain") or ".webook.com"
                        if not domain.startswith(".") and "webook.com" in domain:
                            domain = "." + domain.lstrip(".")
                        pw_cookies.append({
                            "name": name,
                            "value": str(value),
                            "domain": domain,
                            "path": c.get("path") or "/",
                            "secure": True,
                            "httpOnly": False,
                            "sameSite": "Lax",
                        })
                    except Exception:
                        continue
                if pw_cookies:
                    try:
                        await ctx.add_cookies(pw_cookies)
                        out["logs"].append(f"🍪 transplanted {len(pw_cookies)} cookies into browser")
                    except Exception as e:
                        out["logs"].append(f"⚠️ cookie transplant err: {str(e)[:80]}")

            # 3) Set browser-wide auth headers for ALL fetches
            await ctx.set_extra_http_headers({
                "token": WEBOOK_PUBLIC_TOKEN,
                "authorization": f"Bearer {bearer}" if bearer else "Bearer",
                "accept-language": "ar-SA",
                "origin": WEBOOK_ORIGIN,
                "referer": f"{WEBOOK_ORIGIN}/en/events/{slug}",
            })

            page = await ctx.new_page()
            seen_paytabs: list[str] = []
            page.on("request", lambda r: (
                seen_paytabs.append(r.url) if ("paytabs" in r.url.lower() and r.url not in seen_paytabs) else None
            ))
            page.on("response", lambda r: (
                seen_paytabs.append(r.url) if ("paytabs" in r.url.lower() and r.url not in seen_paytabs) else None
            ))

            # 4) Warm-up: visit event page so Cloudflare cookies populate naturally
            try:
                await page.goto(
                    f"{WEBOOK_ORIGIN}/en/events/{slug}",
                    wait_until="domcontentloaded", timeout=45000,
                )
                await page.wait_for_timeout(3000)
                out["logs"].append("🔥 warm-up page loaded (CF cookies primed)")
            except Exception as e:
                out["logs"].append(f"⚠️ warm-up nav err: {str(e)[:80]}")

            if time.time() > deadline:
                out["error"] = "timeout_during_warmup"
                return out

            # 5) Replay the /checkout request from inside the browser context.
            # Browser fetch carries real Chromium TLS/JA3 fingerprint + cookies.
            checkout_url = f"{WEBOOK_API}/event-detail/{slug}/checkout?lang=en"
            replay_script = f"""async () => {{
              try {{
                const r = await fetch({json.dumps(checkout_url)}, {{
                  method: 'POST',
                  credentials: 'include',
                  headers: {{
                    'accept': 'application/json',
                    'content-type': 'application/json',
                    'token': {json.dumps(WEBOOK_PUBLIC_TOKEN)},
                    'authorization': {json.dumps('Bearer ' + bearer if bearer else 'Bearer')},
                    'accept-language': 'ar-SA',
                  }},
                  body: JSON.stringify({json.dumps(body)}),
                }});
                const txt = await r.text();
                return {{ status: r.status, body: txt }};
              }} catch (e) {{
                return {{ status: 0, body: String(e) }};
              }}
            }}"""
            replay = await page.evaluate(replay_script)
            status = replay.get("status", 0)
            raw_body = replay.get("body", "") or ""
            out["logs"].append(f"🌐 browser-replay /checkout → {status}")

            try:
                payload = json.loads(raw_body)
            except Exception:
                payload = {}

            if status == 200 and isinstance(payload, dict) and payload.get("status") == "success":
                data = payload.get("data") or {}
                pay_url = data.get("redirect_url") or (data.get("response") or {}).get("redirect_url") or ""
                if not pay_url and seen_paytabs:
                    pay_url = seen_paytabs[0]
                if pay_url:
                    out["ok"] = True
                    out["payment_url"] = pay_url
                    out["order_id"] = data.get("order_id", "")
                    out["payment_session_id"] = data.get("payment_session_id", "")
                    out["logs"].append("💳 PayTabs URL captured via browser fallback")
                else:
                    out["error"] = "browser_checkout_no_redirect_url"
            else:
                msg = (
                    (payload.get("message") if isinstance(payload, dict) else None)
                    or (payload.get("error") if isinstance(payload, dict) else None)
                    or raw_body[:300]
                )
                out["error"] = f"browser_checkout_failed:{status}:{str(msg)[:200]}"
        except Exception as e:
            out["error"] = f"browser_fallback_exception:{str(e)[:200]}"
        # V13: context auto-closes; browser stays warm for 30 minutes.

    return out
