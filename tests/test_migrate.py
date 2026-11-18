import os

import psycopg
import pytest

from db.migrate import apply


def test_migrate_applies_once() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        pytest.skip("DATABASE_URL is not set")
    apply(database_url)
    assert apply(database_url) == []
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "select filename from schema_migrations order by filename"
        ).fetchall()
    assert rows == [("001_init.sql",)]
