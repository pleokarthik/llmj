# Diagrams

Three diagrams of the current write/read/provenance flow. Each is grounded in the
file:line references given below it — if the code moves, regenerate the diagram from
the code, not from this file.

## (a) Write path: message split into per-row journal entries

Grounded in `LLMClient.call()` ([core/llm_client.py:40-198](../core/llm_client.py#L40-L198)). Validation of any
asserted `provenance` happens during message-row construction, before the first
`store.append()` call — an invalid assertion fails clean with nothing journaled yet.

```mermaid
sequenceDiagram
    participant Caller
    participant LLMClient
    participant Store as Store (journal)
    participant Provider as LLM Provider

    Caller->>LLMClient: call(chat_id, messages, provenance=?)
    LLMClient->>LLMClient: call_id = ulid()

    loop for index, message in messages
        alt index == last AND provenance given
            LLMClient->>LLMClient: derive_provenance(origin, msg.role, provenance)
            note right of LLMClient: raises ValueError here if invalid -<br/>nothing has been journaled yet
        end
        LLMClient->>LLMClient: build Event(event_type="message",<br/>call_id, sequence=index, role=msg.role,<br/>content=msg.content, provenance=?)
    end

    LLMClient->>Store: append(start_event)<br/>event_type="llm_call", content=None,<br/>payload={phase: started}
    loop for each message_event
        LLMClient->>Store: append(message_event)
    end

    LLMClient->>Provider: to_wire -> HTTP call -> from_wire

    alt provider call succeeds
        LLMClient->>Store: append(end_event)<br/>event_type="llm_call", role=assistant,<br/>status=ok, parent_id=start_event.event_id
        LLMClient-->>Caller: canonical_response
    else provider call raises
        LLMClient->>Store: append(end_event)<br/>event_type="llm_call", status=error,<br/>parent_id=start_event.event_id
        LLMClient-->>Caller: re-raise exception
    end
```

Note what's *not* new here: the `llm_call` start/end events keep their existing
shape and purpose (crash detection via the dangling-start-event pattern, `parent_id`
linking). Only the start event's `content` changed (blob → `None`); `call_id` and
`sequence` exist exclusively on the new `event_type="message"` rows.

## (b) Read path: `get_call()`'s modes

Grounded in `get_call()` ([handshake/context_assembler.py:24-65](../handshake/context_assembler.py#L24-L65)).

```mermaid
flowchart TD
    A["get_call(call_id, store, grain, filter)"] --> B{"grain valid?<br/>(message|blob)"}
    B -- no --> B1["raise ValueError"]
    B -- yes --> C["query: event_type='message'<br/>AND call_id=call_id,<br/>sorted by sequence"]
    C --> D{"any rows found?"}
    D -- yes --> F
    D -- no --> E["fallback: store.get(call_id)<br/>treat call_id as a plain event_id"]
    E --> E2{"found?"}
    E2 -- yes --> F["events = [that one Event]"]
    E2 -- no --> E3["events = []"]
    F --> G{"filter given?"}
    E3 --> G
    G -- no --> H
    G -- yes --> G1{"filter key == 'provenance'?"}
    G1 -- yes --> G2["keep e if<br/>derive_provenance(e.origin, e.role, e.provenance) == value"]
    G1 -- no --> G3["keep e if<br/>getattr(e, key) == value"]
    G2 --> H{"grain?"}
    G3 --> H
    H -- message --> I["return events: list[Event],<br/>ordered by sequence"]
    H -- blob --> J["return [{role: e.role, content: e.content}<br/>for e in events]"]
```

The "no rows found" fallback path (`D -- no -->`) is load-bearing today: vector-search
hits passed into `weight_by_provenance()` are still keyed to `llm_call`/`embedding`
event ids, not `call_id`s, since nothing yet embeds individual `message` rows. Without
the fallback, every retrieval hit would resolve to an empty event list. See
[DECISIONS.md](../DECISIONS.md) for the rationale and the debt this leaves behind.

## (c) Provenance derivation flow

Grounded in `derive_provenance()` ([core/provenance.py:6-25](../core/provenance.py#L6-L25)), its write-side caller in
`LLMClient.call()` ([core/llm_client.py:69-71](../core/llm_client.py#L69-L71)), and its read-side callers
`weight_by_provenance()` ([handshake/context_assembler.py:82](../handshake/context_assembler.py#L82)) and
`OKFEnricher.enrich()` ([handshake/okf_enrichment.py:32](../handshake/okf_enrichment.py#L32)).

```mermaid
flowchart TD
    subgraph Write["Write time (core/llm_client.py)"]
        W0["LLMClient.call(provenance=X)"] --> W1{"X given AND<br/>this is the last message?"}
        W1 -- no --> W2["store provenance=None<br/>(derive later, at read time)"]
        W1 -- yes --> W3["derive_provenance(origin, role, asserted=X)"]
    end

    subgraph Derive["derive_provenance(origin, role, asserted)"]
        D0["asserted is not None?"] -- yes --> D1{"asserted in<br/>VALID_PROVENANCE_TIERS?"}
        D1 -- no --> D2["raise ValueError:<br/>invalid tier"]
        D1 -- yes --> D3{"asserted == 'user_confirmed' AND<br/>(role == 'assistant' OR<br/>origin starts with 'system:')?"}
        D3 -- yes --> D4["raise ValueError:<br/>cannot promote model/system output"]
        D3 -- no --> D5["return asserted"]
        D0 -- no --> D6{"origin == 'user' AND<br/>role == 'assistant'?"}
        D6 -- yes --> D7["return 'model_claim'<br/>(model's own prior output, replayed)"]
        D6 -- no --> D8{"origin == 'user'?"}
        D8 -- yes --> D9["return 'user_statement'"]
        D8 -- no --> D10["return 'model_claim'"]
    end

    W3 --> D0
    D5 --> W4["store provenance=asserted<br/>on the Event row"]

    subgraph Read["Read time"]
        R0["stored Event(origin, role, provenance)"] --> R1["derive_provenance(origin, role,<br/>asserted=event.provenance)"]
        R1 --> D0
        D5 --> R2["weight_by_provenance:<br/>score * PROVENANCE_WEIGHTS[tier]"]
        D7 --> R2
        D9 --> R2
        D10 --> R2
        D5 --> R3["OKFEnricher.enrich:<br/>bundle.provenance = bundle.trust_tier = tier"]
        D7 --> R3
        D9 --> R3
        D10 --> R3
    end
```

The write-time and read-time paths both funnel through the same `derive_provenance()`
— there is exactly one place in the codebase that decides what a tier resolves to,
and exactly one invariant (`D3`/`D4`) that a caller cannot bypass by asserting.
