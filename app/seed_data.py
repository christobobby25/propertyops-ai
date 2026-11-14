"""Split the synthetic source dataset into purpose-specific files.

Only ``policy_documents`` become retrievable Markdown. Operational records go
to ``data/seed`` (Firestore collections), demo auth fixtures to
``data/fixtures``, and evaluation cases to ``data/eval``. Evaluation cases and
auth fixtures are never uploaded, seeded, or indexed.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import PROJECT_ROOT
from app.policy_docs import PolicyDocument, render_markdown

DATA_DIR = PROJECT_ROOT / "data"
SOURCE_PATH = DATA_DIR / "source" / "propertyops_synthetic_data.json"
SEED_DIR = DATA_DIR / "seed"

# Firestore collection name -> (source key, ID field). Order matters for display only.
OPERATIONAL_COLLECTIONS: dict[str, str] = {
    "properties": "property_id",
    "units": "unit_id",
    "residents": "resident_id",
    "work_orders": "work_order_id",
}

EXPECTED_TOP_LEVEL_KEYS = frozenset(
    {
        "metadata",
        "properties",
        "units",
        "residents",
        "demo_auth_contexts",
        "policy_documents",
        "work_orders",
        "action_contract",
        "evaluation_cases",
    }
)


class DatasetError(ValueError):
    pass


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def validate_dataset(raw: dict) -> None:
    keys = set(raw)
    if keys != EXPECTED_TOP_LEVEL_KEYS:
        raise DatasetError(
            f"unexpected top-level keys: missing={sorted(EXPECTED_TOP_LEVEL_KEYS - keys)} "
            f"extra={sorted(keys - EXPECTED_TOP_LEVEL_KEYS)}"
        )
    if raw["metadata"].get("synthetic") is not True:
        raise DatasetError("refusing to process a dataset not marked metadata.synthetic=true")

    def ids(key: str, field: str) -> set[str]:
        values = [r[field] for r in raw[key]]
        if len(values) != len(set(values)):
            raise DatasetError(f"duplicate {field} in {key}")
        return set(values)

    props = ids("properties", "property_id")
    units = {u["unit_id"]: u for u in raw["units"]}
    ids("units", "unit_id")
    residents = {r["resident_id"]: r for r in raw["residents"]}
    ids("residents", "resident_id")
    ids("work_orders", "work_order_id")
    ids("policy_documents", "document_id")

    for u in raw["units"]:
        if u["property_id"] not in props:
            raise DatasetError(f"{u['unit_id']}: unknown property {u['property_id']}")
    for rec in [*raw["residents"], *raw["work_orders"]]:
        rid = rec.get("work_order_id") or rec["resident_id"]
        unit = units.get(rec["unit_id"])
        if unit is None or unit["property_id"] != rec["property_id"]:
            raise DatasetError(f"{rid}: unit/property mismatch")
    for wo in raw["work_orders"]:
        res = residents.get(wo["resident_id"])
        if res is None or res["unit_id"] != wo["unit_id"]:
            raise DatasetError(f"{wo['work_order_id']}: resident/unit mismatch")
    for doc in raw["policy_documents"]:
        if doc["property_id"] not in props:
            raise DatasetError(f"{doc['document_id']}: unknown property {doc['property_id']}")


def split_dataset(raw: dict) -> dict[str, str]:
    """Return {path relative to data/: file content} for every derived file."""
    validate_dataset(raw)
    out: dict[str, str] = {}

    for d in raw["policy_documents"]:
        doc = PolicyDocument.from_dict(d)
        out[f"property_docs/{doc.filename}"] = render_markdown(doc)

    for collection in OPERATIONAL_COLLECTIONS:
        out[f"seed/{collection}.json"] = _dump(raw[collection])

    out["fixtures/demo_auth_contexts.json"] = _dump(raw["demo_auth_contexts"])
    out["eval/evaluation_cases.json"] = _dump(raw["evaluation_cases"])
    out["reference/dataset_metadata.json"] = _dump(raw["metadata"])
    out["reference/action_contract.json"] = _dump(raw["action_contract"])
    return out


def load_seed_collection(collection: str, seed_dir: Path = SEED_DIR) -> list[dict]:
    if collection not in OPERATIONAL_COLLECTIONS:
        raise KeyError(collection)
    return json.loads((seed_dir / f"{collection}.json").read_text())
