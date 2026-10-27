"""Ledger operations. Each public function runs in one DB transaction:
any ApiError raised mid-way rolls back everything written so far."""
import math
from decimal import Decimal
from uuid import UUID

from psycopg import Cursor

from .db import pool
from .errors import ApiError, account_not_found
from .schemas import Transaction

TX_COLUMNS = "id, account_id, type, amount, description, reference_id, created_at"


def create_account(name: str) -> dict:
    with pool.connection() as conn:
        return conn.execute(
            "INSERT INTO accounts (name) VALUES (%s) RETURNING id, name, balance, created_at", (name,)
        ).fetchone()


def get_balance(account_id: UUID) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id AS account_id, balance FROM accounts WHERE id = %s", (account_id,)
        ).fetchone()
    if not row:
        raise account_not_found(account_id)
    return row


def add_transaction(account_id: UUID, type: str, amount: Decimal, reference_id: str, description: str | None) -> dict:
    with pool.connection() as conn, conn.cursor() as cur:
        _lock_accounts(cur, account_id)
        return _post(cur, account_id, type, amount, reference_id, description)


def transfer(from_id: UUID, to_id: UUID, amount: Decimal, reference_id: str, description: str | None) -> dict:
    if from_id == to_id:
        raise ApiError(422, "SAME_ACCOUNT_TRANSFER", "Source and destination accounts must differ")
    with pool.connection() as conn, conn.cursor() as cur:
        _lock_accounts(cur, from_id, to_id)
        debit = _post(cur, from_id, "debit", amount, reference_id, description or f"Transfer to {to_id}")
        credit = _post(cur, to_id, "credit", amount, reference_id, description or f"Transfer from {from_id}")
    return {"reference_id": reference_id, "amount": amount, "debit": debit, "credit": credit}


def list_transactions(account_id: UUID, type: str | None, page: int, limit: int) -> dict:
    where = "account_id = %(acc)s AND (%(type)s::text IS NULL OR type = %(type)s)"
    params = {"acc": account_id, "type": type, "limit": limit, "offset": (page - 1) * limit}
    with pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,)).fetchone():
            raise account_not_found(account_id)
        total = conn.execute(f"SELECT count(*) AS n FROM transactions WHERE {where}", params).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {TX_COLUMNS} FROM transactions WHERE {where} "
            "ORDER BY created_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
            params,
        ).fetchall()
    return {
        "data": rows,
        "pagination": {
            "page": page,
            "limit": limit,
            "total_records": total,
            "total_pages": math.ceil(total / limit),
        },
    }


def _lock_accounts(cur: Cursor, *account_ids: UUID) -> None:
    """Row-lock accounts for the rest of the DB transaction. This serialises every
    balance change per account. Locks are taken in id order so two opposite
    transfers cannot deadlock."""
    for account_id in sorted(account_ids, key=str):
        if not cur.execute("SELECT 1 FROM accounts WHERE id = %s FOR UPDATE", (account_id,)).fetchone():
            raise account_not_found(account_id)


def _post(cur: Cursor, account_id: UUID, type: str, amount: Decimal, reference_id: str, description: str | None) -> dict:
    """Record one ledger entry and apply it to the balance. The caller must hold the account lock."""
    existing = cur.execute(
        f"SELECT {TX_COLUMNS} FROM transactions WHERE account_id = %s AND reference_id = %s",
        (account_id, reference_id),
    ).fetchone()
    if existing:
        raise ApiError(
            409, "DUPLICATE_TRANSACTION",
            f"Transaction with reference_id '{reference_id}' already exists for account {account_id}",
            {"transaction": Transaction.model_validate(existing).model_dump(mode="json")},
        )

    delta = amount if type == "credit" else -amount
    updated = cur.execute(
        "UPDATE accounts SET balance = balance + %s WHERE id = %s AND balance + %s >= 0 RETURNING balance",
        (delta, account_id, delta),
    ).fetchone()
    if not updated:
        balance = cur.execute("SELECT balance FROM accounts WHERE id = %s", (account_id,)).fetchone()["balance"]
        raise ApiError(
            422, "INSUFFICIENT_BALANCE", f"Insufficient balance in account {account_id}",
            {"balance": f"{balance:.2f}", "requested": f"{amount:.2f}"},
        )

    return cur.execute(
        f"INSERT INTO transactions (account_id, type, amount, description, reference_id) "
        f"VALUES (%s, %s, %s, %s, %s) RETURNING {TX_COLUMNS}",
        (account_id, type, amount, description, reference_id),
    ).fetchone()
