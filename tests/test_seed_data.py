import copy
import json

import pytest

from app.seed_data import SOURCE_PATH, DatasetError, split_dataset
from scripts import extract_seed_data

RAW = json.loads(SOURCE_PATH.read_text())


@pytest.fixture
def raw():
    return copy.deepcopy(RAW)


def test_split_layout(raw):
    files = split_dataset(raw)
    assert sorted(p for p in files if p.startswith("property_docs/")) == [
        f"property_docs/{d['document_id']}.md"
        for d in sorted(raw["policy_documents"], key=lambda d: d["document_id"])
    ]
    assert {p for p in files if p.startswith("seed/")} == {
        "seed/properties.json",
        "seed/units.json",
        "seed/residents.json",
        "seed/work_orders.json",
    }
    assert "fixtures/demo_auth_contexts.json" in files
    assert "eval/evaluation_cases.json" in files


def test_eval_and_auth_never_reach_retrieval_or_seed(raw):
    files = split_dataset(raw)
    retrievable = "".join(v for k, v in files.items() if k.startswith(("property_docs/", "seed/")))
    for case in raw["evaluation_cases"]:
        assert case["case_id"] not in retrievable
        assert case["user_message"] not in retrievable
    for ctx in raw["demo_auth_contexts"]:
        assert ctx["context_id"] not in retrievable


def test_committed_data_is_up_to_date():
    assert extract_seed_data.main(["--check"]) == 0


def test_rejects_non_synthetic(raw):
    raw["metadata"]["synthetic"] = False
    with pytest.raises(DatasetError, match="synthetic"):
        split_dataset(raw)


def test_rejects_unknown_top_level_key(raw):
    raw["leases"] = []
    with pytest.raises(DatasetError, match="leases"):
        split_dataset(raw)


def test_rejects_broken_references(raw):
    raw["work_orders"][0]["unit_id"] = "UNIT-005"
    with pytest.raises(DatasetError, match="mismatch"):
        split_dataset(raw)


def test_rejects_duplicate_ids(raw):
    raw["units"].append(dict(raw["units"][0]))
    with pytest.raises(DatasetError, match="duplicate"):
        split_dataset(raw)


def test_extract_writes_and_removes_stale(tmp_path):
    (tmp_path / "property_docs").mkdir()
    (tmp_path / "property_docs" / "OLD.md").write_text("old")
    assert extract_seed_data.main(["--out", str(tmp_path)]) == 0
    assert not (tmp_path / "property_docs" / "OLD.md").exists()
    assert len(list((tmp_path / "property_docs").glob("*.md"))) == 6
    assert extract_seed_data.main(["--out", str(tmp_path), "--check"]) == 0
