"""Return the demo instance to exactly its seeded state.

    uv run python scripts/reset_demo.py                       # local
    DATABASE_URL='<render external url>' uv run python scripts/reset_demo.py

Anyone can push events at the public demo, and a walkthrough of the README
leaves a handful behind. This removes everything that is not seed data and
rebuilds the balances from what remains, so the page a reviewer opens shows the
twelve transactions it is meant to show rather than a trail of someone else's
100-cent test top-ups.

Destructive, and deliberately narrow: it only ever deletes rows whose event_id
is not one of the seeded ones. It is never run automatically.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DATABASE_URL  # noqa: E402
from app.db import create_pool, init_db  # noqa: E402
from app.seed import RECOMPUTE_BALANCES_SQL, seed, summary  # noqa: E402

SEED_EVENT_PREFIX = "evt_seed_"


async def main() -> None:
    # The URL can carry a password; show only enough to confirm the target.
    host = DATABASE_URL.rsplit("@", 1)[-1]
    print(f"resetting {host}")

    pool = await create_pool()
    await init_db(pool)

    async with pool.acquire() as conn:
        async with conn.transaction():
            deleted = await conn.execute(
                "DELETE FROM transactions WHERE event_id NOT LIKE $1",
                f"{SEED_EVENT_PREFIX}%",
            )
            # A wallet row whose last transaction just went away would otherwise
            # keep a stale balance: the recompute below only touches pairs that
            # still have ledger rows to sum.
            await conn.execute(
                """
                DELETE FROM wallets w
                WHERE NOT EXISTS (
                    SELECT 1 FROM transactions t
                    WHERE t.user_id = w.user_id AND t.currency = w.currency
                )
                """
            )
            await conn.execute(RECOMPUTE_BALANCES_SQL)

    # Puts back any seed row that had been deleted, and recomputes again.
    await seed(pool)
    print(f"{deleted.lower()}; now {await summary(pool)}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
