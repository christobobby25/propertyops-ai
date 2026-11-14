# data/

Everything here is **synthetic**. It is derived from `source/propertyops_synthetic_data.json`
by `python -m scripts.extract_seed_data`. Don't edit derived files by hand: edit the source,
re-run the extraction, and CI (`--check`) keeps them in sync.

| Path | Contents | Where it goes |
|---|---|---|
| `property_docs/*.md` | The 6 policy documents (front matter + `##` sections with section IDs) | GCS bucket, then Firestore `policy_chunks` (embedded). **The only retrievable data.** |
| `seed/*.json` | properties, units, residents, work_orders | Firestore collections of the same names. Read through authorized tools, never embedded. |
| `fixtures/demo_auth_contexts.json` | Demo identities for local dev and tests | Nowhere. Loaded only by `app/demo_auth.py`. Not credentials. |
| `eval/evaluation_cases.json` | Evaluation prompts with expected answers | Nowhere. Must stay out of retrieval and model context. |
| `reference/` | Dataset metadata and the action contract | Reference for building tools. Not indexed. |
