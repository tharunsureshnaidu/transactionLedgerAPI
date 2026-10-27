from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, Query

from . import errors, ledger
from .db import init_db, pool
from .schemas import (
    Account, AccountCreate, Balance, Transaction, TransactionCreate, TransactionPage,
    Transfer, TransferCreate, TxType,
)

ERRORS = {
    404: {"description": "Account not found"},
    409: {"description": "Duplicate reference_id for this account"},
    422: {"description": "Validation error, or insufficient balance"},
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield
    pool.close()


app = FastAPI(title="Transaction Ledger API", version="1.0.0", lifespan=lifespan)
errors.register(app)


@app.post("/accounts", status_code=201, response_model=Account, responses={422: ERRORS[422]})
def create_account(body: AccountCreate):
    return ledger.create_account(body.name)


@app.get("/accounts/{account_id}/balance", response_model=Balance, responses={404: ERRORS[404]})
def get_balance(account_id: UUID):
    return ledger.get_balance(account_id)


@app.post("/accounts/{account_id}/transactions", status_code=201, response_model=Transaction, responses=ERRORS)
def add_transaction(account_id: UUID, body: TransactionCreate):
    return ledger.add_transaction(account_id, body.type, body.amount, body.reference_id, body.description)


@app.get("/accounts/{account_id}/transactions", response_model=TransactionPage, responses={404: ERRORS[404], 422: ERRORS[422]})
def list_transactions(
    account_id: UUID,
    type: TxType | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    return ledger.list_transactions(account_id, type, page, limit)


@app.post("/transfers", status_code=201, response_model=Transfer, responses=ERRORS)
def create_transfer(body: TransferCreate):
    return ledger.transfer(body.from_account_id, body.to_account_id, body.amount, body.reference_id, body.description)
