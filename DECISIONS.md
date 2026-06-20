# Decisions

## 2026-06-19: Correctness pass — three journal-derived defects fixed

### Fix 1: LLMClient.call() dangling start event on error (core/llm_client.py)

`call()` journaled a `phase="started"` event before calling the provider but had
no try/except, so provider exceptions left an orphaned start event with no
`status="error"` end event. Fixed by wrapping the to_wire/_call_provider/from_wire
block in try/except, mirroring the ToolRunner.run() pattern (core/runner.py:159-211):
on exception, append a `status="error"`, `phase="failed"` end event with
`parent_id=start_event.event_id`, then re-raise.

### Fix 2: Rolling summary self-contamination (core/context.py)

`current_rolling_summary()` queried all `event_type="llm_call"` events filtered by
`role="assistant"`, `status="ok"`, and `content` — but did not exclude
`origin="system:summarizer"`. The summarizer's own prior outputs matched that
filter, so each call ingested its own prior output and contamination compounded.
Fixed with a Python post-filter (`event.origin != "system:summarizer"`) because
`store.query()` only supports equality matching; adding an inequality predicate
for one caller would violate the no-speculative-abstraction invariant.
`summarize_for_handoff()` in core/handshake.py also writes with
`origin="system:summarizer"` and is correctly excluded by the same filter.

### Fix 3: vectors table rebuild-from-journal (core/store.py, core/embedder.py)

`rebuild_vec_index()` rebuilt `vec_events` from `vectors`, but nothing rebuilt
`vectors` itself from the journal. Two upstream gaps made a naive rebuild unfaithful:
(a) the OKF enrichment path embeds a model-generated summary, not `events.content`;
(b) the embedding provider/model was not recorded.

**Case B fix (upstream journaling):** `upsert_vector()` now appends an
`event_type="embedding"` event via `_append_no_commit()` carrying the verbatim
embedded text in `content`, the embedding provider in `provider`, the model name
in `model`, and dimensions in `payload.dims`. The embedding event, vectors row,
and vec_events row are written in a single transaction (one `conn.commit()`).

`_append_no_commit()` was extracted from `append()` to deduplicate the events
INSERT SQL — two real callers (`append()` and `upsert_vector()`) justified the
extraction under the no-speculative-abstraction rule.

`Embedder` gained `provider_name` and `model_name` properties so callers can
record the provider/model as actually used at embed time.

`rebuild_vectors_from_journal()` scans `event_type="embedding"` events, deduplicates
to the latest per source event, groups by provider, instantiates one `Embedder`
per provider using the recorded string, re-embeds each event's verbatim `content`,
and repopulates `vectors`. Then calls `rebuild_vec_index()` if sqlite-vec is loaded.

**Backfill scope:** Forward-only. Existing vectors rows from prior runs have no
embedding event and remain unreconstructable. A one-time full re-enrich through the
updated OKF path would bring them into compliance.

## Closed residual: vec0 virtual table transaction compliance

**Status:** Runtime-verified (2026-06-20, sqlite-vec 0.1.9).

The single-transaction design in `upsert_vector()` (embedding event → vectors →
vec_events → one commit) protects against partial writes: if any step fails before
`commit()`, all uncommitted writes roll back.

Check 4 confirmed: a dimension mismatch (8 dims into a float[4] vec_events table)
raised `sqlite3.OperationalError` at the sqlite-vec INSERT (SQL level, not
`struct.pack`). The vectors row and embedding event — both already executed in the
same transaction — were rolled back. vec0 correctly participates in SQLite's
transaction rollback via `xRollback`. No partial state persisted.

## 2026-06-21: Caller-assertable provenance and schema change

### What changed

Provenance is now stored on the Event when explicitly asserted by the caller, via
a new `provenance: Optional[str] = None` field on the Event dataclass
(`core/models.py`) and a corresponding `provenance TEXT NULL` column on the events
table (`core/store.py`). When `provenance` is None (the default), `derive_provenance`
falls back to the existing origin+role derivation — `user_statement` or `model_claim`.
When set to `"user_confirmed"`, it is honored directly.

### Why

`PROVENANCE_WEIGHTS` defined three tiers (`user_confirmed: 1.0`, `user_statement: 0.8`,
`model_claim: 0.4`) but `derive_provenance` could only emit two — nothing produced
`user_confirmed`. llmj is headless; confirmation is a caller assertion (the calling
application knows a fact was confirmed, llmj cannot infer it), so it must be stored
on the event, not derived from origin+role.

### Design shift: stored when asserted, derived when absent

Provenance was previously always derived at read time from origin+role, never stored.
It is now stored when the caller asserts it, derived when absent. This supersedes
any prior statement that provenance is purely derived. The `provenance` column on the
events table is the durable record; `derive_provenance` (`core/provenance.py`) reads
it as the `asserted` parameter and honors it when present.

### Schema contract change

The events table gained a `provenance TEXT NULL` column. An `ALTER TABLE` migration
in `Store._create_schema()` adds the column to existing journals; new journals
include it in the `CREATE TABLE`. Row-immutability is unaffected (column addition,
not row mutation). Portability implication: readers of the events table must tolerate
the added column. Existing rows have `provenance = NULL`, meaning "derive as before."

### Invariant: model output never promoted

`derive_provenance` raises `ValueError` if `user_confirmed` is asserted on model
output (`role="assistant"` or `origin.startswith("system:")`). This is code-enforced
in `core/provenance.py`, not convention. Valid tiers are constrained by
`VALID_PROVENANCE_TIERS = {"user_confirmed", "user_statement", "model_claim"}`;
any other asserted value raises `ValueError`. No new tiers were added.
