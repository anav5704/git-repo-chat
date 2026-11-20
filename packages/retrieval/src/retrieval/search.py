import os
from typing import Any
from uuid import UUID

import psycopg
from pgvector import Vector

from embeddings import embed
from retrieval.hit import Hit

_DIMS = 1536
_COLUMNS = """
    chunks.id,
    chunks.file_id,
    chunks.kind,
    chunks.content,
    chunks.start_line,
    chunks.end_line,
    files.path,
    commits.sha
"""
_FROM = """
from chunks
left join files on files.id = chunks.file_id
left join commits on commits.id = chunks.commit_id
"""


def symbol(
    conn: psycopg.Connection[Any],
    *,
    repo_id: UUID,
    query: str,
    limit: int = 20,
) -> tuple[Hit, ...]:
    query = query.strip()
    if not query:
        return ()
    rows = conn.execute(
        f"""
        select {_COLUMNS}, word_similarity(%s, chunks.content) as score
        {_FROM}
        where chunks.repo_id = %s
          and chunks.kind in ('code', 'doc')
          and word_similarity(%s, chunks.content) > 0.3
        order by score desc
        limit %s
        """,
        (query, repo_id, query, _limit(limit)),
    ).fetchall()
    return _hits(rows)


def full_text(
    conn: psycopg.Connection[Any],
    *,
    repo_id: UUID,
    query: str,
    limit: int = 20,
) -> tuple[Hit, ...]:
    query = query.strip()
    if not query:
        return ()
    rows = conn.execute(
        f"""
        select {_COLUMNS},
               ts_rank(chunks.content_tsv, plainto_tsquery('simple', %s)) as score
        {_FROM}
        where chunks.repo_id = %s
          and chunks.kind in ('code', 'doc')
          and chunks.content_tsv @@ plainto_tsquery('simple', %s)
        order by score desc
        limit %s
        """,
        (query, repo_id, query, _limit(limit)),
    ).fetchall()
    return _hits(rows)


def vector_search(
    conn: psycopg.Connection[Any],
    *,
    repo_id: UUID,
    text: str,
    limit: int = 20,
    exact: bool = False,
) -> tuple[Hit, ...]:
    text = text.strip()
    if not text:
        return ()
    found = embed([("query", text)], task="RETRIEVAL_QUERY")
    if not found:
        return ()
    vector = Vector(list(found[0].vector))
    order = (
        "chunk_embeddings.embedding <=> %s"
        if exact
        else "(chunk_embeddings.embedding::vector(1536)) <=> %s"
    )
    rows = conn.execute(
        f"""
        select {_COLUMNS},
               (chunk_embeddings.embedding <=> %s) as distance
        from chunk_embeddings
        join chunks on chunks.id = chunk_embeddings.chunk_id
        left join files on files.id = chunks.file_id
        left join commits on commits.id = chunks.commit_id
        where chunks.repo_id = %s
          and chunk_embeddings.model = %s
          and chunk_embeddings.dims = %s
          and chunks.kind in ('code', 'doc')
        order by {order}
        limit %s
        """,
        (vector, repo_id, _model(), _DIMS, vector, _limit(limit)),
    ).fetchall()
    return _hits(rows, distance=True)


def history(
    conn: psycopg.Connection[Any],
    *,
    repo_id: UUID,
    query: str,
    limit: int = 20,
) -> tuple[Hit, ...]:
    query = query.strip()
    if not query:
        return ()
    limit = _limit(limit)
    chunks = _hits(
        conn.execute(
            f"""
            select {_COLUMNS},
                   ts_rank(chunks.content_tsv, plainto_tsquery('simple', %s)) as score
            {_FROM}
            where chunks.repo_id = %s
              and chunks.kind = 'commit'
              and chunks.content_tsv @@ plainto_tsquery('simple', %s)
            order by score desc
            limit %s
            """,
            (query, repo_id, query, limit),
        ).fetchall()
    )
    covered = {hit.sha for hit in chunks if hit.sha}
    messages = conn.execute(
        """
        select sha, message,
               ts_rank(
                   to_tsvector('simple', message),
                   plainto_tsquery('simple', %s)
               ) as score
        from commits
        where repo_id = %s
          and to_tsvector('simple', message) @@ plainto_tsquery('simple', %s)
        order by score desc
        limit %s
        """,
        (query, repo_id, query, limit),
    ).fetchall()
    extra = [
        Hit(score=float(score), kind="commit", content=message, sha=sha)
        for sha, message, score in messages
        if sha not in covered
    ]
    return tuple(sorted((*chunks, *extra), key=lambda hit: hit.score, reverse=True))


def pack(
    conn: psycopg.Connection[Any],
    hits: tuple[Hit, ...] | list[Hit],
    *,
    budget: int = 3000,
) -> tuple[Hit, ...]:
    if budget < 1:
        raise ValueError("budget must be positive")
    packed: list[Hit] = []
    seen: set[UUID] = set()
    used = 0
    for hit in hits:
        for item in _around(conn, hit):
            if item.chunk_id is not None and item.chunk_id in seen:
                continue
            cost = max(1, (len(item.content) + 3) // 4)
            if packed and used + cost > budget:
                return tuple(packed)
            if item.chunk_id is not None:
                seen.add(item.chunk_id)
            packed.append(item)
            used += cost
    return tuple(packed)


def _around(conn: psycopg.Connection[Any], hit: Hit) -> tuple[Hit, ...]:
    if hit.file_id is None or hit.start_line is None or hit.end_line is None:
        return (hit,)
    rows = conn.execute(
        f"""
        select {_COLUMNS}, %s as score
        {_FROM}
        where chunks.file_id = %s
          and chunks.start_line <= %s
          and chunks.end_line >= %s
        order by chunks.start_line
        """,
        (hit.score, hit.file_id, hit.end_line + 80, hit.start_line - 80),
    ).fetchall()
    found = _hits(rows)
    if hit.chunk_id is None or any(item.chunk_id == hit.chunk_id for item in found):
        return found
    return (hit, *found)


def _hits(rows: list[Any], *, distance: bool = False) -> tuple[Hit, ...]:
    hits: list[Hit] = []
    for row in rows:
        raw = float(row[8])
        hits.append(
            Hit(
                score=1.0 - raw if distance else raw,
                kind=str(row[2]),
                content=str(row[3]),
                chunk_id=row[0],
                file_id=row[1],
                path=row[6],
                start_line=row[4],
                end_line=row[5],
                sha=row[7],
            )
        )
    return tuple(hits)


def _model() -> str:
    model = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-2").strip()
    for prefix in ("models/", "google/"):
        if model.startswith(prefix):
            model = model.removeprefix(prefix)
    if not model:
        raise RuntimeError("EMBEDDING_MODEL is required")
    return model


def _limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be positive")
    return limit
