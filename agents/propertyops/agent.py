"""ADK entry point. Agent construction lives in app/agent/factory.py."""

import sys
from pathlib import Path

# `adk web`/`adk run` put only the agents directory on sys.path.
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.agent.factory import build_default_agent  # noqa: E402

root_agent = build_default_agent()
