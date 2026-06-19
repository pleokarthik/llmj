# Cross-Provider LLM Journal — High-Level Design (HLD)

> Working title; replace `<tool>` with the chosen name.
> Status: design converged, pre-implementation. Folds in agentic execution events, durable failure capture, run-status projection, the three extension surfaces, adoptable read-side views, and OKF as the standard curated knowledge layer above the journal.

---

## 1. Thesis

**Your cross-provider LLM history as a portable, queryable asset you own — the one view no single provider can build.**

The tool sits in the path of LLM interactions across providers (OpenAI, Anthropic, Google, …), records every interaction — including tool executions and agent runs — into one immutable journal, and uses that journal to do two things:

- **Serve the model** — assemble bounded, relevant context per turn, including across model switches.
- **Serve the user** — make their own history queryable, reconstructable, and portable.

These two goals pull in opposite directions on the same data (the model wants compression; the user wants fidelity). They are reconcilable only because of the central architectural split: **the journal stays lossless for the user; the views go lossy for the model.** Between the raw journal and the lossy model views sits a third position: **the OKF bundle** — curated, stable, named knowledge extracted from the journal and expressed in a vendor-neutral standard format (Open Knowledge Format v0.1). The split is not incidental — it is what makes the dual value possible.

The single structurally-defensible property is **cross-provider**: no incumbent will store and reason over a competitor's history, and the existing aggregators do not build the history-intelligence layer. That intersection is the kernel.

---

## 2. Scope

### Goals
- Local-first, single-user tool. Headless engine; any UI is a later consumer.
- Capture **every** event — LLM calls, tool executions, and agent runs, user-facing and hidden — into one immutable journal, *including failed runs up to the point of failure*.
- Assemble bounded, query-conditioned context per turn, and hand off context across model switches.
- Provide derived views: retrieval, rolling summary, deterministic evaluations, economics, run status.
- Extract curated knowledge from the journal into **OKF bundles** (Open Knowledge Format v0.1) — the standard portable layer above raw capture, expressed as provenance-tagged markdown + YAML.
- Be interoperable and queryable through read-only surfaces (OTel export, SQL, MCP, OKF export) without coupling the core to any of them.
- Ship as a **tiny, dependency-light library + thin CLI**, forkable at clean interfaces.

### Non-goals (explicit)
- **The tool captures and serves; it does not adjudicate.** No human-in-the-loop annotation, no scoring workflows, no policy generation over the logs. Evaluation is deterministic and mechanical only. Judgment/governance is a different category (and arguably the decision-substrate project's territory), not this one's.
- **The enrichment output format is OKF (concrete, not exploratory); extraction quality is exploratory.** Concept extraction — which events to enrich, concept boundaries, classification — has no ground truth and makes no SOTA claims. The OKF format is stable; what gets extracted into it is labeled frontier.
- **Not a multi-tenant SaaS.** Org-shared memory is opt-in, on-demand, and self-hosted by the org — never the default substrate. Local user memory is the priority.
- **Not the user-facing gateway as a product.** We are in the call path only to capture, not to win the consumer-chat-app distribution fight.
- **No cross-language engine rewrite.** Portability lives in the data format, not in the code running everywhere.
- **No speculative abstraction; no agent orchestration in the core.** Build the journal, capture, context, views, OKF — in that order.

---

## 3. Architecture overview

Spine: **immutable append-only journal = single source of truth. Everything else is a rebuildable materialized view.** Losing any view costs nothing because it can be rebuilt from the log.

```
            ┌──────────────────────────────────────────────┐
            │                  CONSUMERS                     │
            │        (CLI now; React / app later)            │
            └───────────────────────┬──────────────────────┘
                                     │ library API
   ┌─────────────────────────────────────────────────────────┐
   │                     ENGINE (headless)                     │
   │                                                           │
   │  ┌──────────────┐  ┌──────────────────────────────────┐  │
   │  │  CAPTURE      │  │      CONTEXT ASSEMBLY           │  │
   │  │ LLMClient +   │  │  per-turn retrieval + switch    │  │
   │  │ tool-runner   │  │  handshake (the behavioral core)│  │
   │  │ (choke points)│  └──────────────────────────────────┘  │
   │  │ ProviderAdpt  │                  │                      │
   │  └──────┬────────┘                  │                      │
   │         │ durable per-event writes  │ reads                │
   │         ▼                            ▼                      │
   │  ┌──────────────────────────────────────────────────────┐ │
   │  │  JOURNAL  (source of truth, immutable, append-only)   │ │
   │  │  events (llm_call | tool_call | agent_run)            │ │
   │  │  + evaluations    ·    SQLite (WAL) + JSON            │ │
   │  └──────────────────────────────────────────────────────┘ │
   │         │ enrichment pass (async, rebuildable)             │
   │         ▼                                                   │
   │  ┌──────────────────────────────────────────────────────┐ │
   │  │  OKF BUNDLE  (curated knowledge layer — standard fmt) │ │
   │  │  concept docs · provenance-tagged · source_event_ids  │ │
   │  │  markdown + YAML frontmatter (OKF v0.1)               │ │
   │  └──────────────────────────────────────────────────────┘ │
   │         │ embeds into retrieval corpus + other views       │
   │         ▼                                                   │
   │  ┌──────────────────────────────────────────────────────┐ │
   │  │  VIEWS: retrieval · rolling summary · evaluations ·   │ │
   │  │  economics · run projection (status)                  │ │
   │  └──────────────────────────────────────────────────────┘ │
   │         │ read-only surfaces                               │
   │         ▼                                                   │
   │   OTel export · SQL · MCP server · OKF export · replay     │
   └─────────────────────────────────────────────────────────┘
        keys: local only    store: SQLite default / Qdrant opt
```

### Layers
1. **Capture** — two journaled choke points: an `LLMClient` (every provider call) and a `ToolRunner` (every tool execution). Agents are handed the journaled clients, never raw ones. `ProviderAdapter` per provider translates canonical ↔ wire format. Foreign agents are captured via a local journaling proxy (`base_url`); their tool *executions* are only partially recoverable (intentions, not results).
2. **Journal** — `events` (immutable, generalized: `llm_call | tool_call | agent_run`) + `evaluations` (derived). Plain SQLite (WAL) columns + JSON blobs; language-neutral so a future non-Python engine reads it unchanged. Durable per event, so a crash preserves every step up to and including the one it died on.
3. **OKF bundle** — enrichment pipeline extracts stable, named concepts from journal events and writes them as provenance-tagged OKF v0.1 documents (markdown + YAML frontmatter). Derived and rebuildable from the journal. OKF docs become the **primary** source for the retrieval corpus (embedded as vectors). The provenance extension (`provenance`, `source_event_ids`) is non-standard but spec-compliant — OKF mandates tolerant unknown-field handling.
4. **Views** — retrieval corpus (primary: OKF docs; secondary: raw event chunks), rolling summary (journal-derived, separate lifecycle from OKF — dynamic not stable), deterministic evaluations, economics, and the run projection (carries status). All derived, all rebuildable. The rolling summary is regenerated from the journal, never from OKF — different lifecycles, different stability contracts.
5. **Context assembly** — per-turn query-conditioned retrieval + the model-switch handshake. The only irreducibly algorithmic part, where quality and bugs both live.

### Extension surfaces (the self-policing rule)
There are exactly **three legal attachment surfaces**, and nothing else:
1. **Capture-side filters/adapters** (before the write): proxy, scrub/redact, pending-span emit, provider/LangChain adapters.
2. **Egress adapters** (provider boundary): translation only.
3. **Read-side views** (after the journal): OTel export, SQL, MCP, OKF bundle export, replay/diff/fork, economics.

The **journal** (the contract) and **context assembly** (the behavioral core) take no injection. If a proposed feature cannot attach at one of the three surfaces, it is out of scope by construction — this is what ruled out HITL annotation (it wanted to live *inside* the core as a judgment step; there is no seam there).

---

## 4. Data flow

**Normal turn**
```
user query
  → context assembly (rolling summary [bounded] + top-k retrieval [query-conditioned, OKF docs primary])
  → ProviderAdapter projects to provider wire format
  → journaled LLMClient → provider
  → response (+ any hidden child calls) journaled (durable, per event)
  → views updated async; OKF enrichment pass async
```

**Model switch A → B**
```
1. hidden blocking call to A: "summarize for handoff"
     → journaled as model_claim, origin = system:summarizer
2. B's first turn injects:
     - the summary, tagged "lossy overview"
     - retrieval seeded by [first query + summary]   (first query is usually thin: "continue")
3. subsequent turns on B: standard per-turn retrieval, no new summary
```

**Agent run (with failure capture)**
```
1. append agent_run START event (the top/root record)
2. each step (llm_call / tool_call) recorded begin → end, committed durably as it completes
3. clean end  → append terminal event: run_completed | run_failed | run_aborted
   crash       → no terminal event is written (process died)
4. run status is READ from the RUN PROJECTION (a view):
     terminal event present        → its status
     start, no terminal, dead proc → crashed (resolved on restart by reconciliation)
```

**OKF enrichment pass (async, post-journal)**
```
journal events (filtered: user_statement provenance, stable content, recency threshold)
  → OKFEnricher.enrich(events)         # LLM call, journaled (origin=system:enrichment)
  → OKFDoc (frontmatter + markdown body)
  → written to bundle directory
  → Embedder.embed([doc.body])
  → Store.upsert_vector(payload)       # provenance inherited from OKF frontmatter
```

Active context size is **fixed regardless of conversation length** — a 10,000-message thread injects the same budget as a 100-message one. Context cost is O(1) in length by construction.

---

## 5. Key decisions and rationale

| Decision | Rationale |
|---|---|
| Journal is the only source of truth; all else is a view | Bound/prune/quantize/regenerate views freely; rebuild on demand; enables forking and portability |
| One generalized `events` log (`llm_call \| tool_call \| agent_run`) | Agentic execution is first-class; tool calls and run envelopes are events, not a second system |
| Bounded active context = rolling summary + top-k retrieval | Solves context growth; O(1) in length; faithful (real messages) + continuous (summary) |
| Two choke points: LLMClient + ToolRunner | Completeness guarantee extends to tool executions, not just LLM calls |
| Durable per-event writes (SQLite WAL) + begin/end recording | Capture-till-failure: a crash keeps every step run, and the in-flight step is visible |
| Run status via projection (lifecycle events + crash-by-absence) | Status on the top record **without** mutating the immutable journal |
| BYOK, keys never leave the device; local-first | Removes custody/breach liability; no server to attack |
| Typed axes (`origin`, `event_type`, `tool_name`, `status`) + JSON `payload` tail | Query/aggregate on the typed axes; JSON for the heterogeneous rest |
| Provenance-weighted retrieval (user vs model_claim) | Stops the RAG learning its own hallucinations; model output never auto-promoted to ground truth |
| Deterministic supersession (recency tiebreak) before LLM | Semantic similarity returns stale-but-on-topic state; recency heuristic ships now |
| Deterministic evaluations only; no human judgment | The tool captures and serves; it does not adjudicate. Judgment/governance is out of scope |
| Read-side surfaces (OTel export, SQL, MCP, OKF export, replay/diff/fork) | Interop + queryability as read-only views; none touches the contract |
| Tiny core (stdlib + SQLite) + heavy deps as extras | Reconciles "tiny tool" ethos with heavy stack; power is opt-in |
| Mechanism in core, policy in adapters; fork at the adapter boundary | Schema/contract stability is what *grants* safe forkability |
| OKF bundle as the curated knowledge layer above the journal | Standard vendor-neutral format (markdown + YAML); rebuildable from the journal; any OKF-compliant consumer reads the bundle without knowing journal internals |
| Provenance extension on OKF frontmatter (`provenance`, `source_event_ids`) | Journal provenance carries into the curated layer; a `model_claim`-sourced concept is never auto-promoted to ground truth; audit trail back to the source event |
| Rolling summary is journal-derived, not OKF | OKF is for stable named concepts; rolling summary is dynamic and session-scoped. Forcing summary into OKF shape misuses the format and creates false stability |

---

## 6. Invariants (non-negotiable)

1. The journal is the only source of truth; everything else is rebuildable from it.
2. **No un-journaled LLM call or tool execution.** Closed call paths are the completeness guarantee. This includes the enrichment LLM call (`origin=system:enrichment`).
3. Events are immutable. Corrections are new events. Supersession is a pointer, status is a projection — never a mutation.
4. **Capture is durable per event.** Failures preserve all steps up to and including the in-flight one; a failed run is never discarded.
5. **Run status lives on a projection**, folded from immutable lifecycle events; a crash is detected by absence, never written by mutating the journal.
6. API keys never leave the device.
7. The core carries **no heavy dependencies**.
8. The schema/contract is stable; behavior forks freely.
9. Model output is `model_claim` provenance and is never auto-promoted to ground truth.
10. On context conflict, verbatim retrieved source outranks the lossy summary.
11. **Mechanical/deterministic only.** No human-in-the-loop, no scoring workflow, no policy generation.
12. Every feature attaches at one of the three extension surfaces, or it is out of scope.
13. **OKF concepts carry journal provenance.** No OKF concept is treated as authoritative unless its `provenance` traces to a `user_statement` or `user_confirmed` source event. `model_claim`-sourced concepts are tagged and weighted accordingly in retrieval. The OKF bundle is rebuildable; losing it loses no source truth.

---

## 7. Risks and open questions (honest)

- **Enrichment quality has no ground truth.** The OKF format is concrete (v0.1 spec); what gets extracted into it — concept boundaries, classification, decision lineage — is unmeasurable by construction. Keep extraction exploratory and labeled. Format is not exploratory; extraction quality is. *Confidence: OKF output format defensible; extraction quality medium.*
- **OKF v0.1 is Draft status.** The spec may evolve before stabilization. The provenance extension (`provenance`, `source_event_ids` frontmatter fields) is non-standard but spec-compliant — OKF mandates tolerant handling of unknown fields. Pin to v0.1; plan a migration pass on stable release.
- **`sqlite-vec` maturity** for the single-file default store. *Confidence: medium — verify before committing; numpy cosine over stored vectors is the small-scale fallback.*
- **Crash detection** relies on a liveness signal + restart reconciliation to distinguish `running` from `crashed`. Without it, the two are indistinguishable from the log alone.
- **Foreign-agent tool capture is intentions-only** — tool-use blocks in LLM I/O, not executions/results. Capture-by-hope, same caveat as foreign-agent LLM capture.
- **Supersession auto-detection** deferred; v1 is the deterministic recency tiebreak only.
- **Multi-device sync ordering** (if ever added): ULIDs + per-device append streams, not last-write-wins. Out of scope for v1.
- **Switch handshake** adds one blocking summarize call and a re-read cost on the outgoing model. Cheaper than re-sending full history, not free; the economics view will surface it.

---

## 8. What this is, in one paragraph

A local-first, headless engine that captures every cross-provider event — LLM calls, tool executions, and agent runs, including failures up to the point they died — into one immutable journal, extracts curated knowledge from that journal into a standard OKF bundle (provenance-tagged, audit-trailed back to the source event), assembles bounded query-conditioned context per turn (including across model switches), and exposes the history as rebuildable views and read-only surfaces (OTel export, SQL, MCP, OKF export, replay/diff/fork). Shipped as a tiny library plus a thin CLI, forkable at clean interfaces, with the durable artifact — the journal — language-neutral underneath and the curated artifact — the OKF bundle — readable by any compliant consumer without knowing journal internals. The hard engineering is concentrated in one place: context handshaking and injection. Everything else is either static (the schema), mechanical (recording), standard-format output (OKF), or a read-only view. Nothing adjudicates.
