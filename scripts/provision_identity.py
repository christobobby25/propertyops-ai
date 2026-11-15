"""Map a verified Google account to an authorization scope (user_identities).

    python -m scripts.provision_identity --subject google:1234567890 \\
        --role resident --resident RES-002                 # dry run
    python -m scripts.provision_identity ... --apply        # write to Firestore
    python -m scripts.provision_identity --subject google:123 --deactivate --apply

The subject is ``google:<sub>`` from the user's ID token. Scope is derived
from seed data (a resident's property and unit), never typed in by hand, so a
resident cannot be mapped to someone else's unit by mistake.
"""

from __future__ import annotations

import sys

from app.agent.identity import FirestoreIdentityDirectory
from app.gcp import firestore_client
from app.seed_data import load_seed_collection
from scripts._cli import banner, footer, load_settings, parser


def scope_for(role: str, resident_id: str | None, property_ids: list[str]) -> dict:
    if role == "resident":
        res = {r["resident_id"]: r for r in load_seed_collection("residents")}.get(resident_id)
        if res is None:
            raise ValueError(f"unknown resident {resident_id!r}")
        return {
            "role": "resident",
            "resident_id": resident_id,
            "allowed_property_ids": [res["property_id"]],
            "allowed_unit_ids": [res["unit_id"]],
        }
    if role == "property_manager":
        known = {p["property_id"] for p in load_seed_collection("properties")}
        if not property_ids or not set(property_ids) <= known:
            raise ValueError(f"property managers need known --property values: {sorted(known)}")
        units = [
            u["unit_id"] for u in load_seed_collection("units") if u["property_id"] in property_ids
        ]
        return {
            "role": "property_manager",
            "resident_id": None,
            "allowed_property_ids": sorted(property_ids),
            "allowed_unit_ids": sorted(units),
        }
    raise ValueError(f"unknown role {role!r}")


def main(argv: list[str] | None = None) -> int:
    ap = parser(__doc__)
    ap.add_argument("--subject", required=True, help="google:<sub> from the ID token")
    ap.add_argument("--role", choices=["resident", "property_manager"])
    ap.add_argument("--resident")
    ap.add_argument("--property", action="append", default=[])
    ap.add_argument("--deactivate", action="store_true")
    args = ap.parse_args(argv)
    if not args.subject.startswith("google:"):
        print("subject must look like google:<sub>", file=sys.stderr)
        return 2
    s = load_settings()
    if s is None:
        return 2

    if args.deactivate:
        doc = {"active": False}
    else:
        if not args.role:
            print("--role is required unless --deactivate", file=sys.stderr)
            return 2
        try:
            doc = {**scope_for(args.role, args.resident, args.property), "active": True}
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2

    banner(args.apply, s)
    print(f"{FirestoreIdentityDirectory.COLLECTION}/{args.subject} <- {doc}")
    if not args.apply:
        footer(False)
        return 0
    db = firestore_client(s)
    db.collection(FirestoreIdentityDirectory.COLLECTION).document(args.subject).set(doc, merge=True)
    print("written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
