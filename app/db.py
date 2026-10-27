import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ledger:ledger@localhost:5433/ledger")
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"

pool = ConnectionPool(DATABASE_URL, kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"}, max_size=10, open=False)


def init_db() -> None:
    pool.open(wait=True)
    with pool.connection() as conn:
        conn.execute(SCHEMA.read_text())
