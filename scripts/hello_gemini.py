"""Smoke test: call Gemini on Vertex AI using validated settings.

Run from the project root:  python -m scripts.hello_gemini
"""

from __future__ import annotations

import sys

from google import genai
from google.auth.exceptions import DefaultCredentialsError

from app.config import ConfigError, get_settings


def main() -> int:
    try:
        s = get_settings()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    try:
        client = genai.Client(vertexai=True, project=s.project, location=s.location)
        resp = client.models.generate_content(
            model=s.model,
            contents="Say hello from PropertyOps AI in one sentence.",
        )
    except DefaultCredentialsError:
        print(
            "No Google credentials found. Run: gcloud auth application-default login",
            file=sys.stderr,
        )
        return 1
    except Exception as e:  # surface API/permission errors clearly
        print(f"Gemini call failed ({type(e).__name__}): {e}", file=sys.stderr)
        return 1

    print(f"[{s.model} @ {s.project}/{s.location}]")
    print(resp.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
