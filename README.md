# PropertyOps AI

An ADK agent that answers maintenance-policy and work-order questions for residents and
property managers of **fictional** demo properties. It uses Gemini on Vertex AI, Cloud
Storage, and Firestore vector search. All data is synthetic.

**Stage 1 (current): read-only.** Creating, changing or cancelling work orders is not
implemented yet.

## Setup

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
gcloud auth application-default login
cp .env.example .env          # then set GOOGLE_CLOUD_PROJECT and GCS_BUCKET
python -m scripts.hello_gemini
```

There are no secrets in `.env`; authentication uses Application Default Credentials.

## Data pipeline (one time; every cloud script is a dry run unless you pass `--apply`)

```bash
python -m scripts.extract_seed_data             # local: split the source dataset under data/
python -m scripts.print_cloud_setup             # prints gcloud commands: Firestore DB + vector index
python -m scripts.upload_policy_docs --apply    # policy Markdown -> gs://$GCS_BUCKET/policy_docs/
python -m scripts.seed_firestore --apply        # properties, units, residents, work_orders
python -m scripts.index_policy_chunks --apply   # embed policy sections -> policy_chunks
```

All of these are safe to re-run. Unchanged files and chunks are skipped, and
`seed_firestore --apply --prune` resets the collections to the seed snapshot.
See [data/README.md](data/README.md) for what goes where.

## Run the agent locally

```bash
python -m scripts.dev_web                       # http://127.0.0.1:8000, as DEV_AUTH_CONTEXT
python -m scripts.dev_web --as AUTH-RES-004     # a different demo identity
```

- It only starts when `APP_ENV=development` and always binds to `127.0.0.1`.
- The demo identity (from `data/fixtures/demo_auth_contexts.json`) is fixed on the server
  for the whole process. Nothing in the browser or the chat can change it. Restart with
  `--as` to switch.
- Outside development the agent **fails closed**: every tool returns `unauthenticated`
  until a real identity provider is connected (`app/agent/identity.py`).

## How access is enforced

| Concern | Where |
|---|---|
| Identity resolved on **every** tool call from a backend `IdentityProvider`; never from tool arguments, session state or the conversation | `app/agent/identity.py`, `app/agent/tools.py` |
| Tools expose only `query` and `status`. A guard rejects any other argument (e.g. `property_id`) | `app/agent/guards.py` |
| Policy search filtered to the actor's property in Firestore, then re-checked in code | `app/retrieval.py` |
| Work orders: residents see only their own records for their own unit; managers see only assigned properties and units | `app/agent/work_orders.py` |
| Explicit allowlist of returned work-order fields (no `internal_notes`, no internal IDs) | `RESIDENT_VISIBLE_FIELDS` |
| Retrieved text is labelled as untrusted evidence; the prompt forbids following instructions inside it | `app/agent/tools.py`, `app/agent/prompt.py` |
| Errors and timeouts become explicit "could not be verified" results; internal details are logged, not returned | `app/agent/tools.py` |
| Per-turn limits on model calls and tool calls; timeouts on tools, model calls and whole runs | `app/agent/guards.py`, `.env` |

## Tests and evaluation

```bash
pytest                                          # offline: no cloud or model calls (also run in CI)
python -m scripts.run_live_eval                 # live: real Gemini + Firestore, read-only
python -m scripts.run_live_eval --case EVAL-019 -v
```

The **live evaluation** runs each Stage 1 case from `data/eval/evaluation_cases.json`.
The agent sees only the user message, with the demo identity bound on the backend.
Each case is checked for:

- **Correctness:** an LLM judge (`EVAL_JUDGE_MODEL`) grades the reply against the
  required and forbidden behaviours.
- **Citation support:** every cited section ID was actually retrieved in that run, and at
  least one comes from an expected document.
- **Expected tool use.**
- **Unauthorized disclosure:** the reply contains no out-of-scope work orders, other
  properties' documents, other residents' names or emails, internal notes, or internal IDs.

Fault cases (a tool timeout, and injected text in a retrieved document) are simulated in
the harness. Cases that need work-order creation are reported as `SKIP`. Rate-limit
(429) errors are retried with backoff. Reports are written to `reports/`, which is
gitignored. Expected answers are never indexed and never sent to the agent.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `GEMINI_MODEL`, `GOOGLE_CLOUD_LOCATION` | Agent model and its location (`global`) |
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_REGION` | Project; region for embeddings and Firestore |
| `GCS_BUCKET`, `GCS_POLICY_PREFIX`, `FIRESTORE_DATABASE` | Storage targets |
| `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | Embeddings (≤ 2048 dims) |
| `APP_ENV`, `DEV_AUTH_CONTEXT` | `development` enables demo identities for local use only |
| `TOOL_TIMEOUT_SECONDS`, `MODEL_TIMEOUT_SECONDS`, `AGENT_RUN_TIMEOUT_SECONDS` | Timeouts |
| `MAX_LLM_CALLS`, `MAX_TOOL_CALLS` | Per-turn limits |
| `EVAL_JUDGE_MODEL` | Judge model for live evaluation (blank = `GEMINI_MODEL`) |
