# Implementation Prompt — Cross-Provider LLM Journal

> Attach this file alongside HLD-1.md and DDD-3.md at the start of every session.
> Do not summarise or paraphrase these instructions. Follow them exactly.

---

## What you are building

A local-first, headless Python library that captures every cross-provider LLM event
(calls, tool executions, agent runs) into one immutable append-only journal, extracts
curated knowledge into OKF bundles, and assembles bounded query-conditioned context
per turn. The full design is in HLD-1.md (architecture) and DDD-3.md (schema,
interfaces, algorithms). Those two documents are the specification. This prompt is
the operating contract for how you implement them.

---

## Platform

- OS: Windows
- Editor: VS Code
- Language: Python 3.11+
- Database: SQLite (WAL mode); vector store: sqlite-vec (default), Qdrant (optional)
- All imports are root-relative: `from core.models import ...` — never relative imports

---

## Hard constraints — these override all your defaults

### 1. Events are immutable. No exceptions.

The `events` table is append-only. You MUST NOT generate:
- `update_event()`, `delete_event()`, or any UPDATE/DELETE SQL on the events table
- Any ORM method that mutates an existing row
- Any "edit" or "correct" pattern that modifies a written event

Corrections are new events. Supersession is a pointer field on a new row, never a
mutation of the old one. Status is a projection (a separate view), never a column
update on events.

### 2. No factory pattern until ≥2 concrete implementations exist.

The only exception is `ProviderAdapter`, which earns a registry immediately because
three providers (OpenAI, Anthropic, Google) are in scope from the start.

For everything else — `Store`, `Embedder`, `ToolRunner`, `OKFEnricher` — write one
concrete class. No abstract base class. No Protocol. No `create_*` factory function.
No dispatch dict. If you find yourself writing `if provider == "openai": return
OpenAIStore()` for anything other than ProviderAdapter, stop and delete it.

### 3. No speculative abstraction.

Build only what the current step requires. Do not add:
- Methods not called by the current step
- Config objects "for future flexibility"
- Plugin hooks, middleware chains, or event buses not in the DDD
- Async where the step does not call for it (Windows async has overhead; default sync)

### 4. Schema is the contract. Implement it exactly.

The `events` table schema in DDD §1.1 is the durable, language-neutral contract.
Implement every column exactly as specified — name, type, nullability. Do not add
columns. Do not rename columns. Do not reorder them. Any deviation breaks portability.

The typed axes (`event_type`, `origin`, `tool_name`, `status`) are fixed at step 1
and cannot change later. Get them right now.

### 5. No un-journaled LLM call or tool execution.

Every call to a provider must go through the journaled `LLMClient`. Every tool
execution must go through the journaled `ToolRunner`. If you write code that calls
an LLM provider directly (bypassing `LLMClient`), it is a bug, not a feature.
This includes the OKF enrichment LLM call — it is journaled with
`origin=system:enrichment`.

### 6. SQLite WAL mode. Durable per-event writes.

Every event must be committed to the WAL the instant it completes. Do not batch
writes. Do not defer commits. A crash must preserve every completed step — this is
the capture-till-failure guarantee.

### 7. One concrete implementation per interface. No dispatch machinery.

Interfaces are defined in DDD §2. Implement each as a single class with no
inheritance hierarchy, no abstract methods, no Protocol. The fork boundary is the
interface signature — not a class hierarchy.

### 8. Provenance is load-bearing. Never drop it.

Every piece of content in the retrieval corpus carries provenance:
`user_statement | model_claim | user_confirmed`.
Provenance weighting in retrieval is: `user_confirmed > user_statement > model_claim`.
Model output is always `model_claim` and is never injected as ground truth.
OKF concept docs inherit the strongest provenance from their `source_event_ids`.

---

## Interfaces (from DDD §2) — implement exactly these signatures

```python
class Store:
    def append(self, event: Event) -> None: ...          # immutable, durable
    def get(self, event_id: str) -> Event: ...
    def query(self, filter: dict) -> Iterable[Event]: ...
    def upsert_vector(self, event_id: str, text: str, embedding: list[float],
                      embedding_provider: str, embedding_model: str) -> None: ...
    def search(self, query_embedding: list[float], top_k: int = 5,
               chat_id: str | None = None, scope: str | None = None) -> list[tuple[str, float]]: ...

class Embedder:
    def embed(self, text: str) -> list[float]: ...

class ProviderAdapter:   # registry earned; one per provider
    def to_wire(self, canonical_request: dict) -> dict: ...
    def from_wire(self, provider_resp: dict) -> dict: ...

class ToolRunner:
    def run(self, tool_name: str, args: dict) -> Any: ...  # emits begin/end events

class OKFEnricher:
    def enrich(self, events: list[Event]) -> list[OKFDoc]: ...  # LLM call; caller journals
    def write_bundle(self, docs: list[OKFDoc], bundle_path: str) -> None: ...
```

Do not add methods. Do not change signatures. Do not subclass.

---

## Build order — implement exactly one step per session

Work through these in sequence. Do not start step N+1 until step N is verified
end-to-end with a real observable output (not a unit test stub).

**Step 1 — Journal schema + durable writes + LLMClient (one provider)**
- Create the `events` table exactly per DDD §1.1. All columns, correct types.
- Enable WAL mode on the SQLite connection.
- Implement `Store.append()` with per-event WAL commit.
- Implement `LLMClient` wrapping one provider; every call journals a before and after event.
- Verify: make a real LLM call; confirm the event row is in the DB with correct fields.
- Do NOT implement: retrieval, embeddings, ProviderAdapter registry, anything from step 2+.

**Step 2 — Single ProviderAdapter + end-to-end recorded turn**
- Implement `ProviderAdapter` for one provider: `to_wire` and `from_wire` only.
- Wire it into `LLMClient`.
- Implement the provider registry (dict keyed by provider string) — this is the only
  registry earned at step 1.
- Verify: full turn recorded with correct provider/model fields in the event row.

**Step 3 — Per-turn retrieval**
- Implement `Embedder` (one concrete impl — Ollama/nomic-embed-text, 768 dims).
- Implement `Store.upsert_vector()` and `Store.search()`.
- Implement provenance weighting and recency tiebreak (DDD §5 pseudocode exactly).
- Implement rolling summary (regenerated from journal, never appended).
- Implement `assemble_context()` as specified in DDD §5.
- Retrieval corpus source at this step: raw event chunks (OKF docs added at step 7).
- Verify: query returns provenance-weighted hits; context budget is O(1) in length.

**Step 4 — Second ProviderAdapter + model-switch handshake**
- Implement a second ProviderAdapter; registry now has two entries.
- Implement the switch handshake exactly per DDD §4:
  step 1 (summarize with A, journal as model_claim),
  step 2 (B's first turn: retrieval seeded by query + summary),
  step 3 (subsequent turns: standard per-turn assembly).
- Verify: switch A→B produces the correct journaled summarizer event and correct
  context injection on B's first turn.

**Step 5 — ToolRunner + agent_run lifecycle + run projection + crash handling**
- Implement `ToolRunner.run()`: emits begin event before execution, end event after.
- Implement agent_run lifecycle per DDD §1.2: start, step, terminal, crash-by-absence.
- Implement run projection per DDD §1.4: fold lifecycle events; detect crash by absence
  of terminal event + dead process on reconciliation pass.
- The projection is a rebuildable view — it is never a mutation of the journal.
- Verify: simulate a crash mid-run; confirm projection shows `crashed` after
  reconciliation; confirm journal shows "started, no terminal" untouched.

**Step 6 — Views: economics + read-side surfaces**
- Implement economics: true per-turn/per-run cost via root_id aggregation.
- Implement OTel GenAI export: events → gen_ai.* spans per DDD §7.
- Implement SQL surface: journal is already SQLite; expose a query helper.
- Implement MCP read server: wraps Store.query() for agent/IDE consumption.
- Implement replay/diff/fork: fold log to reconstruct point-in-time state.
- Verify: economics view produces correct per-run token/cost totals via root_id.

**Step 7 — OKF enrichment pipeline**
- Implement `OKFEnricher.enrich()`: takes journal events, calls LLM (journaled with
  `origin=system:enrichment`), returns OKFDoc list.
- OKFDoc YAML frontmatter fields per DDD §1.6:
  standard: type (required), title, description, resource, tags, timestamp
  extended: provenance, source_event_ids, superseded_by
- Provenance inheritance rule: strongest provenance in source_event_ids set wins.
- Implement `OKFEnricher.write_bundle()`: writes markdown + YAML files to bundle dir.
- Embed OKF doc bodies into retrieval corpus (Store.upsert_vector with okf_doc_id set).
- OKF docs are now the PRIMARY retrieval source; raw event chunks are secondary.
- Verify: external OKF-compliant reader can consume the bundle directory without
  any knowledge of the journal schema or SQLite internals.

---

## What you will be tempted to generate — do not

- `update_event()` or `DELETE FROM events` — forbidden. See constraint 1.
- `class AbstractStore(ABC)` — forbidden. See constraint 2.
- `async def append()` for the journal write — sync. WAL handles concurrency.
- `EventFactory.create(type=...)` — forbidden. See constraint 2.
- Additional columns on the events table "for future use" — forbidden. See constraint 4.
- Calling `openai.ChatCompletion.create()` directly outside `LLMClient` — forbidden.
  See constraint 5.
- A `rolling_summary` that appends to itself (summary-of-summary) — forbidden.
  It must be regenerated from the journal each time.
- An OKF enrichment call that is NOT journaled — forbidden. See constraint 5.

---

## Per-step verification checklist

Before declaring a step done:
1. Is every LLM call going through `LLMClient`? (grep for direct provider calls)
2. Is every event row written exactly once, never updated?
3. Are imports root-relative? (`from core.` not `from .`)
4. Does the step produce a real observable output (not just passing tests)?
5. Is the next step's interface NOT yet implemented?

If any check fails, fix it before moving on.
