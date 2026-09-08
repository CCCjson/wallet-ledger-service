"""Shared helpers for building signed webhook requests."""

import hashlib
import hmac
import json

from tests.conftest import WEBHOOK_SECRET


def sign(raw: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def event_body(**overrides) -> bytes:
    payload = {
        "event_id": "evt_1",
        "user_id": "u_123",
        "amount": 2500,
        "currency": "USD",
        "type": "top_up",
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


async def post_event(client, raw: bytes, signature: str | None = None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Signature"] = signature
    return await client.post("/webhooks/payment", content=raw, headers=headers)
