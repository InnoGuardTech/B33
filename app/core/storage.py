"""
Persistence layer backed by PostgreSQL, Turso (cloud SQLite) or local sqlite3.

V12 Royal Schema:
  • events.royal_category   — one of {sports, concerts, theater,
                                       experiences, exhibitions}
  • events.end_date         — epoch seconds (used to filter ended events)
  • events.has_availability — 0/1 sold-out flag
  • events.sub_title, events.venue — denormalised for fast UI rendering

V12 fixes the V11 deploy crash (column "royal_category" does not exist):
  1. Migration runs FIRST, BEFORE any INSERT/SELECT on `royal_category`.
  2. Migration is now backend-aware (information_schema on PG,
     PRAGMA table_info on SQLite/Turso).
  3. db.executescript now splits multi-statement scripts and runs each in
     its own savepoint so a single ALTER failure does not poison the rest.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from app.core.db import backend as _backend, connect as _conn

log = logging.getLogger("storage")


# ════════════════════════════════════════════════════════════════════════
# V12 schema migration helper — runs before any DML touches new columns
# ════════════════════════════════════════════════════════════════════════
_V12_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (column, pg_type, sqlite_type)
    ("royal_category",   "TEXT",                 "TEXT"),
    ("end_date",         "BIGINT DEFAULT 0",     "INTEGER DEFAULT 0"),
    ("has_availability", "INTEGER DEFAULT 1",    "INTEGER DEFAULT 1"),
    ("sub_title",        "TEXT",                 "TEXT"),
    ("venue",            "TEXT",                 "TEXT"),
    ("first_seen_at",    "DOUBLE PRECISION",     "REAL"),
    ("last_seen_at",     "DOUBLE PRECISION",     "REAL"),
    ("last_checked_at",  "DOUBLE PRECISION",     "REAL"),
)

# V14: per-account proxy column (added on `accounts` table).
_V14_ACCOUNT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("proxy_url", "TEXT", "TEXT"),
)

_V12_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_events_royal_cat  ON events(royal_category)",
    "CREATE INDEX IF NOT EXISTS idx_events_avail      ON events(has_availability)",
    "CREATE INDEX IF NOT EXISTS idx_events_end_date   ON events(end_date)",
    "CREATE INDEX IF NOT EXISTS idx_events_first_seen ON events(first_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_start_date ON events(start_date)",
    # V13: Partial index — dramatically speeds up the dominant
    # "only_available + newest-first" listing query (uses index-only scan
    # on the hot subset). Skipped automatically on SQLite (still legal SQL).
    "CREATE INDEX IF NOT EXISTS idx_events_active "
    "ON events(first_seen_at DESC, start_date) "
    "WHERE has_availability = 1",
)

_MIGRATED = False


def _ensure_event_v12_columns() -> None:
    """Idempotent migration that works across all 3 backends."""
    global _MIGRATED
    if _MIGRATED:
        return
    backend_name = _backend()
    try:
        with _conn() as con:
            existing: set[str] = set()
            if backend_name == "postgres":
                cur = con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'events'"
                )
                for row in cur.fetchall():
                    name = row.get("column_name") if isinstance(row, dict) else row[0]
                    if name:
                        existing.add(name)
            else:
                cur = con.execute("PRAGMA table_info(events)")
                for row in cur.fetchall():
                    if isinstance(row, dict):
                        nm = row.get("name") or row.get(1)
                        if nm:
                            existing.add(nm)
                    else:
                        try:
                            existing.add(row[1])
                        except Exception:
                            pass

            for col, pg_type, sqlite_type in _V12_COLUMNS:
                if col in existing:
                    continue
                ddl = pg_type if backend_name == "postgres" else sqlite_type
                if backend_name == "postgres":
                    sql = f"ALTER TABLE events ADD COLUMN IF NOT EXISTS {col} {ddl}"
                else:
                    sql = f"ALTER TABLE events ADD COLUMN {col} {ddl}"
                try:
                    con.execute(sql)
                    log.info(f"[migration] events.{col} added ({backend_name})")
                except Exception as e:
                    log.debug(f"[migration] {col}: {e}")

            for ix_sql in _V12_INDEXES:
                try:
                    con.execute(ix_sql)
                except Exception:
                    pass

            # V14: per-account proxy_url column.
            existing_acc: set[str] = set()
            if backend_name == "postgres":
                cur = con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'accounts'"
                )
                for row in cur.fetchall():
                    nm = row.get("column_name") if isinstance(row, dict) else row[0]
                    if nm:
                        existing_acc.add(nm)
            else:
                cur = con.execute("PRAGMA table_info(accounts)")
                for row in cur.fetchall():
                    if isinstance(row, dict):
                        nm = row.get("name") or row.get(1)
                        if nm:
                            existing_acc.add(nm)
                    else:
                        try:
                            existing_acc.add(row[1])
                        except Exception:
                            pass
            for col, pg_type, sqlite_type in _V14_ACCOUNT_COLUMNS:
                if col in existing_acc:
                    continue
                ddl = pg_type if backend_name == "postgres" else sqlite_type
                if backend_name == "postgres":
                    sql = f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS {col} {ddl}"
                else:
                    sql = f"ALTER TABLE accounts ADD COLUMN {col} {ddl}"
                try:
                    con.execute(sql)
                    log.info(f"[migration V14] accounts.{col} added ({backend_name})")
                except Exception as e:
                    log.debug(f"[migration V14] {col}: {e}")

        _MIGRATED = True
    except Exception as e:
        log.error(f"[migration] V12/V14 failed: {e}")


# ════════════════════════════════════════════════════════════════════════
# Schema bootstrap
# ════════════════════════════════════════════════════════════════════════
def init_db() -> None:
    """Create tables if missing, then run V12 migration to add new columns
    to legacy databases that pre-date V12."""
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
            status          TEXT DEFAULT 'new',
            last_used_at    REAL DEFAULT 0,
            tickets_booked  INTEGER DEFAULT 0,
            last_error      TEXT,
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS events (
            slug             TEXT PRIMARY KEY,
            title            TEXT,
            category         TEXT,
            royal_category   TEXT,
            city             TEXT,
            url              TEXT,
            start_date       INTEGER,
            end_date         INTEGER DEFAULT 0,
            is_seated        INTEGER DEFAULT 0,
            has_availability INTEGER DEFAULT 1,
            sub_title        TEXT,
            venue            TEXT,
            poster           TEXT,
            tickets_json     TEXT,
            first_seen_at    REAL,
            last_seen_at     REAL,
            last_checked_at  REAL
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id         TEXT,
            event_slug      TEXT,
            event_title     TEXT,
            ticket_type     TEXT,
            account_id      TEXT,
            quantity        INTEGER,
            seat_info       TEXT,
            payment_url     TEXT,
            total_amount    REAL,
            currency        TEXT,
            status          TEXT,
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
            backup_blocks   TEXT,
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
            blocks_pref     TEXT,
            status          TEXT DEFAULT 'watching',
            created_at      REAL,
            updated_at      REAL
        );

        CREATE TABLE IF NOT EXISTS seat_maps (
            chart_key       TEXT PRIMARY KEY,
            event_key       TEXT,
            rendering_info  TEXT,
            blocks_meta     TEXT,
            updated_at      REAL
        );

        CREATE INDEX IF NOT EXISTS idx_events_last_seen   ON events(last_seen_at);
        CREATE INDEX IF NOT EXISTS idx_events_start_date  ON events(start_date);
        CREATE INDEX IF NOT EXISTS idx_accounts_status    ON accounts(status);
        CREATE INDEX IF NOT EXISTS idx_dropwatch_status   ON drop_watchers(status);
        CREATE INDEX IF NOT EXISTS idx_blocks_chat        ON event_blocks(chat_id);
        """)
    # V12 migration MUST run after CREATE TABLE so legacy DBs (without
    # royal_category etc.) get the columns added BEFORE any INSERT/SELECT.
    _ensure_event_v12_columns()


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
    _ensure_event_v12_columns()
    with _conn() as con:
        cur = con.execute("SELECT 1 FROM events WHERE slug = ?", (slug,)).fetchone()
        is_new = cur is None
        con.execute("""
            INSERT INTO events (slug, title, category, royal_category, city, url,
                                start_date, end_date, is_seated, has_availability,
                                sub_title, venue, poster, tickets_json,
                                first_seen_at, last_seen_at, last_checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
              title = excluded.title,
              category = excluded.category,
              royal_category = excluded.royal_category,
              city = excluded.city,
              url = excluded.url,
              start_date = excluded.start_date,
              end_date = excluded.end_date,
              is_seated = excluded.is_seated,
              has_availability = excluded.has_availability,
              sub_title = excluded.sub_title,
              venue = excluded.venue,
              poster = excluded.poster,
              tickets_json = excluded.tickets_json,
              last_seen_at = excluded.last_seen_at,
              last_checked_at = excluded.last_checked_at
        """, (
            slug,
            data.get("title"),
            data.get("category"),
            data.get("royal_category"),
            data.get("city"),
            data.get("url"),
            data.get("start_date"),
            data.get("end_date") or 0,
            1 if data.get("is_seated") else 0,
            1 if data.get("has_availability", True) else 0,
            data.get("sub_title") or "",
            data.get("venue") or "",
            data.get("poster"),
            json.dumps(data.get("tickets") or [], ensure_ascii=False),
            now, now, now,
        ))
        return is_new


def purge_ended_events(grace_seconds: int = 3600) -> int:
    """V12 dynamic cleanup — delete rows whose end_date passed."""
    _ensure_event_v12_columns()
    cutoff = time.time() - grace_seconds
    deleted = 0
    try:
        with _conn() as con:
            cur = con.execute(
                "DELETE FROM events "
                "WHERE end_date IS NOT NULL "
                "  AND end_date > 0 "
                "  AND end_date < ?",
                (cutoff,),
            )
            try:
                deleted = int(getattr(cur, "rowcount", 0) or 0)
            except Exception:
                deleted = 0
    except Exception as e:
        log.debug(f"purge_ended_events: {e}")
    if deleted:
        log.info(f"🧹 purged {deleted} ended events")
    return deleted


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


def list_recent_events(limit: int = 200,
                       royal_category: Optional[str] = None,
                       only_available: bool = True,
                       hide_ended: bool = True) -> list[dict[str, Any]]:
    """V12 royal listing — newest-first, ended dropped, sold-out hidden."""
    _ensure_event_v12_columns()
    where = []
    params: list[Any] = []

    if hide_ended:
        now = time.time()
        where.append(
            "(end_date IS NULL OR end_date = 0 OR end_date > ? "
            " OR (start_date IS NOT NULL AND start_date > ?))"
        )
        params.extend([now - 3600, now - 6 * 3600])

    if only_available:
        where.append("(has_availability IS NULL OR has_availability = 1)")

    if royal_category:
        where.append("royal_category = ?")
        params.append(royal_category)

    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (
        " ORDER BY first_seen_at DESC,"
        " CASE WHEN start_date IS NULL OR start_date = 0 THEN 9999999999"
        "      ELSE start_date END ASC"
        " LIMIT ?"
    )
    params.append(limit)

    with _conn() as con:
        rows = con.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def count_events_by_royal_category(only_available: bool = True,
                                    hide_ended: bool = True
                                    ) -> dict[str, int]:
    """V12: live counter for the 5 royal sections."""
    _ensure_event_v12_columns()
    where = []
    params: list[Any] = []
    if hide_ended:
        now = time.time()
        where.append(
            "(end_date IS NULL OR end_date = 0 OR end_date > ? "
            " OR (start_date IS NOT NULL AND start_date > ?))"
        )
        params.extend([now - 3600, now - 6 * 3600])
    if only_available:
        where.append("(has_availability IS NULL OR has_availability = 1)")

    sql = "SELECT royal_category, COUNT(*) AS c FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY royal_category"

    out = {"sports": 0, "concerts": 0, "theater": 0,
           "experiences": 0, "exhibitions": 0}
    try:
        with _conn() as con:
            for r in con.execute(sql, tuple(params)).fetchall():
                key = (r["royal_category"] if isinstance(r, dict)
                       else r[0]) or ""
                cnt = (r["c"] if isinstance(r, dict) else r[1]) or 0
                if key in out:
                    out[key] = int(cnt)
    except Exception as e:
        log.debug(f"count_events_by_royal_category: {e}")
    return out


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
# Drop watchers
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
# Bot settings
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
# Event blocks selection
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
# Seat maps cache
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


# Backwards-compat alias for any caller still referencing the V11 helper.
_ensure_event_v11_columns = _ensure_event_v12_columns


# Initialize on import so any module that imports us gets a ready DB
init_db()
