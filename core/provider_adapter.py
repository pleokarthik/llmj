from __future__ import annotations

from typing import Any


class OpenAIProviderAdapter:
    def to_wire(self, canonical_request: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": canonical_request["model"],
            "messages": canonical_request["messages"],
            "temperature": canonical_request.get("temperature", 0.0),
        }

    def from_wire(self, provider_resp: dict[str, Any]) -> dict[str, Any]:
        return provider_resp


class GoogleProviderAdapter:
    _ROLE_MAP = {"user": "user", "assistant": "model", "system": "user"}

    def to_wire(self, canonical_request: dict[str, Any]) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        for message in canonical_request.get("messages", []):
            role = message.get("role", "user")
            text = message.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
                continue
            contents.append({
                "role": self._ROLE_MAP.get(role, "user"),
                "parts": [{"text": text}],
            })
        wire: dict[str, Any] = {
            "model": canonical_request["model"],
            "contents": contents,
            "generationConfig": {
                "temperature": canonical_request.get("temperature", 0.0),
            },
        }
        if system_parts:
            wire["systemInstruction"] = {"parts": system_parts}
        return wire

    def from_wire(self, provider_resp: dict[str, Any]) -> dict[str, Any]:
        text = None
        for candidate in provider_resp.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                if "text" in part:
                    text = part["text"]
                    break
            if text is not None:
                break
        usage_meta = provider_resp.get("usageMetadata", {})
        usage = None
        if usage_meta:
            usage = {
                "prompt_tokens": usage_meta.get("promptTokenCount"),
                "completion_tokens": usage_meta.get("candidatesTokenCount"),
                "total_tokens": usage_meta.get("totalTokenCount"),
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": text,
                    }
                }
            ],
            "usage": usage,
            "raw": provider_resp,
        }


class GroqProviderAdapter:
    def to_wire(self, canonical_request: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": canonical_request["model"],
            "messages": canonical_request["messages"],
            "temperature": canonical_request.get("temperature", 0.0),
        }

    def from_wire(self, provider_resp: dict[str, Any]) -> dict[str, Any]:
        return provider_resp


PROVIDER_ADAPTER_REGISTRY: dict[str, Any] = {
    "openai": OpenAIProviderAdapter(),
    "google": GoogleProviderAdapter(),
    "groq": GroqProviderAdapter(),
}
