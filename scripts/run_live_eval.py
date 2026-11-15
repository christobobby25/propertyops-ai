"""Live evaluation of the agent with real Gemini. Paid; never run in CI.

    python -m scripts.run_live_eval                       # --mode cloud-readonly (default)
    python -m scripts.run_live_eval --mode emulator       # all cases, writes go to emulator
    python -m scripts.run_live_eval --mode emulator --case EVAL-014 -v
    python -m scripts.run_live_eval --no-judge            # deterministic checks only

Modes:
  cloud-readonly  Cloud Firestore, read-only. Creation tools are not registered.
                  Cases that need writes are reported as SKIP.
  emulator        Every Firestore read/write goes to the local emulator
                  (FIRESTORE_EMULATOR_HOST; demo-* project). Fresh seed per case.
                  Fails closed if the emulator is missing/unreachable.

Per case: correctness (LLM judge), citation support, expected tool use,
unauthorized disclosure, side effects (exact number of new work orders, all
in scope) and unconfirmed creation claims. Writes a JSON report to reports/.
Exits 1 if any executed case fails.

The agent receives only the case's user message and a backend-bound demo
identity; expected answers never enter agent context or retrieval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime

from app.config import PROJECT_ROOT, ConfigError, get_settings
from app.demo_auth import load_demo_context
from app.evaluation.cases import MODES, load_cases
from app.evaluation.checks import (
    CheckResult,
    check_citations,
    check_creation_claims,
    check_disclosure,
    check_side_effects,
    check_tool_use,
)
from app.evaluation.fixtures import FixtureError
from app.evaluation.runner import EvalEnvironment, run_case
from app.gcp import FirestoreTargetError

REPORTS_DIR = PROJECT_ROOT / "reports"
RATE_LIMIT_BACKOFF_SECONDS = (15, 45)


def _rate_limited(text: str | None) -> bool:
    return bool(text) and ("RESOURCE_EXHAUSTED" in text or "429" in text)


async def _with_rate_limit_retry(make_call):
    """Retry 429s (quota, not agent behaviour) with backoff. Returns (result, retries)."""
    for attempt, delay in enumerate((*RATE_LIMIT_BACKOFF_SECONDS, None)):
        try:
            result = await make_call()
        except Exception as e:
            if delay is None or not _rate_limited(str(e)):
                raise
        else:
            if delay is None or not _rate_limited(getattr(result, "error", None)):
                return result, attempt
        print(f"     rate limited; retrying in {delay}s")
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def _main(args) -> int:
    try:
        s = get_settings()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    cases = load_cases()
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c.case_id in wanted]
        if missing := wanted - {c.case_id for c in cases}:
            print(f"unknown case ids: {sorted(missing)}", file=sys.stderr)
            return 2

    try:
        env = EvalEnvironment.create(args.mode, s)
    except FirestoreTargetError as e:
        print(f"refusing to run ({args.mode}): {e}", file=sys.stderr)
        return 2
    s = env.settings

    target = "emulator (demo project)" if env.writes_enabled else "cloud (read-only)"
    print(f"mode={args.mode}  firestore={target}  creation_tools={env.writes_enabled}")
    print(f"agent={s.model}  judge={'off' if args.no_judge else s.judge_model}")
    print(
        f"limits: llm_calls={s.max_llm_calls} tool_calls={s.max_tool_calls} "
        f"tool_timeout={s.tool_timeout_seconds}s run_timeout={s.agent_run_timeout_seconds}s\n"
    )

    results, failed = [], 0
    for case in cases:
        if reason := case.skip_reason(args.mode):
            print(f"SKIP {case.case_id}  {reason}")
            results.append({"case_id": case.case_id, "outcome": "skip", "reason": reason})
            continue

        try:
            run, retries = await _with_rate_limit_retry(lambda c=case: run_case(env, c))
        except FixtureError as e:
            print(f"FAIL {case.case_id}  fixture error: {e}")
            failed += 1
            results.append({"case_id": case.case_id, "outcome": "fail", "reason": str(e)})
            continue
        auth = load_demo_context(case.auth_context_id)
        checks = [
            check_tool_use(case, run),
            check_citations(case, run),
            check_disclosure(auth, run),
            check_creation_claims(run),
        ]
        if env.writes_enabled:
            checks.append(check_side_effects(case, auth, run))
        if run.error:
            checks.insert(0, CheckResult("run", False, [run.error]))
        if not args.no_judge:
            from app.evaluation.judge import judge_case

            try:
                verdict, judge_retries = await _with_rate_limit_retry(
                    lambda c=case, r=run: asyncio.to_thread(judge_case, s, c, r)
                )
                retries += judge_retries
                checks.append(verdict)
            except Exception as e:
                checks.append(CheckResult("correctness", False, [f"judge error: {e}"]))

        ok = all(c.passed for c in checks)
        failed += not ok
        marks = "  ".join(f"{c.name}={'ok' if c.passed else 'FAIL'}" for c in checks)
        print(f"{'PASS' if ok else 'FAIL'} {case.case_id}  ({run.seconds}s)  {marks}")
        for gap in run.fixture_gaps:
            print(f"     fixture gap: {gap}")
        if not ok or args.verbose:
            print(f"     user: {case.user_message}")
            print(f"     tools: {[c['name'] for c in run.tool_calls]}")
            for conf in run.confirmations:
                print(f"     confirmation: {conf}")
            for c in checks:
                if not c.passed or args.verbose:
                    for d in c.details:
                        print(f"     {c.name}: {d}")
            print("     answer: " + run.final_text.replace("\n", "\n             "))
        results.append(
            {
                "case_id": case.case_id,
                "outcome": "pass" if ok else "fail",
                "auth_context_id": case.auth_context_id,
                "user_message": case.user_message,
                "answer": run.final_text,
                "tool_calls": run.tool_calls,
                "confirmations": run.confirmations,
                "new_work_orders": [w.get("work_order_id") for w in run.new_work_orders],
                "fixture_gaps": run.fixture_gaps,
                "seconds": run.seconds,
                "rate_limit_retries": retries,
                "checks": [asdict(c) for c in checks],
            }
        )

    executed = sum(r["outcome"] != "skip" for r in results)
    skipped = len(results) - executed
    print(f"\n{executed - failed}/{executed} passed, {failed} failed, {skipped} skipped")

    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"live-eval-{args.mode}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(
        json.dumps({"mode": args.mode, "model": s.model, "results": results}, indent=2, default=str)
    )
    print(f"report: {path}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--mode", choices=MODES, default="cloud-readonly")
    ap.add_argument("--case", action="append", help="run only this case id (repeatable)")
    ap.add_argument("--no-judge", action="store_true", help="skip the LLM correctness judge")
    ap.add_argument("-v", "--verbose", action="store_true", help="print details for passing cases")
    return asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
