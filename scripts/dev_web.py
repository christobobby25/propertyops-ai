"""Start ADK Web for local development, bound to localhost.

    python -m scripts.dev_web                       # identity from DEV_AUTH_CONTEXT
    python -m scripts.dev_web --as AUTH-RES-004     # pick another demo identity
    python -m scripts.dev_web --port 8001

Refuses to start unless APP_ENV=development. Always binds 127.0.0.1. The demo
identity is fixed server-side for the whole process; it cannot be changed from
the browser or the conversation.
"""

from __future__ import annotations

import argparse
import os
import sys

from app.config import PROJECT_ROOT, ConfigError, get_settings
from app.demo_auth import load_demo_context

LOCAL_HOST = "127.0.0.1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--as", dest="context_id", help="demo auth context id (development only)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    if args.context_id:
        os.environ["DEV_AUTH_CONTEXT"] = args.context_id
        get_settings.cache_clear()
    try:
        s = get_settings()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if s.app_env != "development":
        print("refusing to start: APP_ENV must be 'development' for ADK Web", file=sys.stderr)
        return 2
    try:
        ctx = load_demo_context(s.dev_auth_context or "")
    except KeyError as e:
        print(f"DEV_AUTH_CONTEXT: {e}", file=sys.stderr)
        return 2

    print(f"ADK Web on http://{LOCAL_HOST}:{args.port}  (development only)")
    print(f"Demo identity: {s.dev_auth_context}  role={ctx.role}  subject={ctx.subject}")

    from google.adk.cli import main as adk_main

    agents_dir = str(PROJECT_ROOT / "agents")
    sys.argv = ["adk", "web", "--host", LOCAL_HOST, "--port", str(args.port), agents_dir]
    return adk_main()


if __name__ == "__main__":
    sys.exit(main())
