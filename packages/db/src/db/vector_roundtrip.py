import os
import sys
from uuid import UUID, uuid4

import psycopg
from pgvector import Vector
from psycopg.rows import class_row

from db.migrate import apply
from db.pool import open_pool
from db.rows import ChunkEmbedding


def _insert_sample(
    conn: psycopg.Connection,
    source: str,
    embedding: Vector,
) -> tuple[UUID, UUID]:
    with conn.cursor() as cur:
        cur.execute(
            "insert into repos (source) values (%s) returning id",
            (source,),
        )
        repo = cur.fetchone()
        if repo is None:
            raise RuntimeError("repo insert returned no row")
        cur.execute(
            """
            insert into files (repo_id, path, blob_sha)
            values (%s, %s, %s)
            returning id
            """,
            (repo[0], "a.py", "abc"),
        )
        file = cur.fetchone()
        if file is None:
            raise RuntimeError("file insert returned no row")
        cur.execute(
            """
            insert into chunks (
                repo_id, file_id, start_line, end_line, content, kind, content_hash
            )
            values (%s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (repo[0], file[0], 1, 2, "return 1", "code", "a" * 64),
        )
        chunk = cur.fetchone()
        if chunk is None:
            raise RuntimeError("chunk insert returned no row")
        cur.execute(
            """
            insert into chunk_embeddings (chunk_id, model, dims, embedding)
            values (%s, %s, %s, %s)
            """,
            (chunk[0], "text-embedding-3-small", 1536, embedding),
        )
    return repo[0], chunk[0]


def roundtrip(database_url: str) -> ChunkEmbedding:
    apply(database_url)
    source = f"vector-roundtrip-{uuid4()}"
    expected = Vector([0.25] * 1536)
    with open_pool(database_url) as pool, pool.connection() as conn:
        repo_id, chunk_id = _insert_sample(conn, source, expected)
        with conn.cursor(row_factory=class_row(ChunkEmbedding)) as cur:
            cur.execute(
                """
                select chunk_id, model, dims, embedding
                from chunk_embeddings
                where chunk_id = %s
                """,
                (chunk_id,),
            )
            loaded = cur.fetchone()
        if loaded is None:
            raise RuntimeError("embedding select returned no row")
        with conn.cursor() as cur:
            cur.execute("delete from repos where id = %s", (repo_id,))
    if loaded.embedding != expected:
        raise RuntimeError("stored vector did not match")
    return loaded


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        raise SystemExit("DATABASE_URL is not set")
    row = roundtrip(database_url)
    print(f"{row.model} {row.dims}")


if __name__ == "__main__":
    main()
    sys.exit(0)
