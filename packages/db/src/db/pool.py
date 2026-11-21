import os

from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool


def open_pool(database_url: str | None = None) -> ConnectionPool:
    if database_url is None:
        database_url = os.environ["DATABASE_URL"]
    return ConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=10,
        configure=register_vector,
        open=False,
    )
