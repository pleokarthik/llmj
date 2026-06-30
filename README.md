# llmj

Local-first headless Python LLM journaling engine.

Every LLM call, tool execution, and context assembly operation is recorded as an immutable event in a SQLite journal. Provenance is tracked from ingestion to retrieval. Nothing is inferred; everything is stored.

---

## Architecture

Two physical layers with strict dependency direction, plus one functional layer
("projection") realized as specific functions inside the two below rather than a
separate package:

```
handshake/      — orchestration (imports core, never vice versa)
core/           — domain: storage, provenance, LLM client, tool execution
```

Full detail, including the event-sourcing framing (journal = event log, derive =
projection, active state = read model) and the message-grain write path, lives in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — this section is a quick map, not the source of truth.

### Core layer

| Module | Responsibility |
|---|---|
| `journal_store.py` | SQLite journal in WAL mode. Immutable `events` table (no UPDATE/DELETE via triggers). Dual-write to `vectors`/`vec_events` via sqlite-vec. |
| `event_model.py` | Typed `Event` dataclass. Schema is the contract. |
| `provenance.py` | Provenance tier derivation: `user_confirmed > user_statement > model_claim`. Tier is caller-asserted and validated — model output cannot self-promote. |
| `llm_client.py` | Multi-provider HTTP client: OpenAI, Google Gemini, Groq. Journals one `message` event per item in the outgoing message list, plus a start/completion `llm_call` event pair. No un-journaled LLM calls. |
| `tool_runner.py` | Tool execution lifecycle. Journals tool invocation and result as discrete events. |
| `vector_embedder.py` | Embedding client with pluggable adapters: sentence-transformers, Ollama, HuggingFace, Google. Vectors are written to `journal_store` via `upsert_vector()`. |
| `provider_adapter.py` | Uniform adapter abstraction over provider-specific wire formats. |
| `id_generator.py` | Pure-Python ULID generation (Crockford base32). |
| `config.py` | Environment and path configuration. Loads `.env` automatically. |

### Handshake layer

| Module | Responsibility |
|---|---|
| `session_runner.py` | Run lifecycle: `start_run` / `resume_run` / `end_run`, plus `get_run_status` and `reconcile_crashed_runs` for crash detection. |
| `context_assembler.py` | `get_call()` (message/blob projection over a call), `weight_by_provenance()` (retrieval scoring: `user_confirmed=1.0`, `user_statement=0.8`, `model_claim=0.4`), and `assemble_context()` for per-turn context. |
| `okf_enrichment.py` | LLM-driven summarization of assistant responses into OKF bundles. Writes bundles to `okf/` and upserts vectors. |
| `provider_switch.py` | Context-aware provider switching within a session. |

---

## Key design decisions

**Immutable journal.** The `events` table has SQL triggers blocking UPDATE and DELETE. The journal is append-only by construction, not convention.

**Provenance as load-bearing.** Every event resolves to a provenance tier — asserted and validated at write time when the caller knows confirmation occurred, otherwise derived from origin+role at read time. `user_confirmed` requires explicit caller assertion and is blocked on system or assistant origins. Context assembly weights retrieval by tier — provenance is not metadata, it affects recall.

**No un-journaled calls.** `LLMClient` journals a start event before the API call and a completion event after. If the client throws, the start event remains as a dangling marker — visible, not silent.

**sqlite-vec dual-write.** `journal_store.py` attempts to load `sqlite_vec` at runtime. `upsert_vector()` writes an `embedding` event, a `vectors` row, and (if sqlite-vec loaded) a `vec_events` row in one transaction. `rebuild_vectors_from_journal()` reconstructs `vectors`/`vec_events` from the immutable events — the journal is the source of truth, not the index.

**Schema migration inline.** No Alembic. Each schema change (`provenance`, `call_id`, `sequence`) is an `ALTER TABLE ... ADD COLUMN` baked into `journal_store.py` initialization, wrapped in `try/except` so it's a no-op against a journal that already has the column. See [docs/DATABASE_DESIGN.md](docs/DATABASE_DESIGN.md) for the full migration history.

---

## Providers

`LLMClient.call()` takes `provider` as a direct argument (`"openai"` | `"google"` | `"groq"`,
default `"openai"`) — there's no env-var provider switch inside `LLMClient` itself.
The example scripts read `LLMJ_PROVIDER` as their own convenience default:

```env
LLMJ_PROVIDER=openai       # default
LLMJ_PROVIDER=google
LLMJ_PROVIDER=groq
```

API keys:

```env
OPENAI_API_KEY=...
GOOGLE_API_KEY=...
GROQ_API_KEY=...
```

Place in `.env` at project root (see `.env-example`). Loaded automatically on `LLMClient` init.

---

## Run

```bash
# Install
pip install -e .

# End-to-end LLM call with journaling
python examples/demo_llm_call.py

# Full run lifecycle (session open → LLM call → tool execution → close)
python examples/demo_run_lifecycle.py

# OKF enrichment on a completed run
python examples/demo_okf_enrichment.py

# Provider switching within a session
python examples/demo_provider_switch.py

# Retrieval with provenance-weighted scoring
python examples/demo_retrieval.py
```

---

## Project structure

```
core/               # Domain layer
handshake/          # Orchestration layer
examples/           # Runnable demos
docs/
  ARCHITECTURE.md       # Layers, event-sourcing framing, provenance system, write path
  DATABASE_DESIGN.md    # events table schema, migrations, sqlite-vec dual-write
  DIAGRAMS.md           # Write path / get_call() / provenance derivation diagrams
DECISIONS.md        # Architectural decision log, dated entries
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full architecture and [DECISIONS.md](DECISIONS.md) for the
rationale behind it, including options considered and rejected.

---

## Status

Active development on `restructure/layered`. The journal, provenance system
(including caller-asserted `user_confirmed` at message grain), and vector dual-write
are committed and functional. Known debt: vector search/retrieval is still keyed to
whole `llm_call`/`embedding` events, not individual `message` rows — a confirmed
statement is durably tagged but not yet individually retrievable. See
[DECISIONS.md](DECISIONS.md)'s 2026-06-30 entry.
