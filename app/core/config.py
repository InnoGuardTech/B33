"""
Central runtime configuration.

Values are resolved in this order:
    1. os.environ (if set at boot)
    2. settings.get() (DB-backed, set via /admin UI)
    3. default

For secrets that can be rotated at runtime (TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID, WEBOOK_PUBLIC_TOKEN, …) prefer the helper functions
below over reading the module-level constants — they pick up DB updates
made via the admin UI without a restart.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


# ── helpers ───────────────────────────────────────────────────────────────
def _env_or(key: str, default: str = "") -> str:
    """Resolution order: os.environ → bot_settings (DB) → legacy settings → default."""
    v = os.environ.get(key)
    if v:
        return v
    # New unified bot_settings table
    try:
        from app.core.storage import get_bot_setting
        val = get_bot_setting(key, "")
        if val:
            return val
    except Exception:
        pass
    # Legacy fallback
    try:
        from app.core import settings as _s
        return _s.get(key, default)
    except Exception:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env_or(key, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ── Server (always env-driven) ──────────────────────────────────────────
PORT = int(os.getenv("PORT", "10000"))
HOST = os.getenv("HOST", "0.0.0.0")
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL", "")

KEEP_ALIVE_ENABLED = os.getenv("KEEP_ALIVE_ENABLED", "true").lower() == "true"
KEEP_ALIVE_INTERVAL = int(os.getenv("KEEP_ALIVE_INTERVAL", "600"))

# ── Webook API ──────────────────────────────────────────────────────────
WEBOOK_ORIGIN = "https://webook.com"
WEBOOK_API = "https://api.webook.com/api/v2"
WEBOOK_LANG = os.getenv("WEBOOK_LANG", "ar")

# ── Monitoring ─────────────────────────────────────────────────────────
EVENT_POLL_INTERVAL = int(os.getenv("EVENT_POLL_INTERVAL", "300"))

LOGIN_CAPTCHA_TIMEOUT = int(os.getenv("LOGIN_CAPTCHA_TIMEOUT", "180"))
TOKEN_REFRESH_MARGIN = int(os.getenv("TOKEN_REFRESH_MARGIN", "300"))

# ── Paths ──────────────────────────────────────────────────────────────
DATA_DIR = os.getenv("DATA_DIR", "data")
DB_PATH = os.getenv("DB_PATH", f"{DATA_DIR}/webook_bot.db")
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")
LOGS_DIR = os.getenv("LOGS_DIR", "logs")
LOG_FILE = os.getenv("LOG_FILE", f"{LOGS_DIR}/webook_bot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

for _d in (DATA_DIR, SESSIONS_DIR, LOGS_DIR):
    os.makedirs(_d, exist_ok=True)


# ════════════════════════════════════════════════════════════════════════
# Lazy getters for values that can be set via the /admin web UI at runtime
# ════════════════════════════════════════════════════════════════════════
def telegram_bot_token() -> str:
    return _env_or("TELEGRAM_BOT_TOKEN", "")


def telegram_chat_id() -> str:
    return _env_or("TELEGRAM_CHAT_ID", "")


def authorized_chat_ids() -> list[str]:
    raw = _env_or("AUTHORIZED_CHAT_IDS", "")
    ids = [c.strip() for c in raw.split(",") if c.strip()]
    main = telegram_chat_id()
    if main and main not in ids:
        ids.append(main)
    return ids


def webook_public_token() -> str:
    return _env_or(
        "WEBOOK_PUBLIC_TOKEN",
        "e9aac1f2f0b6c07d6be070ed14829de684264278359148d6a582ca65a50934d2",
    )


def admin_password() -> str:
    return _env_or("ADMIN_PASSWORD", "webook-admin")


# ── Seats.io / SeatCloud runtime tuning ────────────────────────────────
def seatsio_enabled() -> bool:
    return _env_bool("SEATSIO_ENABLED", True)


def seatsio_token_ttl() -> int:
    return int(_env_or("SEATSIO_TOKEN_TTL", "3600"))


def seatsio_prewarm_enabled() -> bool:
    return _env_bool("SEATSIO_PREWARM_ENABLED", True)


def seatsio_status_interval() -> float:
    return float(_env_or("SEATSIO_STATUS_INTERVAL", "0.60"))


def seatsio_drop_watcher_enabled() -> bool:
    """Drop-watcher mode replaces the old speed-based stalker. It only
    activates when a chart is fully booked, then waits on WebSocket
    drop events."""
    return _env_bool("SEATSIO_DROP_WATCHER_ENABLED", True)


def seatsio_drop_watcher_max_wait() -> float:
    return float(_env_or("SEATSIO_DROP_WATCHER_MAX_WAIT", "1800"))


def target_blocks() -> list[str]:
    """Legacy global block preference — the new flow uses per-event
    primary/backup block selection (see app.core.storage.event_blocks).
    Kept as a fallback only.
    """
    raw = _env_or("TARGET_BLOCKS", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def default_payment_method() -> str:
    """Unified default payment method applied to every account unless the
    operator explicitly switches to Apple Pay. Defaults to credit_card.
    Allowed: 'credit_card' | 'apple_pay'.
    """
    raw = (_env_or("DEFAULT_PAYMENT_METHOD", "credit_card") or "credit_card").strip().lower()
    return "apple_pay" if raw in {"apple_pay", "applepay", "apple"} else "credit_card"


# ── Paid reliability enhancers ─────────────────────────────────────────
def use_stealth_browser() -> bool:
    return _env_bool("USE_STEALTH_BROWSER", True)


def proxy_server() -> str:
    return _env_or("PROXY_SERVER", "")


def proxy_username() -> str:
    return _env_or("PROXY_USERNAME", "")


def proxy_password() -> str:
    return _env_or("PROXY_PASSWORD", "")


def two_captcha_api_key() -> str:
    return _env_or("CAPTCHA_API_KEY", "")


# ════════════════════════════════════════════════════════════════════════
# Backwards-compatible module-level aliases
# ════════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = telegram_bot_token()
TELEGRAM_CHAT_ID = telegram_chat_id()
AUTHORIZED_CHAT_IDS = authorized_chat_ids()
WEBOOK_PUBLIC_TOKEN = webook_public_token()
SEATSIO_ENABLED = seatsio_enabled()
SEATSIO_PREWARM_ENABLED = seatsio_prewarm_enabled()
USE_STEALTH_BROWSER = use_stealth_browser()
DEFAULT_PAYMENT_METHOD = default_payment_method()
