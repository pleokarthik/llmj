# Database Design

Grounded in `core/journal_store.py` as it exists today. One SQLite file, opened by
`Store.__init__` ([core/journal_store.py:26-37](../core/journal_store.py#L26-L37)), holds everything: the journal, the
similarity index, and (if `sqlite-vec` is loadable) its virtual table.

## Connection configuration

`Store._configure()` ([core/journal_store.py:39-41](../core/journal_store.py#L39-L41)) sets, on every connection:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
```

WAL mode is what allows `_call_provider()`'s blocking HTTP round-trip inside
`LLMClient.call()` to coexist with concurrent readers of the same journal file
without a full-database lock. `foreign_keys=ON` is what makes the `vectors.event_id
REFERENCES events(event_id)` constraint (below) actually enforced.

## `events` table

The full current schema ([core/journal_store.py:44-71](../core/journal_store.py#L44-L71)):

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `event_id` | TEXT | NOT NULL, PRIMARY KEY | ULID, [core/id_generator.py:7](../core/id_generator.py#L7) |
| `event_type` | TEXT | NOT NULL | `agent_run` \| `llm_call` \| `message` \| `tool_call` \| `embedding` |
| `chat_id` | TEXT | NOT NULL | |
| `parent_id` | TEXT | NULL | Set on an end event, pointing at its start event's `event_id` |
| `root_id` | TEXT | NOT NULL | Groups all events in one run/operation |
| `origin` | TEXT | NOT NULL | `"user"` \| `"system:<name>"` |
| `role` | TEXT | NULL | `"user"` \| `"assistant"` \| `"system"` (message-level) |
| `tool_name` | TEXT | NULL | `tool_call` events only |
| `status` | TEXT | NULL | `"ok"` \| `"error"` on end events |
| `provider` | TEXT | NULL | e.g. `"openai"`, `"groq"`, `"google"` |
| `model` | TEXT | NULL | |
| `params` | JSON | NULL | Canonical request, stored as `json.dumps(...)` text |
| `content` | TEXT | NULL | The event's payload text — a single message's content at message grain |
| `tokens_in` | INTEGER | NULL | |
| `tokens_out` | INTEGER | NULL | |
| `cost` | REAL | NULL | |
| `latency_ms` | INTEGER | NULL | |
| `scope` | TEXT | NOT NULL | |
| `payload` | JSON | NULL | Free-form per-event-type metadata, stored as `json.dumps(...)` text |
| `created_at` | TEXT | NOT NULL | UTC, `%Y-%m-%dT%H:%M:%SZ` |
| `provenance` | TEXT | NULL | Caller-asserted tier; `NULL` means "derive from origin+role" |
| `call_id` | TEXT | NULL | Groups one `LLMClient.call()` invocation's `message` rows |
| `sequence` | INTEGER | NULL | Position within `call_id`, 0-indexed by list order in `messages` |

Two triggers make `events` append-only at the database level, not just by convention
([core/journal_store.py:73-90](../core/journal_store.py#L73-L90)):

```sql
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events table is immutable: UPDATE not allowed'); END

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events table is immutable: DELETE not allowed'); END
```

Any `UPDATE`/`DELETE` against `events` — from any code path, present or future —
aborts at the SQLite level.

### Indices

Five, all on `events` ([core/journal_store.py:112-131](../core/journal_store.py#L112-L131)):

| Index | Columns | Serves |
|---|---|---|
| `ix_events_root_id_event_type` | `(root_id, event_type)` | Run-scoped queries (`get_run_status`, `reconcile_crashed_runs`) |
| `ix_events_chat_id_event_type` | `(chat_id, event_type)` | Chat-scoped queries (`current_rolling_summary`) |
| `ix_events_chat_id_origin` | `(chat_id, origin)` | Origin-filtered queries (`summarize_for_handoff`) |
| `ix_events_event_type` | `(event_type)` | Type-only scans (`rebuild_vectors_from_journal`) |
| `ix_events_call_id` | `(call_id)` | `get_call()`'s per-call message lookup |

## `vectors` table

```sql
CREATE TABLE IF NOT EXISTS vectors (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    text     TEXT NOT NULL,
    embedding BLOB NOT NULL
)
```
([core/journal_store.py:103-111](../core/journal_store.py#L103-L111))

`text` is the verbatim string that was embedded — not necessarily `events.content`
for that `event_id` (OKF enrichment embeds a model-generated summary, not the source
event's own text; [handshake/okf_enrichment.py:70-73](../handshake/okf_enrichment.py#L70-L73)). `upsert_vector()` guards
against accidentally embedding raw serialized JSON
(`text.startswith("[{")` or `'{"role'`, [core/journal_store.py:290-291](../core/journal_store.py#L290-L291)) — a leftover
defense from when the journaled blob the embedder might be pointed at could itself
be a serialized message array.

## `vec_events` virtual table (sqlite-vec)

Loaded conditionally — `_try_load_sqlite_vec()` ([core/journal_store.py:15-22](../core/journal_store.py#L15-L22)) attempts
to load the `sqlite_vec` extension and sets `Store._has_vec`. When available,
`_ensure_vec_table(dims)` ([core/journal_store.py:208-214](../core/journal_store.py#L208-L214)) lazily creates:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS vec_events USING vec0(
    event_id TEXT PRIMARY KEY,
    embedding float[{dims}] distance_metric=cosine
)
```

`{dims}` is fixed at first use (from the first row's embedding length) and cached in
`Store._vec_dims`. If `sqlite-vec` isn't loadable, `Store.search()` falls back to a
pure-Python cosine similarity scan over `vectors` (`_search_python`,
[core/journal_store.py:383-407](../core/journal_store.py#L383-L407)) instead of the vec0 index path
(`_search_vec`, [core/journal_store.py:347-381](../core/journal_store.py#L347-L381)).

## Dual-write: `upsert_vector()`

`upsert_vector()` ([core/journal_store.py:282-334](../core/journal_store.py#L282-L334)) writes three things in one transaction,
one `conn.commit()` at the end:

1. An `event_type="embedding"` journal event via `_append_no_commit()` (no
   intermediate commit), carrying the exact embedded text, embedding provider,
   model, and dimension count.
2. A row in `vectors`.
3. A row in `vec_events`, if `sqlite-vec` is loaded.

Single-transaction means a failure at any step (e.g. a `vec_events` dimension
mismatch) rolls back all three together — verified at runtime (sqlite-vec 0.1.9,
2026-06-20) and documented in [DECISIONS.md](../DECISIONS.md)'s "Closed residual: vec0 virtual table
transaction compliance" entry.

## Rebuildability

`vectors` and `vec_events` are derived state, not source of truth — both are
reconstructable from the journal alone:

- `rebuild_vectors_from_journal()` ([core/journal_store.py:247-280](../core/journal_store.py#L247-L280)) replays every
  `event_type="embedding"` event (deduplicated to the latest per source event,
  grouped by recorded provider), re-embeds each event's verbatim `content`, and
  repopulates `vectors`.
- `rebuild_vec_index()` ([core/journal_store.py:230-245](../core/journal_store.py#L230-L245)) drops and repopulates
  `vec_events` from whatever is currently in `vectors`.

## Migration history

Schema changes are applied via an `ALTER TABLE ... ADD COLUMN` pattern inside
`Store._create_schema()`, wrapped in `try/except sqlite3.OperationalError: pass`
for idempotency — a brand-new journal already has the column from `CREATE TABLE`
(so the `ALTER TABLE` raises `OperationalError: duplicate column name` and is
silently skipped), while an existing journal file from before the column existed
gets it added in place:

| Date | Column | Type | Code |
|---|---|---|---|
| 2026-06-21 | `provenance` | `TEXT NULL` | [core/journal_store.py:91-94](../core/journal_store.py#L91-L94) |
| 2026-06-30 | `call_id` | `TEXT NULL` | [core/journal_store.py:95-98](../core/journal_store.py#L95-L98) |
| 2026-06-30 | `sequence` | `INTEGER NULL` | [core/journal_store.py:99-102](../core/journal_store.py#L99-L102) |

All three are also declared directly in the `CREATE TABLE IF NOT EXISTS` statement
([core/journal_store.py:67-69](../core/journal_store.py#L67-L69)) — the `ALTER TABLE` lines exist purely to bring
journals created before each respective change up to date; they're redundant for any
journal created after this code shipped. Each new column is nullable with no default
constraint other than `NULL`, which is what makes the migration safe against the
table's own append-only triggers — adding a column is schema evolution, not a row
mutation, so `events_no_update`/`events_no_delete` are untouched by it.

No ORM, no Alembic. The schema is the `CREATE TABLE` statement plus this list of
`ALTER TABLE` lines, in file order, in `_create_schema()`.
