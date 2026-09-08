"""HMAC signature verification for incoming webhooks."""

import hashlib
import hmac

from fastapi import HTTPException

from app.config import WEBHOOK_SECRET


def verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    """Verify ``X-Signature`` against an HMAC-SHA256 of the raw request body.

    Raises 401 on any failure. Two details matter here:

    1. The signature is computed over the *raw bytes* as they arrived. Parsing
       the JSON and re-serialising it would change key order and whitespace,
       and the signature would never match.
    2. ``hmac.compare_digest`` is used rather than ``==``. A plain comparison
       short-circuits on the first differing byte and leaks how much of the
       signature was correct through its timing.
    """
    if not signature_header:
        raise HTTPException(status_code=401, detail="missing X-Signature header")

    provided = signature_header.strip()
    # Accept both "sha256=<hex>" and a bare hex digest.
    if provided.startswith("sha256="):
        provided = provided[len("sha256=") :]

    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid signature")
