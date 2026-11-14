"""Embed policy sections from data/property_docs/ into Firestore `policy_chunks`.

    python -m scripts.index_policy_chunks                  # dry run
    python -m scripts.index_policy_chunks --apply          # embed new/changed chunks
    python -m scripts.index_policy_chunks --apply --force  # re-embed everything
    python -m scripts.index_policy_chunks --apply --prune  # also delete orphaned chunks

One chunk per policy section; the Firestore document ID is the section ID.
Repeatable: a chunk is re-embedded only when its content hash (text, metadata,
embedding model, dimensions) changes. Only published policy documents are
indexed — no work orders, residents, auth fixtures, or evaluation cases.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from app.gcp import EMBEDDING_FIELD, POLICY_CHUNKS_COLLECTION, firestore_client, gcs_uri
from app.policy_docs import build_chunks, load_policy_documents
from scripts._cli import banner, footer, load_settings, parser


def main(argv: list[str] | None = None) -> int:
    ap = parser(__doc__)
    ap.add_argument("--force", action="store_true", help="re-embed even if unchanged")
    ap.add_argument("--prune", action="store_true", help="delete chunks not in data/")
    args = ap.parse_args(argv)
    s = load_settings()
    if s is None:
        return 2

    docs = load_policy_documents()
    chunks = build_chunks(docs, {d.document_id: gcs_uri(s, d.filename) for d in docs})
    hashes = {c.chunk_id: c.content_hash(s.embedding_model, s.embedding_dimensions) for c in chunks}

    banner(args.apply, s)
    print(f"Collection: {POLICY_CHUNKS_COLLECTION}  (vector field '{EMBEDDING_FIELD}', COSINE)")
    print(
        f"Embedding:  {s.embedding_model}  dims={s.embedding_dimensions}  "
        f"region={s.region}  task=RETRIEVAL_DOCUMENT"
    )
    print(f"Chunks:     {len(chunks)} from {len(docs)} documents")
    for c in chunks:
        print(f"  {c.chunk_id:<24} {c.property_id}  {c.heading!r}  ({len(c.embedding_text)} chars)")
    print("\n  Requires the composite vector index (see: python -m scripts.print_cloud_setup).")
    if args.prune:
        print("  --prune: chunks not listed above will be deleted")

    if not args.apply:
        print("  At apply time, chunks whose content hash is unchanged are skipped.")
        footer(False)
        return 0

    from google.cloud.firestore_v1.vector import Vector

    from app.embeddings import VertexEmbedder

    db = firestore_client(s)
    col = db.collection(POLICY_CHUNKS_COLLECTION)
    refs = [col.document(c.chunk_id) for c in chunks]
    existing = {snap.id: snap.to_dict() for snap in db.get_all(refs) if snap.exists}

    todo = [
        c
        for c in chunks
        if args.force or (existing.get(c.chunk_id) or {}).get("content_hash") != hashes[c.chunk_id]
    ]
    vectors = VertexEmbedder(s).embed([c.embedding_text for c in todo], "RETRIEVAL_DOCUMENT")

    now = datetime.now(UTC)
    batch = db.batch()
    for c, vec in zip(todo, vectors, strict=True):
        batch.set(
            col.document(c.chunk_id),
            {
                "chunk_id": c.chunk_id,
                "section_id": c.section_id,
                "document_id": c.document_id,
                "property_id": c.property_id,
                "title": c.title,
                "heading": c.heading,
                "text": c.text,
                "version": c.version,
                "effective_from": c.effective_from,
                "status": c.status,
                "audience": list(c.audience),
                "language": c.language,
                "source_uri": c.source_uri,
                "content_hash": hashes[c.chunk_id],
                "embedding_model": s.embedding_model,
                "embedding_dimensions": s.embedding_dimensions,
                "indexed_at": now,
                EMBEDDING_FIELD: Vector(vec),
            },
        )
        print(f"embedded {c.chunk_id}")

    pruned = 0
    if args.prune:
        keep = {c.chunk_id for c in chunks}
        for ref in col.list_documents():
            if ref.id not in keep:
                batch.delete(ref)
                pruned += 1
                print(f"deleting {ref.id}")
    batch.commit()

    print(f"\nembedded={len(todo)} unchanged={len(chunks) - len(todo)} pruned={pruned}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
