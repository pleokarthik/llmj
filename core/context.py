from __future__ import annotations

from typing import Any

from core.embedder import Embedder
from core.llm_client import LLMClient
from core.store import Store


PROVENANCE_WEIGHTS: dict[str, float] = {
    "user_confirmed": 1.0,
    "user_statement": 0.8,
    "model_claim": 0.4,
}

SUMMARY_CHAR_BUDGET = 2000
RECENCY_TIE_THRESHOLD = 0.05


def _derive_provenance(origin: str, role: str | None) -> str:
    if origin == "user" and role == "assistant":
        return "model_claim"
    if origin == "user":
        return "user_statement"
    return "model_claim"


def weight_by_provenance(
    hits: list[tuple[str, float]],
    store: Store,
) -> list[tuple[str, float, str, str]]:
    """Multiply raw cosine scores by provenance weight.

    Returns list of (event_id, weighted_score, provenance, created_at).
    """
    weighted: list[tuple[str, float, str, str]] = []
    for event_id, raw_score in hits:
        event = store.get(event_id)
        provenance = _derive_provenance(event.origin, event.role)
        weight = PROVENANCE_WEIGHTS.get(provenance, 0.4)
        weighted.append((event_id, raw_score * weight, provenance, event.created_at))
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
