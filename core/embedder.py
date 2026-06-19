from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class SentenceTransformersEmbeddingAdapter:
    MODEL_NAME = "all-MiniLM-L6-v2"
    DIMS = 384

    def __init__(self) -> None:
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise ImportError(
                    "sentence-transformers is required for the sentence_transformers "
                    "embedding adapter. Install with: pip install sentence-transformers"
                )
            self._model = SentenceTransformer(self.MODEL_NAME)
        return self._model

    def embed(self, text: str) -> list[float]:
        model = self._load()
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()

    def dims(self) -> int:
        return self.DIMS


class OllamaEmbeddingAdapter:
    MODEL_NAME = "nomic-embed-text"
    DIMS = 768

    def __init__(self) -> None:
        self._base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

    def embed(self, text: str) -> list[float]:
        endpoint = f"{self._base_url}/api/embed"
        body = json.dumps({"model": self.MODEL_NAME, "input": text}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Ollama request failed ({self._base_url}): {exc}"
            ) from exc
        embeddings = data.get("embeddings")
        if not embeddings or not embeddings[0]:
            raise RuntimeError(f"Ollama returned no embeddings: {data}")
        return embeddings[0]

    def dims(self) -> int:
        return self.DIMS


class HuggingFaceEmbeddingAdapter:
    MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
    DIMS = 384

    def __init__(self) -> None:
        self._api_key = os.environ.get("HF_API_KEY", "")

    def embed(self, text: str) -> list[float]:
        endpoint = f"https://api-inference.huggingface.co/models/{self.MODEL_NAME}"
        body = json.dumps({"inputs": text}).encode("utf-8")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            content = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(
                f"HuggingFace Inference API failed: {exc.code} {content}"
            ) from exc
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"HuggingFace returned unexpected format: {data}")
        return data

    def dims(self) -> int:
        return self.DIMS


class GoogleEmbeddingAdapter:
    MODEL_NAME = "gemini-embedding-001"
    DIMS = 3072

    def __init__(self) -> None:
        self._api_key = os.environ.get("GOOGLE_API_KEY", "")
        if not self._api_key:
            raise ValueError("GOOGLE_API_KEY must be set for the google embedding adapter.")

    def embed(self, text: str) -> list[float]:
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.MODEL_NAME}:embedContent?key={self._api_key}"
        )
        body = json.dumps({
            "model": f"models/{self.MODEL_NAME}",
            "content": {"parts": [{"text": text}]},
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            content = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(
                f"Google embedding API failed: {exc.code} {content}"
            ) from exc
        values = data.get("embedding", {}).get("values")
        if not values:
            raise RuntimeError(f"Google returned no embedding: {data}")
        return values

    def dims(self) -> int:
        return self.DIMS


EMBEDDING_ADAPTER_REGISTRY: dict[str, Any] = {
    "sentence_transformers": SentenceTransformersEmbeddingAdapter,
    "ollama": OllamaEmbeddingAdapter,
    "huggingface": HuggingFaceEmbeddingAdapter,
    "google": GoogleEmbeddingAdapter,
}

DEFAULT_EMBEDDING_PROVIDER = os.environ.get("LLMJ_EMBEDDING_PROVIDER", "google")


class Embedder:
    def __init__(self, provider: str | None = None) -> None:
        provider = provider or DEFAULT_EMBEDDING_PROVIDER
        adapter_cls = EMBEDDING_ADAPTER_REGISTRY.get(provider)
        if adapter_cls is None:
            raise ValueError(
                f"Unknown embedding provider: {provider!r}. "
                f"Available: {list(EMBEDDING_ADAPTER_REGISTRY.keys())}"
            )
        self._provider = provider
        self._adapter = adapter_cls()

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def model_name(self) -> str:
        return self._adapter.MODEL_NAME

    def embed(self, text: str) -> list[float]:
        return self._adapter.embed(text)

    def dims(self) -> int:
        return self._adapter.dims()
