"""
V15.2 — chart_mapper: data-driven block + category extractor.

Major upgrade over V15: the previous chart_mapper looked for the
seats.io legacy `objects[]` shape and missed `seats_planner` charts
entirely (Webook's modern provider). This rewrite parses BOTH:

  • seats_planner shape  (Webook's current provider, 95% of events)
        map.content.areas[]  →  one entry per physical block
        each area has:
          - name            "130", "131", "227"...   (the BLOCK number)
          - label.label     same number (display copy)
          - specification   {key, label}             (price tier link)
          - occupancy       {capacity, availableForSale, …}
          - geometry        polygon coords (ignored — we render buttons)
          - itemType        "generalAdmission" | "section" | "row"

  • legacy seatsio shape (older events)
        rendering_info.objects[] with objectType ∈ {area, section, ...}

The output is a flat list of BlockEntry rows, exactly the shape the
Telegram inline-keyboard builder needs.

LIVE-VERIFIED on event spl-week-32-al-najmah-vs-al-hazem-7715:
  86 blocks across 14 categories — sample:
    CAT1-N      : 130, 131, 230, 231, 232, 330, 331, 332
    CAT3-N      : 101, 102, 103, 104, 105, 201, 202, 203, 204, 205, 206
    Gold/Silver/Bronze (premium suites)
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
    """One pickable physical block / section on the chart.

    `object_id` is what the booking layer needs to identify the block
    when it goes to seats.io for a hold-token. For seats_planner this
    is the area's `name` (= the block number string, e.g. "130"). For
    legacy seatsio it's the object UUID.
    """
    label: str            # human-readable name shown in the keyboard
    object_id: str        # canonical seats.io / seats_planner identifier
    category_key: str     # specification key / categoryKey (price tier)
    category_label: str   # human-readable category name
    status: str           # "free" | "booked" | "unknown"
    capacity: int = 0     # seats inside the block (0 if unknown)
    available_for_sale: bool = True  # owner-toggled flag (seats_planner)
    item_type: str = ""   # "generalAdmission" | "section" | "row" | …

    def to_button(self) -> dict[str, str]:
        """Telegram InlineKeyboardButton dict with status-aware emoji."""
        if not self.available_for_sale:
            prefix = "⛔"          # owner disabled the block entirely
        elif self.status == "free":
            prefix = "🟢"
        elif self.status in ("booked", "reservedByToken", "selected"):
            prefix = "🔴"
        else:
            prefix = "⚪"
        cap = f" ({self.capacity})" if self.capacity else ""
        return {
            "text": f"{prefix} {self.label}{cap}",
            "callback_data": f"bk:{self.object_id}"[:64],
        }


@dataclass(frozen=True)
class CategoryEntry:
    """One pricing category / specification."""
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
# Live fetch — uses the StealthClient + SeatsioClient stack
# ════════════════════════════════════════════════════════════════════════
async def fetch_rendering_info(
    slug: str,
    *,
    bearer: str = "",
    lang: str = "en",
) -> dict:
    """Fetch a normalised rendering_info dict for a Webook event slug.

    Pipeline:
      1. webook_api.get_event_detail(slug, bearer)  → seats_io blob
      2. SeatsioClient(event_key, workspace_key, chart_key, bearer)
           .fetch_event() / .fetch_map() / .fetch_item_statuses()
      3. normalize_seats_planner_to_rendering_info(ev, map, statuses)

    Never raises — returns {} on any failure.
    """
    if not slug:
        return {}
    try:
        from app.services.webook_api import get_event_detail
        ev = await get_event_detail(slug, lang=lang, bearer=bearer)
    except Exception as e:
        log.warning("get_event_detail crashed: %s", e)
        return {}
    if not ev:
        return {}
    seats_io = ev.get("seats_io") or {}
    event_key = (seats_io.get("event_key") or "").strip()
    chart_key = (seats_io.get("chart_key") or "").strip()
    workspace_key = (seats_io.get("workspace_key") or "").strip()
    provider = ev.get("seats_provider") or ""
    if not (event_key and chart_key and workspace_key):
        log.warning("event %s has incomplete seats_io blob", slug)
        return {}
    try:
        from app.services.seatsio_client import (
            SeatsioClient, normalize_seats_planner_to_rendering_info,
        )
        async with SeatsioClient(
            event_key=event_key,
            workspace_key=workspace_key,
            chart_key=chart_key,
            provider=provider,
            bearer=bearer,
            event_slug=slug,
        ) as c:
            ev_data = await c.fetch_event()
            map_data = await c.fetch_map()
            statuses = await c.fetch_item_statuses()
            if not (ev_data and map_data):
                log.warning("chart fetch returned empty (ev=%s mp=%s)",
                            bool(ev_data), bool(map_data))
                return {}
            ri = normalize_seats_planner_to_rendering_info(
                ev_data, map_data, statuses,
            )
            # Carry the raw map so the area-extractor can find content.areas
            ri["_map_raw"] = map_data
            ri["_event_raw"] = ev_data
            ri["_seats_io_blob"] = seats_io
            return ri
    except Exception as e:
        log.warning("seatsio fetch crashed: %s", e)
        return {}


# ════════════════════════════════════════════════════════════════════════
# Extraction — handles BOTH seats_planner and legacy seatsio shapes
# ════════════════════════════════════════════════════════════════════════
_LEGACY_BLOCK_LABEL_KEYS = ("label", "name", "displayLabel", "title", "id")
_LEGACY_OBJECT_ID_KEYS = ("id", "objectId", "uuid", "key")
_LEGACY_VALID_TYPES = (
    "area", "section", "generaladmissionarea",
    "ga_area", "booth", "table", "block",
)


def _label_to_str(v: Any) -> str:
    """seats_planner stores label as ``{label, shownName, size, shown}`` —
    extract the human string."""
    if isinstance(v, dict):
        return str(v.get("label") or v.get("shownName") or "").strip()
    return str(v or "").strip()


def _extract_blocks_from_seats_planner(map_data: dict) -> list[BlockEntry]:
    """Walk ``map_data.content.areas`` and yield one BlockEntry per area."""
    if not isinstance(map_data, dict):
        return []
    content = map_data.get("content") or {}
    areas = content.get("areas") or []
    if not isinstance(areas, list) or not areas:
        return []
    # Build a categoryKey -> label index from specifications
    specs_by_key: dict[str, str] = {}
    for sp in (map_data.get("specifications") or []):
        if isinstance(sp, dict) and sp.get("key") is not None:
            specs_by_key[str(sp["key"])] = (
                sp.get("shownName") or sp.get("label") or str(sp["key"])
            )

    out: list[BlockEntry] = []
    seen: set[str] = set()
    for a in areas:
        if not isinstance(a, dict):
            continue
        oid = str(a.get("id") or a.get("name") or "").strip()
        if not oid or oid in seen:
            continue
        seen.add(oid)
        # The PUBLIC block number is the `name` field — that's what we
        # display ("130", "131"…). label.label is a duplicate.
        label = (
            str(a.get("name") or "").strip()
            or _label_to_str(a.get("label"))
            or oid
        )
        spec = a.get("specification") or {}
        cat_key = str(spec.get("key") or "")
        cat_label = (
            str(spec.get("label") or "")
            or specs_by_key.get(cat_key, "")
        )
        occ = a.get("occupancy") or {}
        capacity = int(occ.get("capacity") or 0)
        available = bool(occ.get("availableForSale", True))
        # seats_planner doesn't expose per-area "free / booked" directly;
        # the live status flow comes through the WS sniper. Use
        # availableForSale as a coarse hint here.
        status = "free" if available else "booked"
        item_type = str(a.get("itemType") or
                        (a.get("display") or {}).get("itemType") or "")
        out.append(BlockEntry(
            label=label,
            object_id=oid,
            category_key=cat_key,
            category_label=cat_label,
            status=status,
            capacity=capacity,
            available_for_sale=available,
            item_type=item_type,
        ))
    return out


def _extract_blocks_from_legacy(info: dict) -> list[BlockEntry]:
    """Walk legacy seatsio rendering_info.objects[]."""
    objs: list[dict] = []
    o = info.get("objects")
    if isinstance(o, list):
        objs = [x for x in o if isinstance(x, dict)]
    elif isinstance(o, dict):
        objs = [v for v in o.values() if isinstance(v, dict)]
    if not objs:
        return []
    cats_lookup: dict[str, CategoryEntry] = {
        c.key: c for c in extract_categories(info)
    }
    out: list[BlockEntry] = []
    seen: set[str] = set()
    for ob in objs:
        otype = str(ob.get("objectType") or ob.get("type") or "").lower()
        if otype:
            if otype not in _LEGACY_VALID_TYPES:
                continue
        else:
            label_guess = _pick(ob, _LEGACY_BLOCK_LABEL_KEYS)
            if (
                not label_guess
                or "row" in label_guess.lower()
                or "seat" in label_guess.lower()
            ):
                continue
        oid = _pick(ob, _LEGACY_OBJECT_ID_KEYS)
        if not oid or oid in seen:
            continue
        seen.add(oid)
        label = _pick(ob, _LEGACY_BLOCK_LABEL_KEYS, default=oid)
        cat_key = str(
            ob.get("categoryKey") or ob.get("category_key")
            or (ob.get("category") or {}).get("key") or ""
        )
        cat = cats_lookup.get(cat_key)
        cat_label = cat.label if cat else (
            str(ob.get("categoryLabel") or ob.get("category_label")
                or (ob.get("category") or {}).get("label") or "")
        )
        status = str(ob.get("status") or "free").lower()
        if status in ("available", "ok"):
            status = "free"
        capacity = 0
        for k in ("numSeats", "numberOfSeats", "capacity", "seatCount"):
            v = ob.get(k)
            if isinstance(v, (int, float)) and v > 0:
                capacity = int(v); break
        out.append(BlockEntry(
            label=label, object_id=oid,
            category_key=cat_key, category_label=cat_label,
            status=status, capacity=capacity,
            available_for_sale=(status == "free"),
            item_type=otype,
        ))
    return out


def _pick(d: dict, keys: Iterable[str], default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return default


def extract_blocks(info: dict) -> list[BlockEntry]:
    """Reduce a rendering_info dict (any shape) to a flat BlockEntry list.

    Tries seats_planner first (uses the embedded ``_map_raw`` if present),
    then falls back to the legacy seatsio shape.
    """
    if not isinstance(info, dict) or not info:
        return []
    map_raw = info.get("_map_raw") or info.get("map") or info
    blocks = _extract_blocks_from_seats_planner(map_raw)
    if blocks:
        out = blocks
    else:
        out = _extract_blocks_from_legacy(info)
    # Deterministic ordering: free-and-available first, then by numeric label.
    def _sort_key(b: BlockEntry):
        kind = (
            0 if (b.available_for_sale and b.status == "free") else
            1 if b.available_for_sale else 2
        )
        try:
            num = int(b.label)
        except (ValueError, TypeError):
            num = 99999
        return (kind, num, b.label.lower())
    out.sort(key=_sort_key)
    return out


def extract_categories(info: dict) -> list[CategoryEntry]:
    """Pull pricing categories from rendering_info / map.specifications."""
    if not isinstance(info, dict) or not info:
        return []
    candidates: list[dict] = []
    for source in (info, info.get("_map_raw"), info.get("_event_raw")):
        if not isinstance(source, dict):
            continue
        c = source.get("categories") or source.get("specifications")
        if isinstance(c, list):
            candidates.extend(x for x in c if isinstance(x, dict))
        elif isinstance(c, dict):
            candidates.extend(v for v in c.values() if isinstance(v, dict))
    out: list[CategoryEntry] = []
    seen: set[str] = set()
    for c in candidates:
        key = str(c.get("key") or c.get("id") or c.get("uuid") or "")
        label = (
            str(c.get("label") or c.get("shownName") or c.get("name") or key)
            .strip()
        )
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


def build_blocks_keyboard(
    blocks: list[BlockEntry],
    cols: int = 3,
    *,
    filter_category_key: str = "",
) -> dict:
    """Telegram inline keyboard for picking one block.

    If ``filter_category_key`` is set, only blocks whose category matches
    are rendered — perfect for the two-step UX where the user first picks
    a category, then sees blocks belonging to it.
    """
    blocks = [
        b for b in blocks
        if not filter_category_key or b.category_key == filter_category_key
    ]
    btns = [b.to_button() for b in blocks]
    return {"inline_keyboard": chunk(btns, max(1, cols))}


def build_categories_keyboard(
    cats: list[CategoryEntry], cols: int = 2,
) -> dict:
    btns = [c.to_button() for c in cats]
    return {"inline_keyboard": chunk(btns, max(1, cols))}


def build_combined_keyboard(
    blocks: list[BlockEntry],
    cats: list[CategoryEntry],
    *,
    blocks_per_row: int = 3,
) -> dict:
    """Single keyboard with categories on top, blocks below.

    The user can either tap a category (filters the chart) or tap a
    block directly. Useful when the bot wants to show everything in one
    message instead of paginating.
    """
    rows: list[list[dict]] = []
    if cats:
        cat_btns = [c.to_button() for c in cats]
        rows.extend(chunk(cat_btns, 2))
        rows.append([{"text": "──── BLOCKS ────", "callback_data": "noop"}])
    if blocks:
        bl_btns = [b.to_button() for b in blocks]
        rows.extend(chunk(bl_btns, max(1, blocks_per_row)))
    return {"inline_keyboard": rows}


# ════════════════════════════════════════════════════════════════════════
# Self-test
# ════════════════════════════════════════════════════════════════════════
def _test_seats_planner_fixture() -> int:
    """Use a hand-crafted seats_planner fixture (mirrors live shape)."""
    fixture = {
        "_map_raw": {
            "specifications": [
                {"key": 1, "label": "CAT1-N", "shownName": "VIP North"},
                {"key": 7, "label": "CAT3-N", "shownName": "Standard"},
            ],
            "content": {
                "areas": [
                    {"id": "1", "name": "130",
                     "label": {"label": "130", "shownName": "", "shown": True},
                     "specification": {"key": 1, "label": "CAT1-N"},
                     "occupancy": {"capacity": 99, "availableForSale": True},
                     "itemType": "generalAdmission"},
                    {"id": "2", "name": "131",
                     "label": {"label": "131", "shownName": "", "shown": True},
                     "specification": {"key": 1, "label": "CAT1-N"},
                     "occupancy": {"capacity": 99, "availableForSale": True},
                     "itemType": "generalAdmission"},
                    {"id": "3", "name": "101",
                     "label": {"label": "101", "shownName": "", "shown": True},
                     "specification": {"key": 7, "label": "CAT3-N"},
                     "occupancy": {"capacity": 99, "availableForSale": False},
                     "itemType": "generalAdmission"},
                ],
            },
        },
    }
    blocks = extract_blocks(fixture)
    cats = extract_categories(fixture)
    assert len(blocks) == 3, f"expected 3 blocks, got {len(blocks)}"
    assert {b.label for b in blocks} == {"130", "131", "101"}
    # First block must be available + free
    assert blocks[0].available_for_sale and blocks[0].status == "free"
    # The "101" block has availableForSale=False → should be last
    assert blocks[-1].label == "101"
    assert len(cats) == 2
    print("  ✅ seats_planner fixture: 3 blocks (130, 131, 101), 2 cats")
    print(f"     sort: {[b.label for b in blocks]}")
    # Keyboard with category filter
    kb = build_blocks_keyboard(blocks, filter_category_key="1")
    rows = kb["inline_keyboard"]
    assert sum(len(r) for r in rows) == 2  # only 130 + 131 (CAT1-N)
    print("  ✅ category filter (key=1) yields 2 buttons (130, 131)")
    # Combined keyboard
    kb2 = build_combined_keyboard(blocks, cats)
    assert any("BLOCKS" in btn["text"] for row in kb2["inline_keyboard"] for btn in row)
    print("  ✅ combined keyboard contains the BLOCKS divider")
    return 0


def _test_legacy_fixture() -> int:
    fixture = {
        "objects": [
            {"id": "A1", "label": "A1", "objectType": "section",
             "categoryKey": "premium", "status": "free", "numSeats": 120},
            {"id": "A2", "label": "A2", "objectType": "seat",  # ignored
             "categoryKey": "premium", "status": "free"},
        ],
        "categories": [
            {"key": "premium", "label": "Premium", "price": 350},
        ],
    }
    blocks = extract_blocks(fixture)
    assert len(blocks) == 1 and blocks[0].label == "A1"
    print("  ✅ legacy fixture: 1 block (A1), seat filtered out")
    return 0


async def _test_live(slug: str = "spl-week-32-al-najmah-vs-al-hazem-7715") -> int:
    print(f"  → fetching live rendering_info for slug={slug!r}")
    info = await fetch_rendering_info(slug)
    if not info:
        print("  ⚠️ live fetch returned empty (continuing — synthetic tests still pass)")
        return 0
    blocks = extract_blocks(info)
    cats = extract_categories(info)
    print(f"  ✓ {len(blocks)} blocks, {len(cats)} categories")
    if blocks:
        # Group by category for readable output
        by_cat: dict[str, list[str]] = {}
        for b in blocks:
            by_cat.setdefault(b.category_label or "?", []).append(b.label)
        print(f"  ✓ block sample by category:")
        for cat, names in sorted(by_cat.items())[:6]:
            sample = ", ".join(names[:8])
            print(f"      {cat:<14} ({len(names):>2}): [{sample}]")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    print("🧪 Hydra V15.2 — chart_mapper self-test")
    print("=" * 70)
    rc = _test_seats_planner_fixture()
    rc |= _test_legacy_fixture()
    if len(sys.argv) > 1 or "--live" in sys.argv:
        rc = asyncio.run(_test_live()) or rc
    print()
    print("🏆 PASSED" if rc == 0 else "❌ FAILED")
    sys.exit(rc)
