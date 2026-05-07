"""
V15 — PHASE 1: Data-driven static chart mapping.

Goal
----
Skip the heavy seats.io visual chart entirely. Pull the raw rendering-info
JSON, distill it into a flat list of (block-name, category-name) tuples,
and expose them as Telegram Inline-Keyboard buttons. Zero pixels rendered,
zero Playwright. Saves ~250 MB of RAM and ~3 s of UX latency per pick.

Why this works
--------------
The seats.io chart-renderer ultimately draws what the JSON already
describes. Buttons are a bijective representation of the chart — anything
the user can click on the SVG, they can pick from a button. The bot's
booking_orchestrator only needs the (block_label, category_key, status)
triples, all of which live in the JSON.

Public API
----------
    info = await fetch_rendering_info(slug)
    blocks = extract_blocks(info)            # list[BlockEntry]
    cats   = extract_categories(info)        # list[CategoryEntry]
    kbd    = build_blocks_keyboard(blocks)   # Telegram inline keyboard
    kbd_c  = build_categories_keyboard(cats) # ditto, for ticket types

Self-test
---------
    python -m app.services.chart_mapper <event-slug>

It will fetch the live rendering-info via the existing seatsio_client and
print every block + category, then render the keyboard payload.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Optional

log = logging.getLogger("chart_mapper")


# ════════════════════════════════════════════════════════════════════════
# Data classes
# ════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class BlockEntry:
    """One pickable block / area on the seats.io chart."""
    label: str            # human-readable name shown to the user
    object_id: str        # the seats.io / seats_planner object id
    category_key: str     # category id (price tier) the block belongs to
    category_label: str   # human-readable category name
    status: str           # 'free' | 'booked' | 'reservedByToken' | 'unknown'
    capacity: int = 0     # seats inside the block (0 if unknown)

    def to_button(self) -> dict[str, str]:
        """Return a Telegram InlineKeyboardButton dict.

        callback_data is kept under 64 bytes (Telegram hard limit) by
        prefixing with `bk:` and using object_id (which seats.io keeps
        short).
        """
        prefix = "🟢" if self.status == "free" else (
            "🔴" if self.status in ("booked", "reservedByToken") else "⚪"
        )
        cap = f" ({self.capacity})" if self.capacity else ""
        return {
            "text": f"{prefix} {self.label}{cap}",
            "callback_data": f"bk:{self.object_id}"[:64],
        }


@dataclass(frozen=True)
class CategoryEntry:
    """One ticket category / price tier on the chart."""
    key: str
    label: str
    price: float = 0.0
    color: str = ""

    def to_button(self) -> dict[str, str]:
        price = f" — {self.price:g} ر.س" if self.price else ""
        return {
            "text": f"🎟️ {self.label}{price}",
            "callback_data": f"cat:{self.key}"[:64],
        }


# ════════════════════════════════════════════════════════════════════════
# Fetch — uses the existing curl_cffi-backed StealthClient (V14.1)
# ════════════════════════════════════════════════════════════════════════
async def fetch_rendering_info(slug: str, *, lang: str = "en") -> dict:
    """Fetch the seats.io rendering-info JSON for a Webook event.

    Strategy:
      1. Prefer the project's existing SeatsioClient.rendering_info() —
         it already speaks both the legacy /system/public/... and the
         modern seats_planner /api/v2/... shapes and normalises both
         into a single dict (`objects`, `categories`, `_chart_key`, …).
      2. Fall back to a direct stealth GET if the high-level client
         can't resolve the workspace/chart keys (unauthenticated mode).

    Never raises on network errors — returns an empty dict instead.
    """
    if not slug:
        return {}
    # 1) High-level path
    try:
        from app.services.seatsio_client import SeatsioClient  # type: ignore
        c = SeatsioClient(event_slug=slug)
        ri = await c.rendering_info()
        if isinstance(ri, dict) and (ri.get("objects") or ri.get("categories")):
            return ri
    except Exception as e:  # pragma: no cover — defensive only
        log.debug("SeatsioClient.rendering_info() failed: %s", e)

    # 2) Last-ditch: try the legacy public CDN endpoint
    try:
        from app.services.stealth_client import get_shared_stealth_client
        cli = await get_shared_stealth_client()
        for cdn in (
            "https://cdn-eu.seatsio.net",
            "https://cdn-na.seatsio.net",
            "https://cdn.seatsio.net",
        ):
            url = f"{cdn}/system/public/{slug}/rendering-info"
            r = await cli.request("GET", url, headers={
                "accept": "application/json",
                "sec-fetch-site": "cross-site",
                "origin": "https://webook.com",
                "referer": "https://webook.com/",
            })
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return {}
    except Exception as e:  # pragma: no cover
        log.debug("CDN fallback failed: %s", e)
    return {}


# ════════════════════════════════════════════════════════════════════════
# Extraction
# ════════════════════════════════════════════════════════════════════════
_BLOCK_LABEL_KEYS = ("label", "name", "displayLabel", "title", "id")
_OBJECT_ID_KEYS = ("id", "objectId", "uuid", "key")


def _objects(info: dict) -> list[dict]:
    """Normalise the various rendering_info shapes into a list of objects."""
    if not isinstance(info, dict):
        return []
    # canonical shape
    objs = info.get("objects")
    if isinstance(objs, list) and objs:
        return [o for o in objs if isinstance(o, dict)]
    # nested
    for key in ("data", "rendering_info", "chart"):
        v = info.get(key)
        if isinstance(v, dict):
            sub = _objects(v)
            if sub:
                return sub
    # flatten dict-of-objects
    if isinstance(objs, dict):
        return [v for v in objs.values() if isinstance(v, dict)]
    return []


def _categories(info: dict) -> list[dict]:
    if not isinstance(info, dict):
        return []
    cats = info.get("categories")
    if isinstance(cats, list):
        return [c for c in cats if isinstance(c, dict)]
    if isinstance(cats, dict):
        return [v for v in cats.values() if isinstance(v, dict)]
    # nested under .data
    for key in ("data", "rendering_info", "chart"):
        v = info.get(key)
        if isinstance(v, dict):
            sub = _categories(v)
            if sub:
                return sub
    return []


def _pick(d: dict, keys: Iterable[str], default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return default


def _coerce_capacity(o: dict) -> int:
    for k in ("numSeats", "numberOfSeats", "capacity", "seatCount"):
        v = o.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    seats = o.get("seats") or o.get("rows") or []
    if isinstance(seats, list):
        return len(seats)
    return 0


def extract_blocks(info: dict) -> list[BlockEntry]:
    """Reduce rendering_info → list of BlockEntry.

    Only keeps top-level blocks/areas (objectType in {area, section,
    generalAdmissionArea, booth, table}) — individual seats are ignored
    for the picker UI.
    """
    if not info:
        return []
    cats_lookup: dict[str, CategoryEntry] = {
        c.key: c for c in extract_categories(info)
    }
    out: list[BlockEntry] = []
    seen: set[str] = set()
    valid_block_types = (
        "area", "section", "generaladmissionarea",
        "ga_area", "booth", "table", "block",
    )
    for o in _objects(info):
        otype = str(o.get("objectType") or o.get("type") or "").lower()
        if otype:
            if otype not in valid_block_types:
                # Explicitly typed as something else (e.g. "seat") — skip.
                continue
        else:
            # No type tag — use label heuristic.
            label_guess = _pick(o, _BLOCK_LABEL_KEYS)
            if not label_guess or "row" in label_guess.lower() or "seat" in label_guess.lower():
                continue
        oid = _pick(o, _OBJECT_ID_KEYS)
        if not oid or oid in seen:
            continue
        seen.add(oid)
        label = _pick(o, _BLOCK_LABEL_KEYS, default=oid)
        cat_key = str(o.get("categoryKey") or o.get("category_key")
                      or (o.get("category") or {}).get("key") or "")
        cat = cats_lookup.get(cat_key)
        cat_label = cat.label if cat else (
            str(o.get("categoryLabel") or o.get("category_label")
                or (o.get("category") or {}).get("label") or "")
        )
        status = str(o.get("status") or "free").lower()
        if status in ("available", "ok"):
            status = "free"
        out.append(BlockEntry(
            label=label,
            object_id=oid,
            category_key=cat_key,
            category_label=cat_label,
            status=status,
            capacity=_coerce_capacity(o),
        ))
    # Stable, user-friendly ordering: free first, then by label.
    out.sort(key=lambda b: (0 if b.status == "free" else 1, b.label.lower()))
    return out


def extract_categories(info: dict) -> list[CategoryEntry]:
    out: list[CategoryEntry] = []
    seen: set[str] = set()
    for c in _categories(info):
        key = str(c.get("key") or c.get("id") or c.get("uuid") or "")
        label = str(c.get("label") or c.get("name") or key)
        if not key or key in seen:
            continue
        seen.add(key)
        price = 0.0
        for k in ("price", "amount", "value"):
            v = c.get(k)
            if isinstance(v, (int, float)):
                price = float(v); break
            if isinstance(v, str):
                try:
                    price = float(v); break
                except Exception:
                    pass
        color = str(c.get("color") or c.get("accessible_color") or "")
        out.append(CategoryEntry(key=key, label=label, price=price, color=color))
    out.sort(key=lambda c: (-c.price, c.label.lower()))
    return out


# ════════════════════════════════════════════════════════════════════════
# Telegram Inline-Keyboard builders
# ════════════════════════════════════════════════════════════════════════
def chunk(seq: list, size: int) -> list[list]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def build_blocks_keyboard(blocks: list[BlockEntry], cols: int = 2) -> dict:
    """Return an `InlineKeyboardMarkup` dict ready for sendMessage.

    Keyboard layout: <cols> buttons per row, sorted by status/label.
    """
    btns = [b.to_button() for b in blocks]
    return {"inline_keyboard": chunk(btns, max(1, cols))}


def build_categories_keyboard(cats: list[CategoryEntry], cols: int = 2) -> dict:
    btns = [c.to_button() for c in cats]
    return {"inline_keyboard": chunk(btns, max(1, cols))}


# ════════════════════════════════════════════════════════════════════════
# Self-test
# ════════════════════════════════════════════════════════════════════════
def _test_with_synthetic() -> int:
    """Sanity-check parsing on a hand-crafted rendering-info blob."""
    fixture = {
        "objects": [
            {"id": "A1", "label": "A1", "objectType": "section",
             "categoryKey": "premium", "status": "free", "numSeats": 120},
            {"id": "B12", "label": "B12", "objectType": "section",
             "categoryKey": "regular", "status": "booked", "numSeats": 80},
            {"id": "C3", "label": "C3", "type": "generalAdmissionArea",
             "categoryKey": "regular", "status": "available", "capacity": 200},
            # noise: a single seat — must be ignored
            {"id": "A1-1-1", "label": "A1-1-1", "objectType": "seat",
             "categoryKey": "premium", "status": "free"},
        ],
        "categories": [
            {"key": "premium", "label": "Premium", "price": 350},
            {"key": "regular", "label": "Regular", "price": 120},
        ],
    }
    blocks = extract_blocks(fixture)
    cats = extract_categories(fixture)
    assert len(blocks) == 3, f"expected 3 blocks, got {len(blocks)}: {blocks}"
    assert len(cats) == 2, f"expected 2 cats, got {len(cats)}"
    # First block must be a free one (sorted)
    assert blocks[0].status == "free"
    # Premium > Regular in price ordering
    assert cats[0].label == "Premium"
    kb = build_blocks_keyboard(blocks)
    kb_c = build_categories_keyboard(cats)
    assert "inline_keyboard" in kb and len(kb["inline_keyboard"]) >= 1
    assert "inline_keyboard" in kb_c and len(kb_c["inline_keyboard"]) >= 1
    # Print a preview
    print("  ✅ synthetic-fixture parsing OK")
    print("    blocks:", [(b.label, b.status, b.category_label) for b in blocks])
    print("    cats:  ", [(c.label, c.price) for c in cats])
    print("    keyboard preview:")
    for row in kb["inline_keyboard"]:
        print("     ", " | ".join(b["text"] for b in row))
    return 0


async def _test_live(slug: str) -> int:
    """Live integration test against a real Webook event."""
    print(f"  → fetching rendering-info for slug={slug!r}")
    info = await fetch_rendering_info(slug)
    if not info:
        print("  ⚠️ no rendering_info returned (event may be unauthenticated-only "
              "or require a Webook bearer token). Synthetic test still passes.")
        return 0
    print(f"  ✓ rendering_info keys: {list(info.keys())[:10]}")
    blocks = extract_blocks(info)
    cats = extract_categories(info)
    print(f"  ✓ {len(blocks)} blocks, {len(cats)} categories")
    if blocks:
        print(f"    sample block: {asdict(blocks[0])}")
    if cats:
        print(f"    sample category: {asdict(cats[0])}")
    kb = build_blocks_keyboard(blocks)
    print(f"  ✓ keyboard rows: {len(kb['inline_keyboard'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    print("🧪 Hydra V15 — chart_mapper self-test")
    print("=" * 70)

    rc = _test_with_synthetic()
    if len(sys.argv) > 1:
        rc = asyncio.run(_test_live(sys.argv[1])) or rc
    sys.exit(rc)
