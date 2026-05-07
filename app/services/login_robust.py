"""
V15.1 — Robust HTTP-only login flow (no Playwright, no browser).

Why this exists
---------------
The legacy `auth_service._do_login_once` opens a Playwright Chromium
context just to obtain a reCAPTCHA v3 token, which costs ~250 MB RAM and
~5 s of latency on every login attempt. On Render's free tier this is
the difference between "boots" and "OOM-killed".

This module:
  1. Solves the captcha headlessly via 2Captcha's HTTP API.
  2. POSTs the login JSON via curl_cffi so the TLS/JA3 + HTTP/2
     fingerprint matches a real Chrome (the V14.1 Cloudflare bypass).
  3. Uses reCAPTCHA v3 as PRIMARY (server returns
     "Please complete the recaptcha to submit the form" otherwise) and
     Turnstile as fallback for adjacent flows.

Discovery facts (locked in by reverse-engineering Webook's bundle):
  • Turnstile site-key (login form):  `0x4AAAAAAAw0ci3Vi2Xv3txt`
  • reCAPTCHA v3 site-key:            `6LcvYHooAAAAAC-G46bpymJKtIwfDQpg9DsHPMpL`
  • Login endpoint:                   `POST /api/v2/login`
  • Payload field for the captcha:    `captcha`
  • REQUIRED public token (header `token`):
        VITE_PUBLIC_TICKETS_API_TOKEN =
        e9aac1f2f0b6c07d6be070ed14829de684264278359148d6a582ca65a50934d2
    (The 32-char token previously used in env was a READ-ONLY token —
    it gets a silent 403 on /login. The 64-char tickets token unlocks
    /login and returns the user's actual JWT.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger("login_robust")

# ════════════════════════════════════════════════════════════════════════
# Constants — verified live against webook.com bundle
# ════════════════════════════════════════════════════════════════════════
WEBOOK_LOGIN_PAGE = "https://webook.com/login"
WEBOOK_LOGIN_API = "https://api.webook.com/api/v2/login"

TURNSTILE_SITE_KEY = "0x4AAAAAAAw0ci3Vi2Xv3txt"
RECAPTCHA_V3_SITE_KEY = "6LcvYHooAAAAAC-G46bpymJKtIwfDQpg9DsHPMpL"

TWO_CAPTCHA_API_BASE = "https://api.2captcha.com"
TWO_CAPTCHA_LEGACY_BASE = "https://2captcha.com"

DEFAULT_IMPERSONATE = "chrome120"

# Built-in public token discovered in
# https://wbk-assets.webook.com/0.6.0/assets/index-BDgka6ow.js
# under VITE_PUBLIC_TICKETS_API_TOKEN. Required for /login to bypass the
# 403 silent rejection.
BUILTIN_PUBLIC_TOKEN = (
    "e9aac1f2f0b6c07d6be070ed14829de684264278359148d6a582ca65a50934d2"
)


# ════════════════════════════════════════════════════════════════════════
# Result dataclass
# ════════════════════════════════════════════════════════════════════════
@dataclass
class LoginResult:
    ok: bool
    access_token: Optional[str] = None
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    expires_at: Optional[int] = None
    captcha_kind: Optional[str] = None  # "turnstile" | "recaptcha"
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    http_status: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════
# Captcha solvers (2Captcha HTTP — no browser)
# ════════════════════════════════════════════════════════════════════════
async def solve_turnstile(
    api_key: str,
    *,
    site_key: str = TURNSTILE_SITE_KEY,
    page_url: str = WEBOOK_LOGIN_PAGE,
    poll_interval: float = 3.0,
    max_polls: int = 30,
    timeout: float = 100.0,
) -> str:
    if not api_key:
        raise RuntimeError("2Captcha API key missing")
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{TWO_CAPTCHA_API_BASE}/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                },
            },
        )
        data = _safe_json(r)
        if (data.get("errorId") or 0) != 0:
            raise RuntimeError(
                f"2Captcha createTask err: "
                f"{data.get('errorCode')} {data.get('errorDescription')}"
            )
        task_id = data.get("taskId")
        if not task_id:
            raise RuntimeError(f"createTask returned no taskId: {data}")
        for _ in range(max_polls):
            await asyncio.sleep(poll_interval)
            r = await client.post(
                f"{TWO_CAPTCHA_API_BASE}/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            )
            data = _safe_json(r)
            if (data.get("errorId") or 0) != 0:
                raise RuntimeError(
                    f"getTaskResult err: "
                    f"{data.get('errorCode')} {data.get('errorDescription')}"
                )
            if data.get("status") == "ready":
                tok = (data.get("solution") or {}).get("token")
                if not tok:
                    raise RuntimeError(f"solution missing token: {data}")
                return tok
        raise RuntimeError("2Captcha Turnstile timeout")


async def solve_recaptcha_v3(
    api_key: str,
    *,
    site_key: str = RECAPTCHA_V3_SITE_KEY,
    page_url: str = WEBOOK_LOGIN_PAGE,
    action: str = "login",
    min_score: float = 0.7,
    poll_interval: float = 5.0,
    max_polls: int = 24,
    timeout: float = 120.0,
) -> str:
    if not api_key:
        raise RuntimeError("2Captcha API key missing")
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{TWO_CAPTCHA_LEGACY_BASE}/in.php",
            data={
                "key": api_key,
                "method": "userrecaptcha",
                "version": "v3",
                "action": action,
                "min_score": str(min_score),
                "googlekey": site_key,
                "pageurl": page_url,
                "json": "1",
            },
        )
        data = _safe_json(r)
        if str(data.get("status")) != "1":
            raise RuntimeError(f"2Captcha in.php err: {data}")
        captcha_id = data.get("request")
        if not captcha_id:
            raise RuntimeError("2Captcha in.php returned no request id")
        for _ in range(max_polls):
            await asyncio.sleep(poll_interval)
            r = await client.get(
                f"{TWO_CAPTCHA_LEGACY_BASE}/res.php",
                params={
                    "key": api_key, "action": "get",
                    "id": captcha_id, "json": 1,
                },
            )
            data = _safe_json(r)
            if str(data.get("status")) == "1":
                req = data.get("request") or ""
                if isinstance(req, str) and req.startswith("CAPCHA_NOT_READY"):
                    continue
                return str(req)
            if data.get("request") != "CAPCHA_NOT_READY":
                raise RuntimeError(f"2Captcha res.php err: {data}")
        raise RuntimeError("2Captcha reCAPTCHA v3 timeout")


def _safe_json(r) -> dict:
    try:
        return r.json()
    except Exception:
        try:
            return {"raw": (r.text or "")[:500]}
        except Exception:
            return {"raw": ""}


# ════════════════════════════════════════════════════════════════════════
# JWT helpers
# ════════════════════════════════════════════════════════════════════════
def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        import base64
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(seg.encode()))
    except Exception:
        return {}


def _jwt_expiry(token: str) -> Optional[int]:
    p = _jwt_payload(token)
    exp = p.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def _jwt_sub(token: str) -> Optional[str]:
    p = _jwt_payload(token)
    s = p.get("sub") or p.get("user_id") or p.get("_id")
    return str(s) if s else None


# ════════════════════════════════════════════════════════════════════════
# Public-token resolver — supports the BUILTIN_FALLBACK env flag
# ════════════════════════════════════════════════════════════════════════
def resolve_public_token(explicit: str = "") -> str:
    """Resolve the value to send in the `token` header for /login.

    Order:
      1. ``explicit`` argument (caller-supplied)
      2. env ``WEBOOK_PUBLIC_TOKEN``
      3. Built-in token from the JS bundle, IF env
         ``WEBOOK_PUBLIC_TOKEN_BUILTIN_FALLBACK`` is truthy ("1"/"true"/
         "yes"/"on") — defaults to ENABLED so /login keeps working even
         without a manually-configured token.
    """
    if explicit:
        return explicit.strip()
    try:
        from app.core.config import webook_public_token
        env_tok = (webook_public_token() or "").strip()
    except Exception:
        env_tok = (os.getenv("WEBOOK_PUBLIC_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    fallback_flag = (os.getenv("WEBOOK_PUBLIC_TOKEN_BUILTIN_FALLBACK", "true")
                     or "").strip().lower()
    if fallback_flag in ("1", "true", "yes", "on", "y"):
        return BUILTIN_PUBLIC_TOKEN
    return ""


# ════════════════════════════════════════════════════════════════════════
# The login itself
# ════════════════════════════════════════════════════════════════════════
async def _post_login(
    *,
    email: str,
    password: str,
    captcha_token: str,
    captcha_kind: str,
    proxy_url: Optional[str],
    impersonate: str,
    lang: str,
    public_token: str,
    timeout: float,
) -> tuple[int, dict]:
    """Single POST to /api/v2/login with the chosen captcha token."""
    from curl_cffi.requests import AsyncSession  # type: ignore

    payload: dict[str, Any] = {
        "email": email,
        "password": password,
        "lang": lang,
    }
    payload["captcha"] = captcha_token
    if captcha_kind == "turnstile":
        payload["cf-turnstile-response"] = captcha_token
    elif captcha_kind == "recaptcha":
        payload["g-recaptcha-response"] = captcha_token

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "origin": "https://webook.com",
        "referer": "https://webook.com/",
        "authorization": "Bearer",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }
    if public_token:
        headers["token"] = public_token

    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

    async with AsyncSession(
        impersonate=impersonate,
        timeout=timeout,
        proxies=proxies,
    ) as s:
        try:
            await s.get(WEBOOK_LOGIN_PAGE, headers={
                "accept-language": headers["accept-language"]})
        except Exception:
            pass
        r = await s.post(WEBOOK_LOGIN_API, json=payload, headers=headers)
        return r.status_code, _safe_json(r)


async def robust_login(
    email: str,
    password: str,
    *,
    captcha_api_key: Optional[str] = None,
    proxy_url: Optional[str] = None,
    prefer: str = "recaptcha",     # "recaptcha" | "turnstile" | "auto"
    impersonate: str = DEFAULT_IMPERSONATE,
    lang: str = "ar",
    public_token: str = "",
    timeout: float = 30.0,
) -> LoginResult:
    """HTTP-only Webook login with auto captcha solving."""
    t0 = time.perf_counter()
    api_key = (captcha_api_key or "").strip()
    if not api_key:
        try:
            from app.core.config import two_captcha_api_key
            api_key = two_captcha_api_key().strip()
        except Exception:
            api_key = (os.getenv("CAPTCHA_API_KEY") or "").strip()

    pubt = resolve_public_token(public_token)

    if not api_key:
        return LoginResult(
            ok=False,
            error="2Captcha API key not configured (CAPTCHA_API_KEY)",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    order: list[str]
    if prefer == "auto":
        order = ["recaptcha", "turnstile"]
    elif prefer == "turnstile":
        order = ["turnstile", "recaptcha"]
    else:
        order = ["recaptcha", "turnstile"]

    last_err: str = "unknown"
    last_http: int = 0
    last_raw: dict = {}
    for kind in order:
        try:
            log.info("login: solving %s captcha for %s…", kind, email)
            if kind == "turnstile":
                tok = await solve_turnstile(api_key)
            else:
                tok = await solve_recaptcha_v3(api_key)
        except Exception as e:
            last_err = f"{kind} solve failed: {e}"
            log.warning(last_err)
            continue

        try:
            status, body = await _post_login(
                email=email, password=password,
                captcha_token=tok, captcha_kind=kind,
                proxy_url=proxy_url, impersonate=impersonate,
                lang=lang, public_token=pubt, timeout=timeout,
            )
        except Exception as e:
            last_err = f"login POST crashed: {e}"
            log.warning(last_err)
            continue

        last_http = status
        last_raw = body if isinstance(body, dict) else {"raw": str(body)[:500]}

        if status == 200 and isinstance(body, dict) and \
                body.get("status") == "success":
            data = body.get("data") or {}
            access_token = data.get("access_token") or data.get("token") or ""
            if not access_token:
                last_err = "login OK but no access_token in response"
                continue
            exp = _jwt_expiry(access_token) or int(time.time() + 7 * 86400)
            return LoginResult(
                ok=True,
                access_token=access_token,
                user_id=data.get("_id") or _jwt_sub(access_token),
                user_name=data.get("name") or data.get("first_name", ""),
                expires_at=exp,
                captcha_kind=kind,
                http_status=status,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
                raw=body,
            )

        errs = (body.get("errors") or body.get("error") or {}) \
            if isinstance(body, dict) else {}
        msg = (
            (body.get("message") if isinstance(body, dict) else "")
            or json.dumps(errs)[:200]
        )
        last_err = f"server rejected ({status}): {msg or 'empty response'}"
        log.warning(last_err)
        if isinstance(errs, dict) and (
            "captcha" in errs or "turnstile" in errs or "recaptcha" in errs
        ):
            continue
        if status in (401, 403, 404, 422):
            break

    return LoginResult(
        ok=False,
        http_status=last_http,
        error=last_err,
        elapsed_ms=(time.perf_counter() - t0) * 1000,
        raw=last_raw,
    )


# ════════════════════════════════════════════════════════════════════════
# Self-test
# ════════════════════════════════════════════════════════════════════════
def _self_test_offline() -> int:
    assert TURNSTILE_SITE_KEY.startswith("0x4AAAAAA")
    assert RECAPTCHA_V3_SITE_KEY.startswith("6Lc")
    assert WEBOOK_LOGIN_API == "https://api.webook.com/api/v2/login"
    assert len(BUILTIN_PUBLIC_TOKEN) == 64
    print("  ✅ constants validated")
    print(f"     Turnstile site_key:  {TURNSTILE_SITE_KEY}")
    print(f"     reCAPTCHA v3 key:    {RECAPTCHA_V3_SITE_KEY}")
    print(f"     Login endpoint:      {WEBOOK_LOGIN_API}")
    print(f"     Built-in pub token:  {BUILTIN_PUBLIC_TOKEN[:20]}…")

    fake_jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiJ1c2VyXzEiLCJleHAiOjE5MDAwMDAwMDB9."
        + ("X" * 30)
    )
    assert _jwt_expiry(fake_jwt) == 1900000000
    assert _jwt_sub(fake_jwt) == "user_1"
    print("  ✅ JWT exp/sub helpers correct")

    # resolve_public_token: env-flag fallback
    os.environ.pop("WEBOOK_PUBLIC_TOKEN", None)
    os.environ["WEBOOK_PUBLIC_TOKEN_BUILTIN_FALLBACK"] = "true"
    assert resolve_public_token() == BUILTIN_PUBLIC_TOKEN
    os.environ["WEBOOK_PUBLIC_TOKEN_BUILTIN_FALLBACK"] = "false"
    assert resolve_public_token() == ""
    os.environ["WEBOOK_PUBLIC_TOKEN_BUILTIN_FALLBACK"] = "true"
    assert resolve_public_token("explicit-tok") == "explicit-tok"
    print("  ✅ resolve_public_token() honors BUILTIN_FALLBACK flag")
    return 0


async def _self_test_no_creds() -> int:
    res = await robust_login(
        email="", password="", captcha_api_key="", prefer="recaptcha",
    )
    assert res.ok is False
    assert "2Captcha" in (res.error or "") or \
        "captcha" in (res.error or "").lower()
    print(f"  ✅ no-creds path returns LoginResult(ok=False)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    print("🧪 Hydra V15.1 — login_robust self-test")
    print("=" * 70)
    rc = _self_test_offline()
    rc |= asyncio.run(_self_test_no_creds())
    if rc == 0:
        print("\n🏆 login_robust self-test PASSED.")
    sys.exit(rc)
