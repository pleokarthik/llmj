import secrets
import time

CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """Generate a ULID string (26 characters) using time-based ordering."""
    timestamp = int(time.time() * 1000)
    if timestamp < 0 or timestamp >= 2**48:
        raise ValueError("Timestamp out of ULID range")
    timestamp_bytes = timestamp.to_bytes(6, "big")
    rand_bytes = secrets.token_bytes(10)
    value = int.from_bytes(timestamp_bytes + rand_bytes, "big")
    encoded = []
    for _ in range(26):
        encoded.append(CROCKFORD_BASE32[value & 0x1F])
        value >>= 5
    return "".join(reversed(encoded))
