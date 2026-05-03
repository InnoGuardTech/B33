"""
Persistence layer backed by either Turso (cloud SQLite) or local sqlite3.

Tables:
  • accounts       — email/password + JWT tokens per account
  • events         — cached events seen on Webook
  • bookings       — successful bookings (one row per account per booking)
  • bot_settings   — runtime-tunable settings managed via admin UI
  • event_blocks   — user-selected primary/backup blocks per event
  • drop_watchers  — accounts watching for seat drops on full charts
  • seat_maps      — cached seats.io rendering_info per chart_key
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from app.core.db import connect as _conn


def init_db() -> None:
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id              TEXT PRIMARY KEY,
            label           TEXT,
            email           TEXT NOT NULL,
            password        TEXT NOT NULL,
            access_token    TEXT,
            refresh_token   TEXT,
            token_expires_at REAL DEFAULT 0,
            user_id         TEXT,
            status          TEXT DEFAULT 'new',   -- new/ready/refreshing/blocked/needs_relogin
            last_used_at    REAL DEFAULT 0,
            tickets_booked  INTEGER DEFAULT 0,
            last_error      TEXT,
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS events (
            slug            TEXT PRIMARY KEY,
            title           TEXT,
            category        TEXT,
            city            TEXT,
            url             TEXT,
            start_date      INTEGER,
            is_seated       INTEGER DEFAULT 0,
            poster          TEXT,
            tickets_json    TEXT,
            first_seen_at   REAL,
            last_seen_at    REAL,
            last_checked_at REAL
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id         TEXT,
            event_slug      TEXT,
            event_title     TEXT,
            ticket_type     TEXT,
            account_id      TEXT,
            quantity        INTEGER,
            seat_info       TEXT,     -- JSON
            payment_url     TEXT,
            total_amount    REAL,
            currency        TEXT,
            status          TEXT,     -- pending/paid/cancelled/expired
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS bot_settings (
            key           TEXT PRIMARY KEY,
            value         TEXT,
            updated_at    REAL,
            updated_by    TEXT
        );

        CREATE TABLE IF NOT EXISTS event_blocks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id         TEXT,
            event_slug      TEXT,
            ticket_type_id  TEXT,
            primary_block   TEXT,
            backup_blocks   TEXT,    -- JSON list, in order
            quantity        INTEGER,
            payment_method  TEXT DEFAULT 'credit_card',
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS drop_watchers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id         TEXT,
            account_id      TEXT,
            event_slug      TEXT,
            event_key       TEXT,
            ticket_type_id  TEXT,
            quantity        INTEGER,
            blocks_pref     TEXT,     -- JSON list (primary,backup,neighbors)
            status          TEXT DEFAULT 'watching',  -- watching/captured/cancelled
            created_at      REAL,
            updated_at      REAL
        );

        CREATE TABLE IF NOT EXISTS seat_maps (
            chart_key       TEXT PRIMARY KEY,
            event_key       TEXT,
            rendering_info  TEXT,     -- JSON
            blocks_meta     TEXT,     -- JSON: [{name, center_x, center_y, free_count}]
            updated_at      REAL
        );

        CREATE INDEX IF NOT EXISTS idx_events_last_seen   ON events(last_seen_at);
        CREATE INDEX IF NOT EXISTS idx_events_start_date  ON events(start_date);
        CREATE INDEX IF NOT EXISTS idx_accounts_status    ON accounts(status);
        CREATE INDEX IF NOT EXISTS idx_dropwatch_status   ON drop_watchers(status);
        CREATE INDEX IF NOT EXISTS idx_blocks_chat        ON event_blocks(chat_id);
        """)


# ════════════════════════════════════════════════════════════════════════
# Accounts
# ════════════════════════════════════════════════════════════════════════
def upsert_account(account_id: str, email: str, password: str,
                   label: str = "") -> None:
    with _conn() as con:
        con.execute("""
            INSERT INTO accounts (id, label, email, password, status, created_at)
            VALUES (?, ?, ?, ?, 'new', ?)
            ON CONFLICT(id) DO UPDATE SET
              label = excluded.label,
              email = excluded.email,
              password = excluded.password
        """, (account_id, label or email.split("@")[0], email, password, time.time()))


def save_tokens(account_id: str, access: str, refresh: str,
                expires_at: float, user_id: Optional[str] = None) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE accounts
            SET access_token = ?, refresh_token = ?, token_expires_at = ?,
                user_id = COALESCE(?, user_id), status = 'ready',
                last_error = NULL
            WHERE id = ?
        """, (access, refresh, expires_at, user_id, account_id))


def set_account_status(account_id: str, status: str,
                       error: Optional[str] = None) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE accounts SET status = ?, last_error = ? WHERE id = ?",
            (status, error, account_id),
        )


def mark_account_used(account_id: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE accounts SET last_used_at = ?, tickets_booked = tickets_booked + 1 "
            "WHERE id = ?",
            (time.time(), account_id),
        )


def get_account(account_id: str) -> Optional[dict[str, Any]]:
    with _conn() as con:
        r = con.execute("SELECT * FROM accounts WHERE id = ?",
                        (account_id,)).fetchone()
        return dict(r) if r else None


def list_accounts(status: Optional[str] = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM accounts"
    params: list[Any] = []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY created_at ASC"
    with _conn() as con:
        return [dict(r) for r in con.execute(q, params).fetchall()]


def delete_account(account_id: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


# ════════════════════════════════════════════════════════════════════════
# Events
# ════════════════════════════════════════════════════════════════════════
def upsert_event(slug: str, data: dict[str, Any]) -> bool:
    """Returns True if this is a brand-new slug we hadn't seen before."""
    now = time.time()
    with _conn() as con:
        cur = con.execute("SELECT 1 FROM events WHERE slug = ?", (slug,)).fetchone()
        is_new = cur is None
        con.execute("""
            INSERT INTO events (slug, title, category, city, url, start_date,
                                is_seated, poster, tickets_json,
                                first_seen_at, last_seen_at, last_checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
              title = excluded.title,
              category = excluded.category,
              city = excluded.city,
              url = excluded.url,
              start_date = excluded.start_date,
              is_seated = excluded.is_seated,
              poster = excluded.poster,
              tickets_json = excluded.tickets_json,
              last_seen_at = excluded.last_seen_at,
              last_checked_at = excluded.last_checked_at
        """, (
            slug,
            data.get("title"),
            data.get("category"),
            data.get("city"),
            data.get("url"),
            data.get("start_date"),
            1 if data.get("is_seated") else 0,
            data.get("poster"),
            json.dumps(data.get("tickets") or [], ensure_ascii=False),
            now, now, now,
        ))
        return is_new


def get_event(slug: str) -> Optional[dict[str, Any]]:
    with _conn() as con:
        r = con.execute("SELECT * FROM events WHERE slug = ?",
                        (slug,)).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["tickets"] = json.loads(d.get("tickets_json") or "[]")
        except Exception:
            d["tickets"] = []
        return d


def list_recent_events(limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM events ORDER BY last_seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════
# Bookings
# ════════════════════════════════════════════════════════════════════════
def add_booking(chat_id: str, event_slug: str, event_title: str,
                ticket_type: str, account_id: str, quantity: int,
                seat_info: dict, payment_url: str,
                total_amount: float = 0.0, currency: str = "SAR",
                status: str = "pending") -> int:
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO bookings (chat_id, event_slug, event_title, ticket_type,
                                  account_id, quantity, seat_info, payment_url,
                                  total_amount, currency, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, event_slug, event_title, ticket_type, account_id,
              quantity, json.dumps(seat_info, ensure_ascii=False),
              payment_url, total_amount, currency, status, time.time()))
        return cur.lastrowid


def list_bookings(chat_id: Optional[str] = None,
                  limit: int = 20) -> list[dict[str, Any]]:
    q = "SELECT * FROM bookings"
    params: list[Any] = []
    if chat_id:
        q += " WHERE chat_id = ?"
        params.append(chat_id)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as con:
        rows = con.execute(q, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["seat_info"] = json.loads(d.get("seat_info") or "{}")
            except Exception:
                d["seat_info"] = {}
            out.append(d)
        return out


# ════════════════════════════════════════════════════════════════════════
# Drop watchers (replaces old sniper system — fires on actual seat drops,
# not on speed-based polling)
# ════════════════════════════════════════════════════════════════════════
def add_drop_watcher(*, chat_id: str, account_id: str, event_slug: str,
                    event_key: str, ticket_type_id: str, quantity: int,
                    blocks_pref: list[str]) -> int:
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO drop_watchers (chat_id, account_id, event_slug,
                                       event_key, ticket_type_id, quantity,
                                       blocks_pref, status, created_at,
                                       updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'watching', ?, ?)
        """, (chat_id, account_id, event_slug, event_key, ticket_type_id,
              quantity, json.dumps(blocks_pref, ensure_ascii=False),
              time.time(), time.time()))
        return cur.lastrowid


def list_drop_watchers(status: Optional[str] = "watching",
                       event_key: Optional[str] = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM drop_watchers WHERE 1=1"
    params: list[Any] = []
    if status:
        q += " AND status = ?"
        params.append(status)
    if event_key:
        q += " AND event_key = ?"
        params.append(event_key)
    q += " ORDER BY created_at"
    with _conn() as con:
        rows = [dict(r) for r in con.execute(q, params).fetchall()]
    for r in rows:
        try:
            r["blocks_pref"] = json.loads(r.get("blocks_pref") or "[]")
        except Exception:
            r["blocks_pref"] = []
    return rows


def set_drop_watcher_status(watcher_id: int, status: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE drop_watchers SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), watcher_id),
        )


def cancel_drop_watchers(chat_id: str) -> int:
    with _conn() as con:
        cur = con.execute(
            "UPDATE drop_watchers SET status='cancelled', updated_at=? "
            "WHERE chat_id = ? AND status='watching'",
            (time.time(), chat_id),
        )
        return cur.rowcount or 0


# ════════════════════════════════════════════════════════════════════════
# Bot settings (runtime-tunable via /admin UI)
# ════════════════════════════════════════════════════════════════════════
def set_bot_setting(key: str, value: str, updated_by: str = "admin") -> None:
    with _conn() as con:
        con.execute("""
            INSERT INTO bot_settings (key, value, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
        """, (key, value, time.time(), updated_by))


def get_bot_setting(key: str, default: str = "") -> str:
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ).fetchone()
        return (row["value"] if row else default) or default


def list_bot_settings() -> dict[str, str]:
    with _conn() as con:
        return {r["key"]: r["value"]
                for r in con.execute("SELECT key, value FROM bot_settings").fetchall()}


# ════════════════════════════════════════════════════════════════════════
# Event blocks selection (user picks primary + backup blocks)
# ════════════════════════════════════════════════════════════════════════
def save_event_blocks(*, chat_id: str, event_slug: str, ticket_type_id: str,
                     primary_block: str, backup_blocks: list[str],
                     quantity: int, payment_method: str = "credit_card") -> int:
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO event_blocks (chat_id, event_slug, ticket_type_id,
                                     primary_block, backup_blocks, quantity,
                                     payment_method, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, event_slug, ticket_type_id, primary_block,
              json.dumps(backup_blocks, ensure_ascii=False), quantity,
              payment_method, time.time()))
        return cur.lastrowid


def get_event_blocks(blocks_id: int) -> Optional[dict[str, Any]]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM event_blocks WHERE id = ?", (blocks_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["backup_blocks"] = json.loads(d.get("backup_blocks") or "[]")
        except Exception:
            d["backup_blocks"] = []
        return d


# ════════════════════════════════════════════════════════════════════════
# Seat maps cache (reusable across booking sessions)
# ════════════════════════════════════════════════════════════════════════
def save_seat_map(*, chart_key: str, event_key: str, rendering_info: dict,
                 blocks_meta: list[dict]) -> None:
    with _conn() as con:
        con.execute("""
            INSERT INTO seat_maps (chart_key, event_key, rendering_info,
                                   blocks_meta, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chart_key) DO UPDATE SET
                event_key = excluded.event_key,
                rendering_info = excluded.rendering_info,
                blocks_meta = excluded.blocks_meta,
                updated_at = excluded.updated_at
        """, (chart_key, event_key,
              json.dumps(rendering_info, ensure_ascii=False),
              json.dumps(blocks_meta, ensure_ascii=False),
              time.time()))


def get_seat_map(chart_key: str, max_age: float = 86400) -> Optional[dict[str, Any]]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM seat_maps WHERE chart_key = ?", (chart_key,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if (time.time() - float(d.get("updated_at") or 0)) > max_age:
            return None
        try:
            d["rendering_info"] = json.loads(d.get("rendering_info") or "{}")
            d["blocks_meta"] = json.loads(d.get("blocks_meta") or "[]")
        except Exception:
            pass
        return d


# Initialize on import so any module that imports us gets a ready DB
init_db()
