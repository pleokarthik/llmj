from __future__ import annotations

from typing import Any

from core.event_model import Event
from core.vector_embedder import Embedder
from core.llm_client import LLMClient
from core.journal_store import Store


PROVENANCE_WEIGHTS: dict[str, float] = {
    "user_confirmed": 1.0,
    "user_statement": 0.8,
    "model_claim": 0.4,
}

SUMMARY_CHAR_BUDGET = 2000
RECENCY_TIE_THRESHOLD = 0.05


from core.provenance import derive_provenance


def get_call(
    call_id: str,
    store: Store,
    grain: str = "message",
    filter: dict[str, Any] | None = None,
) -> list[Event] | list[dict[str, Any]]:
    """Project a call's per-message events.

    grain='message' returns raw Event rows ordered by sequence.
    grain='blob' reassembles them into the original [{role, content}, ...] shape.

    A call_id with no matching 'message' rows falls back to treating call_id as
    a plain event_id, so callers built around single events (e.g. retrieval
    hits keyed to pre-split or non-message events) still resolve to one row.
    """
    if grain not in ("message", "blob"):
        raise ValueError(f"Invalid grain: {grain!r}. Must be 'message' or 'blob'.")

    events = sorted(
        store.query({"call_id": call_id, "event_type": "message"}),
        key=lambda e: e.sequence,
    )

    if not events:
        try:
            events = [store.get(call_id)]
        except KeyError:
            events = []

    if filter:
        for key, value in filter.items():
            if key == "provenance":
                events = [
                    e for e in events
                    if derive_provenance(e.origin, e.role, e.provenance) == value
                ]
            else:
                events = [e for e in events if getattr(e, key, None) == value]

    if grain == "message":
        return events
    return [{"role": e.role, "content": e.content} for e in events]


def weight_by_provenance(
    hits: list[tuple[str, float]],
    store: Store,
) -> list[tuple[str, float, str, str]]:
    """Multiply raw cosine scores by provenance weight.

    Each hit's call_id is projected at message grain — a hit covering several
    messages expands into one weighted tuple per message, since provenance is
    per-statement, not per-call. Returns list of (event_id, weighted_score,
    provenance, created_at).
    """
    weighted: list[tuple[str, float, str, str]] = []
    for event_id, raw_score in hits:
        for event in get_call(event_id, store, grain="message"):
            provenance = derive_provenance(event.origin, event.role, event.provenance)
            weight = PROVENANCE_WEIGHTS.get(provenance, 0.4)
            weighted.append((event.event_id, raw_score * weight, provenance, event.created_at))
    return weighted


def recency_tiebreak(
    hits: list[tuple[str, float, str, str]],
) -> list[tuple[str, float, str, str]]:
    """Among hits within RECENCY_TIE_THRESHOLD of each other, prefer most recent."""
    if not hits:
        return hits
    hits_sorted = sorted(hits, key=lambda h: h[1], reverse=True)
    result: list[tuple[str, float, str, str]] = []
    i = 0
    while i < len(hits_sorted):
        group = [hits_sorted[i]]
        j = i + 1
        while j < len(hits_sorted) and (hits_sorted[i][1] - hits_sorted[j][1]) < RECENCY_TIE_THRESHOLD:
            group.append(hits_sorted[j])
            j += 1
        group.sort(key=lambda h: h[3], reverse=True)
        result.extend(group)
        i = j
    return result


def current_rolling_summary(
    chat_id: str,
    store: Store,
    llm: LLMClient,
    provider: str = "groq",
    model: str = "llama-3.3-70b-versatile",
) -> str:
    """Regenerate a rolling summary from the journal. Never appended to a prior summary."""
    assistant_events = []
    for event in store.query({"chat_id": chat_id, "event_type": "llm_call"}):
        if event.role == "assistant" and event.status == "ok" and event.content \
                and event.origin != "system:summarizer":
            assistant_events.append(event)

    if not assistant_events:
        return ""

    combined = "\n---\n".join(e.content for e in assistant_events)

    if len(combined) < SUMMARY_CHAR_BUDGET:
        return combined

    messages = [
        {
            "role": "user",
            "content": (
                "Summarize the following conversation responses concisely. "
                "Preserve key facts, decisions, and user preferences. "
                "Keep it under 500 words.\n\n" + combined
            ),
        }
    ]
    response = llm.call(
        chat_id=chat_id,
        messages=messages,
        model=model,
        provider=provider,
        origin="system:summarizer",
    )
    choices = response.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def assemble_context(
    chat_id: str,
    query: str,
    store: Store,
    embedder: Embedder,
    llm: LLMClient,
    top_k: int = 5,
) -> str:
    """Per-turn context assembly per DDD §5."""
    query_vec = embedder.embed(query)

    raw_hits = store.search(query_vec, top_k=top_k * 2, chat_id=chat_id)

    weighted = weight_by_provenance(raw_hits, store)
    ranked = recency_tiebreak(weighted)
    ranked = ranked[:top_k]

    hit_lines: list[str] = []
    for event_id, score, provenance, created_at in ranked:
        event = store.get(event_id)
        text = event.content or ""
        if provenance == "model_claim":
            hit_lines.append(f"[unverified prior model response] {text}")
        else:
            hit_lines.append(text)

    summary = current_rolling_summary(chat_id, store, llm)

    verbatim_block = "[VERBATIM SOURCE — authoritative]\n" + "\n".join(hit_lines) if hit_lines else ""
    overview_block = "[LOSSY OVERVIEW]\n" + summary if summary else ""

    blocks = [b for b in [verbatim_block, overview_block] if b]
    return "\n\n".join(blocks)
