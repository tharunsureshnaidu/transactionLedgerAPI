# Transaction Ledger API

A small ledger service: create accounts, post credit/debit transactions, read balances and paginated history, and transfer money between accounts atomically.

**Stack:** Python 3.12, FastAPI, PostgreSQL (psycopg 3, raw SQL, no ORM).

```
app/
  main.py      routes (HTTP layer only)
  ledger.py    business logic + SQL, one DB transaction per operation
  schemas.py   request validation / response models (Pydantic)
  errors.py    consistent error format + exception handlers
  db.py        connection pool, schema bootstrap
schema.sql     tables, constraints, immutability trigger (applied on startup)
tests/         end-to-end tests against a real Postgres
```

---

## Running locally

### Option A: Docker (one command)

```bash
docker compose up --build
```

The API runs on http://localhost:8000 and Postgres on `localhost:5433` (user/password/db: `ledger`).

### Option B: Without Docker

Requirements: Python 3.12+ and PostgreSQL 13+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# create a database, then point the app at it
createdb ledger
export DATABASE_URL=postgresql://<user>:<password>@localhost:5432/ledger

uvicorn app.main:app --reload --port 8000
```

The schema in `schema.sql` is applied automatically on startup. It is idempotent, so restarting is safe.

### Interactive docs

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI JSON: http://localhost:8000/openapi.json

### Tests

The tests run against a real Postgres. Use a **separate** database:

```bash
pip install -r requirements-dev.txt
createdb ledger_test
DATABASE_URL=postgresql://<user>:<password>@localhost:5432/ledger_test pytest -q
```

If `DATABASE_URL` is unset, the tests use `postgresql://ledger:ledger@localhost:5433/ledger_test`, which is the Docker Compose DB once you create `ledger_test` in it.

There are 41 tests. They cover balance math, rejections, duplicates, validation, pagination, sorting, immutability, transfer rollback, and concurrency: parallel debits never overdraw, a duplicate `reference_id` submitted in parallel is applied once, and opposite transfers don't deadlock.

---

## API documentation

Base URL: `http://localhost:8000`. All bodies are JSON.

**Conventions**
- IDs are UUIDs.
- Money (`balance`, `amount`) is returned as a **string with 2 decimals**, e.g. `"150.00"`, to avoid float rounding. Requests accept either a number (`50`, `10.5`) or a string (`"10.50"`).
- Timestamps are ISO-8601 in UTC, e.g. `"2026-10-27T13:18:18.650928Z"`.
- Request bodies reject unknown fields. For example, sending `balance` when creating an account returns a validation error.

### 1. Create account

`POST /accounts`

```json
{ "name": "Alice" }
```

**201 Created**
```json
{
  "id": "4622dccb-9f4f-4ed1-8a7a-64c4d3525748",
  "name": "Alice",
  "balance": "0.00",
  "created_at": "2026-10-27T13:18:08.459253Z"
}
```

| Field | Rules |
|---|---|
| `name` | required, 1–255 chars after trimming whitespace |

`id`, `balance` (always 0) and `created_at` are set by the server. Sending any of them returns **422**.

### 2. Add transaction

`POST /accounts/{account_id}/transactions`

```json
{
  "type": "credit",
  "amount": 100,
  "reference_id": "INV-001",
  "description": "Salary"
}
```

| Field | Rules |
|---|---|
| `type` | required, `"credit"` or `"debit"` (lowercase) |
| `amount` | required, > 0, at most 2 decimal places, at most 16 integer digits |
| `reference_id` | required, 1–100 chars after trimming, unique per account |
| `description` | optional, ≤ 500 chars |

**201 Created**
```json
{
  "id": "d22f1fe8-da47-4461-b68b-b2df0dfa7a93",
  "account_id": "4622dccb-9f4f-4ed1-8a7a-64c4d3525748",
  "type": "credit",
  "amount": "100.00",
  "description": "Salary",
  "reference_id": "INV-001",
  "created_at": "2026-10-27T13:18:08.495753Z"
}
```

| Status | Code | When |
|---|---|---|
| 404 | `ACCOUNT_NOT_FOUND` | account does not exist |
| 409 | `DUPLICATE_TRANSACTION` | `reference_id` already used on this account. Nothing is created, the balance is unchanged, and `details.transaction` holds the original transaction. |
| 422 | `INSUFFICIENT_BALANCE` | debit amount > current balance. Nothing is created and the balance is unchanged. |
| 422 | `VALIDATION_ERROR` | invalid type, amount, reference_id, body, or malformed account id |

Duplicate response example:
```json
{
  "error": {
    "code": "DUPLICATE_TRANSACTION",
    "message": "Transaction with reference_id 'INV-001' already exists for account 4622dccb-...",
    "details": { "transaction": { "id": "d22f1fe8-...", "amount": "100.00", "...": "..." } }
  }
}
```

### 3. Get balance

`GET /accounts/{account_id}/balance`

**200 OK**
```json
{ "account_id": "4622dccb-9f4f-4ed1-8a7a-64c4d3525748", "balance": "100.00" }
```

Returns **404** `ACCOUNT_NOT_FOUND` for an unknown account and **422** `VALIDATION_ERROR` for a malformed id.

### 4. Transaction history

`GET /accounts/{account_id}/transactions?type=credit&page=1&limit=20`

| Query param | Default | Rules |
|---|---|---|
| `type` | (both) | `credit` or `debit` |
| `page` | 1 | integer ≥ 1 |
| `limit` | 20 | integer 1–100 |

Results are sorted latest first (`created_at DESC`, then `id DESC` as a tie-breaker).

**200 OK**
```json
{
  "data": [
    {
      "id": "d22f1fe8-...",
      "account_id": "4622dccb-...",
      "type": "credit",
      "amount": "100.00",
      "description": "Salary",
      "reference_id": "INV-001",
      "created_at": "2026-10-27T13:18:08.495753Z"
    }
  ],
  "pagination": { "page": 1, "limit": 20, "total_records": 1, "total_pages": 1 }
}
```

A page beyond the last returns `200` with `data: []` and the real totals. Invalid `page`, `limit` or `type` returns **422** `VALIDATION_ERROR`. An unknown account returns **404**.

### 5. Transfer (bonus)

`POST /transfers`

```json
{
  "from_account_id": "…uuid A…",
  "to_account_id": "…uuid B…",
  "amount": 30,
  "reference_id": "TRF-001",
  "description": "Rent share"
}
```

**201 Created**
```json
{
  "reference_id": "TRF-001",
  "amount": "30.00",
  "debit":  { "account_id": "…A…", "type": "debit",  "amount": "30.00", "...": "..." },
  "credit": { "account_id": "…B…", "type": "credit", "amount": "30.00", "...": "..." }
}
```

A transfer is recorded as two ledger entries: a debit on the source and a credit on the destination, both carrying the same `reference_id`. Both entries and both balance updates happen in **one DB transaction**. Any failure rolls back everything.

| Status | Code | When |
|---|---|---|
| 404 | `ACCOUNT_NOT_FOUND` | either account does not exist |
| 409 | `DUPLICATE_TRANSACTION` | `reference_id` already used on the source or destination account |
| 422 | `INSUFFICIENT_BALANCE` | source balance < amount |
| 422 | `SAME_ACCOUNT_TRANSFER` | source and destination are the same account |
| 422 | `VALIDATION_ERROR` | invalid body |

### Error format

Every error response has the same shape:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "details": [
      { "field": "body.type",   "message": "Input should be 'credit' or 'debit'" },
      { "field": "body.amount", "message": "Input should be greater than 0" }
    ]
  }
}
```

| HTTP | Codes |
|---|---|
| 404 | `ACCOUNT_NOT_FOUND`, `NOT_FOUND` (unknown route) |
| 405 | `METHOD_NOT_ALLOWED` |
| 409 | `DUPLICATE_TRANSACTION` |
| 422 | `VALIDATION_ERROR`, `INSUFFICIENT_BALANCE`, `SAME_ACCOUNT_TRANSFER` |
| 500 | `INTERNAL_ERROR` (internal details are never leaked) |

---

## How data integrity is guaranteed

| Requirement | Mechanism |
|---|---|
| **Atomicity** | Each write operation runs in one Postgres transaction. Inserting the entry and updating the balance commit together, or roll back together on any error. |
| **No overdraft, even under concurrency** | The account row is locked (`SELECT … FOR UPDATE`) before reading or changing its balance, so concurrent writes to one account are serialised. As a second guard, the balance update is conditional (`WHERE balance + delta >= 0`) and there is a `CHECK (balance >= 0)` constraint. |
| **Duplicate `reference_id`** | Checked under the account lock, and backed by a `UNIQUE (account_id, reference_id)` constraint. |
| **Immutability** | The API has no update or delete endpoints. A DB trigger also rejects `UPDATE`/`DELETE` on `transactions`, even from direct SQL. |
| **Valid data in the DB** | `CHECK` constraints on `type`, `amount > 0`, non-blank `name`/`reference_id`, `NUMERIC(18,2)` for money (never float), and an FK from transactions to accounts with `ON DELETE RESTRICT`. |
| **Deadlock-free transfers** | Both account rows are locked in a fixed order (by id), so an A→B transfer and a B→A transfer cannot deadlock. |
| **Balance = sum of transactions** | The balance changes only inside the same transaction that inserts a ledger entry. You can verify it with `SELECT sum(CASE type WHEN 'credit' THEN amount ELSE -amount END) FROM transactions WHERE account_id = …`. |

---

## Assumptions

1. **Currency:** single currency, 2 decimal places. Amounts with more than 2 decimals are rejected, not rounded. The maximum amount fits `NUMERIC(18,2)`.
2. **Duplicate → 409 Conflict.** The response includes the original transaction, so clients can treat the 409 as "already done" when retrying safely. A resubmission is a duplicate based only on `reference_id`, even if the amount or type differs.
3. **Duplicate check comes before the balance check.** Retrying an already-applied debit returns 409, not 422, even if the balance has since dropped.
4. **Business-rule failures use 422** (`INSUFFICIENT_BALANCE`, `SAME_ACCOUNT_TRANSFER`), with a distinct `code` so clients can tell them apart from validation errors. Validation errors, including malformed JSON and malformed UUIDs, also use 422, following FastAPI's convention.
5. **`type` is case-sensitive:** `"CREDIT"` is rejected.
6. **`description` is optional.** For transfers without one, it defaults to `"Transfer to <id>"` / `"Transfer from <id>"`.
7. **Strings are trimmed.** `name` and `reference_id` are trimmed and must not be blank.
8. **A transfer reuses its `reference_id` on both legs.** Because uniqueness is per account, this is allowed. It also makes a retried transfer idempotent (409 on the second attempt), and it fails if the destination already has an unrelated entry with that reference.
9. **No authentication or authorisation.** This is out of scope for the exercise.
10. **No account deletion or update endpoints**, since none were requested. The FK uses `RESTRICT`, so an account with transactions can never be removed.
11. **Pagination** is offset-based. A page past the end returns an empty list, not an error.
12. **Schema management:** an idempotent `schema.sql` runs on startup instead of a migration tool. With a real deployment pipeline, a migration tool such as Alembic would replace this.
