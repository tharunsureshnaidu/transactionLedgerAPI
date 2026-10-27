"""End-to-end tests against a real Postgres (DATABASE_URL, default: local ledger_test DB)."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("DATABASE_URL", "postgresql://ledger:ledger@localhost:5433/ledger_test")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import ledger  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # runs lifespan -> schema init
        yield c


def new_account(client, name="Alice"):
    r = client.post("/accounts", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def tx(client, acc, type, amount, ref=None, **extra):
    body = {"type": type, "amount": amount, "reference_id": ref or uuid.uuid4().hex, **extra}
    return client.post(f"/accounts/{acc}/transactions", json=body)


def balance(client, acc):
    return client.get(f"/accounts/{acc}/balance").json()["balance"]


# --- accounts ---------------------------------------------------------------

def test_create_account(client):
    r = client.post("/accounts", json={"name": "  Bob  "})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Bob" and body["balance"] == "0.00"
    assert uuid.UUID(body["id"]) and body["created_at"]


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}, {"name": "X", "balance": 500}, None])
def test_create_account_invalid(client, body):
    r = client.post("/accounts", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_balance_not_found_and_malformed_id(client):
    r = client.get(f"/accounts/{uuid.uuid4()}/balance")
    assert r.status_code == 404 and r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"
    assert client.get("/accounts/not-a-uuid/balance").status_code == 422


def test_malformed_json(client):
    r = client.post("/accounts", content="{bad", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


# --- transactions -----------------------------------------------------------

def test_balance_consistency(client):
    acc = new_account(client)
    for t, a in [("credit", 100), ("credit", 50), ("debit", 30), ("debit", 20)]:
        assert tx(client, acc, t, a).status_code == 201
    assert client.get(f"/accounts/{acc}/balance").json() == {"account_id": acc, "balance": "100.00"}


def test_insufficient_balance_rejected(client):
    acc = new_account(client)
    tx(client, acc, "credit", 100)
    r = tx(client, acc, "debit", 150)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert balance(client, acc) == "100.00"
    assert client.get(f"/accounts/{acc}/transactions").json()["pagination"]["total_records"] == 1


def test_debit_to_exactly_zero(client):
    acc = new_account(client)
    tx(client, acc, "credit", "10.50")
    assert tx(client, acc, "debit", "10.50").status_code == 201
    assert balance(client, acc) == "0.00"


def test_duplicate_reference(client):
    a, b = new_account(client), new_account(client)
    first = tx(client, a, "credit", 100, ref="REF-1")
    assert first.status_code == 201
    dup = tx(client, a, "credit", 999, ref="REF-1")
    assert dup.status_code == 409
    err = dup.json()["error"]
    assert err["code"] == "DUPLICATE_TRANSACTION"
    assert err["details"]["transaction"] == first.json()
    assert balance(client, a) == "100.00"
    # same reference on a different account is fine
    assert tx(client, b, "credit", 5, ref="REF-1").status_code == 201


@pytest.mark.parametrize("type_, amount", [
    ("refund", 10), ("CREDIT", 10), ("credit", 0), ("credit", -5),
    ("credit", "1.234"), ("credit", "abc"), ("credit", None),
])
def test_invalid_transaction(client, type_, amount):
    acc = new_account(client)
    assert tx(client, acc, type_, amount).status_code == 422
    assert balance(client, acc) == "0.00"


@pytest.mark.parametrize("body", [
    {"type": "credit", "amount": 10},                                    # no reference_id
    {"type": "credit", "amount": 10, "reference_id": "  "},              # blank reference_id
    {"type": "credit", "amount": 10, "reference_id": "r", "id": "x"},    # unknown field
])
def test_invalid_transaction_body(client, body):
    acc = new_account(client)
    assert client.post(f"/accounts/{acc}/transactions", json=body).status_code == 422


def test_transaction_unknown_account(client):
    r = tx(client, uuid.uuid4(), "credit", 10)
    assert r.status_code == 404 and r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_transactions_immutable(client):
    acc = new_account(client)
    t = tx(client, acc, "credit", 10).json()
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)(f"/accounts/{acc}/transactions/{t['id']}").status_code in (404, 405)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for sql in ("UPDATE transactions SET amount = 1 WHERE id = %s", "DELETE FROM transactions WHERE id = %s"):
            with pytest.raises(psycopg.errors.RaiseException):
                conn.execute(sql, (t["id"],))
            conn.rollback()


# --- history ----------------------------------------------------------------

def test_history_filter_sort_paginate(client):
    acc = new_account(client)
    for i in range(5):
        tx(client, acc, "credit", 10, ref=f"c{i}")
    for i in range(2):
        tx(client, acc, "debit", 1, ref=f"d{i}")

    r = client.get(f"/accounts/{acc}/transactions", params={"page": 1, "limit": 3}).json()
    assert r["pagination"] == {"page": 1, "limit": 3, "total_records": 7, "total_pages": 3}
    assert [t["reference_id"] for t in r["data"]] == ["d1", "d0", "c4"]  # latest first

    last = client.get(f"/accounts/{acc}/transactions", params={"page": 3, "limit": 3}).json()
    assert [t["reference_id"] for t in last["data"]] == ["c0"]

    credits = client.get(f"/accounts/{acc}/transactions", params={"type": "credit"}).json()
    assert credits["pagination"]["total_records"] == 5
    assert {t["type"] for t in credits["data"]} == {"credit"}

    beyond = client.get(f"/accounts/{acc}/transactions", params={"page": 99}).json()
    assert beyond["data"] == [] and beyond["pagination"]["total_records"] == 7


@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"limit": 0}, {"limit": 101}, {"page": "x"}, {"type": "refund"}])
def test_history_invalid_params(client, params):
    acc = new_account(client)
    r = client.get(f"/accounts/{acc}/transactions", params=params)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_history_unknown_account(client):
    assert client.get(f"/accounts/{uuid.uuid4()}/transactions").status_code == 404


def test_history_empty_account(client):
    r = client.get(f"/accounts/{new_account(client)}/transactions").json()
    assert r == {"data": [], "pagination": {"page": 1, "limit": 20, "total_records": 0, "total_pages": 0}}


# --- transfers --------------------------------------------------------------

def transfer(client, src, dst, amount, ref=None):
    return client.post("/transfers", json={
        "from_account_id": str(src), "to_account_id": str(dst), "amount": amount,
        "reference_id": ref or uuid.uuid4().hex,
    })


def test_transfer(client):
    a, b = new_account(client, "A"), new_account(client, "B")
    tx(client, a, "credit", 100)
    tx(client, b, "credit", 50)
    r = transfer(client, a, b, 30)
    assert r.status_code == 201
    body = r.json()
    assert body["debit"]["account_id"] == a and body["credit"]["account_id"] == b
    assert (balance(client, a), balance(client, b)) == ("70.00", "80.00")


def test_transfer_insufficient_rolls_back(client):
    a, b = new_account(client), new_account(client)
    tx(client, a, "credit", 10)
    r = transfer(client, a, b, 30)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert (balance(client, a), balance(client, b)) == ("10.00", "0.00")


def test_transfer_unknown_destination_rolls_back(client):
    a = new_account(client)
    tx(client, a, "credit", 100)
    assert transfer(client, a, uuid.uuid4(), 30).status_code == 404
    assert balance(client, a) == "100.00"


def test_transfer_fails_on_credit_leg_rolls_back_debit(client):
    # Debit leg succeeds, credit leg hits a duplicate reference: the debit must be undone.
    a, b = new_account(client), new_account(client)
    tx(client, a, "credit", 100)
    tx(client, b, "credit", 1, ref="T-1")
    r = transfer(client, a, b, 30, ref="T-1")
    assert r.status_code == 409
    assert (balance(client, a), balance(client, b)) == ("100.00", "1.00")
    assert client.get(f"/accounts/{a}/transactions").json()["pagination"]["total_records"] == 1


def test_transfer_duplicate_and_same_account(client):
    a, b = new_account(client), new_account(client)
    tx(client, a, "credit", 100)
    assert transfer(client, a, b, 10, ref="T-9").status_code == 201
    assert transfer(client, a, b, 10, ref="T-9").status_code == 409
    assert (balance(client, a), balance(client, b)) == ("90.00", "10.00")
    r = transfer(client, a, a, 10)
    assert r.status_code == 422 and r.json()["error"]["code"] == "SAME_ACCOUNT_TRANSFER"


# --- concurrency ------------------------------------------------------------

def test_concurrent_debits_never_overdraw(client):
    acc = uuid.UUID(new_account(client))
    tx(client, acc, "credit", 100)

    def debit(i):
        try:
            ledger.add_transaction(acc, "debit", 10, f"d{i}", None)
            return True
        except ApiError:
            return False

    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(debit, range(25)))
    assert sum(results) == 10
    assert balance(client, acc) == "0.00"


def test_concurrent_duplicate_reference_applied_once(client):
    acc = uuid.UUID(new_account(client))

    def credit(_):
        try:
            ledger.add_transaction(acc, "credit", 10, "SAME", None)
            return True
        except ApiError:
            return False

    with ThreadPoolExecutor(10) as ex:
        assert sum(ex.map(credit, range(10))) == 1
    assert balance(client, acc) == "10.00"


def test_opposite_transfers_do_not_deadlock(client):
    a, b = uuid.UUID(new_account(client)), uuid.UUID(new_account(client))
    tx(client, a, "credit", 1000)
    tx(client, b, "credit", 1000)
    jobs = [(a, b) if i % 2 else (b, a) for i in range(40)]
    with ThreadPoolExecutor(10) as ex:
        list(ex.map(lambda p: ledger.transfer(p[0], p[1], 1, uuid.uuid4().hex, None), jobs))
    assert (balance(client, a), balance(client, b)) == ("1000.00", "1000.00")
