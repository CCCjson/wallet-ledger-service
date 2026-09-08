"""Daely wallet API."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from app import repository
from app.config import CORS_ORIGINS
from app.db import create_pool, init_db
from app.models import PaymentEvent, WalletResponse
from app.search import parse_query
from app.security import verify_signature

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    await init_db(app.state.pool)
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


@app.get("/wallets/{user_id}", response_model=WalletResponse)
async def get_wallet(request: Request, user_id: str, q: str | None = None) -> WalletResponse:
    pool = request.app.state.pool

    search = await parse_query(q) if q else None
    # An unparseable query still returns every transaction rather than an error.
    query_understood = True if search is None else search.understood

    balances = await repository.get_balances(pool, user_id)
    transactions = await repository.list_transactions(pool, user_id, search)

    # Balances always show every currency in full. A search narrows the
    # transaction list only - it must never change what the user is worth.
    return WalletResponse(
        user_id=user_id,
        balances=balances,
        transactions=transactions,
        query_understood=query_understood,
    )
