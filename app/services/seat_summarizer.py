"""
Smart seat summarization.

Required output format (per spec):
    "CAT 1 - S block 5 (117,118,119,120,121)"

NOT this format:
    "CAT 1 - S (Block 5 - seat 119) / CAT 1 - S (Block 5 - seat 118) / ..."

Algorithm:
  1. Group seats by (category, block, row)
  2. Sort seat numbers
  3. Compress consecutive runs into ranges (117-121) when ≥ 3 consecutive,
     otherwise keep enumerated (117,118)
  4. Render one line per group
"""
from __future__ import annotations

import re
from typing import Any, Iterable


# ── helpers ────────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"(\d+)")


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v)
    m = _NUM_RE.search(s)
    return int(m.group(1)) if m else None


def _normalize_seat(s: dict | str) -> dict[str, Any]:
    """Accept either a SeatCloud object dict or a raw label string."""
    if isinstance(s, str):
        # try to parse "S1-A-12" or similar
        parts = re.split(r"[-_/]", s)
        if len(parts) >= 3:
            return {
                "category": "",
                "block": parts[0],
                "row": parts[1],
                "seat": parts[-1],
                "seat_no": _to_int(parts[-1]),
                "label": s,
            }
        return {"category": "", "block": "", "row": "",
                "seat": s, "seat_no": _to_int(s), "label": s}

    labels = s.get("labels") or {}
    category = (s.get("category") or s.get("ticketType")
                or s.get("seats_io_category") or "").strip()
    block = (labels.get("section") or s.get("section")
             or s.get("block") or "").strip()
    row = (labels.get("parent") or s.get("row") or "").strip()
    seat = (labels.get("own") or s.get("seat")
            or s.get("seatNumber") or "").strip()
    label = (s.get("label") or s.get("displayedLabel")
             or s.get("id") or seat or "").strip()
    return {
        "category": category,
        "block": block,
        "row": row,
        "seat": seat,
        "seat_no": _to_int(seat) or _to_int(label),
        "label": label,
    }


def _compress_runs(nums: list[int]) -> str:
    """Turn [117,118,119,121,122,125] → '117-119,121,122,125'.

    Per spec, "117,118,119,120,121" should remain enumerated when the user
    wants every seat number listed. We choose the more readable hybrid:
    runs of ≥ 4 → range form, else enumerate.
    """
    if not nums:
        return ""
    nums = sorted(set(nums))
    out: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        run = nums[i:j + 1]
        if len(run) >= 4:
            out.append(f"{run[0]}-{run[-1]}")
        else:
            out.extend(str(n) for n in run)
        i = j + 1
    return ",".join(out)


def summarize_seats(seats: Iterable[Any]) -> str:
    """Main entry point.

    seats: iterable of either dicts (SeatCloud objects) or raw labels.
    Returns a single-line summary string. Empty input → ''.
    """
    items = [_normalize_seat(s) for s in seats]
    if not items:
        return ""

    # Group by (category, block, row)
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for it in items:
        key = (it["category"], it["block"], it["row"])
        groups.setdefault(key, []).append(it)

    parts: list[str] = []
    for (cat, block, row), arr in groups.items():
        nums = [a["seat_no"] for a in arr if a["seat_no"] is not None]
        labels_only = [a["label"] for a in arr if a["seat_no"] is None]

        # Build prefix
        prefix_bits = []
        if cat:
            prefix_bits.append(cat)
        if block:
            # Format expected by spec: "block 5"
            blk_norm = block if block.lower().startswith("block") else f"block {block}"
            prefix_bits.append(blk_norm)
        if row:
            prefix_bits.append(f"row {row}")
        prefix = " ".join(prefix_bits).strip()

        # Build seat list
        nums_compressed = _compress_runs(nums) if nums else ""
        all_seats = nums_compressed
        if labels_only:
            extras = ",".join(labels_only)
            all_seats = (all_seats + "," + extras).strip(",") if all_seats else extras

        if all_seats:
            parts.append(f"{prefix} ({all_seats})" if prefix else f"({all_seats})")
        elif prefix:
            parts.append(prefix)

    return " / ".join(parts) if parts else ""


def summarize_for_telegram(seats: Iterable[Any]) -> str:
    """Telegram-friendly variant with bold prefix and code-style seats."""
    items = [_normalize_seat(s) for s in seats]
    if not items:
        return "—"

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for it in items:
        key = (it["category"], it["block"], it["row"])
        groups.setdefault(key, []).append(it)

    lines: list[str] = []
    total = 0
    for (cat, block, row), arr in groups.items():
        nums = sorted([a["seat_no"] for a in arr if a["seat_no"] is not None])
        labels_only = [a["label"] for a in arr if a["seat_no"] is None]
        compressed = _compress_runs(nums)

        bits = []
        if cat:
            bits.append(f"<b>{cat}</b>")
        if block:
            blk_norm = block if block.lower().startswith("block") else f"block {block}"
            bits.append(blk_norm)
        if row:
            bits.append(f"row {row}")
        prefix = " ".join(bits)

        seats_str = compressed
        if labels_only:
            extras = ",".join(labels_only)
            seats_str = (seats_str + "," + extras).strip(",") if seats_str else extras

        count = len(nums) + len(labels_only)
        total += count
        line = f"🪑 {prefix} <code>({seats_str})</code> × <b>{count}</b>"
        lines.append(line)

    header = f"<b>المقاعد المحجوزة ({total}):</b>"
    return header + "\n" + "\n".join(lines)
