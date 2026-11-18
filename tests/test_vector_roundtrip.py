import os

import pytest

from db.vector_roundtrip import roundtrip


def test_vector_roundtrip() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        pytest.skip("DATABASE_URL is not set")
    row = roundtrip(database_url)
    assert row.model == "text-embedding-3-small"
    assert row.dims == 1536
    assert row.embedding.to_list() == [0.25] * 1536
