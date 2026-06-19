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

## Open residual: vec0 virtual table transaction compliance

**Status:** Unverified inference.

The single-transaction design in `upsert_vector()` (embedding event → vectors →
vec_events → one commit) protects against partial writes: if any step fails before
`commit()`, all uncommitted writes roll back. This was runtime-verified for the
standard SQLite path (Check 3b: vectors INSERT fails → embedding event rolled back).

The vec_events path uses sqlite-vec's vec0 virtual table. Virtual tables *can* have
non-standard transaction behavior if their `xBegin`/`xRollback` methods don't
implement the full protocol. Whether vec0 correctly participates in SQLite's
transaction rollback has **not been runtime-verified** — sqlite-vec is not installed
in the current environment.

**Trigger to close:** Run `python check_gaps.py` on a machine with sqlite-vec
installed. If Check 4 passes (dimension mismatch in vec_events INSERT rolls back
the vectors row and embedding event), item 4 is fully closed. Check 4 is armed and
will exercise the real vec0 path automatically when the extension is available.
