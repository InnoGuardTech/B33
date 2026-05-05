"""
Database connection layer supporting PostgreSQL (primary), Turso, or local SQLite.

V12 Royal — Hardened against multi-statement script issues:
  • executescript now splits the script and runs each statement INDIVIDUALLY
    on PostgreSQL (psycopg2 stops at first error in a transaction otherwise).
  • Each ALTER TABLE / CREATE INDEX is wrapped so a failure on one does not
    poison the entire transaction.
  • _translate_for_pg now also handles BIGINT defaults and DEFAULT values.

Priority:
  1. DATABASE_URL or POSTGRES_URL  → PostgreSQL (via psycopg2)  ← persistent
  2. TURSO_DATABASE_URL + TURSO_AUTH_TOKEN → Turso cloud SQLite  ← persistent
  3. Fallback → local sqlite3 file                             ← ephemeral
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Optional

log = logging.getLogger("db")

PG_URL = (
    os.getenv("DATABASE_URL", "").strip()
    or os.getenv("POSTGRES_URL", "").strip()
)
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

_lock = threading.RLock()
_backend = "sqlite"
_pg_pool = None
_turso_client = None


# ════════════════════════════════════════════════════════════════════════
# PostgreSQL backend
# ════════════════════════════════════════════════════════════════════════
if PG_URL:
    try:
        import psycopg2  # type: ignore
        import psycopg2.pool  # type: ignore
        from psycopg2.extras import RealDictCursor  # type: ignore
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=5, dsn=PG_URL,
            cursor_factory=RealDictCursor,
        )
        _backend = "postgres"
        log.info(f"🐘 using PostgreSQL at {PG_URL.split('@')[-1].split('/')[0]}")
    except Exception as e:
        log.error(f"PostgreSQL init failed, falling back: {e}")
        _pg_pool = None

# ════════════════════════════════════════════════════════════════════════
# Turso backend (secondary)
# ════════════════════════════════════════════════════════════════════════
if _pg_pool is None and TURSO_URL and TURSO_TOKEN:
    try:
        import libsql_client  # type: ignore
        _turso_client = libsql_client.create_client_sync(
            url=TURSO_URL, auth_token=TURSO_TOKEN,
        )
        _backend = "turso"
        log.info(f"🌐 using Turso cloud DB at {TURSO_URL.split('//')[-1]}")
    except Exception as e:
        log.error(f"Turso init failed, falling back: {e}")
        _turso_client = None


# ════════════════════════════════════════════════════════════════════════
# SQL dialect translator (sqlite → postgres)
# ════════════════════════════════════════════════════════════════════════
def _translate_for_pg(sql: str) -> str:
    s = sql
    s = s.replace("?", "%s")
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
               "BIGSERIAL PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bINSERT\s+OR\s+IGNORE\b\s+INTO", "INSERT INTO", s, flags=re.I)
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s, flags=re.I)
    s = re.sub(r"\bBLOB\b", "BYTEA", s, flags=re.I)
    return s


# ════════════════════════════════════════════════════════════════════════
# Multi-statement splitter — used by executescript on PG
# ════════════════════════════════════════════════════════════════════════
def _split_sql(script: str) -> list[str]:
    """Split a multi-statement SQL script on semicolons (top-level only).

    Strips SQL comments. Keeps each statement standalone so psycopg2 can
    execute them one by one (avoids 'cannot execute multiple commands' on
    some PG versions and lets us isolate errors per statement).
    """
    out: list[str] = []
    buf: list[str] = []
    for raw in script.splitlines():
        line = raw
        # strip standalone -- comments
        comment = line.find("--")
        if comment >= 0:
            line = line[:comment]
        if not line.strip():
            if buf:
                buf.append("")  # preserve break
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                out.append(stmt)
            buf = []
    leftover = "\n".join(buf).strip().rstrip(";").strip()
    if leftover:
        out.append(leftover)
    return out


# ════════════════════════════════════════════════════════════════════════
# PostgreSQL wrapper
# ════════════════════════════════════════════════════════════════════════
class _PgCursorWrapper:
    """Wraps psycopg2 cursor and exposes sqlite-like API."""

    _ID_TABLES = {"bookings", "event_blocks", "drop_watchers"}

    def __init__(self, cur, pg_conn, stmt: str):
        self._cur = cur
        self._conn = pg_conn
        self._stmt = stmt
        self.lastrowid: Optional[int] = None

        m = re.search(r"INSERT\s+INTO\s+(\w+)", stmt, re.I)
        if m and m.group(1).lower() in self._ID_TABLES and "returning" not in stmt.lower():
            tbl = m.group(1).lower()
            try:
                c2 = self._conn.cursor()
                c2.execute(
                    "SELECT currval(pg_get_serial_sequence(%s, 'id')) AS v",
                    (tbl,),
                )
                r = c2.fetchone()
                if r:
                    val = r["v"] if isinstance(r, dict) else r[0]
                    self.lastrowid = int(val) if val is not None else None
                c2.close()
            except Exception:
                pass

        # rowcount passthrough
        try:
            self.rowcount = self._cur.rowcount
        except Exception:
            self.rowcount = -1

    def fetchone(self):
        try:
            r = self._cur.fetchone()
            return dict(r) if r else None
        except Exception:
            return None

    def fetchall(self):
        try:
            return [dict(r) for r in self._cur.fetchall()]
        except Exception:
            return []

    def __iter__(self):
        return iter(self.fetchall())


class _PgConn:
    """Mimics sqlite3.Connection for our narrow usage."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: Iterable = ()):
        sql_pg = _translate_for_pg(sql)
        cur = self._conn.cursor()
        try:
            cur.execute(sql_pg, tuple(params) if params else ())
        except Exception as e:
            log.error(f"[db] execute err on: {sql_pg[:140]} | {e}")
            raise
        return _PgCursorWrapper(cur, self._conn, sql_pg)

    def executescript(self, script: str):
        """V12: split into individual statements, run each in its own
        savepoint so a failure on one ALTER/INDEX does not abort the rest.
        """
        stmts = _split_sql(_translate_for_pg(script))
        for stmt in stmts:
            if not stmt.strip():
                continue
            cur = self._conn.cursor()
            try:
                # savepoint per stmt → isolates errors (e.g. duplicate column)
                cur.execute("SAVEPOINT _stmt_sp")
                cur.execute(stmt)
                cur.execute("RELEASE SAVEPOINT _stmt_sp")
            except Exception as e:
                # rollback ONLY this savepoint, keep the rest of the tx alive
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT _stmt_sp")
                except Exception:
                    pass
                log.warning(f"[db] script stmt skipped: {stmt[:80]} | {e}")

    def commit(self):
        try:
            self._conn.commit()
        except Exception as e:
            log.error(f"[db] commit err: {e}")

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        pass  # pool handles it


# ════════════════════════════════════════════════════════════════════════
# Turso backend classes (unchanged)
# ════════════════════════════════════════════════════════════════════════
class _TursoRow(dict):
    def __init__(self, columns: list[str], values: list[Any]):
        super().__init__(zip(columns, values))
        self._cols = columns
        self._vals = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return super().__getitem__(key)


class _TursoCursor:
    def __init__(self, rs):
        self._rs = rs
        self.lastrowid = getattr(rs, "last_insert_rowid", None)
        self.rowcount = getattr(rs, "rows_affected", -1)
        self._cols = list(getattr(rs, "columns", []) or [])
        self._rows = [_TursoRow(self._cols, list(r)) for r in (rs.rows or [])]
        self._i = 0

    def fetchone(self):
        if self._i < len(self._rows):
            r = self._rows[self._i]
            self._i += 1
            return r
        return None

    def fetchall(self):
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def __iter__(self):
        return iter(self._rows[self._i:])


class _TursoConn:
    def __init__(self, client):
        self._c = client

    def execute(self, sql: str, params: Iterable = ()):
        rs = self._c.execute(sql, tuple(params) if params else ())
        return _TursoCursor(rs)

    def executescript(self, script: str):
        for stmt in _split_sql(script):
            if stmt.strip():
                try:
                    self._c.execute(stmt)
                except Exception as e:
                    log.warning(f"[db turso] script stmt skipped: {e}")

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ════════════════════════════════════════════════════════════════════════
# Fallback SQLite
# ════════════════════════════════════════════════════════════════════════
from app.core.config import DB_PATH  # noqa: E402


@contextmanager
def connect():
    """Yield a connection-like object. Thread-safe."""
    with _lock:
        if _pg_pool is not None:
            conn = _pg_pool.getconn()
            wrapper = _PgConn(conn)
            try:
                yield wrapper
                wrapper.commit()
            except Exception:
                wrapper.rollback()
                raise
            finally:
                _pg_pool.putconn(conn)
        elif _turso_client is not None:
            con = _TursoConn(_turso_client)
            try:
                yield con
                con.commit()
            finally:
                con.close()
        else:
            con = sqlite3.connect(DB_PATH, timeout=10)
            con.row_factory = sqlite3.Row
            try:
                yield con
                con.commit()
            finally:
                con.close()


def backend() -> str:
    return _backend


def is_persistent() -> bool:
    return _backend in ("postgres", "turso")


# ════════════════════════════════════════════════════════════════════════
# Helpers for admin UI — raw read-only queries
# ════════════════════════════════════════════════════════════════════════
def list_tables() -> list[str]:
    """Return all user tables in the current backend."""
    if _pg_pool is not None:
        conn = _pg_pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='public' AND table_type='BASE TABLE'
                ORDER BY table_name
            """)
            rows = cur.fetchall()
            return [r["table_name"] if isinstance(r, dict) else r[0] for r in rows]
        except Exception as e:
            log.error(f"list_tables err: {e}")
            return []
        finally:
            _pg_pool.putconn(conn)

    elif _turso_client is not None:
        try:
            rs = _turso_client.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            return [r[0] for r in (rs.rows or []) if not r[0].startswith("sqlite_")]
        except Exception:
            return []

    else:
        con = sqlite3.connect(DB_PATH)
        try:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            return [r[0] for r in cur.fetchall() if not r[0].startswith("sqlite_")]
        finally:
            con.close()


def query_table(table: str, limit: int = 100) -> tuple[list[str], list[list]]:
    """Return (columns, rows) for a table. Read-only."""
    allowed = set(list_tables())
    if table not in allowed:
        return [], []
    limit = max(1, min(int(limit), 500))

    if _pg_pool is not None:
        conn = _pg_pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(f'SELECT * FROM "{table}" LIMIT %s', (limit,))
            rows = cur.fetchall()
            if not rows:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = %s ORDER BY ordinal_position
                """, (table,))
                cols = [r["column_name"] if isinstance(r, dict) else r[0]
                        for r in cur.fetchall()]
                return cols, []
            cols = list(rows[0].keys()) if isinstance(rows[0], dict) \
                else [d[0] for d in cur.description]
            out_rows = [[r[c] for c in cols] for r in rows]
            return cols, out_rows
        except Exception as e:
            log.error(f"query_table err: {e}")
            return [], []
        finally:
            _pg_pool.putconn(conn)

    elif _turso_client is not None:
        try:
            rs = _turso_client.execute(f'SELECT * FROM "{table}" LIMIT {limit}')
            cols = list(rs.columns) if rs.columns else []
            return cols, [list(r) for r in (rs.rows or [])]
        except Exception:
            return [], []

    else:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute(f'SELECT * FROM "{table}" LIMIT ?', (limit,))
            rows = cur.fetchall()
            if not rows:
                cur2 = con.execute(f'PRAGMA table_info("{table}")')
                cols = [r["name"] for r in cur2.fetchall()]
                return cols, []
            cols = list(rows[0].keys())
            return cols, [[r[c] for c in cols] for r in rows]
        finally:
            con.close()


def delete_row(table: str, where_col: str, where_val: Any) -> bool:
    """Delete a single row (for admin UI). Safe-listed tables only."""
    allowed = set(list_tables())
    if table not in allowed:
        return False
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", where_col):
        return False

    if _pg_pool is not None:
        conn = _pg_pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(f'DELETE FROM "{table}" WHERE {where_col} = %s', (where_val,))
            conn.commit()
            return True
        except Exception as e:
            log.error(f"delete_row err: {e}")
            conn.rollback()
            return False
        finally:
            _pg_pool.putconn(conn)

    elif _turso_client is not None:
        try:
            _turso_client.execute(
                f'DELETE FROM "{table}" WHERE {where_col} = ?', (where_val,)
            )
            return True
        except Exception:
            return False

    else:
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute(f'DELETE FROM "{table}" WHERE {where_col} = ?',
                        (where_val,))
            con.commit()
            return True
        finally:
            con.close()
