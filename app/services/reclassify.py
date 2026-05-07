"""
V13 — One-shot reclassification of legacy NULL royal_category rows.

Background job that scans the events table for rows where
royal_category IS NULL (legacy V10/V11 imports) and reapplies the
V12 classify_event() heuristic. Runs once at startup, then exits.

Safe to call repeatedly: rows that already carry a royal_category are
skipped, and the operation is wrapped in SAVEPOINTs at the DB layer so
a malformed row cannot poison the whole batch.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.core.db import connect as _conn
from app.core.storage import _ensure_event_v12_columns
from app.services.event_discovery import classify_event

log = logging.getLogger("reclassify")


async def reclassify_null_categories(batch_size: int = 100,
                                     delay_between_batches: float = 0.5) -> int:
    """Walk every event with royal_category IS NULL and re-classify it.

    Returns the count of rows updated. Yields control between batches
    so the FastAPI event loop stays responsive on Render's small CPU.
    """
    # Belt-and-suspenders: ensure V12 columns exist before SELECT.
    _ensure_event_v12_columns()
    # Defer slightly so the rest of startup completes first.
    await asyncio.sleep(8)

    started = time.time()
    total_updated = 0
    offset = 0

    while True:
        try:
            with _conn() as con:
                rows = con.execute(
                    "SELECT slug, title, sub_title, category, url "
                    "FROM events "
                    "WHERE royal_category IS NULL OR royal_category = '' "
                    "LIMIT ?",
                    (batch_size,),
                ).fetchall()
        except Exception as e:
            log.warning(f"reclassify SELECT failed: {e}")
            return total_updated

        if not rows:
            break

        batch_updates = 0
        for r in rows:
            d = dict(r) if not isinstance(r, dict) else r
            slug = d.get("slug")
            if not slug:
                continue
            try:
                key = classify_event(
                    title=d.get("title") or "",
                    sub_title=d.get("sub_title") or "",
                    webook_category=d.get("category") or "",
                    url=d.get("url") or "",
                )
            except Exception as e:
                log.debug(f"classify failed for {slug}: {e}")
                continue

            try:
                with _conn() as con:
                    con.execute(
                        "UPDATE events SET royal_category = ? WHERE slug = ?",
                        (key, slug),
                    )
                batch_updates += 1
            except Exception as e:
                log.debug(f"update {slug} failed: {e}")

        total_updated += batch_updates
        log.info(
            f"♻️  reclassified batch: +{batch_updates} "
            f"(running total: {total_updated})"
        )

        if len(rows) < batch_size:
            break
        offset += batch_size
        await asyncio.sleep(delay_between_batches)

    elapsed = time.time() - started
    log.info(
        f"✅ reclassify_null_categories done — updated {total_updated} rows "
        f"in {elapsed:.1f}s"
    )
    return total_updated
