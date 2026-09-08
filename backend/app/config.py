"""Environment configuration.

Everything the app needs comes from environment variables so that no secret
ever has to live in the repository.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _normalise_database_url(url: str) -> str:
    """Render hands out ``postgres://`` URLs, asyncpg wants ``postgresql://``.

    Also strips the ``+asyncpg`` SQLAlchemy dialect suffix if someone pastes one
    in, and drops query parameters asyncpg does not understand.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url.replace("postgresql+asyncpg://", "postgresql://")


DATABASE_URL = _normalise_database_url(
    os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/daely")
)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "whsec_test_secret")

# Deployment convenience: Render's free tier has no shell to run the seed
# script from, and its free Postgres can be recreated. Seeding is idempotent,
# so the demo instance simply reseeds itself on boot. Off by default.
SEED_ON_STARTUP = os.environ.get("SEED_ON_STARTUP", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# Comma-separated list. Must include the deployed frontend origin or the browser
# will block every request even though the API itself works fine.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

# Currencies and transaction types the system knows about. Fed to the search
# prompt as an enum so the model has less room to hallucinate.
SUPPORTED_CURRENCIES = ["USD", "EUR"]
TRANSACTION_TYPES = ["top_up", "payment"]
