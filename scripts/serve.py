"""Run the PropertyOps web app locally (development).

    python -m scripts.serve                     # demo identities, cloud Firestore, read-only
    FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 python -m scripts.serve --emulator
                                                # creation enabled, emulator Firestore (reseeded)
    python -m scripts.serve --port 8081

Local runs always bind 127.0.0.1 and use AUTH_MODE=demo, which the app refuses
unless APP_ENV=development. Deployments run ``uvicorn app.web.server:create_app
--factory`` with AUTH_MODE=google_id_token instead.
"""

from __future__ import annotations

import argparse
import os
import sys

from app.config import ConfigError, get_settings

LOCAL_HOST = "127.0.0.1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--emulator", action="store_true", help="emulator Firestore + creation enabled")
    args = ap.parse_args(argv)

    os.environ["AUTH_MODE"] = "demo"
    if args.emulator:
        os.environ["FIRESTORE_TARGET"] = "emulator"
        os.environ["ENABLE_WORK_ORDER_CREATION"] = "true"
    get_settings.cache_clear()
    try:
        s = get_settings()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if s.app_env != "development":
        print("refusing to start: local serving requires APP_ENV=development", file=sys.stderr)
        return 2

    if args.emulator:
        from app import emulator
        from app.embeddings import VertexEmbedder
        from app.gcp import FirestoreTargetError, firestore_client

        try:
            db = firestore_client(s)
        except FirestoreTargetError as e:
            print(f"refusing to start: {e}", file=sys.stderr)
            return 2
        emulator.reset(db, emulator.build_policy_chunk_docs(s, VertexEmbedder(s)))
        print(f"Emulator reseeded at {os.environ['FIRESTORE_EMULATOR_HOST']} (demo project)")
    elif s.enable_work_order_creation:
        print("WARNING: work-order creation is enabled against CLOUD Firestore.")

    import uvicorn

    from app.logging_config import configure_logging
    from app.web.server import create_app

    configure_logging()
    print(f"PropertyOps web app on http://{LOCAL_HOST}:{args.port}  (development)")
    uvicorn.run(create_app(s), host=LOCAL_HOST, port=args.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
