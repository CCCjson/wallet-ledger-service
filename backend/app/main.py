"""Daely wallet API."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from app import repository
from app.config import CORS_ORIGINS, DEFAULT_CURRENCIES, SEED_ON_STARTUP
from app.db import create_pool, init_db
from app.models import PaymentEvent, WalletResponse
from app.search import parse_query
from app.seed import seed, summary
from app.security import verify_signature

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    await init_db(app.state.pool)
    if SEED_ON_STARTUP:
        # Idempotent: existing rows conflict on event_id and are skipped, and
        # balances are recomputed from the ledger, so a restart cannot double
        # anything or clobber events received in the meantime.
        await seed(app.state.pool)
        logging.getLogger(__name__).info("seeded: %s", await summary(app.state.pool))
    yield
    await app.state.pool.close()


app = FastAPI(title="Daely Wallet API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/webhooks/payment")
async def receive_payment(
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> dict:
    # Order matters: take the raw bytes, verify them, and only then parse.
    # Untrusted data does not reach the parser.
    raw = await request.body()
    verify_signature(raw, x_signature)

    try:
        event = PaymentEvent.model_validate_json(raw)
    except ValidationError as exc:
        # include_input=False: the raw body may not be JSON-serialisable, and
        # echoing an unparsed payload back is not useful to the provider anyway.
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_input=False),
        )

    status = await repository.record_event(request.app.state.pool, event)
    # A duplicate is a 200. Returning 4xx would make the provider retry, and
    # retrying is exactly the thing we are trying to absorb.
    return {"status": status, "event_id": event.event_id}


# A search query is a prompt we pay for and wait on. 200 characters is more
# than any real question needs, and without a bound the endpoint is both a cost
# amplifier and a denial-of-service surface: an unauthenticated GET would
# forward an arbitrarily large string straight to the model.
MAX_QUERY_LENGTH = 200


@app.get("/wallets/{user_id}", response_model=WalletResponse)
async def get_wallet(
    request: Request,
    user_id: str,
    q: str | None = Query(default=None, max_length=MAX_QUERY_LENGTH),
) -> WalletResponse:
    pool = request.app.state.pool

    # Balances are loaded first because the search prompt's currency enum is
    # built from them: the model is only ever offered currencies this user
    # actually holds, and no hard-coded list can drift out of step with the
    # ledger.
    balances = await repository.get_balances(pool, user_id)
    currencies = [b.currency for b in balances] or DEFAULT_CURRENCIES

    search = await parse_query(q, currencies) if q else None
    # An unparseable query still returns every transaction rather than an error.
    query_understood = True if search is None else search.understood

    transactions = await repository.list_transactions(pool, user_id, search)

    # Balances always show every currency in full. A search narrows the
    # transaction list only - it must never change what the user is worth.
    return WalletResponse(
        user_id=user_id,
        balances=balances,
        transactions=transactions,
        query_understood=query_understood,
    )
