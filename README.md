# llmj

Local-first headless Python LLM journaling engine.

Every LLM call, tool execution, and context assembly operation is recorded as an immutable event in a SQLite journal. Provenance is tracked from ingestion to retrieval. Nothing is inferred; everything is stored.

---

## Architecture

Three-layer separation with strict dependency direction:

```
handshake/      — orchestration (imports core, never vice versa)
core/           — domain: storage, provenance, LLM client, tool execution
```

The `projection/` layer is reserved for read-model derivations and not yet populated.

### Core layer

| Module | Responsibility |
|---|---|
| `journal_store.py` | SQLite journal in WAL mode. Immutable `events` table (no UPDATE/DELETE via triggers). Dual-write to `vectors` table via sqlite-vec. |
| `event_model.py` | Typed `Event` dataclass. Schema is the contract. |
| `provenance.py` | Provenance tier derivation: `user_confirmed > user_statement > model_claim`. Tier is caller-asserted and validated — model output cannot self-promote. |
| `llm_client.py` | Multi-provider HTTP client: OpenAI, Google Gemini, Anthropic, Ollama. Every call journals a start event and a completion event. No un-journaled LLM calls. |
| `tool_runner.py` | Tool execution lifecycle. Journals tool invocation and result as discrete events. |
| `vector_embedder.py` | OpenAI embedding client. Writes vectors to `journal_store` on completion. |
| `provider_adapter.py` | Uniform adapter abstraction over provider-specific APIs. |
| `id_generator.py` | Pure-Python ULID generation (Crockford base32). |
| `config.py` | Environment and path configuration. Loads `.env` automatically. |

### Handshake layer

| Module | Responsibility |
|---|---|
| `session_runner.py` | Run lifecycle management. Start, checkpoint, close. |
| `context_assembler.py` | Retrieval with provenance-weighted scoring: `user_confirmed=1.0`, `user_statement=0.8`, `model_claim=0.4`. |
| `okf_enrichment.py` | LLM-driven summarization of event runs into OKF bundles. Writes bundles to `okf/` and upserts vectors. |
| `provider_switch.py` | Context-aware provider switching within a session. |

---

## Key design decisions

**Immutable journal.** The `events` table has SQL triggers blocking UPDATE and DELETE. The journal is append-only by construction, not convention.

**Provenance as load-bearing.** Every event carries a provenance tier derived at write time. `user_confirmed` requires explicit caller assertion and is blocked on system or assistant origins. Context assembly weights retrieval by tier — provenance is not metadata, it affects recall.

**No un-journaled calls.** `LLMClient` journals a start event before the API call and a completion event after. If the client throws, the start event remains as a dangling marker — visible, not silent.

**sqlite-vec dual-write.** `journal_store.py` attempts to load `sqlite_vec` at runtime. If available, every completion event writes a vector to the `vectors` table. `rebuild_vectors_from_journal()` reconstructs the vectors table from the immutable events — the journal is the source of truth, not the index.

**Schema migration inline.** No Alembic. The `provenance` column migration is an `ALTER TABLE ... ADD COLUMN` baked into `journal_store.py` initialization with `IF NOT EXISTS` guard.

---

## Providers

Set via environment variable:

```env
LLMJ_PROVIDER=openai       # default
LLMJ_PROVIDER=google
LLMJ_PROVIDER=anthropic
LLMJ_PROVIDER=ollama
```

API keys:

```env
OPENAI_API_KEY=...
GOOGLE_API_KEY=...
ANTHROPIC_API_KEY=...
```

Place in `.env` at project root. Loaded automatically on `LLMClient` init.

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
  public/           # ARCHITECTURE.md, DESIGN.md
  internal/         # HLD, DDD, design notes
DECISIONS.md        # Architectural decision log
```

See [DECISIONS.md](DECISIONS.md) for architectural rationale.

---

## Status

Active development on `restructure/layered`. The three-layer split is established and the core journal, provenance system, and vector dual-write are committed and functional. The projection layer (read-model derivations) is not yet implemented.
