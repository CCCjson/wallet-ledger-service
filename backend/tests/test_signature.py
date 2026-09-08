"""Webhook authentication and payload validation."""

from tests.helpers import event_body, post_event, sign


async def test_valid_signature_is_accepted(client, balance_of):
    raw = event_body()
    r = await post_event(client, raw, sign(raw))
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert await balance_of("u_123", "USD") == 2500


async def test_tampered_body_is_rejected(client, balance_of):
    raw = event_body(amount=2500)
    signature = sign(raw)
    tampered = event_body(amount=999999)  # signed for 2500, sent as 999999
    r = await post_event(client, tampered, signature)
    assert r.status_code == 401
    assert await balance_of("u_123", "USD") is None


async def test_wrong_secret_is_rejected(client):
    raw = event_body()
    r = await post_event(client, raw, sign(raw, secret="whsec_wrong"))
    assert r.status_code == 401


async def test_missing_signature_header_is_rejected(client):
    r = await post_event(client, event_body(), signature=None)
    assert r.status_code == 401


async def test_bare_hex_signature_without_prefix_is_accepted(client):
    raw = event_body()
    r = await post_event(client, raw, sign(raw).removeprefix("sha256="))
    assert r.status_code == 200


async def test_negative_amount_is_rejected_after_signature_passes(client):
    raw = event_body(amount=-500)
    r = await post_event(client, raw, sign(raw))
    assert r.status_code == 422


async def test_unknown_type_is_rejected(client):
    raw = event_body(type="refund")
    r = await post_event(client, raw, sign(raw))
    assert r.status_code == 422


async def test_malformed_json_is_rejected_with_422_not_500(client):
    raw = b"{not json at all"
    r = await post_event(client, raw, sign(raw))
    assert r.status_code == 422


# --- Hardening regressions ---------------------------------------------------
# Every case below returned 500 or would have. The header is unauthenticated
# attacker-controlled input, so a crash in it is a crash anyone can trigger.


async def test_non_ascii_signature_is_rejected_not_a_crash(client):
    """hmac.compare_digest raises TypeError on non-ASCII str arguments.

    Before the format check this reached the comparison and answered 500. The
    header is sent as raw bytes, the way a real client puts it on the wire -
    httpx refuses to encode a non-ASCII str, but nothing stops curl.
    """
    r = await client.post(
        "/webhooks/payment",
        content=event_body(),
        headers={
            "Content-Type": "application/json",
            "X-Signature": "sha256=你好".encode(),
        },
    )
    assert r.status_code == 401


async def test_signature_of_wrong_length_is_rejected(client):
    r = await post_event(client, event_body(), signature="sha256=deadbeef")
    assert r.status_code == 401


async def test_uppercase_hex_signature_is_rejected(client):
    """We only ever emit lowercase hex, so uppercase is malformed, not wrong."""
    raw = event_body()
    r = await post_event(client, raw, sign(raw).upper())
    assert r.status_code == 401


async def test_garbage_signature_is_rejected(client):
    r = await post_event(client, event_body(), signature="not-a-signature")
    assert r.status_code == 401


async def test_amount_above_int64_is_rejected_with_422_not_500(client, balance_of):
    """BIGINT's ceiling. Past it asyncpg raises OverflowError at bind time and
    the endpoint answered 500 - which tells the provider to retry an event that
    can never be accepted, forever."""
    raw = event_body(amount=2**63)
    r = await post_event(client, raw, sign(raw))
    assert r.status_code == 422
    assert await balance_of("u_123", "USD") is None


async def test_largest_representable_amount_is_accepted(client, balance_of):
    raw = event_body(amount=2**63 - 1)
    r = await post_event(client, raw, sign(raw))
    assert r.status_code == 200
    assert await balance_of("u_123", "USD") == 2**63 - 1
