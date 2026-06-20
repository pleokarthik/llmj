# Cross-Provider LLM Journal — Detailed Design (DDD)

> Companion to HLD.md. Specifies the contract, interfaces, and the algorithmic core.
> Convention reminder: complexity lives in the schema (structural) and in context assembly (behavioral). Recording is a trivial method call earned by the choke points.

---

## 1. The contract: journal schema

The schema is the durable, language-neutral contract. Invest here; get it right once. Plain SQLite (WAL) columns + JSON blobs only — nothing language-locked.

### 1.1 `events` (immutable, append-only — the source of truth)

A generalized event log. One row per event, regardless of kind. LLM-specific columns are nullable because tool calls and run envelopes do not have them.

| Field | Type | Notes |
|---|---|---|
| `event_id` | TEXT (ULID) | Globally unique, **time-sortable** (free ordering across devices) |
| `event_type` | TEXT | `llm_call \| tool_call \| agent_run` (+ optional `step` for non-LLM reasoning) — **typed, queryable** |
| `chat_id` | TEXT | Conversation grouping |
| `parent_id` | TEXT, nullable | Threading: regeneration trees, agent sub-calls, run children |
| `root_id` | TEXT | The user-visible turn / agent run an event rolls up to; enables true per-turn & per-run cost |
| `origin` | TEXT | `user \| agent:<name> \| system:summarizer \| system:retrieval \| system:enrichment` — *who* initiated; typed, queryable |
| `role` | TEXT, nullable | `user \| assistant \| system` (LLM events) |
| `tool_name` | TEXT, nullable | For `tool_call` events — typed, queryable |
| `status` | TEXT, nullable | Outcome for tool calls and run terminal events: `ok \| error \| timeout \| aborted \| max_steps` |
| `provider` | TEXT, nullable | LLM only: `openai \| anthropic \| google \| …` |
| `model` | TEXT, nullable | LLM only |
| `params` | JSON, nullable | LLM only: temperature, max_tokens, etc. |
| `content` | TEXT, nullable | Message body / text payload |
| `tokens_in` | INTEGER, nullable | LLM only |
| `tokens_out` | INTEGER, nullable | LLM only |
| `cost` | REAL, nullable | LLM only; from a provider/model pricing table (a maintenance tail) |
| `latency_ms` | INTEGER, nullable | Duration — applies to LLM calls, tool calls, and runs |
| `scope` | TEXT | `user` default; `org:<id>` only when explicitly opted in |
| `payload` | JSON, nullable | Heterogeneous tail: tool args/results, run trigger, agent role/step index, reasoning tag |
| `created_at` | TEXT (ISO-8601) | Begin time |

Written **once** per event, never updated. Committed durably (WAL) the instant an event completes (view/embedding updates may stay async). The structure of a run is the threading tree: an `agent_run` is a `root_id`, and its `llm_call` / `tool_call` children point at it.

A hidden agent/system call is **not a new entity** — it is an `events` row with non-`user` `origin`, threaded under the turn or run that spawned it. This includes the enrichment LLM call (`origin=system:enrichment`), which is journaled like any other call.

### 1.2 Run lifecycle (how a run is bracketed)

A run is recorded as immutable events, never as a mutable status field:

- **start** — append `agent_run` (the top/root record; carries `trigger` in `payload`).
- **steps** — append `llm_call` / `tool_call` children; each may be recorded **begin → end** (pending-span style): a "started" row, finalized on completion. An unfinished record marks the exact in-flight step a crash died on.
- **terminal** — on a clean end, append one terminal event with `status` in `{run_completed | run_failed | run_aborted}`.
- **crash** — no terminal event is written (the process died). Detected by absence (see §1.4).

### 1.3 `evaluations` (derived, append-only — a metric bag, deterministic only)

| Field | Type | Notes |
|---|---|---|
| `event_id` | TEXT | FK → events |
| `metric_name` | TEXT | e.g. `regen_count`, `followups`, `tool_error_rate` |
| `value` | REAL/TEXT | |
| `evaluator_version` | TEXT | Re-runnable; metrics evolve |
| `created_at` | TEXT | |

A **bag**, not fixed columns: adding a metric is a row, not a migration. **Deterministic/mechanical only** — counts, durations, costs, rates. No human annotation, no scoring workflow, no LLM-as-judge in core (that is the eval/judgment category, out of scope; a fork may add it). Metrics deliberately do **not** live on the immutable event — they are *queries* over the log (e.g. regeneration count = siblings under a `parent_id`).

### 1.4 Run projection (derived view — carries status)

The "top record with status" the user queries. A view that **folds the run's lifecycle events**; mutable/rebuildable because it is a view, not the journal.

| Field | Notes |
|---|---|
| `root_id` | The run |
| `status` | `running \| completed \| failed \| aborted \| timeout \| max_steps \| crashed` |
| derived metrics | steps, tool-call count, error rate, depth, iterations, total tokens/cost, wall-clock duration, termination reason |

Status resolution: terminal event present → its status. Start with no terminal event + live process → `running`. Start with no terminal event + dead process → **`crashed`**. On engine restart, a **reconciliation pass** marks orphaned runs `crashed` *in the projection only* — the journal already correctly shows "started, no terminal," and is never mutated.

### 1.5 Vector payload (derived view — the retrieval corpus)

Stored in the active vector store (SQLite/`sqlite-vec` by default, Qdrant optional). Payload carries:

| Field | Notes |
|---|---|
| `event_id`, `chat_id` | Links back to the journal (for raw event chunks) |
| `okf_doc_id` | nullable; links to the OKF bundle doc (for OKF-sourced embeddings) |
| `provenance` | `user_statement \| model_claim` (+ `user_confirmed` only if derived **passively** from the user restating/affirming); inherited from OKF frontmatter for OKF-sourced embeddings |
| `model`, `created_at` | |
| `superseded_by` | nullable pointer; supersession by metadata, not mutation |
| `content` | Enriched into payload to avoid a journal round-trip on retrieval |

**Retrieval corpus sources (two tiers):**
- **Primary (step 7+): OKF concept docs** — curated, named, stable, provenance-tagged. Embedded from the OKF bundle doc body. Higher provenance weight in retrieval.
- **Secondary: raw event chunks** — granular, recent, unenriched. Embedded directly from `events.content`. Used for recency and specificity where no OKF concept covers the content.

Provenance weighting applies uniformly: `user_confirmed > user_statement > model_claim`, regardless of source tier.

### 1.6 OKF bundle (derived view — the curated knowledge layer)

An OKF bundle is a directory of markdown files with YAML frontmatter, produced by the enrichment pipeline from journal events. It is a **derived, rebuildable view** — losable without data loss, since the journal is the source. Output format is Open Knowledge Format v0.1 (Google Cloud, published June 12, 2026).

**Standard OKF fields (v0.1 spec):**

| Field | Type | Notes |
|---|---|---|
| `type` | TEXT | Required. e.g. `concept \| decision \| runbook \| metric` |
| `title` | TEXT | Human-readable concept name |
| `description` | TEXT | One-line summary |
| `resource` | TEXT | Link to authoritative source (URL or file path) |
| `tags` | list[TEXT] | Classification tags |
| `timestamp` | TEXT (ISO-8601) | When this concept document was produced |

**Extended fields (non-standard; spec-compliant — OKF mandates tolerant unknown-field handling):**

| Field | Type | Notes |
|---|---|---|
| `provenance` | TEXT | `user_statement \| model_claim \| user_confirmed` — inherited from the strongest-provenance source event in `source_event_ids` |
| `source_event_ids` | list[TEXT (ULID)] | Back-pointers to the journal events that produced this concept; enables full audit trail |
| `superseded_by` | TEXT, nullable | ULID of the OKF doc that replaces this one; supersession by pointer, never deletion |

**Body:** Free-form markdown. Cross-links to other OKF docs via standard markdown links. The body is the content that gets embedded into the retrieval corpus.

**Provenance inheritance rule:** The OKF doc inherits the strongest provenance in its `source_event_ids` set. If any source event is `user_statement`, the doc is `user_statement`. If all source events are `model_claim`, the doc is `model_claim` and is tagged accordingly in retrieval — never auto-promoted to ground truth.

**Enrichment pipeline:**
```
journal events (filtered: recency threshold, stable content, min provenance)
  → OKFEnricher.enrich(events)          # LLM call; journaled (origin=system:enrichment)
  → OKFDoc (frontmatter + markdown body)
  → written to bundle directory
  → Embedder.embed([doc.body])
  → Store.upsert_vector(event_id, text, embedding, provider, model)
```

---

## 2. Interfaces (the fork points)

One concrete implementation each — **no factory until ≥2 implementations exist**, except the provider registry, which is earned immediately (OpenAI/Anthropic/Google).

```
Store           # persistence of journal + views
  .append(event)              -> None          # immutable, durable write
  .get(event_id)              -> Event
  .query(filter)              -> Iterable[Event]
  .upsert_vector(event_id, text, embedding,
                 embedding_provider, embedding_model)  -> None
  .search(query_embedding, top_k, chat_id, scope)     -> list[tuple[str, float]]

Embedder        # text -> vector
  .embed(text: str)           -> list[float]

ProviderAdapter # canonical <-> wire format, one per provider (registry earned)
  .to_wire(canonical_request) -> provider_payload
  .from_wire(provider_resp)   -> canonical_response

ToolRunner      # the tool-execution choke point
  .run(tool_name, args)       -> result        # emits begin/end tool_call events

OKFEnricher     # journal events -> OKF concept documents (one impl; no factory)
  .enrich(events: list[Event]) -> list[OKFDoc]  # LLM call; caller journals it
  .write_bundle(docs: list[OKFDoc], bundle_path: str) -> None
```

`Store`, `Embedder`, `ToolRunner`, `OKFEnricher`: one impl each, no dispatch machinery. `ProviderAdapter`: a small registry keyed by provider is the **only** place the registry/factory earns its place at v1.

Mechanism lives in the engine; policy (which summarizer, what `k`, retrieval weighting, which events to enrich, precedence rules) is configurable and is where forks diverge.

---

## 3. Capture mechanism

**Goal: provable completeness across LLM calls *and* tool executions.** Do not rely on agents to report their calls — that is opt-in and fragile.

- **Two choke points (best).** Every provider call goes through the journaled `LLMClient`; every tool execution goes through the journaled `ToolRunner`. Agents are handed these, never raw clients. Recording becomes a property of the only call paths that exist.
- **Durable, begin/end recording.** Each event commits to the WAL as it completes; steps are recorded started → finalized. A crash leaves every completed step plus the started-but-unfinished one — capture-till-failure.
- **Foreign agents you can configure (good).** Point the agent's `base_url` at a **local journaling proxy** (the only legitimate place the "gateway" idea lives — capture path, never product surface). LLM calls are captured at the wire; tool *executions* are only partially recoverable — you get tool-use *intentions* from the LLM I/O, not durations/results.
- **Framework callbacks / OTel (last resort).** Only when neither above is possible. Cannot guarantee completeness.

Because the call paths are closed, **recording is a single method call with loaded params** — the minimalism is *earned* by this decision, not free.

---

## 4. Model-switch handshake protocol

```
on switch A -> B:

  step 1 (handshake):
    call A with a "summarize this conversation for handoff" prompt
    journal the result: role=assistant, origin=system:summarizer, provenance=model_claim

  step 2 (B, first turn):
    retrieval_query = expand(user_first_query, handoff_summary)   # first query is usually thin
    hits = retrieve(retrieval_query)                              # see §5
    inject context blocks, labelled:
      [VERBATIM SOURCE — authoritative]  hits (provenance-weighted; OKF docs primary)
      [LOSSY OVERVIEW]                   handoff_summary
    on conflict, B favours SOURCE over OVERVIEW

  step 3 (B, subsequent turns):
    standard per-turn assembly (§5); no new summary unless switching again
```

The summary's real job is twofold: continuity **and** seeding the first retrieval when the literal first query carries too little signal.

---

## 5. Per-turn context assembly (the behavioral core)

```
assemble_context(chat_id, query):
    qv      = Embedder.embed([query])[0]
    hits    = Store.search(qv, filter={chat_id, scope}, k)
    hits    = weight_by_provenance(hits)   # user_confirmed > user_statement > model_claim
                                           # OKF-sourced hits inherit OKF frontmatter provenance
    hits    = recency_tiebreak(hits)       # prefer most recent among near-duplicates (supersession v1)
    summary = current_rolling_summary(chat_id)   # bounded; regenerated from journal, never appended
                                                  # journal-derived, not OKF (different lifecycle)
    return render([
        block("VERBATIM SOURCE (authoritative)", hits),
        block("LOSSY OVERVIEW", summary),
    ])
```

Properties: query-conditioned every turn (not just at switch time); bounded budget → O(1) in conversation length; `model_claim` chunks injected only with an "unverified prior model response" tag, never as ground truth. OKF-sourced hits with `user_statement` provenance rank highest in the hit set.

---

## 6. Agentic execution metrics (all derived views — never stored)

Once the events of §1.1–§1.2 exist, every execution metric folds out of the run tree deterministically. None is stored; all are queries:

- steps per run; tool-call frequency by `tool_name`
- error rate (`status`); nesting depth (parent tree)
- iteration / loop count → the runaway-loop alert (deterministic threshold over repeated `tool_name` under one `root_id`)
- true tokens & cost per run (sum of `llm_call` children via `root_id`; includes `system:enrichment` calls for enrichment cost tracking)
- wall-clock duration (terminal − start); termination reason (run terminal `status`)

No judgment is involved — these are facts about what the agent did.

---

## 7. Read-side surfaces (the third extension surface)

All read-only over the journal; none touches the contract or context assembly. Optional extras.

- **OTel GenAI export** — a serializer view mapping `events` → `gen_ai.*` spans (`gen_ai.request.model`, `gen_ai.provider.name`, `gen_ai.operation.name` ← `event_type`, `gen_ai.input/output.messages`). "Instrument once, analyze anywhere" — pipe to any OTel backend without coupling the core.
- **SQL** — the journal is already SQLite; query it directly. No proprietary DSL.
- **MCP read server** — wraps the query surface so an agent/IDE can query the cross-provider history in-context ("your history, queryable by your coding agent").
- **OKF bundle export** — the enrichment pipeline's output is itself a portable read-side artifact: a directory of markdown + YAML files any OKF-compliant consumer can read without knowing journal internals. Provenance-tagged so consumers know what to trust. Any agent, IDE, or tool that reads files can consume it.
- **Replay / diff / fork** — fold the append-only log to reconstruct point-in-time state; diff branches (regeneration trees already encode them); fork from a point to test a variant. Free, because event-sourcing. (Reconstruct, not re-execute.)
- **Economics & loop/cost alerts** — deterministic aggregation (true per-turn/per-run cost via `root_id`; threshold alerts; enrichment LLM costs surfaced separately under `origin=system:enrichment`).

---

## 8. Growth management

Disambiguate; only one surface is a real problem.

- **Journal** — never pruned. The asset, and cheap. Partition by time only if it ever matters.
- **Active context** — already O(1) by construction (§5).
- **Vector corpus** (the one needing management; safe because rebuildable): dedup at write (content hash); scalar quantization (Qdrant); hot/cold tiering; selective embedding by provenance (user/confirmed eager, `model_claim` lean/lazy). OKF-sourced docs are always eagerly embedded (they are pre-curated).
- **OKF bundle** — concepts are upserted by identity (`title` + `type` = overwrite, not accumulate); old doc gets a `superseded_by` pointer. Bundle size stays proportional to distinct concept count, not event count. Rebuildable from the journal on demand.
- **Rolling summary** — regenerated from the journal periodically (not appended, not summary-of-summary). Possible only because the log is lossless. Distinct lifecycle from OKF; not stored in the bundle.

Spine: **bound every view, never the journal; when a bound forces a drop, rebuild from the log.**

---

## 9. Packaging

The **data** portability is settled: SQLite + JSON (journal), markdown + YAML (OKF bundle) — both language-neutral. This is the **engine** question.

- **Core** = stdlib + SQLite only. Journal, schema, recording, context assembly. Importable anywhere Python runs; effectively a single portable file. Default vector store via `sqlite-vec` (verify maturity; numpy-cosine fallback at small scale).
- **Heavy/optional deps as extras** — `pip install <tool>[qdrant]`, `[ollama]`, `[otel]`, `[mcp]`. Power is opt-in; the core never carries weight it doesn't need.
- **Thin CLI** on top — the library does the work; the CLI is a wrapper.
- **Fork boundary** = the interfaces (§2). Core ships mechanism; adapters/extras are policy.

Ruled out: Docker (deployment convenience, not a library); standalone binary (only for non-Python CLI users, much later); Rust/WASM cross-language core (speculative; the data format already carries cross-language portability).

---

## 10. Suggested build order (one block at a time)

1. **`events` schema + journal + durable writes + choke-point `LLMClient`** — the contract and capture, one provider. Verify completeness (no path bypasses the journal). The `event_type / tool_name / status / origin` axes are fixed here, since the contract is immovable. Note `system:enrichment` in the origin enum — it costs nothing to add now and avoids a schema touch later.
2. **Single `ProviderAdapter`** + a real end-to-end recorded turn.
3. **Per-turn retrieval** (`Embedder`, `Store.search`, provenance weighting, recency tiebreak). Embeds raw event chunks at this stage — OKF docs replace/supplement this in step 7.
4. **Second `ProviderAdapter`** + the switch handshake (§4). Registry earns its place here.
5. **`ToolRunner` + agent_run lifecycle + run projection + failure/crash handling** (§1.2, §1.4, §3) — agentic execution and capture-till-failure.
6. **Views: economics first** (deterministic aggregation — cheapest, hardest to get wrong), then the read-side surfaces (OTel export, SQL, MCP, replay) as optional extras.
7. **OKF enrichment pipeline** — `OKFEnricher`: journal events → OKF concept docs (provenance-tagged, `source_event_ids` back to journal) → embed OKF docs as the primary retrieval corpus. Write bundle to disk. Verify: an external OKF-compliant reader can consume the bundle without knowing anything about journal internals. Concept/decision registries now emit to OKF v0.1 standard format. Extraction quality is exploratory; output format and provenance trail are not.

Each step is observable end-to-end before the next begins. No speculative abstraction ahead of a second concrete implementation.
