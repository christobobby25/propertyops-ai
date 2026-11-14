import pytest

from app.auth import AuthorizationError, PropertyScopeRequired
from app.demo_auth import load_demo_context
from app.retrieval import MAX_LIMIT, PolicyRetriever


class FakeEmbedder:
    dimensions = 3

    def __init__(self):
        self.calls = []

    def embed(self, texts, task_type):
        self.calls.append((texts, task_type))
        return [[0.1, 0.2, 0.3] for _ in texts]


def row(chunk_id, property_id, **kw):
    base = dict(
        chunk_id=chunk_id,
        document_id=chunk_id.rsplit("-", 1)[0],
        section_id=chunk_id,
        property_id=property_id,
        title="t",
        heading="h",
        text="x",
        version="1.0",
        effective_from="2026-11-01",
        status="published",
        audience=["resident", "property_manager"],
        source_uri="gs://b/x.md",
        vector_distance=0.1,
    )
    return {**base, **kw}


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def find_nearest(self, *, property_id, query_vector, limit):
        self.calls.append(dict(property_id=property_id, limit=limit))
        return self.rows


def make(rows=()):
    store, emb = FakeStore(list(rows)), FakeEmbedder()
    return PolicyRetriever(store, emb), store, emb


def test_resident_search_is_scoped_to_their_property():
    r, store, emb = make([row("DOC-CG-HVAC-S01", "PROP-001")])
    results = r.search(load_demo_context("AUTH-RES-001"), "AC is broken")
    assert store.calls == [dict(property_id="PROP-001", limit=4)]
    assert emb.calls == [(["AC is broken"], "RETRIEVAL_QUERY")]
    assert [x.chunk_id for x in results] == ["DOC-CG-HVAC-S01"]


def test_untrusted_property_hint_outside_scope_is_rejected_before_search():
    r, store, emb = make()
    with pytest.raises(AuthorizationError):
        r.search(load_demo_context("AUTH-RES-001"), "AC", property_id="PROP-002")
    assert store.calls == [] and emb.calls == []


def test_defence_in_depth_drops_foreign_property_rows():
    r, _, _ = make([row("DOC-LV-HVAC-S01", "PROP-002"), row("DOC-CG-HVAC-S01", "PROP-001")])
    results = r.search(load_demo_context("AUTH-RES-001"), "AC")
    assert [x.property_id for x in results] == ["PROP-001"]


def test_drops_unpublished_and_wrong_audience():
    rows = [
        row("DOC-CG-A-S01", "PROP-001", status="draft"),
        row("DOC-CG-B-S01", "PROP-001", audience=["property_manager"]),
        row("DOC-CG-C-S01", "PROP-001"),
    ]
    r, _, _ = make(rows)
    assert [x.chunk_id for x in r.search(load_demo_context("AUTH-RES-001"), "q")] == [
        "DOC-CG-C-S01"
    ]


def test_manager_sees_manager_audience_docs():
    r, _, _ = make([row("DOC-CG-B-S01", "PROP-001", audience=["property_manager"])])
    assert len(r.search(load_demo_context("AUTH-MGR-001"), "q")) == 1


def test_multi_property_context_requires_explicit_scope():
    from app.auth import AuthContext

    ctx = AuthContext(
        subject="m", role="property_manager", allowed_property_ids={"PROP-001", "PROP-002"}
    )
    r, store, _ = make()
    with pytest.raises(PropertyScopeRequired):
        r.search(ctx, "q")
    r.search(ctx, "q", property_id="PROP-002")
    assert store.calls[-1]["property_id"] == "PROP-002"


def test_limit_clamped_and_query_required():
    r, store, _ = make()
    ctx = load_demo_context("AUTH-RES-004")
    r.search(ctx, "q", limit=999)
    r.search(ctx, "q", limit=0)
    assert [c["limit"] for c in store.calls] == [MAX_LIMIT, 1]
    with pytest.raises(ValueError):
        r.search(ctx, "   ")
