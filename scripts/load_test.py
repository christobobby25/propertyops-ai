"""Staged load test against a running PropertyOps web app (demo auth, read-only prompts).

    python -m scripts.load_test --url http://127.0.0.1:8091 --stages 1,3,5 --turns 2
    python -m scripts.load_test --url ... --stages 3 --label faults
        # (start the server with FAULT_INJECTION=... for the failure run)

Each virtual user gets its own session and a resident identity (round robin),
and sends ``--turns`` read-only questions. Each request is a full agent turn
(live Gemini). Reports p50/p95/max latency, HTTP errors, and turns the app
answered with an explicit error or a "could not be verified" reply. Writes JSON
to reports/. Uses only demo auth, so run it against a local/dev server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import statistics
import sys
import time
from datetime import datetime

import httpx

from app.config import PROJECT_ROOT

RESIDENTS = ["AUTH-RES-001", "AUTH-RES-002", "AUTH-RES-003", "AUTH-RES-004", "AUTH-RES-005"]
QUESTIONS = [
    "What is the policy if my AC stops working?",
    "Do I have any open maintenance requests?",
    "What counts as a maintenance emergency here?",
    "How long until someone contacts me about a routine request?",
]
DEGRADED_MARKERS = ("could not be verified", "couldn't verify", "unable to verify", "try again")


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(q * (len(values) - 1))))
    return round(values[k])


async def virtual_user(client: httpx.AsyncClient, vu: int, turns: int, results: list):
    identity = RESIDENTS[vu % len(RESIDENTS)]
    session = "lt" + secrets.token_hex(8)
    for t in range(turns):
        question = QUESTIONS[(vu + t) % len(QUESTIONS)]
        started = time.monotonic()
        rec = {"vu": vu, "identity": identity, "turn": t}
        try:
            r = await client.post(
                "/api/chat",
                headers={"X-Demo-Identity": identity},
                json={"session_id": session, "message": question},
            )
            rec["status"] = r.status_code
            body = (
                r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            )
            rec["app_error"] = body.get("error")
            reply = (body.get("reply") or "").lower()
            rec["degraded"] = any(m in reply for m in DEGRADED_MARKERS)
            rec["tools"] = body.get("tools")
        except Exception as e:
            rec["status"] = None
            rec["exception"] = type(e).__name__
        rec["latency_ms"] = (time.monotonic() - started) * 1000
        results.append(rec)


async def run_stage(url: str, users: int, turns: int, timeout: float) -> dict:
    results: list[dict] = []
    async with httpx.AsyncClient(base_url=url, timeout=timeout) as client:
        started = time.monotonic()
        await asyncio.gather(*(virtual_user(client, vu, turns, results) for vu in range(users)))
        wall = time.monotonic() - started
    lat = [r["latency_ms"] for r in results if r.get("status") == 200]
    http_errors = [r for r in results if r.get("status") != 200]
    return {
        "users": users,
        "requests": len(results),
        "wall_seconds": round(wall, 1),
        "throughput_rpm": round(len(results) / wall * 60, 1) if wall else None,
        "p50_ms": pct(lat, 0.5),
        "p95_ms": pct(lat, 0.95),
        "max_ms": round(max(lat)) if lat else None,
        "mean_ms": round(statistics.mean(lat)) if lat else None,
        "http_errors": len(http_errors),
        "app_errors": sum(1 for r in results if r.get("app_error")),
        "degraded_replies": sum(1 for r in results if r.get("degraded")),
        "records": results,
    }


async def _main(args) -> int:
    stages = [int(x) for x in args.stages.split(",")]
    async with httpx.AsyncClient(base_url=args.url, timeout=10) as c:
        if (await c.get("/health")).status_code != 200:
            print("server not healthy", file=sys.stderr)
            return 2
    report = {"url": args.url, "label": args.label, "turns": args.turns, "stages": []}
    cols = ("users", "reqs", "p50", "p95", "max", "http_err", "app_err", "degraded", "rpm")
    print("".join(f"{c:>9}" for c in cols))
    for users in stages:
        st = await run_stage(args.url, users, args.turns, args.timeout)
        report["stages"].append(st)
        row = (
            st["users"],
            st["requests"],
            st["p50_ms"],
            st["p95_ms"],
            st["max_ms"],
            st["http_errors"],
            st["app_errors"],
            st["degraded_replies"],
            st["throughput_rpm"],
        )
        print("".join(f"{'-' if v is None else v:>9}" for v in row))
    out = PROJECT_ROOT / "reports" / f"load-{args.label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"report: {out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--stages", default="1,3,5")
    ap.add_argument("--turns", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--label", default="baseline")
    return asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
