# Detailed Design Document — Generated from Code

> Generated 2026-06-20 from the `restructure/layered` branch.
> Every claim cites file:line. Behavior described as-is, not as-intended.

---

## 1. Layers and dependency arrows

Two packages: `core/` and `handshake/`. No `projection/` layer exists.

### core/ imports

Core modules import only from within core and stdlib. No core module imports from handshake.

| Module | Imports from core |
|---|---|
| `core/store.py:11-12` | `core.models.Event`, `core.ulid.ulid` |
| `core/llm_client.py:10-14` | `core.config`, `core.models`, `core.provider_adapter`, `core.store`, `core.ulid` |
| `core/tool_runner.py:6-8` | `core.models`, `core.store`, `core.ulid` |
| `core/embedder.py` | No core imports (stdlib + `urllib` only) |
| `core/provider_adapter.py` | No core imports (stdlib only) |
| `core/models.py` | No core imports (stdlib only) |
| `core/provenance.py` | No core imports |
| `core/config.py` | No core imports (stdlib only) |
| `core/ulid.py` | No core imports (stdlib only) |

**One deferred import:** `core/store.py:231` — `rebuild_vectors_from_journal()` does `from core.embedder import Embedder` inside the function body to avoid a top-level dependency on Embedder from Store.

### handshake/ imports

All handshake modules import from core. No handshake module imports from another handshake module except `handshake/model_switch.py:5` which imports `handshake.context.assemble_context` (handshake-internal, legal).

| Module | Imports from core | Imports from handshake |
|---|---|---|
| `handshake/runner.py:6-8` | `core.models`, `core.store`, `core.ulid` | — |
| `handshake/context.py:5-7,20` | `core.embedder`, `core.llm_client`, `core.store`, `core.provenance` | — |
| `handshake/model_switch.py:5-8` | `core.embedder`, `core.llm_client`, `core.store` | `handshake.context` |
| `handshake/enrichment.py:9-12` | `core.provenance`, `core.embedder`, `core.llm_client`, `core.store` | — |

**Arrow:** handshake → core. Core → neither. Handshake modules do not cross-import except the one legal internal reference above.

### core/__init__.py exports (core/__init__.py:1-8)

```python
__all__ = ["Embedder", "LLMClient", "Store", "Event", "ToolRunner", "ulid"]
```

**Note:** `Store`, `LLMClient`, and `ToolRunner` are all publicly exported. The restructure spec calls for Store and LLMClient to become private behind a Journal API — this has not been done yet.

---

## 2. Schema

### events table (core/store.py:46-67)

```sql
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT NOT NULL PRIMARY KEY,
    event_type TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    parent_id  TEXT NULL,
    root_id    TEXT NOT NULL,
    origin     TEXT NOT NULL,
    role       TEXT NULL,
    tool_name  TEXT NULL,
    status     TEXT NULL,
    provider   TEXT NULL,
    model      TEXT NULL,
    params     JSON NULL,
    content    TEXT NULL,
    tokens_in  INTEGER NULL,
    tokens_out INTEGER NULL,
    cost       REAL NULL,
    latency_ms INTEGER NULL,
    scope      TEXT NOT NULL,
    payload    JSON NULL,
    created_at TEXT NOT NULL
)
```

20 columns. NOT NULL on: `event_id`, `event_type`, `chat_id`, `root_id`, `origin`, `scope`, `created_at`. All others nullable.

### Immutability triggers (core/store.py:70-86)

```sql
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is immutable: UPDATE not allowed');
END

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is immutable: DELETE not allowed');
END
```

These fire unconditionally — no UPDATE or DELETE can succeed on the events table.

### vectors table (core/store.py:90-94)

```sql
CREATE TABLE IF NOT EXISTS vectors (
    event_id  TEXT PRIMARY KEY REFERENCES events(event_id),
    text      TEXT NOT NULL,
    embedding BLOB NOT NULL
)
```

FK to events. No immutability triggers — `INSERT OR REPLACE` is used (`core/store.py:299`). Vectors is a derived table, rebuildable from embedding events in the journal.

### vec_events virtual table (core/store.py:187-191)

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS vec_events USING vec0(
    event_id TEXT PRIMARY KEY,
    embedding float[{dims}] distance_metric=cosine
)
```

Created dynamically with dimensions from first vector seen. Only exists if sqlite-vec is loaded (`core/store.py:32`, `_has_vec` flag). Rebuildable from vectors via `rebuild_vec_index()` (`core/store.py:208-223`).

### Indexes (core/store.py:97-112)

| Index | Columns |
|---|---|
| `ix_events_root_id_event_type` | `(root_id, event_type)` |
| `ix_events_chat_id_event_type` | `(chat_id, event_type)` |
| `ix_events_chat_id_origin` | `(chat_id, origin)` |
| `ix_events_event_type` | `(event_type)` |

### SQLite configuration (core/store.py:39-41)

```python
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
```

### Event domain object (core/models.py:8-80)

`@dataclass(frozen=True)` — immutable at the Python level. Fields mirror schema columns 1:1. `to_row()` serializes `params` and `payload` as JSON strings (`core/models.py:44,51`). `from_row()` deserializes them back (`core/models.py:57-58`).

---

## 3. Interfaces — actual signatures

### Store (core/store.py:25)

```python
class Store:
    def __init__(self, path: str) -> None                                    # :26
    def _append_no_commit(self, event: Event) -> None                        # :115
    def append(self, event: Event) -> None                                   # :144
    def get(self, event_id: str) -> Event                                    # :148
    def query(self, filter: dict[str, Any]) -> Iterable[Event]               # :158
    def upsert_vector(self, event_id: str, text: str,
                      embedding: list[float],
                      embedding_provider: str,
                      embedding_model: str) -> None                          # :260
    def search(self, query_embedding: list[float], top_k: int = 5,
               chat_id: str | None = None,
               scope: str | None = None) -> list[tuple[str, float]]          # :314
    def rebuild_vec_index(self) -> None                                       # :208
    def rebuild_vectors_from_journal(self) -> None                            # :225
```

**Notable:** `query()` only supports equality matching — iterates dict keys, generates `key = ?` per entry (`core/store.py:161-162`). No inequality, no OR, no LIKE. Callers that need exclusion (e.g., `origin != "system:summarizer"`) must post-filter in Python.

**Notable:** `store.conn` is a public attribute (`core/store.py:30`). Any code with a Store reference can execute arbitrary SQL. The immutability guarantee rests on the triggers, not on access control.

### LLMClient (core/llm_client.py:17)

```python
class LLMClient:
    def __init__(self, store: Store,
                 api_keys: dict[str, str] | None = None) -> None             # :18
    def call(self, chat_id: str,
             messages: list[dict[str, str]],
             model: str = "gpt-3.5-turbo",
             temperature: float = 0.0,
             provider: str = "openai",
             scope: str = "user",
             origin: str = "user",
             root_id: str | None = None) -> dict[str, Any]                   # :39
```

**Notable:** `self.store` is a public attribute (`core/llm_client.py:20`). Exposes Store to any code holding an LLMClient reference. `handshake/model_switch.py:43` exploits this: `llm_client.store.query(...)` — reads the journal through the LLMClient's store attribute rather than receiving Store as a separate parameter.

### ToolRunner (core/tool_runner.py:11)

```python
class ToolRunner:
    def __init__(self, store: Store) -> None                                 # :12
    def run(self, tool_name: str,
            tool_fn: Callable[..., Any],
            args: dict[str, Any],
            root_id: str,
            chat_id: str) -> Any                                             # :15
```

Tool functions are injected as callables — ToolRunner holds no registry and imports nothing tool-specific.

### Embedder (core/embedder.py:143)

```python
class Embedder:
    def __init__(self, provider: str | None = None) -> None                  # :144
    @property
    def provider_name(self) -> str                                           # :155
    @property
    def model_name(self) -> str                                              # :159
    def embed(self, text: str) -> list[float]                                # :163
    def dims(self) -> int                                                    # :166
```

Four embedding backends registered in `EMBEDDING_ADAPTER_REGISTRY` (`core/embedder.py:133-138`): `sentence_transformers` (384d), `ollama` (768d), `huggingface` (384d), `google` (3072d). Default provider from env `LLMJ_EMBEDDING_PROVIDER`, defaulting to `"google"` (`core/embedder.py:140`).

### ProviderAdapter (core/provider_adapter.py)

Three LLM adapter instances in `PROVIDER_ADAPTER_REGISTRY` (`core/provider_adapter.py:88-92`):

```python
PROVIDER_ADAPTER_REGISTRY = {
    "openai": OpenAIProviderAdapter(),
    "google": GoogleProviderAdapter(),
    "groq": GroqProviderAdapter(),
}
```

Each implements `to_wire(canonical_request) -> dict` and `from_wire(provider_resp) -> dict`. OpenAI and Groq adapters are pass-through; Google adapter translates between canonical messages format and Google's `contents`/`parts` format.

### Provenance (core/provenance.py:4)

```python
def derive_provenance(origin: str, role: str | None) -> str
```

Pure function. Rules:
- `origin == "user"` and `role == "assistant"` → `"model_claim"`
- `origin == "user"` (and role is not "assistant") → `"user_statement"`
- Everything else → `"model_claim"`

**Notable:** No code path ever returns `"user_confirmed"`. The tier is defined in `PROVENANCE_WEIGHTS` (`handshake/context.py:11`) with weight 1.0 but is never assigned by `derive_provenance` or any other code.

### Run lifecycle (handshake/runner.py)

```python
def start_run(chat_id: str, trigger: str, store: Store) -> str               # :14
def resume_run(root_id: str, store: Store) -> str                            # :42
def end_run(root_id: str, status: str, store: Store) -> None                 # :86
def get_run_status(root_id: str, store: Store,
                   crashed_threshold_seconds: int = 600) -> dict[str, Any]   # :122
def reconcile_crashed_runs(store: Store,
                           crashed_threshold_seconds: int = 600) -> list[str] # :207
```

`TERMINAL_STATUSES = {"run_completed", "run_failed", "run_aborted"}` (`handshake/runner.py:11`).

All lifecycle functions take `store: Store` directly — they do not go through a Journal API.

### Context assembly (handshake/context.py)

```python
PROVENANCE_WEIGHTS = {"user_confirmed": 1.0, "user_statement": 0.8, "model_claim": 0.4}  # :10
SUMMARY_CHAR_BUDGET = 2000                                                                 # :16
RECENCY_TIE_THRESHOLD = 0.05                                                               # :17

def weight_by_provenance(hits, store) -> list[tuple]                          # :23
def recency_tiebreak(hits) -> list[tuple]                                     # :40
def current_rolling_summary(chat_id, store, llm, ...) -> str                  # :61
def assemble_context(chat_id, query, store, embedder, llm, top_k=5) -> str   # :106
```

### OKF Enrichment (handshake/enrichment.py)

```python
class OKFEnricher:
    def __init__(self, store: Store, embedder: Embedder,
                 llm_client: LLMClient) -> None                              # :25
    def enrich(self, event_id: str,
               root_id: str | None = None) -> dict[str, Any]                 # :30

def enrich_run(root_id: str, store: Store,
               embedder: Embedder,
               llm_client: LLMClient) -> list[str]                           # :78
```

### Model-switch handshake (handshake/model_switch.py)

```python
def summarize_for_handoff(chat_id, messages, llm_client, ...) -> tuple[str, str]  # :17
def expand_query(user_query: str, handoff_summary: str) -> str                     # :50
def switch_model(chat_id, messages, user_first_query, store,
                 embedder, llm_client, ...) -> str                                 # :55
```

---

## 4. Enforced invariants

| # | Invariant | Enforcement | File:Line | Failure prevented |
|---|---|---|---|---|
| 1 | Events table is append-only (no UPDATE) | SQL trigger `events_no_update` | `core/store.py:72-77` | Mutation of recorded events |
| 2 | Events table is append-only (no DELETE) | SQL trigger `events_no_delete` | `core/store.py:80-86` | Deletion of recorded events |
| 3 | Per-event durable WAL commit | `self.conn.commit()` after every `append()` | `core/store.py:146` | Crash losing uncommitted events |
| 4 | LLM calls always produce an end event (success or error) | try/except in `LLMClient.call()` — success path at `:130`, error path at `:156` | `core/llm_client.py:84-157` | Dangling start events on provider failure |
| 5 | Tool calls always produce an end event (success or error) | try/except in `ToolRunner.run()` — success path at `:73`, error path at `:99` | `core/tool_runner.py:48-100` | Dangling start events on tool failure |
| 6 | Embedding event + vectors + vec_events commit atomically | `_append_no_commit` + vectors write + vec_events write + single `commit()` | `core/store.py:295-312` | Partial state: embedding event without vector, or vector without embedding event |
| 7 | Rolling summary excludes its own prior outputs | Post-filter `event.origin != "system:summarizer"` | `handshake/context.py:72` | Self-contamination: summary ingesting its own prior output |
| 8 | Raw JSON not embedded into vectors | Guard `text.startswith("[{") or text.startswith('{"role')` | `core/store.py:268-269` | Embedding serialized message arrays instead of content text |
| 9 | Terminal run status must be from allowed set | `status not in TERMINAL_STATUSES` check | `handshake/runner.py:87-88` | Invalid terminal status values |
| 10 | Resume rejected if run already terminated | Query for terminal event before resuming | `handshake/runner.py:53-58` | Resuming a completed/failed run |

---

## 5. Key algorithms as implemented

### Context assembly (handshake/context.py:106-138)

1. Embed query text via `embedder.embed(query)` (`:115`)
2. Search vectors for `top_k * 2` candidates scoped to `chat_id` (`:117`)
3. Weight each hit by provenance: look up event, derive provenance via `derive_provenance()`, multiply raw cosine score by weight from `PROVENANCE_WEIGHTS` (`:119`, `:23-37`)
4. Recency tiebreak: group hits within `RECENCY_TIE_THRESHOLD` (0.05) of each other by score, sort each group by `created_at` descending (`:120`, `:40-58`)
5. Take top `top_k` from ranked results (`:121`)
6. For each hit, fetch event content; if `model_claim` provenance, prefix with `[unverified prior model response]` (`:123-130`)
7. Generate rolling summary via `current_rolling_summary()` (`:132`)
8. Concatenate verbatim block (`[VERBATIM SOURCE — authoritative]`) and overview block (`[LOSSY OVERVIEW]`) (`:134-138`)

### Rolling summary (handshake/context.py:61-103)

1. Query all `llm_call` events for the `chat_id` (`:70`)
2. Post-filter: `role == "assistant"` AND `status == "ok"` AND `content` truthy AND `origin != "system:summarizer"` (`:71-73`)
3. Concatenate content with `\n---\n` separator (`:78`)
4. If combined length < `SUMMARY_CHAR_BUDGET` (2000 chars), return combined directly — no LLM call (`:80-81`)
5. Otherwise, call LLM with summarization prompt, `origin="system:summarizer"` (`:83-99`)

### Retrieval — python fallback (core/store.py:361-385)

When sqlite-vec is not loaded, search falls back to Python:
1. JOIN vectors with events, filter by `chat_id`/`scope` (`:368-377`)
2. Compute cosine similarity in Python for each candidate (`:382`)
3. Sort descending, take top_k (`:384-385`)

### Retrieval — vec0 path (core/store.py:325-359)

When sqlite-vec is loaded:
1. Pack query embedding as binary blob (`:332`)
2. Use vec0's `MATCH` operator with `k` parameter (`:338`)
3. If `chat_id` or `scope` specified, add subquery joining through vectors→events (`:340-357`)
4. Convert distance to similarity: `1.0 - distance` (`:359`)

### Enrichment (handshake/enrichment.py:30-75)

1. Look up source event by `event_id` (`:31`)
2. Derive provenance from event's origin/role (`:32`)
3. Call LLM with `ENRICHMENT_PROMPT` + `event.content`, origin=`"system:enrichment"` (`:34-44`)
4. Extract summary from response (`:46-49`)
5. Build bundle dict with metadata (`:51-63`)
6. Write bundle to filesystem as JSON (`:65-68`)
7. Embed summary text (not original content) and upsert vector (`:70-73`)

### Vector rebuild from journal (core/store.py:225-258)

1. Query all `event_type="embedding"` events (`:227`)
2. Deduplicate: keep latest embedding event per `parent_id` (source event) by `created_at` (`:233-237`)
3. Group by `provider` (`:239-241`)
4. For each provider group, instantiate `Embedder(provider=recorded_provider)` (`:244`)
5. Re-embed each event's `content` (the verbatim text), write to vectors (`:245-253`)
6. Single commit, then `rebuild_vec_index()` if sqlite-vec available (`:255-258`)

---

## 6. Provenance

### Definition (core/provenance.py:4-9)

`derive_provenance(origin, role)` maps to one of three tiers:
- `"user_statement"` — when `origin == "user"` and `role` is not `"assistant"`
- `"model_claim"` — everything else (including `origin == "user"` with `role == "assistant"`, and all system origins)

### Weights (handshake/context.py:10-14)

```python
PROVENANCE_WEIGHTS = {
    "user_confirmed": 1.0,
    "user_statement": 0.8,
    "model_claim": 0.4,
}
```

### Usage

- `derive_provenance` is called by `weight_by_provenance()` (`handshake/context.py:34`) and `OKFEnricher.enrich()` (`handshake/enrichment.py:32`)
- `PROVENANCE_WEIGHTS` is used in `weight_by_provenance()` (`handshake/context.py:35`) with a default fallback of 0.4 for unknown provenance strings

### user_confirmed

`"user_confirmed"` has weight 1.0 in `PROVENANCE_WEIGHTS` but **no code path ever assigns it**. `derive_provenance()` never returns it. No event in the codebase is created with provenance `"user_confirmed"`. The tier is defined but deliberately unimplemented — documented in `docs/DESIGN.md` as "reserved for a future promotion mechanism; deliberately unimplemented rather than inferred from model behavior."

---

## 7. Known inconsistencies / drift

### 7.1 — `LLMClient.store` is publicly accessible

`LLMClient` exposes `self.store` as a public attribute (`core/llm_client.py:20`). `handshake/model_switch.py:43` reads from the journal via `llm_client.store.query(...)` — bypassing any Store-access boundary. This works but means anyone with an LLMClient reference has unmediated journal access, undermining the choke-point design.

### 7.2 — `handshake/model_switch.py` imports `Any` but never uses it

`from typing import Any` at `handshake/model_switch.py:3` — `Any` appears nowhere else in the file. Dead import introduced or left behind during the move.

### 7.3 — Provenance weights live in handshake, provenance rules live in core

`derive_provenance()` is in `core/provenance.py`. `PROVENANCE_WEIGHTS` is in `handshake/context.py:10-14`. Both define aspects of the provenance hierarchy. If provenance is core domain logic, the weights arguably belong alongside the derivation function. Currently split across layers.

### 7.4 — `handshake/context.py` has a displaced import

`from core.provenance import derive_provenance` appears at line 20, separated from the other imports (lines 5-7) by the `PROVENANCE_WEIGHTS` constant definition. Functional but inconsistent with Python import conventions (all imports at top).

### 7.5 — Cost calculation is hardcoded for specific models

`LLMClient.call()` at `core/llm_client.py:94-98` contains hardcoded per-token pricing for `gpt-4` and `gpt-3.5-turbo` only. Google and Groq calls record `cost=None`. The pricing numbers themselves appear to be stale (GPT-4 at $0.03/$0.06 per 1K tokens predates current pricing).

### 7.6 — `_call_provider` duplicates routing that adapters should own

`LLMClient._call_provider()` (`core/llm_client.py:159-194`) contains a provider-specific `if/elif` chain for endpoint URLs, auth headers, and request construction. This duplicates the adapter boundary — `ProviderAdapter` handles wire-format translation but the HTTP transport (endpoint, auth, headers) is hardcoded in `_call_provider`. A new provider requires changes in both `provider_adapter.py` AND `_call_provider`.

### 7.7 — OKF bundle format is JSON; docs specify markdown+YAML frontmatter

`OKFEnricher.enrich()` writes bundles as JSON (`handshake/enrichment.py:66-68`). The internal DDD and HLD documents specify OKF bundles as "markdown + YAML frontmatter." This is a known, unsettled divergence flagged previously (see DECISIONS.md context — the format question was surfaced but not resolved).

### 7.8 — `enrich()` hardcodes provider and model

`OKFEnricher.enrich()` hardcodes `model="llama-3.3-70b-versatile"` and `provider="groq"` for the enrichment LLM call (`handshake/enrichment.py:40-41`). These are not parameterized or configurable — changing the enrichment model requires editing source code.

### 7.9 — Stale path references in DECISIONS.md

`DECISIONS.md` references old file paths: `core/runner.py:159-211` (line 10), `core/context.py` (line 14), `core/handshake.py` (line 23). All three files have moved. The decision rationale is still valid but the path citations are stale.

### 7.10 — Stale DDD section references in docstrings

`handshake/context.py:114` references `"DDD §5"` and `handshake/model_switch.py:67` references `"DDD S4"`. These refer to the old internal DDD document which is being replaced. The section numbers are opaque to anyone without the old doc.

None found that I'm unsure about — all items above are directly observable in the code.
