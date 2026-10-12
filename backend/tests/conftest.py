"""Test fixtures.

The idempotency tests run against a real Postgres instance on purpose. The
behaviour under test - two concurrent INSERTs serialising on a unique index -
is a property of the database, so a mock or SQLite would test nothing.
"""

import os

# Point the app at a throwaway database before anything imports app.config.
_ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
_TEST_DB = "wallet_test"
os.environ["DATABASE_URL"] = _ADMIN_URL.rsplit("/", 1)[0] + f"/{_TEST_DB}"
os.environ.setdefault("WEBHOOK_SECRET", "whsec_test_secret")

import asyncpg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db import create_pool, init_db  # noqa: E402
from app.main import app  # noqa: E402

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]


@pytest_asyncio.fixture(scope="session")
async def pool():
    admin = await asyncpg.connect(_ADMIN_URL)
    exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", _TEST_DB)
    if not exists:
        await admin.execute(f'CREATE DATABASE "{_TEST_DB}"')
    await admin.close()

    p = await create_pool()
    await init_db(p)
    yield p
    await p.close()


@pytest_asyncio.fixture(autouse=True)
async def clean(pool):
    await pool.execute("TRUNCATE transactions, wallets")
    yield


@pytest_asyncio.fixture
async def client(pool):
    # Set the pool directly rather than running the lifespan, so the same pool
    # is shared with assertions made straight against the database.
    app.state.pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def balance_of(pool):
    async def _balance(user_id: str, currency: str) -> int | None:
        return await pool.fetchval(
            "SELECT balance FROM wallets WHERE user_id = $1 AND currency = $2",
            user_id,
            currency,
        )

    return _balance
