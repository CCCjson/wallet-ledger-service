"""Seed the database with demo transactions. Safe to run repeatedly.

    uv run python scripts/seed.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import create_pool, init_db  # noqa: E402
from app.seed import seed, summary  # noqa: E402


async def main() -> None:
    pool = await create_pool()
    await init_db(pool)
    await seed(pool)
    print("seeded:", await summary(pool))
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
