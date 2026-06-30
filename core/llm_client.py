from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from core.config import load_project_env
from core.event_model import Event
from core.provenance import derive_provenance
from core.provider_adapter import PROVIDER_ADAPTER_REGISTRY
from core.journal_store import Store
from core.id_generator import ulid


class LLMClient:
    def __init__(self, store: Store, api_keys: dict[str, str] | None = None) -> None:
        load_project_env()
        self.store = store
        self._api_keys = api_keys or {}

    def _resolve_api_key(self, provider: str) -> str:
        if provider in self._api_keys:
            return self._api_keys[provider]
        env_map = {
            "openai": "OPENAI_API_KEY",
            "google": "GOOGLE_API_KEY",
            "groq": "GROQ_API_KEY",
        }
        env_var = env_map.get(provider)
        if not env_var:
            raise ValueError(f"No API key env var known for provider: {provider}")
        key = os.environ.get(env_var)
        if not key:
            raise ValueError(f"{env_var} must be set in the environment.")
        return key

    def call(
        self,
        chat_id: str,
        messages: list[dict[str, str]],
        model: str = "gpt-3.5-turbo",
        temperature: float = 0.0,
        provider: str = "openai",
        scope: str = "user",
        origin: str = "user",
        root_id: str | None = None,
        provenance: str | None = None,
    ) -> dict[str, Any]:
        if provider not in PROVIDER_ADAPTER_REGISTRY:
            raise ValueError(f"Unsupported provider: {provider}")
        adapter = PROVIDER_ADAPTER_REGISTRY[provider]
        root_id = root_id or ulid()
        canonical_request = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        call_id = ulid()
        message_created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # `provenance` is an assertion about the newest statement being submitted
        # this turn, not the replayed history, so only the last message is eligible.
        message_events = []
        last_index = len(messages) - 1
        for index, message in enumerate(messages):
            msg_role = message.get("role")
            msg_provenance = None
            if index == last_index and provenance is not None:
                msg_provenance = derive_provenance(origin, msg_role, provenance)
            message_events.append(Event(
                event_id=ulid(),
                event_type="message",
                chat_id=chat_id,
                parent_id=None,
                root_id=root_id,
                origin=origin,
                role=msg_role,
                tool_name=None,
                status=None,
                provider=provider,
                model=model,
                params=None,
                content=message.get("content"),
                tokens_in=None,
                tokens_out=None,
                cost=None,
                latency_ms=None,
                scope=scope,
                payload=None,
                created_at=message_created_at,
                call_id=call_id,
                sequence=index,
                provenance=msg_provenance,
            ))

        start_event = Event(
            event_id=ulid(),
            event_type="llm_call",
            chat_id=chat_id,
            parent_id=None,
            root_id=root_id,
            origin=origin,
            role="user",
            tool_name=None,
            status=None,
            provider=provider,
            model=model,
            params=canonical_request,
            content=None,
            tokens_in=None,
            tokens_out=None,
            cost=None,
            latency_ms=None,
            scope=scope,
            payload={"phase": "started"},
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self.store.append(start_event)
        for message_event in message_events:
            self.store.append(message_event)

        started = time.perf_counter()
        try:
            provider_payload = adapter.to_wire(canonical_request)
            response_json = self._call_provider(provider, provider_payload)
            canonical_response = adapter.from_wire(response_json)
            elapsed_ms = int((time.perf_counter() - started) * 1000)

            usage = canonical_response.get("usage", {}) or {}
            tokens_in = usage.get("prompt_tokens")
            tokens_out = usage.get("completion_tokens")
            cost = None
            if provider == "openai":
                if model.startswith("gpt-4") and tokens_in is not None and tokens_out is not None:
                    cost = 0.03 * tokens_in / 1000 + 0.06 * tokens_out / 1000
                elif model.startswith("gpt-3.5") and tokens_in is not None and tokens_out is not None:
                    cost = 0.0015 * tokens_in / 1000 + 0.002 * tokens_out / 1000

            assistant_message = None
            choices = canonical_response.get("choices", [])
            if choices:
                assistant_message = choices[0].get("message", {}).get("content")

            end_event = Event(
                event_id=ulid(),
                event_type="llm_call",
                chat_id=chat_id,
                parent_id=start_event.event_id,
                root_id=root_id,
                origin=origin,
                role="assistant",
                tool_name=None,
                status="ok",
                provider=provider,
                model=model,
                params=canonical_request,
                content=assistant_message,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost=cost,
                latency_ms=elapsed_ms,
                scope=scope,
                payload={
                    "phase": "completed",
                    "response": canonical_response,
                },
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            self.store.append(end_event)
            return canonical_response
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            end_event = Event(
                event_id=ulid(),
                event_type="llm_call",
                chat_id=chat_id,
                parent_id=start_event.event_id,
                root_id=root_id,
                origin=origin,
                role="assistant",
                tool_name=None,
                status="error",
                provider=provider,
                model=model,
                params=canonical_request,
                content=str(exc),
                tokens_in=None,
                tokens_out=None,
                cost=None,
                latency_ms=elapsed_ms,
                scope=scope,
                payload={"phase": "failed", "error": str(exc)},
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            self.store.append(end_event)
            raise

    def _call_provider(self, provider: str, provider_payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self._resolve_api_key(provider)
        if provider == "openai":
            endpoint = "https://api.openai.com/v1/chat/completions"
            body = json.dumps(provider_payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
        elif provider == "google":
            model = provider_payload.pop("model")
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            body = json.dumps(provider_payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
            }
        elif provider == "groq":
            endpoint = "https://api.groq.com/openai/v1/chat/completions"
            body = json.dumps(provider_payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "llmj/0.1",
            }
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            content = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(
                f"{provider.capitalize()} request failed: {exc.code} {exc.reason} {content}"
            ) from exc
