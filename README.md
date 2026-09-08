# Daely Wallet

A wallet service that ingests signed payment webhooks into an idempotent ledger,
and a page that lets you search the resulting history in plain English.

- **Live demo:** _<frontend URL>_
- **API:** _<backend URL>_
- **Repo:** _<repo URL>_

> ⚠️ The API runs on Render's free tier, which sleeps when idle. **The first
> request after a period of inactivity takes about 50 seconds** while the
> instance wakes up. Subsequent requests are fast. If the demo page looks stuck
> on first load, that is what is happening.

---

## Quick start

```bash
# 1. Database
docker run -d --name daely-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=daely -p 5432:5432 postgres:17

# 2. API  (http://localhost:8000)
cd backend && cp .env.example .env && uv sync
uv run python scripts/seed.py
uv run uvicorn app.main:app --port 8000

# 3. Web  (http://localhost:3000)
cd frontend && cp .env.example .env.local && npm install && npm run dev
```

The seed script writes twelve transactions for `u_123` across USD and EUR,
spread over the last three weeks, so the example searches have something
meaningful to narrow down.

---

## Design

```
search box ──► GET /wallets/{id}?q=…  ──► LLM ──► SearchFilter (JSON) ──► parameterised SQL
provider   ──► POST /webhooks/payment ──► HMAC ──► idempotent insert ──► atomic balance update
                                                        │
                                              transactions  (ledger, source of truth)
                                              wallets       (materialised balances)
```

Three rules hold throughout:

1. **The ledger is the source of truth**; `wallets` is a materialised
   convenience. Both are written in the same transaction, always.
2. **Money is `BIGINT` in minor units.** `2500` is `$25.00`. There is no
   floating point anywhere in this codebase.
3. **Balances are one row per `(user_id, currency)`.** Currencies are never
   summed and never converted. For custody of other people's money that is not
   a simplification, it is a requirement.

---

## Handling duplicate deliveries

The provider says it will sometimes deliver the same event twice *at the same
moment*. That phrase rules out the obvious solution: any `SELECT` to check
whether an event exists, followed by an `INSERT`, has a window between the two
where a second delivery reads the same "not there yet" answer. Both then insert.

So nothing checks. `transactions.event_id` carries a `UNIQUE` constraint and the
insert is:

```sql
INSERT INTO transactions (event_id, user_id, amount, currency, type)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (event_id) DO NOTHING
RETURNING id
```

Two concurrent inserts of the same `event_id` serialise on the unique index: the
first commits, the second finds the conflict and comes back with no row. **The
database adjudicates, not the application code.** No row returned means the
event was already applied, so we return early and never touch the balance.

The balance update is the other half:

```sql
INSERT INTO wallets (user_id, currency, balance) VALUES ($1, $2, $3)
ON CONFLICT (user_id, currency)
DO UPDATE SET balance = wallets.balance + EXCLUDED.balance
```

The addition happens *inside the database*. Reading a balance, adding to it in
Python and writing it back would be a lost update: two concurrent events read
the same old value and one of the two amounts silently disappears.

Both statements share one transaction, so a crash cannot leave a recorded event
whose money was never applied.

A duplicate returns **200** with `{"status": "duplicate"}`, not 409. A 4xx would
tell the provider to retry, and retrying is exactly the thing being absorbed.

### Signature verification

`X-Signature` is an HMAC-SHA256 of the request body, checked before anything
else happens:

- The digest is computed over the **raw bytes as they arrived**. Parsing the
  JSON and re-serialising it would change key order and whitespace, and no
  signature would ever match again.
- The comparison uses `hmac.compare_digest`, not `==`. A plain comparison
  short-circuits at the first differing byte and leaks, through its timing, how
  much of a guessed signature was correct.
- **Verify first, parse second.** Unauthenticated bytes never reach the parser.

A bad or missing signature is **401**. A valid signature over a malformed or
invalid payload is **422** — those are different failures and the provider
should be able to tell them apart.

---

## Search approach

The model translates English into a filter. **It never generates SQL.**

```
"payments over $50"  ──►  {"tx_type": "payment", "min_amount": 5000, …}  ──►  WHERE type = $2 AND amount >= $3
```

The LLM's entire output is a `SearchFilter` object, constrained by OpenAI's
structured-output mode and then validated again by Pydantic. Every value reaches
Postgres as a bind parameter; nothing from the model is ever interpolated into
SQL text.

This is the decision the task really turns on. A model that writes SQL is an
injection surface you cannot audit, cannot unit-test, and cannot bound the
failure modes of — one bad generation is a dropped table or a read of another
user's rows. A model that fills in seven optional fields fails in exactly one
way: a wrong field value. That is recoverable, and it is testable without
calling the model at all, which is what `tests/test_search.py` does.

The prompt carries today's date (so "last week" resolves), the available
currencies and types as enums (less room to hallucinate), and states that
amounts are in minor units with a worked example.

---

## Unrecognised queries

Search must never be able to break the page, so failure degrades in three
layers:

1. An empty query skips the LLM entirely and returns everything.
2. A timeout (8s), a network error, a missing API key, a response that fails
   validation, or a filter with no constraints in it → the query is marked not
   understood.
3. Not understood → the API returns **every transaction** with
   `query_understood: false`, and the page shows a banner:
   *"Couldn't understand 'qwerty nonsense' — showing all transactions instead."*

Typing gibberish shows you your history with an explanation, never an error
page and never a blank one. Every exception in the search path is caught; the
worst outcome is an unfiltered list.

Related: a search filters the **transaction list only**. Balances always show
every currency in full, because a search should never change what you are
worth.

---

## Assumptions

The spec was clear that events carry an explicit `type`, that amounts are always
positive minor units, that each currency has its own balance, and that there is
no FX conversion. Beyond that:

- **Balances may go negative.** A signed webhook reports something that already
  happened. Refusing a valid event because the balance is short would only make
  the provider retry forever. We record the truth; a negative balance is a
  reconciliation problem, not an ingestion problem.
- **Same `event_id`, different payload → first write wins.** The later delivery
  is discarded as a duplicate. Treating `event_id` as the identity of the event
  is the whole basis of the idempotency guarantee, so it cannot also be
  mutable.
- **Unknown user → empty wallet, 200, not 404.** A user with no transactions and
  a user who does not exist are indistinguishable here, and the frontend has one
  less case to handle.

---

## Try it

Against the live API (replace the host), or `http://localhost:8000` locally:

```bash
BODY='{"event_id":"evt_demo_1","user_id":"u_123","amount":2500,"currency":"USD","type":"top_up"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "whsec_test_secret" | awk '{print $2}')

curl -X POST https://<api-host>/webhooks/payment \
  -H "Content-Type: application/json" \
  -H "X-Signature: sha256=$SIG" \
  -d "$BODY"
# {"status":"ok","event_id":"evt_demo_1"}
```

Use `printf '%s'`, not `echo` — `echo` appends a newline, which changes the
digest and the signature will not match.

Check the balance moved:

```bash
curl -s "https://<api-host>/wallets/u_123" | python3 -m json.tool
```

**Send the same event ten times, concurrently:**

```bash
seq 1 10 | xargs -P 10 -I{} curl -s -X POST https://<api-host>/webhooks/payment \
  -H "Content-Type: application/json" -H "X-Signature: sha256=$SIG" -d "$BODY"
```

One response says `"ok"`, nine say `"duplicate"`, and the balance moves by
$25.00 exactly once.

Signature checks:

```bash
# tampered body -> 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<api-host>/webhooks/payment \
  -H "X-Signature: sha256=$SIG" \
  -d '{"event_id":"evt_x","user_id":"u_123","amount":999999,"currency":"USD","type":"top_up"}'

# no signature -> 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<api-host>/webhooks/payment -d "$BODY"
```

Search:

```bash
curl -s -G --data-urlencode 'q=payments over $50'      "https://<api-host>/wallets/u_123"
curl -s -G --data-urlencode 'q=anything from last week' "https://<api-host>/wallets/u_123"
curl -s -G --data-urlencode 'q=top ups in euros'        "https://<api-host>/wallets/u_123"
curl -s -G --data-urlencode 'q=asdfgh'                  "https://<api-host>/wallets/u_123"
```

---

## Tests

```bash
cd backend && uv run pytest -v
```

They run against a real Postgres (`daely_test`, created automatically). That is
deliberate: the property under test — two concurrent inserts serialising on a
unique index — belongs to the database, so a mock or SQLite would be testing
nothing. `test_ten_concurrent_deliveries_of_one_event_apply_once` fires ten
simultaneous requests carrying the same `event_id` and asserts exactly one `ok`,
nine `duplicate`, one ledger row, and one balance movement.

---

## API

| | |
|---|---|
| `POST /webhooks/payment` | Signed event. `200 ok` / `200 duplicate` / `401` bad signature / `422` invalid payload |
| `GET /wallets/{user_id}` | Balances per currency plus transactions, newest first |
| `GET /wallets/{user_id}?q=…` | Same, with the transaction list narrowed by a natural-language query |
| `GET /health` | Liveness |

---

## What I'd do differently with more time

- **A reconciliation job.** `wallets` is derived data and derived data drifts.
  The recompute already exists as one query in `scripts/seed.py`; it should run
  on a schedule and alert when a materialised balance disagrees with the sum of
  the ledger.
- **Pagination and a covering index on the search path.** The transaction list
  is capped at 200 rows and read in one shot. Real accounts have years of
  history, which needs keyset pagination on `(created_at, id)`.
- **An event replay endpoint.** Because the ledger is the source of truth and
  ingestion is idempotent, replaying a provider's history is safe by
  construction — it is worth exposing that deliberately rather than leaving it
  as a property nobody knows they have.
