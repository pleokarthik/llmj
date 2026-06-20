# Design Overview

llmj is a journaling system for LLM interactions. Every model call, tool execution, and embedding operation is recorded as an immutable event in a single append-only SQLite journal. Everything else the system produces — vector indices, rolling summaries, knowledge bundles, run status projections — is a derived view, reconstructable from that journal.

The design problem is interesting because LLM-backed systems accumulate state across conversations (context, preferences, decisions) but typically do so in opaque, non-auditable ways. llmj treats this as an event-sourcing problem: capture every interaction with full provenance, then build read-side views on top. If any derived state is lost or corrupted, it can be rebuilt from the journal alone.

## Core invariants

**Immutable append-only journal.** Events are never updated or deleted. This is enforced at the database level with SQL triggers that abort any UPDATE or DELETE on the events table — not just a convention, a hard constraint the application cannot bypass. Every event is durably committed in WAL mode before control returns to the caller.

**No un-journaled LLM call or tool execution.** Every LLM call emits a start event before the request and an end event after the response (or on error). Every tool execution follows the same pattern. There are no "fire and forget" interactions — if the system talked to a model or ran a tool, the journal has the record.

**Derived views are rebuildable, not precious.** The vector search index, the OKF knowledge bundles, rolling summaries — all are projections of the journal. Losing any of them is an operational inconvenience, not data loss. The rebuild paths are tested: embedding events in the journal carry the exact text that was embedded and the model that produced the embedding, so vector reconstruction is faithful, not approximate.

**Schema is the contract.** The events table schema and the SQL triggers that protect it define the system's guarantees. There is no ORM, no migration framework, no abstraction layer between the application and SQLite. The schema is read directly by anyone who needs to understand what the system promises.

## Provenance as a first-class concept

Every event carries an `origin` field that records where it came from. A provenance hierarchy governs how much weight each origin receives in downstream decisions:

- **user_confirmed** — the user explicitly validated this information (highest trust). The hierarchy reserves this tier for a future promotion mechanism; it is deliberately unimplemented rather than inferred from model behavior.
- **user_statement** — the user said it, but hasn't been cross-checked
- **model_claim** — a model generated it (lowest trust)

The critical design rule: **model output is always `model_claim`, never auto-promoted.** When the system uses a model to summarize, enrich, or generate, the resulting event is tagged accordingly. The system never infers confirmation from silence or repetition — promotion to a higher tier will require an explicit user action, and until that mechanism exists, the tier boundary is enforced by omission.

This matters in retrieval: when assembling context for a new turn, model-sourced content is weighted at roughly half the score of user-sourced content and labeled as `[unverified prior model response]`. The system actively distinguishes what it was told from what it generated.

## Event correlation

Events form trees via two correlation fields: `parent_id` links an end event to its start event (e.g., an LLM response to its request), and `root_id` groups all events belonging to the same logical run or operation. This enables run-level projections — aggregating cost, latency, error rates, and status across an entire agent run from its constituent events.

## The derived-view model

Everything outside the events table is a derived view:

- **vectors / vec_events** — the embedding search index. Each embedding operation journals an `event_type="embedding"` event carrying the verbatim text and the embedding model identity. If the vectors table is lost, `rebuild_vectors_from_journal()` scans these events, re-embeds with the recorded model, and repopulates the index.
- **OKF bundles** — enriched knowledge extracted from journal events by an LLM. Derived and rebuildable by re-running the enrichment pipeline.
- **Rolling summary** — a compressed conversation overview, regenerated on demand from journal events. Excludes its own prior outputs to prevent self-contamination.
- **Run status** — a projection of agent run lifecycle events, computed on read, never stored as mutable state.

## Context assembly

Per-turn context assembly combines two sources at a fixed budget regardless of conversation length:

1. **Verbatim retrieval** — vector search over embedded events, weighted by provenance and broken by recency for close scores. Model-sourced hits are explicitly labeled as unverified.
2. **Lossy overview** — a rolling summary providing broad context that vector search might miss.

Context cost is O(1) in conversation length by construction — a 10,000-message thread produces the same context budget as a 100-message one.

## Verification philosophy

Defects in this system were surfaced through multi-agent adversarial verification: independent agents (Claude, Copilot, Codex) audited the codebase and cross-verified each other's findings. Each confirmed defect was proven by a behavioral check that fails on the unfixed code and passes after the fix — not "the tests pass," but "here is a specific scenario that demonstrates the broken invariant, and here is that same scenario succeeding after the fix."

This discipline extends to the checks themselves. When the vector rebuild path was first proposed, a naive check (does search return results after rebuild?) would have passed while hiding a faithfulness bug — the rebuild could re-embed the wrong text and still return search results. The actual check constructs a scenario where the embedded text intentionally differs from the source event's content, then asserts the rebuild recovers the embedded text, not the source content. A wrong rebuild fails the check.

The checks also verify transaction semantics: that a failure partway through a multi-write operation (journaling an embedding event, writing the vector, updating the search index) rolls back all writes atomically, leaving no partial state.

## Technology choices

**stdlib + SQLite only.** No external HTTP library, no ORM, no framework. HTTP calls use `urllib.request`; persistence is raw SQLite with WAL journaling. The dependency surface is minimal by design — the system's correctness properties are visible in the schema and the SQL, not hidden behind abstractions.

**No speculative abstraction.** There are no factories, no abstract base classes, no interfaces with a single implementation. Abstraction is introduced only when a second real implementation exists (the provider adapter registry being the example — it was earned by having multiple LLM providers from the start). Three similar lines are preferred to a premature generalization.
