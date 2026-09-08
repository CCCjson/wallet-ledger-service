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
