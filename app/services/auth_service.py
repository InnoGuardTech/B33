"""
Authentication service — V15.1 robust HTTP-only path.

Strategies (in order):
  1) Robust HTTP login (login_robust.py) — curl_cffi + 2Captcha, no browser.
     This is the FAST PATH and the only one used in production V15.1+.
  2) Legacy Playwright login — retained as a deep fallback for diagnostics
     when the robust path can't be used (no captcha key, etc.).
  3) Manual JWT paste fallback — unchanged.

After login, everything else stays HTTP-only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import aiohttp

from app.core.config import (
    HEADLESS,
    TOKEN_REFRESH_MARGIN,
    WEBOOK_API,
    WEBOOK_LANG,
    WEBOOK_ORIGIN,
    WEBOOK_PUBLIC_TOKEN,
    proxy_password,
    proxy_server,
    proxy_username,
    two_captcha_api_key,
    use_stealth_browser,
)
from app.core.storage import get_account, save_tokens, set_account_status
from app.services.login_robust import robust_login as _robust_login

log = logging.getLogger("auth")
WEBOOK_RECAPTCHA_SITE_KEY = "6LcvYHooAAAAAC-G46bpymJKtIwfDQpg9DsHPMpL"

BLOCKED_DOMAINS = (
    "googletagmanager.com", "google-analytics.com", "doubleclick.net",
    "facebook.net", "facebook.com", "amplitude.com", "taboola.com",
    "hotjar.com", "clarity.ms", "twitter.com", "t.co", "linkedin.com",
    "pinterest.com", "tiktok.com", "bing.com", "yandex.ru", "branch.io",
)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

_playwright_err: Optional[Exception] = None
_pw_backend = "playwright"
try:  # Prefer a stealth backend when available.
    if use_stealth_browser():
        from patchright.async_api import async_playwright, TimeoutError as PWTimeout  # type: ignore
        _pw_backend = "patchright"
    else:
        raise ImportError("stealth disabled")
except Exception:
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout  # type: ignore
        _pw_backend = "playwright"
    except Exception as _e:  # pragma: no cover
        _playwright_err = _e
        PWTimeout = Exception  # type: ignore


class AuthError(Exception):
    pass


def _proxy_config() -> Optional[dict[str, str]]:
    server = proxy_server().strip()
    if not server:
        return None
    cfg = {"server": server}
    if proxy_username().strip():
        cfg["username"] = proxy_username().strip()
    if proxy_password().strip():
        cfg["password"] = proxy_password().strip()
    return cfg


async def login_account(account_id: str, notifier=None, max_attempts: int = 2) -> dict[str, Any]:
    """V15.1 — HTTP-only login via login_robust (curl_cffi + 2Captcha).

    Falls back to the legacy Playwright path ONLY if the robust path
    fails AND a browser backend is available. The fallback is mainly
    there for diagnostics; in production the robust path always wins.
    """
    acc = get_account(account_id)
    if not acc:
        return {"ok": False, "error": "الحساب غير موجود"}

    proxy_url = (acc.get("proxy_url") or "").strip() or None
    last_error = ""

    # ─── PRIMARY PATH: HTTP-only via login_robust (V15.1) ───
    for attempt in range(1, max_attempts + 1):
        log.info(
            f"🔐 robust(http) login attempt {attempt}/{max_attempts} "
            f"for {account_id}"
        )
        set_account_status(account_id, "refreshing")
        try:
            res = await _robust_login(
                email=acc["email"],
                password=acc["password"],
                captcha_api_key=two_captcha_api_key().strip() or None,
                proxy_url=proxy_url,
                prefer="recaptcha",
                lang=WEBOOK_LANG or "ar",
            )
            if res.ok and res.access_token:
                save_tokens(
                    account_id=account_id,
                    access=res.access_token,
                    refresh="",
                    expires_at=res.expires_at
                        or int(time.time() + 7 * 86400),
                    user_id=res.user_id,
                )
                log.info(
                    f"✅ robust login OK for {account_id} "
                    f"(captcha={res.captcha_kind}, "
                    f"elapsed={res.elapsed_ms:.0f} ms)"
                )
                return {
                    "ok": True,
                    "tokens": {
                        "access_token": res.access_token,
                        "expires_at": res.expires_at,
                        "user_id": res.user_id,
                    },
                    "user": {
                        "name": res.user_name or "",
                        "email": acc["email"],
                    },
                    "meta": {
                        "path": "robust_http",
                        "captcha_kind": res.captcha_kind,
                        "elapsed_ms": res.elapsed_ms,
                    },
                }
            last_error = res.error or f"robust login failed (http={res.http_status})"
            log.warning(f"robust login attempt {attempt} failed: {last_error}")
        except Exception as e:
            last_error = f"robust login crashed: {str(e)[:200]}"
            log.exception(f"robust login crashed for {account_id}")
        if attempt < max_attempts:
            await asyncio.sleep(2.0)

    # ─── FALLBACK PATH: legacy Playwright (only if available) ───
    if _playwright_err is None:
        try:
            log.info(f"🔀 falling back to Playwright login for {account_id}")
            result = await _do_login_once(acc["email"], acc["password"])
            if result.get("ok"):
                save_tokens(
                    account_id=account_id,
                    access=result["access_token"],
                    refresh="",
                    expires_at=result["expires_at"],
                    user_id=result.get("user_id"),
                )
                return {
                    "ok": True,
                    "tokens": {
                        "access_token": result["access_token"],
                        "expires_at": result["expires_at"],
                        "user_id": result.get("user_id"),
                    },
                    "user": result.get("user") or {},
                    "meta": {"path": "playwright_fallback"},
                }
            last_error = result.get("error", last_error or "غير معروف")
        except Exception as e:
            last_error = f"playwright fallback crashed: {str(e)[:200]}"
            log.exception(f"playwright fallback crashed for {account_id}")

    set_account_status(account_id, "needs_relogin", last_error[:300])
    return {"ok": False, "error": last_error}


async def _launch_context():
    browser_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=browser_args,
            proxy=_proxy_config(),
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ar-SA",
            permissions=[],
        )

        async def _route(route):
            req = route.request
            url = req.url
            if req.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            if any(d in url for d in BLOCKED_DOMAINS):
                await route.abort()
                return
            await route.continue_()

        await ctx.route("**/*", _route)
        yield browser, ctx
        await browser.close()


async def _solve_recaptcha_v3_with_2captcha(page_url: str, action: str = "login") -> str:
    api_key = two_captcha_api_key().strip()
    if not api_key:
        return ""

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://2captcha.com/in.php",
            data={
                "key": api_key,
                "method": "userrecaptcha",
                "version": "v3",
                "action": action,
                "googlekey": WEBOOK_RECAPTCHA_SITE_KEY,
                "pageurl": page_url,
                "json": 1,
                "min_score": 0.3,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            data = await r.json(content_type=None)
        if data.get("status") != 1:
            return ""
        captcha_id = data.get("request")
        for _ in range(24):
            await asyncio.sleep(5)
            async with session.get(
                "https://2captcha.com/res.php",
                params={"key": api_key, "action": "get", "id": captcha_id, "json": 1},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                poll = await r.json(content_type=None)
            if poll.get("status") == 1:
                return str(poll.get("request") or "")
            if poll.get("request") not in {"CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"}:
                break
    return ""


async def _obtain_captcha_token(page) -> str:
    try:
        await page.wait_for_function(
            "() => window.grecaptcha && typeof window.grecaptcha.execute === 'function'",
            timeout=30000,
        )
    except Exception:
        await page.evaluate(f"""
          () => new Promise((resolve) => {{
            if (window.grecaptcha && grecaptcha.execute) return resolve();
            const s = document.createElement('script');
            s.src = 'https://www.google.com/recaptcha/api.js?render={WEBOOK_RECAPTCHA_SITE_KEY}';
            s.onload = () => resolve();
            s.onerror = () => resolve();
            document.head.appendChild(s);
          }})
        """)
        try:
            await page.wait_for_function(
                "() => window.grecaptcha && typeof window.grecaptcha.execute === 'function'",
                timeout=15000,
            )
        except Exception:
            pass

    token = ""
    try:
        token = await page.evaluate(f"""
          () => new Promise((resolve, reject) => {{
            if (!(window.grecaptcha && grecaptcha.ready && grecaptcha.execute)) return reject('grecaptcha unavailable');
            grecaptcha.ready(() => {{
              grecaptcha.execute('{WEBOOK_RECAPTCHA_SITE_KEY}', {{action: 'login'}}).then(resolve).catch(reject);
            }});
          }})
        """)
    except Exception:
        token = ""

    if token and len(str(token)) > 30:
        return str(token)
    return await _solve_recaptcha_v3_with_2captcha(f"{WEBOOK_ORIGIN}/en/login", action="login")


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


async def _do_login_once(email: str, password: str) -> dict[str, Any]:
    async for browser, ctx in _launch_context():
        page = await ctx.new_page()
        page.set_default_timeout(60000)
        page.set_default_navigation_timeout(60000)
        try:
            await page.goto(f"{WEBOOK_ORIGIN}/en/login", wait_until="domcontentloaded", timeout=45000)
            await _dismiss_cookies(page)
            captcha_token = await _obtain_captcha_token(page)
            if not captcha_token or len(captcha_token) < 30:
                raise AuthError("تعذّر الحصول على reCAPTCHA token صالح")

            result = await page.evaluate(f"""
              async () => {{
                const r = await fetch('{WEBOOK_API}/login', {{
                  method: 'POST',
                  credentials: 'include',
                  headers: {{
                    'accept': 'application/json',
                    'content-type': 'application/json',
                    'token': '{WEBOOK_PUBLIC_TOKEN}',
                    'authorization': 'Bearer',
                    'accept-language': 'ar-SA',
                  }},
                  body: JSON.stringify({{
                    email: {json.dumps(email)},
                    password: {json.dumps(password)},
                    captcha: {json.dumps(captcha_token)},
                    lang: {json.dumps(WEBOOK_LANG)},
                  }}),
                }});
                return {{status: r.status, body: await r.text()}};
              }}
            """)
            try:
                body = json.loads(result.get("body") or "{}")
            except Exception:
                body = {}
            if result.get("status") != 200 or body.get("status") != "success":
                err = body.get("error") or body.get("message") or str(result.get("body") or "")[:200]
                raise AuthError(f"رفض الخادم: {err}")

            data = body.get("data") or {}
            access_token = data.get("access_token")
            if not access_token:
                raise AuthError("الاستجابة لا تحتوي على access_token")
            return {
                "ok": True,
                "access_token": access_token,
                "expires_at": _jwt_expiry(access_token) or (time.time() + 7 * 86400),
                "user_id": data.get("_id"),
                "user": {
                    "name": data.get("name") or data.get("first_name", ""),
                    "email": data.get("email", email),
                },
            }
        except (PWTimeout, AuthError) as e:
            return {"ok": False, "error": str(e)[:260]}
        except Exception as e:
            return {"ok": False, "error": f"خطأ داخلي: {str(e)[:220]}"}


async def login_with_manual_token(account_id: str, access_token: str) -> dict[str, Any]:
    acc = get_account(account_id)
    if not acc:
        return {"ok": False, "error": "الحساب غير موجود"}

    access_token = (access_token or "").strip()
    if access_token.lower().startswith("bearer "):
        access_token = access_token[7:].strip()
    if not access_token.startswith("eyJ") or access_token.count(".") != 2:
        return {"ok": False, "error": "التوكن ليس بصيغة JWT صالحة"}

    expires_at = _jwt_expiry(access_token)
    if not expires_at:
        return {"ok": False, "error": "تعذّر قراءة صلاحية التوكن من محتواه"}
    if expires_at < time.time() + 300:
        return {"ok": False, "error": "التوكن منتهي الصلاحية أو على وشك الانتهاء"}

    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(
                f"{WEBOOK_API}/currencies?lang={WEBOOK_LANG}&visible_in=rs",
                headers={
                    "accept": "application/json",
                    "token": WEBOOK_PUBLIC_TOKEN,
                    "authorization": f"Bearer {access_token}",
                    "accept-language": WEBOOK_LANG,
                    "user-agent": "Mozilla/5.0",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return {"ok": False, "error": f"التوكن غير مقبول من webook ({r.status})"}
        except Exception as e:
            return {"ok": False, "error": f"تعذّر التحقق: {e}"}

    user_id = _jwt_sub(access_token) or ""
    save_tokens(account_id=account_id, access=access_token, refresh="", expires_at=expires_at, user_id=user_id)
    return {
        "ok": True,
        "tokens": {
            "access_token": access_token,
            "expires_at": expires_at,
            "user_id": user_id,
        },
    }


async def get_valid_bearer(account_id: str, notifier=None, auto_relogin: bool = True) -> Optional[str]:
    acc = get_account(account_id)
    if not acc:
        return None
    token = acc.get("access_token") or ""
    expires_at = acc.get("token_expires_at") or 0
    if token and time.time() < (expires_at - TOKEN_REFRESH_MARGIN):
        return token
    if not auto_relogin:
        return token or None
    res = await login_account(account_id, notifier)
    if res.get("ok"):
        return res["tokens"]["access_token"]
    return None


def _jwt_payload(token: str) -> Optional[dict]:
    try:
        import base64
        parts = token.split(".")
        if len(parts) != 3:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return None


def _jwt_expiry(token: str) -> Optional[float]:
    p = _jwt_payload(token) or {}
    exp = p.get("exp")
    return float(exp) if exp else None


def _jwt_sub(token: str) -> Optional[str]:
    p = _jwt_payload(token) or {}
    return p.get("sub") or None
