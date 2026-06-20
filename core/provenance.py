from __future__ import annotations


def derive_provenance(origin: str, role: str | None) -> str:
    if origin == "user" and role == "assistant":
        return "model_claim"
    if origin == "user":
        return "user_statement"
    return "model_claim"
