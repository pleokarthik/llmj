# Architecture

This document describes llmj's architecture as it exists in the current codebase. It
supersedes nothing by way of vision or roadmap — every claim below is grounded in a
specific file and, where useful, a line range. If the code changes, this document is
wrong until updated; treat it as a snapshot, not a spec.

## Three layers

llmj is organized into two physical layers (`core/`, `handshake/`) and one functional
layer (projection) that is not a separate directory — it is a set of read-deriving
functions distributed across the other two.

### 1. Core (`core/`)

The lowest layer. Owns the journal, the event schema, provenance rules, and all
outbound I/O (LLM providers, embedding providers). Nothing here knows about
multi-turn conversations or run lifecycles.

| Module | Responsibility |
|---|---|
| `event_model.py` | The `Event` dataclass — the schema is the contract. Frozen (immutable in Python, not just in SQL). |
| `journal_store.py` | `Store`: SQLite journal in WAL mode, append-only `events` table, dual-write to `vectors`/`vec_events` for similarity search. |
| `provenance.py` | `derive_provenance()` — resolves the effective provenance tier for an event, honoring a caller-asserted tier when present and enforcing the one hard invariant (model output can never claim `user_confirmed`). |
| `llm_client.py` | `LLMClient.call()` — multi-provider HTTP client. Journals every call's constituent messages and its outcome; no LLM round-trip happens without a journal entry. |
| `tool_runner.py` | `ToolRunner.run()` — journals tool invocation start/end as `tool_call` events, mirroring `LLMClient.call()`'s start/end pattern. |
| `vector_embedder.py` | `Embedder` + provider adapters (sentence-transformers, Ollama, HuggingFace, Google). |
| `provider_adapter.py` | Translates the canonical request/response shape to/from each LLM provider's wire format. |
| `id_generator.py` | Pure-Python ULID generation (`ulid()`, [core/id_generator.py:7](../core/id_generator.py#L7)) — every `event_id` and `call_id` is one of these. |
| `config.py` | `.env` loading (`load_project_env()`). |

### 2. Handshake (`handshake/`)

Sits above core. Orchestrates multi-event sequences (a run, a context-assembly pass,
a provider switch) by composing core primitives. Nothing here writes SQL directly —
everything goes through `Store`.

| Module | Responsibility |
|---|---|
| `session_runner.py` | `start_run` / `resume_run` / `end_run` / `get_run_status` / `reconcile_crashed_runs` — run lifecycle, scoped by `root_id`. |
| `context_assembler.py` | `get_call`, `weight_by_provenance`, `recency_tiebreak`, `current_rolling_summary`, `assemble_context` — turns the journal into per-turn context for the next LLM call. |
| `okf_enrichment.py` | `OKFEnricher.enrich` / `enrich_run` — LLM-driven summarization of assistant responses into OKF bundle files (`okf/*.json`) plus a vector upsert. |
| `provider_switch.py` | `summarize_for_handoff` / `expand_query` / `switch_model` — handoff between providers/models mid-conversation. |

### 3. Projection (functional, not a directory)

There is no `projection/` package. "Projection" here is the event-sourcing term for
the functions that read the immutable journal and derive a view from it — the layer
exists conceptually, realized as specific functions inside `core/` and `handshake/`:

- `derive_provenance()` ([core/provenance.py:6-25](../core/provenance.py#L6-L25)) — projects an event's `(origin, role, provenance)` into one of three tiers.
- `get_call()` ([handshake/context_assembler.py:24-65](../handshake/context_assembler.py#L24-L65)) — projects a `call_id`'s message rows into either raw rows or the original request shape.
- `weight_by_provenance()` ([handshake/context_assembler.py:68-85](../handshake/context_assembler.py#L68-L85)) — projects raw vector-search hits into provenance-weighted scores.
- `current_rolling_summary()` ([handshake/context_assembler.py:109-151](../handshake/context_assembler.py#L109-L151)) — projects all assistant responses in a chat into a rolling summary (itself journaled as a new `llm_call`, `origin="system:summarizer"`).
- `assemble_context()` ([handshake/context_assembler.py:154-186](../handshake/context_assembler.py#L154-L186)) — composes the above into the context string handed to the next LLM call.
- `OKFEnricher.enrich()` / `enrich_run()` ([handshake/okf_enrichment.py:30-93](../handshake/okf_enrichment.py#L30-L93)) — projects an assistant response into an OKF knowledge bundle.
- `get_run_status()` / `reconcile_crashed_runs()` ([handshake/session_runner.py:122-230](../handshake/session_runner.py#L122-L230)) — projects a run's events into status, cost, token totals, crash detection.
- `rebuild_vectors_from_journal()` / `rebuild_vec_index()` ([core/journal_store.py:230-280](../core/journal_store.py#L230-L280)) — projects `embedding`-typed events back into the `vectors`/`vec_events` tables.

## Event-sourcing framing

llmj's `events` table is an event log in the literal event-sourcing sense:

- **Journal = event log.** `events` is append-only by construction — `events_no_update`
  and `events_no_delete` triggers ([core/journal_store.py:73-90](../core/journal_store.py#L73-L90)) raise `ABORT` on any
  `UPDATE`/`DELETE`. The journal is the only durable source of truth; every other
  table or file in the system is derived from it.
- **Derive = projection.** Every function listed above reads journal rows and
  computes a view. None of them mutate the journal. `derive_provenance()` is the
  purest example — a stateless function from `(origin, role, asserted)` to a tier
  string, with no I/O at all.
- **Active state = read model.** The materialized outputs are all rebuildable from
  the journal, never the other way around:
  - `vectors` / `vec_events` — rebuildable via `rebuild_vectors_from_journal()`
    ([core/journal_store.py:247-280](../core/journal_store.py#L247-L280)), which replays `event_type="embedding"` events.
  - `okf/*.json` bundle files — regenerable by re-running `enrich_run()` over a
    `root_id`.
  - The per-turn context string from `assemble_context()` — never persisted at all;
    recomputed fresh on every call.
  - Run status from `get_run_status()` — computed on demand from `agent_run` /
    `llm_call` / `tool_call` events scoped to a `root_id`, never stored.

If `vectors`, `vec_events`, or the `okf/` directory were deleted, every one of them is
reconstructable from `events` alone. The journal is the only piece of state that isn't.

## Provenance tier system

Three tiers, ordered `user_confirmed > user_statement > model_claim`, defined in
`VALID_PROVENANCE_TIERS` ([core/provenance.py:3](../core/provenance.py#L3)) and weighted in `PROVENANCE_WEIGHTS`
(`{"user_confirmed": 1.0, "user_statement": 0.8, "model_claim": 0.4}`,
[handshake/context_assembler.py:11-15](../handshake/context_assembler.py#L11-L15)).

`derive_provenance(origin, role, asserted=None)` ([core/provenance.py:6-25](../core/provenance.py#L6-L25)) is the
single function that resolves a tier:

- If `asserted` is given, it's validated against `VALID_PROVENANCE_TIERS` and
  returned — **except** `user_confirmed` is rejected (`ValueError`) when
  `role == "assistant"` or `origin` starts with `"system:"`. Model and system
  output can never self-promote to the highest tier; this is code-enforced, not a
  convention callers are trusted to follow.
- If `asserted` is `None`, the tier is derived from `origin`/`role`:
  `origin == "user" and role == "assistant"` → `model_claim` (a model's own prior
  output, replayed back to it); `origin == "user"` → `user_statement`; anything
  else → `model_claim`.

Storage follows a "store when asserted, derive when absent" contract: `Event.provenance`
([core/event_model.py:30](../core/event_model.py#L30)) is `Optional[str]`, defaulting to `None`. A `None` value
means "derive this at read time"; a non-`None` value is the caller's durable assertion,
written once and never recomputed.

**Where assertion actually happens today:** `LLMClient.call()` accepts a `provenance`
parameter ([core/llm_client.py:50](../core/llm_client.py#L50)) and, if given, validates and stores it on the
*last* message of the call via `derive_provenance(origin, msg_role, provenance)`
([core/llm_client.py:69-71](../core/llm_client.py#L69-L71)) — before any event is written, so an invalid assertion
(e.g. asserting `user_confirmed` on a call whose final message is an assistant turn)
fails before anything is journaled. This is the only write path in the system that can
produce a stored `user_confirmed` tier.

**Where it's consumed:** `weight_by_provenance()` ([handshake/context_assembler.py:68-85](../handshake/context_assembler.py#L68-L85))
calls `derive_provenance()` on every retrieved event to compute its retrieval weight;
`OKFEnricher.enrich()` ([handshake/okf_enrichment.py:32](../handshake/okf_enrichment.py#L32)) calls it to tag an OKF
bundle's `trust_tier`. Both treat a stored tier and a derived one identically — the
caller of `derive_provenance` never needs to know which case it is.

## Today's write path: message grain

`LLMClient.call()` takes one `messages` list per invocation (the full conversation
history plus the new turn, OpenAI-style `{"role", "content"}` dicts) and journals it
at **message grain**, not call grain:

- A `call_id` (a fresh ULID) is minted once per `call()` invocation
  ([core/llm_client.py:61](../core/llm_client.py#L61)).
- One `Event` with `event_type="message"` is written per item in `messages`, in
  order, with `sequence` set to that item's list index and `call_id` shared across
  all of them ([core/llm_client.py:65-96](../core/llm_client.py#L65-L96)). Each row's `role` is taken from the
  message dict itself (`message.get("role")`), not hardcoded.
- The existing `event_type="llm_call"` start/end events are unchanged in shape and
  purpose — they still bookend the call for crash-detection and parent/child linking
  — except the start event's `content` no longer carries the serialized `messages`
  blob (`content=None`, [core/llm_client.py:111](../core/llm_client.py#L111)); that data now lives in the
  `message` rows instead of being duplicated. Neither the start nor end event carries
  a `call_id` — `call_id`/`sequence` exist only on `event_type="message"` rows.

`get_call(call_id, store, grain="message"|"blob", filter=None)`
([handshake/context_assembler.py:24-65](../handshake/context_assembler.py#L24-L65)) is the single read entry point over this
grain:

- `grain="message"` returns the raw `Event` rows for that `call_id`, ordered by
  `sequence`.
- `grain="blob"` reassembles them into the original `[{"role": ..., "content": ...}]`
  shape — the same structure that used to be the start event's `content` blob.
- `filter` narrows either grain before the grain transform is applied. A
  `{"provenance": tier}` filter runs `derive_provenance()` per row (so it matches
  both asserted and derived tiers); any other key is matched by plain attribute
  equality (e.g. `{"role": "user"}`).
- If no `event_type="message"` row matches the given `call_id`, `get_call()` falls
  back to treating the argument as a plain `event_id` and returns that single event
  ([handshake/context_assembler.py:47-51](../handshake/context_assembler.py#L47-L51)). This is what keeps
  `weight_by_provenance()` working against today's vector-search hits, which are
  still keyed to `llm_call`/`embedding` event ids — see [DECISIONS.md](../DECISIONS.md) for why this
  fallback exists and what closing it would require.
