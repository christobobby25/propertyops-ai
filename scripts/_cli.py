"""Shared helpers for the dry-run-by-default cloud scripts."""

from __future__ import annotations

import argparse
import sys

from app.config import ConfigError, Settings, get_settings


def parser(description: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=description, formatter_class=argparse.RawTextHelpFormatter
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="perform cloud writes (default: dry run, no network calls)",
    )
    return ap


def load_settings() -> Settings | None:
    try:
        return get_settings()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return None


def banner(apply: bool, s: Settings) -> None:
    mode = "APPLY — writing to Google Cloud" if apply else "DRY RUN — no cloud calls"
    print(f"== {mode} ==")
    print(f"project={s.project}  region={s.region}  database={s.firestore_database}")
    print()


def footer(apply: bool) -> None:
    if not apply:
        print("\nDry run only. Re-run with --apply to perform these writes.")
