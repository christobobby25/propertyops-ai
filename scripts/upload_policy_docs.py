"""Upload policy Markdown (data/property_docs/*.md) to the existing GCS bucket.

    python -m scripts.upload_policy_docs                 # dry run
    python -m scripts.upload_policy_docs --apply         # upload changed files
    python -m scripts.upload_policy_docs --apply --prune # also delete orphaned .md objects

Repeatable: objects whose MD5 already matches are skipped. Never creates the bucket.
"""

from __future__ import annotations

import base64
import hashlib
import sys

from app.gcp import gcs_uri, storage_client
from app.policy_docs import POLICY_DOCS_DIR, load_policy_documents
from scripts._cli import banner, footer, load_settings, parser

CONTENT_TYPE = "text/markdown; charset=utf-8"


def _md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def main(argv: list[str] | None = None) -> int:
    ap = parser(__doc__)
    ap.add_argument("--prune", action="store_true", help="delete .md objects not in data/")
    args = ap.parse_args(argv)
    s = load_settings()
    if s is None:
        return 2

    docs = load_policy_documents()  # validates every file before any write
    planned = []
    for doc in docs:
        data = (POLICY_DOCS_DIR / doc.filename).read_bytes()
        planned.append((doc, s.gcs_policy_prefix + doc.filename, data))

    banner(args.apply, s)
    print(f"Target: gs://{s.gcs_bucket}/{s.gcs_policy_prefix}")
    for doc, _name, data in planned:
        print(f"  {doc.document_id:<18} {doc.property_id}  v{doc.version}  {len(data):>5} B")
        print(f"      -> {gcs_uri(s, doc.filename)}")
    if args.prune:
        print("  --prune: other .md objects under the prefix will be deleted")

    if not args.apply:
        footer(False)
        return 0

    bucket = storage_client(s).bucket(s.gcs_bucket)
    if not bucket.exists():
        print(f"error: bucket {s.gcs_bucket!r} not found (this script never creates it)")
        return 1

    uploaded = skipped = 0
    for doc, name, data in planned:
        blob = bucket.get_blob(name)
        if blob is not None and blob.md5_hash == _md5_b64(data):
            skipped += 1
            continue
        blob = bucket.blob(name)
        blob.metadata = {
            "document_id": doc.document_id,
            "property_id": doc.property_id,
            "version": doc.version,
        }
        blob.upload_from_string(data, content_type=CONTENT_TYPE)
        uploaded += 1
        print(f"uploaded {name}")

    pruned = 0
    if args.prune:
        keep = {name for _, name, _ in planned}
        for blob in bucket.list_blobs(prefix=s.gcs_policy_prefix):
            if blob.name.endswith(".md") and blob.name not in keep:
                blob.delete()
                pruned += 1
                print(f"deleted  {blob.name}")

    print(f"\nuploaded={uploaded} unchanged={skipped} pruned={pruned}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
