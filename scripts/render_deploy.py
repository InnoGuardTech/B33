"""
V15 — PHASE 5 (final step): autonomous Render deploy trigger.

Calls the Render API to trigger a fresh deploy of the B33 service
right after `git push origin master` lands.

Usage
-----
    RENDER_TOKEN=rnd_xxx RENDER_SERVICE_ID=srv-xxx \
        python scripts/render_deploy.py
    # or, with command-line override:
    python scripts/render_deploy.py --token rnd_xxx --service srv-xxx

If --service is omitted, the script auto-discovers the B33 service by
listing /v1/services and matching on name == 'b33' / repo URL.

API reference
-------------
  POST https://api.render.com/v1/services/{serviceId}/deploys
        Authorization: Bearer {RENDER_TOKEN}
        body: { "clearCache": "do_not_clear" }
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

try:
    import requests
except Exception:
    print("⚠️  Please `pip install requests`", file=sys.stderr)
    sys.exit(2)

API_BASE = "https://api.render.com/v1"
DEFAULT_REPO_HINT = "InnoGuardTech/B33"


def _h(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def find_service_id(token: str, repo_hint: str = DEFAULT_REPO_HINT,
                    name_hint: str = "b33") -> Optional[str]:
    """Auto-discover the service id by querying the Render API."""
    try:
        r = requests.get(f"{API_BASE}/services?limit=50",
                         headers=_h(token), timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"⚠️  list services failed: {e}", file=sys.stderr)
        return None

    items = data if isinstance(data, list) else data.get("data") or []
    for it in items:
        svc = it if isinstance(it, dict) and "id" in it else it.get("service") or {}
        if not isinstance(svc, dict):
            continue
        sid = svc.get("id") or ""
        name = (svc.get("name") or "").lower()
        repo = (svc.get("repo") or "").lower()
        if (name_hint and name_hint in name) or (repo_hint.lower() in repo):
            print(f"  → discovered service: id={sid}  name={name}  repo={repo}")
            return sid
    return None


def trigger_deploy(token: str, service_id: str,
                   clear_cache: bool = False) -> dict:
    url = f"{API_BASE}/services/{service_id}/deploys"
    body = {"clearCache": "clear" if clear_cache else "do_not_clear"}
    r = requests.post(url, headers=_h(token), json=body, timeout=30)
    return {
        "status_code": r.status_code,
        "json": _safe_json(r),
        "elapsed_ms": int(r.elapsed.total_seconds() * 1000),
    }


def _safe_json(r) -> dict:
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:500]}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--token", default=os.getenv("RENDER_TOKEN", ""),
                   help="Render API token (env: RENDER_TOKEN)")
    p.add_argument("--service", default=os.getenv("RENDER_SERVICE_ID", ""),
                   help="Service id (env: RENDER_SERVICE_ID). "
                        "Auto-discovered when omitted.")
    p.add_argument("--repo", default=os.getenv("RENDER_REPO_HINT",
                                                DEFAULT_REPO_HINT),
                   help="Repo hint for auto-discovery (default: %(default)s)")
    p.add_argument("--clear-cache", action="store_true",
                   help="Clear build cache for this deploy")
    args = p.parse_args()

    if not args.token:
        print("❌ Render API token missing. Pass --token or set RENDER_TOKEN.",
              file=sys.stderr)
        return 2

    print("🚀 Hydra V15 — Render deploy trigger")
    print("=" * 70)

    service_id = args.service.strip()
    if not service_id:
        print("  → no --service given, auto-discovering…")
        service_id = find_service_id(args.token, args.repo) or ""
    if not service_id:
        print("❌ could not resolve a service id. Pass --service explicitly.",
              file=sys.stderr)
        return 1

    print(f"  service_id: {service_id}")
    print(f"  triggering deploy (clear_cache={args.clear_cache})…")
    res = trigger_deploy(args.token, service_id, clear_cache=args.clear_cache)
    print(f"  http {res['status_code']}  ({res['elapsed_ms']} ms)")
    print(f"  body: {json.dumps(res['json'], indent=2)[:600]}")

    if res["status_code"] in (200, 201, 202):
        deploy_id = (res["json"] or {}).get("id") or "?"
        print()
        print(f"🏆 Deploy queued — deploy_id={deploy_id}")
        print(f"   monitor at: https://dashboard.render.com/web/{service_id}/deploys/{deploy_id}")
        return 0
    print("❌ deploy trigger failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
