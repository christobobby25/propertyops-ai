"""Seed operational collections (properties, units, residents, work_orders).

    python -m scripts.seed_firestore                  # dry run
    python -m scripts.seed_firestore --apply          # upsert seed documents
    python -m scripts.seed_firestore --apply --prune  # also delete non-seed docs (reset)

Repeatable: each record is written with its natural ID as the document ID and
fully replaced (set, not merge). --prune resets a collection to the exact
snapshot, e.g. between evaluation runs. Demo auth fixtures and evaluation
cases are never written.
"""

from __future__ import annotations

import sys

from app.gcp import firestore_client
from app.seed_data import OPERATIONAL_COLLECTIONS, load_seed_collection
from scripts._cli import banner, footer, load_settings, parser

BATCH_LIMIT = 400  # Firestore allows 500 writes per batch


def main(argv: list[str] | None = None) -> int:
    ap = parser(__doc__)
    ap.add_argument("--prune", action="store_true", help="delete docs not present in seed data")
    args = ap.parse_args(argv)
    s = load_settings()
    if s is None:
        return 2

    plan = {c: load_seed_collection(c) for c in OPERATIONAL_COLLECTIONS}

    banner(args.apply, s)
    for collection, id_field in OPERATIONAL_COLLECTIONS.items():
        records = plan[collection]
        ids = [r[id_field] for r in records]
        print(f"  {collection:<12} {len(records):>3} docs  (id={id_field}): {', '.join(ids)}")
    staff_only = sum(1 for r in plan["work_orders"] if r.get("internal_notes"))
    print(f"\n  note: {staff_only} work orders carry staff-only internal_notes;")
    print("        resident-facing tools must strip that field.")
    if args.prune:
        print("  --prune: documents in these collections not listed above will be deleted")

    if not args.apply:
        footer(False)
        return 0

    db = firestore_client(s)
    batch, pending, written, pruned = db.batch(), 0, 0, 0

    def flush():
        nonlocal batch, pending
        if pending:
            batch.commit()
            batch, pending = db.batch(), 0

    for collection, id_field in OPERATIONAL_COLLECTIONS.items():
        col = db.collection(collection)
        seed_ids = set()
        for record in plan[collection]:
            seed_ids.add(record[id_field])
            batch.set(col.document(record[id_field]), record)
            pending += 1
            written += 1
            if pending >= BATCH_LIMIT:
                flush()
        if args.prune:
            for ref in col.list_documents():
                if ref.id not in seed_ids:
                    batch.delete(ref)
                    pending += 1
                    pruned += 1
                    print(f"deleting {collection}/{ref.id}")
                    if pending >= BATCH_LIMIT:
                        flush()
    flush()
    print(f"\nwritten={written} pruned={pruned}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
