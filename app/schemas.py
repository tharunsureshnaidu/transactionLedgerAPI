from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Money: positive, at most 2 decimal places, fits NUMERIC(18, 2).
Amount = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
TxType = Literal["credit", "debit"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ReferenceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None


class Request(BaseModel):
    # Unknown fields are rejected, so a client cannot sneak in e.g. "balance".
    model_config = ConfigDict(extra="forbid")


class AccountCreate(Request):
    name: Name


class TransactionCreate(Request):
    type: TxType
    amount: Amount
    reference_id: ReferenceId
    description: Description = None


class TransferCreate(Request):
    from_account_id: UUID
    to_account_id: UUID
    amount: Amount
    reference_id: ReferenceId
    description: Description = None


class Account(BaseModel):
    id: UUID
    name: str
    balance: Decimal
    created_at: datetime


class Balance(BaseModel):
    account_id: UUID
    balance: Decimal


class Transaction(BaseModel):
    id: UUID
    account_id: UUID
    type: TxType
    amount: Decimal
    description: str | None
    reference_id: str
    created_at: datetime


class Pagination(BaseModel):
    page: int
    limit: int
    total_records: int
    total_pages: int


class TransactionPage(BaseModel):
    data: list[Transaction]
    pagination: Pagination


class Transfer(BaseModel):
    reference_id: str
    amount: Decimal
    debit: Transaction
    credit: Transaction
