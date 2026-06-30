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

## 2026-06-30: Message-grain provenance write path

### The problem

The 2026-06-21 change above made `provenance` storable and caller-assertable in
principle, but no real write path ever exercised it. `LLMClient.call()` journaled
exactly one event per invocation — a single `event_type="llm_call"` start event whose
`content` was `json.dumps(messages)`, the entire conversation history serialized as
one blob. A `provenance` assertion is a claim about one statement ("the user
confirmed X"); there was no row at single-statement granularity to attach that claim
to. Asserting `user_confirmed` on the whole blob would also be wrong whenever the
blob's history contained a prior assistant turn — `derive_provenance` would (correctly)
reject it, but there was no way to assert it on *just* the new turn either, because
the new turn didn't have its own row.

### Options considered

1. **Group-store-then-parse.** Keep the single blob event; recover per-message
   structure on demand by re-parsing `json.dumps(messages)` content at read time.
   Rejected: the events table is immutable, so a provenance assertion still has
   nowhere durable to live without a second, correlated event — this option doesn't
   avoid that problem, it just defers it while adding a JSON-parsing step to every
   read.
2. **Span-referencing confirmation events.** Leave the write path alone; add a new
   event type that marks a confirmation by referencing an offset/span inside an
   existing blob event's `content`. Rejected: span references into immutable text
   are fragile (any future change to serialization — key order, whitespace, encoding
   — silently invalidates existing spans), and resolving a span back to "which
   logical message is this" still requires a parser. More moving parts than writing
   messages as their own rows in the first place.
3. **Fresh standalone confirmation events.** Emit an independent
   `event_type="confirmation"` event per asserted statement, decoupled from how the
   underlying message was journaled. Rejected: confirmation would exist in the
   journal but retrieval (`weight_by_provenance`) still operates over whatever
   granularity the original message was journaled at (the blob). A standalone
   confirmation event doesn't change that grain, so recall still can't act on the
   per-statement assertion without yet another join back to the blob it's about.
4. **Granular write, flexible read (chosen).** Journal one event per message at
   write time (`call_id` + `sequence`), so a provenance assertion has a real,
   single-statement row to live on with no follow-up event required. Add one
   projection function, `get_call()`, as the sole read entry point, so anything that
   needs the original call-shaped view back can reconstitute it on demand
   (`grain="blob"`) without every existing caller having to know the journal changed
   shape underneath it.

Option 4 won because it's the only one that makes provenance assertion a normal
column write instead of a correlation problem, and it pays for that with one new read
function (`get_call`) rather than a parser, a span-resolution step, or a join.

### What changed

- `events` gained two columns, `call_id TEXT NULL` and `sequence INTEGER NULL`
  (`core/journal_store.py`, `Event` dataclass in `core/event_model.py`) — see
  [DATABASE_DESIGN.md](docs/DATABASE_DESIGN.md) for the full migration.
- `LLMClient.call()` mints one `call_id` per invocation and writes one
  `event_type="message"` row per item in `messages`, `sequence` = list index, role
  taken from the message dict itself. The existing `llm_call` start/end events are
  unchanged except the start event's `content` is now `None` instead of the
  serialized blob.
- `LLMClient.call()` gained a `provenance` parameter, validated via
  `derive_provenance()` and stored on the per-message row before any event is
  appended (fail-fast on an invalid assertion).
- `get_call(call_id, store, grain, filter)` was added to `handshake/context_assembler.py`
  as the projection over this grain. `weight_by_provenance()` was updated to call it
  explicitly at `grain="message"` instead of `store.get(event_id)` directly.

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) and [DIAGRAMS.md](docs/DIAGRAMS.md) for the full shape of
the write/read paths.

### Judgment call 1: provenance applies to the last message only

`call()` takes one scalar `provenance` argument but `messages` is a full history —
applying the assertion to every row would raise on the first prior assistant turn in
that history, since `derive_provenance` correctly blocks `user_confirmed` on
`role="assistant"`. The assertion is applied only to the *last* message in the array
— the new turn being submitted this call, which is the only statement the caller could
plausibly be confirming "right now." Every earlier message in the array gets
`provenance=None` (derived as before, unaffected by this change). This wasn't fully
specified going in; it's the interpretation that makes a single scalar argument
coherent against `derive_provenance`'s existing invariant rather than fighting it.

### Judgment call 2: `get_call()`'s legacy-id fallback is known debt

Vector-search hits from `Store.search()` are still keyed to `llm_call`/`embedding`
event ids, not `call_id`s — nothing yet embeds individual `message` rows;
`OKFEnricher.enrich_run()` still only embeds assistant `llm_call` responses
(`handshake/okf_enrichment.py`, unchanged by this work). Without a fallback,
`get_call(event_id, grain="message")` called from `weight_by_provenance()` on one of
these ids would match zero `message` rows and return an empty list — silently zeroing
out all retrieval. `get_call()` falls back to `store.get(call_id)` (treating the
argument as a plain `event_id`) whenever no `message` rows match, which is what keeps
existing retrieval and `check_5_user_confirmed_provenance` passing unmodified.

This is deliberate, not accidental, but it is debt: it means `message`-grain rows are
not yet part of the retrieval corpus at all. A user statement can now be marked
`user_confirmed`, but nothing makes that statement individually retrievable or
individually weighted in `assemble_context()` — only the full assistant-response
blob that OKF enrichment summarizes is. Closing this requires embedding `message`
rows directly (or running them through enrichment), which is out of scope for this
change and not yet scheduled.
