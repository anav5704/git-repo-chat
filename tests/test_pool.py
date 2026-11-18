import os

import pytest
from pgvector import Vector

from db.migrate import apply
from db.pool import open_pool


def test_pool_loads_vectors() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        pytest.skip("DATABASE_URL is not set")
    apply(database_url)
    expected = Vector([1.0, 2.0, 3.0])
    with (
        open_pool(database_url) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        cur.execute("select %s::vector", (expected,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == expected
