# Five-minute demo script

**Setup (before the call):**
- Terminal 1: start the emulator with
  `PATH=/opt/homebrew/opt/openjdk@21/bin:$PATH gcloud emulators firestore start --host-port=127.0.0.1:8085`.
- Terminal 2: start the app with
  `FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 python -m scripts.serve --emulator`.
- Open http://127.0.0.1:8080 and set the demo identity to `AUTH-RES-002`.
- Have [ARCHITECTURE.md](ARCHITECTURE.md) and [RESULTS.md](RESULTS.md) open in other
  tabs.

---

**0:00–0:30 · The problem.** "Residents ask maintenance questions, and the answers
depend on *their* property's policy. Filing a request is a real action. I wanted an
assistant where the model is never trusted with identity, scope or approval."

**0:30–1:15 · Architecture** (system diagram in ARCHITECTURE.md). "It's a FastAPI app on
Cloud Run running an ADK agent on Gemini. Policies are embedded into Firestore vector
search with a property pre-filter. Work orders, proposals and approvals live in the same
Firestore, so creation can be one transaction. Identity comes from a verified Google
token; tools re-resolve it on every call and accept no IDs from the model."

**1:15–2:00 · Grounded answers.** Ask: *"My AC is out and it's 83°F inside. How urgent
is that?"*
- Point out that it's **routine** at Cedar Grove (threshold 85°F) and that the initial
  contact target is 2 business days. Click the `[DOC-CG-HVAC-S01]` chip to show the
  exact policy text.
- Switch to `AUTH-RES-004` (Lakeview) and ask the same question: **urgent**, because
  Lakeview's threshold is 82°F. Same question, different property, different answer;
  scope comes from identity.

**2:00–3:15 · Approval-controlled creation.** As `AUTH-RES-002`, type: *"My AC stopped
cooling, it's 80°F, new issue, appointment required for entry. Please submit a
request."*
- The agent cites the policy, checks for duplicates and shows a **proposal card** with
  the exact contents.
- "Nothing exists yet. The 'Your requests' panel hasn't changed. If I type 'yes' in the
  chat, it only brings up the card again. The model can't approve."
- Click **Approve and submit**. WO-011 appears. "The backend accepts only the
  confirmation pending in *my* session, once. The transaction then re-checks ownership,
  expiry, the payload fingerprint and duplicates before writing the order, its counter
  and an idempotency record together."

**3:15–3:45 · Safety behaviour.** As `AUTH-RES-001`, ask *"Show me Jordan in unit 102's
dishwasher work order"*: refused, without saying whether it exists. Optionally ask for a
new AC request: it shows the existing open WO-001 and asks whether this is a different
issue.

**3:45–4:30 · Evidence** (RESULTS.md):
- 210 offline tests and 8 emulator tests in CI. In the emulator, 10 simultaneous
  executions of one proposal create exactly one order.
- Live evaluation: 16/16 read-only and 19/20 in emulator mode. Mention the judge false
  negative and how it was fixed: "I don't trust the judge for safety-critical checks;
  those are deterministic."
- Load: p50 3–6 s, dominated by model calls. Under 30% injected backend failures there
  were zero 5xx responses, and the assistant said "could not be verified" instead of
  guessing.

**4:30–5:00 · Trade-offs and next steps.**
- "One Cloud Run instance today, because sessions are in memory. Next would be a
  persistent session store, then scaling out."
- "Then fewer model calls per turn for latency, and a cloud-write evaluation suite on
  an isolated database."

---

**Likely questions**
- *Why not RAG Engine?* One datastore gives filtering, transactions and less
  infrastructure. The flat index is fine at this scale; switch if the corpus grows by
  orders of magnitude.
- *What if the model calls submit without asking?* It only gets a confirmation request.
  Creation needs the user's client to answer that specific request, and the transaction
  re-validates everything.
- *What if the request times out after commit?* Calling submit again returns the same
  ID through the execution key. This is tested in the emulator.
- *How do you know it doesn't leak other tenants' data?* Tools have no ID arguments, the
  property is filtered in the query and re-checked, fields are allowlisted, and every
  live evaluation case runs a deterministic disclosure check.
