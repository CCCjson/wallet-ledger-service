"""HMAC signature verification for incoming webhooks."""

import hashlib
import hmac
import re

from fastapi import HTTPException

from app.config import WEBHOOK_SECRET

# An untrusted header is converged to a known shape *before* it reaches any
# cryptographic code. Anything that is not 64 lowercase hex digits, with or
# without the conventional "sha256=" prefix, is not a signature we could ever
# have produced, so there is nothing to compare and no reason to try.
SIGNATURE_RE = re.compile(r"^(?:sha256=)?[0-9a-f]{64}$")


def verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    """Verify ``X-Signature`` against an HMAC-SHA256 of the raw request body.

    Raises 401 on any failure. Three details matter here:

    1. The signature is computed over the *raw bytes* as they arrived. Parsing
       the JSON and re-serialising it would change key order and whitespace,
       and the signature would never match.
    2. The header is validated against ``SIGNATURE_RE`` first. Header contents
       are entirely attacker-controlled, and ``hmac.compare_digest`` raises
       ``TypeError`` when handed a ``str`` containing non-ASCII characters -
       which would turn an unauthenticated request into a 500. Rejecting on
       shape removes that path outright, and it does so without silently
       rewriting the input: a malformed header is refused, not repaired.
    3. ``hmac.compare_digest`` is used rather than ``==``. A plain comparison
       short-circuits on the first differing byte and leaks how much of the
       signature was correct through its timing.
    """
    if not signature_header:
        raise HTTPException(status_code=401, detail="missing X-Signature header")

    provided = signature_header.strip()
    if not SIGNATURE_RE.match(provided):
        raise HTTPException(status_code=401, detail="malformed X-Signature header")

    # Guaranteed pure ASCII hex by the check above.
    provided = provided.removeprefix("sha256=")

    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid signature")
