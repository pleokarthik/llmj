from __future__ import annotations

from typing import Any

from core.context import assemble_context
from core.embedder import Embedder
from core.llm_client import LLMClient
from core.store import Store


HANDOFF_PROMPT = (
    "Summarize this conversation concisely for handoff to another model. "
    "Capture key decisions, open questions, and context a new model needs."
)


def summarize_for_handoff(
    chat_id: str,
    messages: list[dict[str, str]],
    llm_client: LLMClient,
    provider: str = "groq",
    model: str = "llama-3.3-70b-versatile",
) -> tuple[str, str]:
    """Call model A with a handoff summarization prompt.

    Returns (summary_text, end_event_id).
    """
    handoff_messages = messages + [{"role": "user", "content": HANDOFF_PROMPT}]
    response = llm_client.call(
        chat_id=chat_id,
        messages=handoff_messages,
        model=model,
        provider=provider,
        origin="system:summarizer",
    )

    summary_text = ""
    choices = response.get("choices", [])
    if choices:
        summary_text = choices[0].get("message", {}).get("content", "")

    end_event_id = ""
    for event in llm_client.store.query({"chat_id": chat_id, "origin": "system:summarizer"}):
        if event.role == "assistant" and event.status == "ok":
            end_event_id = event.event_id

    return summary_text, end_event_id


def expand_query(user_query: str, handoff_summary: str) -> str:
    """Expand a thin first query with handoff context for retrieval seeding."""
    return f"{user_query}\n\nContext from prior conversation:\n{handoff_summary}"


def switch_model(
    chat_id: str,
    messages: list[dict[str, str]],
    user_first_query: str,
    store: Store,
    embedder: Embedder,
    llm_client: LLMClient,
    from_provider: str = "groq",
    from_model: str = "llama-3.3-70b-versatile",
    to_provider: str = "groq",
    to_model: str = "llama-3.3-70b-versatile",
) -> str:
    """Execute the full A->B model switch handshake per DDD S4."""
    summary_text, _ = summarize_for_handoff(
        chat_id, messages, llm_client, provider=from_provider, model=from_model,
    )

    expanded = expand_query(user_first_query, summary_text)

    context = assemble_context(chat_id, expanded, store, embedder, llm_client)

    b_messages: list[dict[str, str]] = []
    if context:
        b_messages.append({"role": "system", "content": context})
    b_messages.append({"role": "user", "content": user_first_query})

    response = llm_client.call(
        chat_id=chat_id,
        messages=b_messages,
        model=to_model,
        provider=to_provider,
        origin="user",
    )

    choices = response.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""
