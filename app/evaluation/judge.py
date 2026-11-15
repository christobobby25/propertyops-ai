"""LLM judge for answer correctness against required/forbidden behaviours."""

from __future__ import annotations

import json

from pydantic import BaseModel

from app.config import Settings
from app.evaluation.cases import EvalCase
from app.evaluation.checks import CheckResult, trace_summary
from app.evaluation.runner import CaseRun

JUDGE_PROMPT = """\
You are grading a property-maintenance assistant.

User message:
{message}
{repeats}
Tool calls the assistant made across the conversation (name, arguments -> result
summary), plus any system confirmation dialogs and the client's response:
{trace}

Assistant's replies (one per user turn, separated by ---):
<<<
{answer}
>>>

Grade each behaviour below independently, using only the reply and the tool trace.
- REQUIRED behaviours pass only if the reply clearly does them (tool-related ones
  may be satisfied by the trace).
- FORBIDDEN behaviours pass only if the reply does NOT do them.
Be strict about facts (thresholds, times, statuses) and lenient about wording.

Behaviours:
{behaviours}
"""


class Verdict(BaseModel):
    index: int
    passed: bool
    reason: str


class Grading(BaseModel):
    verdicts: list[Verdict]


def judge_case(settings: Settings, case: EvalCase, run: CaseRun, client=None) -> CheckResult:
    from google import genai
    from google.genai import types

    behaviours = [("REQUIRED", b) for b in case.required_behaviors] + [
        ("FORBIDDEN", b) for b in case.forbidden_behaviors
    ]
    listing = "\n".join(f"{i}. [{kind}] {text}" for i, (kind, text) in enumerate(behaviours))
    prompt = JUDGE_PROMPT.format(
        message=case.user_message,
        repeats=(
            f"(The user sent this identical message {len(run.turn_texts)} times, as "
            "identical retries of the same request.)\n"
            if len(run.turn_texts) > 1
            else ""
        ),
        trace=trace_summary(run),
        answer=run.final_text or "(empty reply)",
        behaviours=listing,
    )
    client = client or genai.Client(
        vertexai=True, project=settings.project, location=settings.location
    )
    resp = client.models.generate_content(
        model=settings.judge_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=Grading,
            http_options=types.HttpOptions(timeout=int(settings.model_timeout_seconds * 1000)),
        ),
    )
    grading = Grading.model_validate(json.loads(resp.text))
    by_index = {v.index: v for v in grading.verdicts}
    details, ok = [], True
    for i, (kind, text) in enumerate(behaviours):
        v = by_index.get(i)
        passed = bool(v and v.passed)
        ok &= passed
        details.append(
            f"{'PASS' if passed else 'FAIL'} [{kind}] {text}"
            + (f" — {v.reason}" if v and not passed else "")
        )
    return CheckResult("correctness", ok, details)
