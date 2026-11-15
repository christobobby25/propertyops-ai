# PropertyOps AI

An ADK agent that answers maintenance-policy and work-order questions for residents and
property managers of **fictional** demo properties, and creates work orders once a
resident explicitly approves a proposal. It uses Gemini on Vertex AI, Cloud Storage, and
Firestore (vector search, plus transactions for writes). All data is synthetic.

- **Stage 1:** read-only policy Q&A with citations, plus work-order lookup.
- **Stage 2:** approval-controlled work-order creation. It is **off by default**
  (`ENABLE_WORK_ORDER_CREATION=false`).

## Setup

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
gcloud auth application-default login
cp .env.example .env          # then set GOOGLE_CLOUD_PROJECT and GCS_BUCKET
python -m scripts.hello_gemini
```

There are no secrets in `.env`; authentication uses Application Default Credentials.

For the Firestore emulator (Stage 2 tests and local creation), install it with
`gcloud components install cloud-firestore-emulator`. It needs **Java 21+**; on macOS,
`brew install openjdk@21` installs it without replacing your default Java.

## Data pipeline (one time; every cloud script is a dry run unless you pass `--apply`)

```bash
python -m scripts.extract_seed_data             # local: split the source dataset under data/
python -m scripts.print_cloud_setup             # prints gcloud commands: Firestore DB + vector index
python -m scripts.upload_policy_docs --apply    # policy Markdown -> gs://$GCS_BUCKET/policy_docs/
python -m scripts.seed_firestore --apply        # properties, units, residents, work_orders, counter
python -m scripts.index_policy_chunks --apply   # embed policy sections -> policy_chunks
```

All of these are safe to re-run. Unchanged files and chunks are skipped.
`seed_firestore --apply --prune` resets everything to the seed snapshot, including
proposals, execution records and the work-order counter.
See [data/README.md](data/README.md) for what goes where.

## Run the agent locally

```bash
python -m scripts.dev_web                       # read-only, cloud Firestore, as DEV_AUTH_CONTEXT
python -m scripts.dev_web --as AUTH-RES-004     # a different demo identity

gcloud emulators firestore start --host-port=127.0.0.1:8085 &   # needs Java 21+ on PATH
FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 python -m scripts.dev_web --emulator --as AUTH-RES-002
```

- It only starts when `APP_ENV=development` and always binds to `127.0.0.1`.
- The demo identity is fixed on the server for the whole process. Nothing in the browser
  or the chat can change it.
- `--emulator` reseeds the emulator (this makes live embedding calls) and enables
  creation. Everything is written to the emulator's `demo-propertyops` project, never to
  the cloud.
- Outside development the agent **fails closed**: every tool returns `unauthenticated`
  until a real identity provider is connected (`app/agent/identity.py`).

## Work-order creation (Stage 2)

```
prepare ─▶ AWAITING_APPROVAL ─submit─▶ APPROVAL_REQUESTED ─user confirms─▶ EXECUTED (work order created)
             │ new prepare ─▶ SUPERSEDED     │ user declines ─▶ DECLINED
             │ cancel ──────▶ CANCELLED      │ cancel ────────▶ CANCELLED
             └ past expiry ─▶ EXPIRED        └ past expiry ───▶ EXPIRED
```

The agent's steps:
1. Gather the facts.
2. Search the policy and set priority from the cited sections.
3. Check active requests for possible duplicates.
4. Ask the resident about entry permission, as its own question.
5. `prepare_work_order_proposal`, which saves the proposal and shows its exact contents.
6. `submit_work_order_proposal`.
7. ADK **tool confirmation**.
8. A transaction that creates the order.

`list_my_proposals` finds proposals from earlier conversations.

**Approval boundary.**
- On its first call, `submit` records an approval request bound to
  `(proposal_id, payload_hash, function_call_id)` and asks ADK for confirmation.
- ADK pauses the run and shows the backend-rendered proposal. Only the user's client can
  resume the run, by sending an `adk_request_confirmation` function response. The model
  cannot author that event, and ADK checks it against the original call in the session
  history.
- No tool has an `approved` argument, and the guard rejects any extra argument. A typed
  "yes" only leads to another confirmation request.
- On resume, the transaction re-checks ownership, status, expiry, payload integrity,
  the approval binding and duplicates.
- **Limitation:** ADK's dev web server does not authenticate users. In production, the
  API that relays confirmations must authenticate the session owner.

**Transactions and idempotency** (`app/agent/proposals.py`):
- Execution is one Firestore transaction. It reads the execution record, proposal, the
  unit's work orders and the counter, then writes the work order, the counter increment,
  the execution record and `EXECUTED`, all together.
- The execution key is `sha256(proposal_id:payload_hash)`. A repeated, concurrent or
  post-timeout retry therefore returns the same work-order ID.
- Contention is retried with backoff and ends in a retryable `busy` error.
- Nothing external, such as a model call, runs inside the transaction function.

**Duplicates.** Exact retries are prevented by the execution key. *Semantic* duplicates
(a different proposal for possibly the same issue) are handled in two places:
- At prepare time, active orders in the same category must be shown to the resident,
  and listed in `acknowledged_work_order_ids` if the resident says it's a different
  issue. Category alone never blocks a request.
- At execution, the check runs again inside the transaction, so two proposals racing
  for the same issue cannot both succeed.

Completed and cancelled orders never block a new request.

## How access is enforced

| Concern | Where |
|---|---|
| Identity resolved on **every** tool call from a backend `IdentityProvider`; never from tool arguments, session state or the conversation | `app/agent/identity.py` |
| Tools expose no identity or approval arguments; a guard rejects anything not allowlisted | `app/agent/guards.py`, `TOOL_ARGUMENTS` |
| Policy search filtered to the actor's property in Firestore, then re-checked in code | `app/retrieval.py` |
| Work orders: residents see only their own unit's records; managers only assigned scope | `app/agent/work_orders.py` |
| Proposals bound to owner, unit, payload hash and expiry; foreign proposals look the same as missing ones | `app/agent/proposals.py` |
| Explicit allowlists for returned work-order and proposal fields | `RESIDENT_VISIBLE_FIELDS`, `PROPOSAL_VIEW_FIELDS` |
| Retrieved text labelled as untrusted evidence | `app/agent/tools.py`, `app/agent/prompt.py` |
| Failures become "could not be verified" or `outcome_unknown` (safe to retry); never a guessed result | tools |
| Per-turn model and tool call limits; tool, model and run timeouts | `app/agent/guards.py`, `.env` |
| Firestore target: `emulator` needs a reachable local host and a `demo-*` project; `cloud` refuses if `FIRESTORE_EMULATOR_HOST` is set. Neither falls back to the other | `app/gcp.py` |

## Tests and evaluation

| Suite | Command | Needs | Cost | In CI |
|---|---|---|---|---|
| Offline | `pytest` | nothing | free | yes (job `test`) |
| Emulator integration | `scripts/run_emulator_tests.sh` | gcloud emulator, Java 21+ | free | yes (job `emulator`) |
| Live, read-only cloud | `python -m scripts.run_live_eval --mode cloud-readonly` | ADC, seeded cloud Firestore | Gemini calls (~20 runs + judge) | no |
| Live, emulator | `FIRESTORE_EMULATOR_HOST=… python -m scripts.run_live_eval --mode emulator` | ADC (Vertex only), running emulator | Gemini calls plus 18 embedding calls per run | no |

- **Offline tests** cover:
  - proposal ownership, expiry and payload changes;
  - missing or mismatched approval, and entry permission;
  - duplicates and repeated execution;
  - timeout-after-commit recovery;
  - argument guards and fail-closed targets.
- **Emulator tests** cover what mocks cannot: atomicity, rollback on a failed commit,
  10-way concurrent execution of one proposal, gap-free numbering under contention,
  racing duplicate proposals, timeout-after-commit recovery, and supersession.
- **`cloud-readonly`** never registers creation tools, so it cannot write. Cases that
  need writes are reported as SKIP.
- **`emulator` mode:**
  - Every Firestore read and write goes to the emulator, with a fresh seed per case and
    the dataset's snapshot clock.
  - The harness plays the user's client: it answers confirmation dialogs only as the
    case's approval fixture allows, and only for the exact proposal and payload hash.
  - Fixture gaps are filled in the harness and printed with each case
    (`app/evaluation/fixtures.py`).
- **Every live case is checked for:**
  - correctness (an LLM judge);
  - citation support and expected tool use;
  - unauthorized disclosure and unconfirmed creation claims;
  - in emulator mode, side effects: the exact number of new work orders, all in scope.
- Rate-limit (429) errors are retried with backoff. Reports go to `reports/`.
- **Not done yet:** cloud write evaluations. They are deferred to a separate opt-in
  suite that will need an isolated eval-only database, dedicated credentials and
  deterministic cleanup. The default evaluation path has no cloud write access.

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
| `ENABLE_WORK_ORDER_CREATION`, `PROPOSAL_TTL_SECONDS` | Stage 2 creation (off by default); proposal lifetime |
| `FIRESTORE_TARGET`, `EMULATOR_PROJECT_ID` | `cloud` or `emulator` (demo-* project) |
| `EVAL_JUDGE_MODEL` | Judge model for live evaluation (blank = `GEMINI_MODEL`) |
