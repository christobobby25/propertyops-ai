"""Split the synthetic dataset into local files (no cloud access).

python -m scripts.extract_seed_data              # write files under data/
python -m scripts.extract_seed_data --check      # fail if files are stale (CI)
python -m scripts.extract_seed_data --source path/to/dataset.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.seed_data import DATA_DIR, SOURCE_PATH, DatasetError, split_dataset


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--source", type=Path, default=SOURCE_PATH)
    ap.add_argument("--out", type=Path, default=DATA_DIR)
    ap.add_argument("--check", action="store_true", help="verify only; write nothing")
    args = ap.parse_args(argv)

    try:
        files = split_dataset(json.loads(args.source.read_text()))
    except (OSError, json.JSONDecodeError, DatasetError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Markdown files in property_docs/ that the dataset no longer produces are stale.
    docs_dir = args.out / "property_docs"
    expected_docs = {Path(p).name for p in files if p.startswith("property_docs/")}
    stale = sorted(p for p in docs_dir.glob("*.md") if p.name not in expected_docs)

    changed = [rel for rel, content in files.items() if _read(args.out / rel) != content]

    if args.check:
        for rel in changed:
            print(f"out of date: data/{rel}")
        for p in stale:
            print(f"stale: {p}")
        ok = not changed and not stale
        print("data/ is up to date" if ok else "run: python -m scripts.extract_seed_data")
        return 0 if ok else 1

    for rel in changed:
        path = args.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(files[rel])
        print(f"wrote   data/{rel}")
    for p in stale:
        p.unlink()
        print(f"removed {p}")
    print(f"{len(files)} files ({len(changed)} changed, {len(stale)} removed)")
    return 0


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


if __name__ == "__main__":
    sys.exit(main())
