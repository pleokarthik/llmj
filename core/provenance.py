from __future__ import annotations

VALID_PROVENANCE_TIERS = {"user_confirmed", "user_statement", "model_claim"}


def derive_provenance(
    origin: str,
    role: str | None,
    asserted: str | None = None,
) -> str:
    if asserted is not None:
        if asserted not in VALID_PROVENANCE_TIERS:
            raise ValueError(f"Invalid provenance tier: {asserted!r}")
        if asserted == "user_confirmed":
            if role == "assistant" or origin.startswith("system:"):
                raise ValueError(
                    f"Cannot assert user_confirmed on model output "
                    f"(origin={origin!r}, role={role!r})"
                )
        return asserted
    if origin == "user" and role == "assistant":
        return "model_claim"
    if origin == "user":
        return "user_statement"
    return "model_claim"
