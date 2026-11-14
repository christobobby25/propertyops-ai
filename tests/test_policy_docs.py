import json
from dataclasses import replace

import pytest

from app.policy_docs import (
    PolicyDocument,
    build_chunks,
    load_policy_documents,
    parse_markdown,
    render_markdown,
)
from app.seed_data import SOURCE_PATH

SOURCE_DOCS = json.loads(SOURCE_PATH.read_text())["policy_documents"]


@pytest.mark.parametrize("raw", SOURCE_DOCS, ids=lambda d: d["document_id"])
def test_markdown_round_trip_preserves_ids_and_text(raw):
    doc = PolicyDocument.from_dict(raw)
    assert parse_markdown(render_markdown(doc)) == doc


def test_committed_docs_match_source():
    docs = {d.document_id: d for d in load_policy_documents()}
    assert len(docs) == 6
    for raw in SOURCE_DOCS:
        doc = docs[raw["document_id"]]
        assert doc.property_id == raw["property_id"]
        assert doc.version == raw["version"]
        assert [s.section_id for s in doc.sections] == [s["section_id"] for s in raw["sections"]]


def test_rejects_foreign_section_id():
    raw = json.loads(json.dumps(SOURCE_DOCS[0]))
    raw["sections"][0]["section_id"] = "DOC-OTHER-S01"
    with pytest.raises(ValueError, match="does not belong"):
        PolicyDocument.from_dict(raw)


def test_rejects_missing_front_matter_field():
    md = render_markdown(PolicyDocument.from_dict(SOURCE_DOCS[0]))
    md = "\n".join(line for line in md.splitlines() if not line.startswith("version:"))
    with pytest.raises(ValueError, match="version"):
        parse_markdown(md)


def _chunks(docs):
    return build_chunks(docs, {d.document_id: f"gs://b/{d.filename}" for d in docs})


def test_one_chunk_per_section_with_metadata():
    docs = load_policy_documents()
    chunks = _chunks(docs)
    assert len(chunks) == 18
    c = next(c for c in chunks if c.chunk_id == "DOC-LV-HVAC-S01")
    assert (c.document_id, c.property_id, c.version) == ("DOC-LV-HVAC", "PROP-002", "1.0")
    assert c.source_uri == "gs://b/DOC-LV-HVAC.md"
    assert c.embedding_text.startswith("Lakeview HVAC Maintenance Policy\nCooling priority")


def test_unpublished_documents_not_chunked():
    docs = load_policy_documents()
    docs[0] = replace(docs[0], status="draft")
    ids = {c.document_id for c in _chunks(docs)}
    assert docs[0].document_id not in ids


def test_content_hash_tracks_model_dims_and_text():
    [c] = _chunks(load_policy_documents())[:1]
    h = c.content_hash("m", 768)
    assert h == c.content_hash("m", 768)
    assert h != c.content_hash("m2", 768)
    assert h != c.content_hash("m", 512)
    assert h != replace(c, text=c.text + " changed").content_hash("m", 768)
