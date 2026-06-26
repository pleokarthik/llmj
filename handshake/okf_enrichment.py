# OKF enrichment: summarizes journal events via LLM, writes bundles and vectors.
from __future__ import annotations

import json
import os
import time
from typing import Any

from core.provenance import derive_provenance
from core.vector_embedder import Embedder
from core.llm_client import LLMClient
from core.journal_store import Store


ENRICHMENT_PROMPT = (
    "You are a knowledge extraction system. Given the following model response, "
    "produce a concise summary (1-3 sentences) that captures the key facts, "
    "decisions, or claims. Output ONLY the summary text, nothing else."
)

OKF_BUNDLE_DIR = "okf"


class OKFEnricher:
    def __init__(self, store: Store, embedder: Embedder, llm_client: LLMClient) -> None:
        self.store = store
        self.embedder = embedder
        self.llm_client = llm_client

    def enrich(self, event_id: str, root_id: str | None = None) -> dict[str, Any]:
        event = self.store.get(event_id)
        provenance = derive_provenance(event.origin, event.role, event.provenance)

        messages = [
            {"role": "user", "content": f"{ENRICHMENT_PROMPT}\n\n{event.content}"},
        ]
        response = self.llm_client.call(
            chat_id=event.chat_id,
            messages=messages,
            model="llama-3.3-70b-versatile",
            provider="groq",
            origin="system:enrichment",
            root_id=root_id,
        )

        summary = ""
        choices = response.get("choices", [])
        if choices:
            summary = choices[0].get("message", {}).get("content", "")

        bundle: dict[str, Any] = {
            "id": event_id,
            "source_event_id": event_id,
            "provenance": provenance,
            "trust_tier": provenance,
            "summary": summary,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": event.model,
            "provider": event.provider,
            "source_content": event.content,
            "chat_id": event.chat_id,
            "root_id": event.root_id,
        }

        os.makedirs(OKF_BUNDLE_DIR, exist_ok=True)
        path = os.path.join(OKF_BUNDLE_DIR, f"{event_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2, ensure_ascii=False)

        vec = self.embedder.embed(summary)
        self.store.upsert_vector(event_id, summary, vec,
                                 embedding_provider=self.embedder.provider_name,
                                 embedding_model=self.embedder.model_name)

        return bundle


def enrich_run(
    root_id: str,
    store: Store,
    embedder: Embedder,
    llm_client: LLMClient,
) -> list[str]:
    enricher = OKFEnricher(store, embedder, llm_client)
    paths: list[str] = []
    for event in store.query({"root_id": root_id}):
        if event.origin.startswith("system:"):
            continue
        if event.role == "assistant" and event.status == "ok" and event.content:
            enricher.enrich(event.event_id, root_id=root_id)
            path = os.path.join(OKF_BUNDLE_DIR, f"{event.event_id}.json")
            paths.append(path)
    return paths
