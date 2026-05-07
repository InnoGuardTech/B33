"""
V14.1 — Bulletproof URL → slug extractor.

Reduces ALL Webook URL shapes to the canonical event slug used by
api.webook.com endpoints, e.g. `spl-week-32-al-najmah-vs-al-hazem-7715`.

Accepted inputs (all return the same slug):
  • https://webook.com/ar/sa/bur/sports-event/events/<slug>(?...)
  • https://webook.com/en/events/<slug>/book
  • https://webook.com/events/<slug>
  • webook.com/.../events/<slug>          (no scheme)
  • //webook.com/events/<slug>            (protocol-relative)
  • <slug>                                 (raw, must look like a slug)

Falsy / non-slug inputs return None.
Stdlib only — safe to import anywhere.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse, unquote

_SLUG_RE = re.compile(
    r"/events?/([A-Za-z0-9][A-Za-z0-9._\-]{1,200})",
    flags=re.IGNORECASE,
)
_RAW_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{2,200}$")
_TAIL_RE = re.compile(
    r"/(book|checkout|payment|seats?|reviews?|tickets?)$",
    flags=re.IGNORECASE,
)


def extract_slug(text: Optional[str]) -> Optional[str]:
    """Return the canonical event slug from any Webook URL or raw input.

    Returns None when no slug can be confidently extracted. Never raises.
    """
    if not text:
        return None
    try:
        s = unquote(str(text)).strip().strip('"').strip("'")
    except Exception:
        return None
    if not s:
        return None

    if " " in s:
        for tok in s.split():
            if (
                "://" in tok
                or tok.startswith("//")
                or "/events/" in tok
                or "/event/" in tok
            ):
                s = tok
                break
        else:
            return None

    looks_like_url = ("/" in s) or s.startswith(("http://", "https://", "//"))
    if looks_like_url:
        if "://" not in s:
            s = "https://" + s.lstrip("/")
        try:
            p = urlparse(s)
        except Exception:
            p = None
        if p and p.path:
            m = _SLUG_RE.search(p.path)
            if m:
                slug = m.group(1).rstrip("/").strip(".")
                slug = _TAIL_RE.sub("", slug)
                if slug:
                    return slug
        m = _SLUG_RE.search(s)
        if m:
            return m.group(1).rstrip("/")
        return None

    if _RAW_SLUG_RE.match(s) and (
        "-" in s or s.isdigit() or "_" in s or "." in s
    ):
        return s
    return None


# ════════════════════════════════════════════════════════════════════════
# Self-test
# ════════════════════════════════════════════════════════════════════════
def _self_test() -> int:
    cases = [
        ("https://webook.com/ar/sa/bur/sports-event/events/"
         "spl-week-32-al-najmah-vs-al-hazem-7715",
         "spl-week-32-al-najmah-vs-al-hazem-7715"),
        ("https://webook.com/en/events/test-slug-123", "test-slug-123"),
        ("https://webook.com/en/events/test-slug-123/", "test-slug-123"),
        ("https://webook.com/en/events/test-slug-123/book", "test-slug-123"),
        ("https://webook.com/en/events/test-slug-123?lang=en", "test-slug-123"),
        ("http://webook.com/events/test-slug-123#anchor", "test-slug-123"),
        ("webook.com/events/test-slug-123", "test-slug-123"),
        ("//webook.com/events/test-slug-123", "test-slug-123"),
        ("https://www.webook.com/events/test-slug-123", "test-slug-123"),
        ("test-slug-123", "test-slug-123"),
        ("spl-week-32-al-najmah-vs-al-hazem-7715",
         "spl-week-32-al-najmah-vs-al-hazem-7715"),
        ("https://Webook.com/AR/SA/events/Some-Slug-1", "Some-Slug-1"),
        ("https://webook.com/en/event/concert-2025", "concert-2025"),
        ("   https://webook.com/events/test-slug-123   ", "test-slug-123"),
        ('"https://webook.com/events/test-slug-123"', "test-slug-123"),
        ("https://webook.com/en/events/correct-slug?ref=/events/wrong",
         "correct-slug"),
        ("https://webook.com/events/test%2Dslug%2D123", "test-slug-123"),
        ("", None), ("   ", None), (None, None),
        ("not a slug at all because it has spaces", None),
        ("https://google.com/", None),
    ]
    fails = 0
    for inp, expected in cases:
        got = extract_slug(inp)
        if got != expected:
            fails += 1
            print(f"  ❌ {str(inp)[:65]:<65} → {got!r} expected {expected!r}")
        else:
            print(f"  ✅ {str(inp)[:65]:<65} → {got}")
    print(f"\n{'🏆' if fails == 0 else '❌'} {len(cases) - fails}/{len(cases)} passed.")
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() == 0 else 1)
