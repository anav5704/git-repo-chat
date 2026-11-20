import hashlib
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
import psycopg
import trafilatura
from pgvector import Vector

from chunking import split_text
from embeddings import embed

_TAVILY = "https://api.tavily.com/search"
_DIMS = 1536


@dataclass(frozen=True, slots=True)
class WebHit:
    score: float
    url: str
    content: str
    start_line: int | None = None
    end_line: int | None = None


def search(
    conn: psycopg.Connection[Any], query: str, *, limit: int = 5
) -> tuple[WebHit, ...]:
    query = query.strip()
    if not query:
        raise ValueError("query is empty")
    if limit < 1:
        raise ValueError("limit must be positive")
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        urls = [url for url in _tavily(client, query, min(limit, 10)) if _allowed(url)]
        hits: list[WebHit] = []
        for rank, url in enumerate(urls, start=1):
            hits.extend(_page(conn, client, url, 1.0 / rank))
    return tuple(hits)


def _tavily(client: httpx.Client, query: str, count: int) -> tuple[str, ...]:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TAVILY_API_KEY is required")
    response = client.post(
        _TAVILY,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "query": query,
            "search_depth": "basic",
            "max_results": count,
            "include_answer": False,
            "include_raw_content": False,
            "auto_parameters": False,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"web search failed: {response.status_code} {response.text[:500]}"
        )
    payload = response.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return ()
    urls: list[str] = []
    for item in results:
        url = item.get("url") if isinstance(item, dict) else None
        if isinstance(url, str) and url:
            urls.append(url)
    return tuple(urls)


def _page(
    conn: psycopg.Connection[Any], client: httpx.Client, url: str, score: float
) -> tuple[WebHit, ...]:
    response = client.get(url, headers={"User-Agent": "repo-chat"})
    if response.status_code != 200 or not _allowed(str(response.url)):
        return ()
    text = trafilatura.extract(response.text, url=str(response.url))
    if not isinstance(text, str) or not text.strip():
        return ()
    return _remember(conn, url, text, score)


def _remember(
    conn: psycopg.Connection[Any], url: str, text: str, score: float
) -> tuple[WebHit, ...]:
    digest = hashlib.sha256(text.encode()).hexdigest()
    row = conn.execute(
        "select id, content_hash from web_pages where url = %s",
        (url,),
    ).fetchone()
    if row is not None and row[1] == digest:
        conn.execute(
            "update web_pages set fetched_at = now() where id = %s",
            (row[0],),
        )
        conn.commit()
        return _load(conn, _id(row), url, score)
    page_id = _id(
        conn.execute(
            """
        insert into web_pages (url, domain, content_hash)
        values (%s, %s, %s)
        on conflict (url) do update
        set domain = excluded.domain,
            content_hash = excluded.content_hash,
            fetched_at = now()
        returning id
        """,
            (url, _domain(url), digest),
        ).fetchone()
    )
    pieces = [
        chunk
        for chunk in split_text(text, kind="doc")
        if chunk.start_line is not None and chunk.end_line is not None
    ]
    known = _known(conn, [chunk.content_hash for chunk in pieces])
    copied = _known_vectors(conn, list(known))
    conn.execute("delete from web_chunks where page_id = %s", (page_id,))
    stored: list[tuple[UUID, str]] = []
    for chunk in pieces:
        stored.append(
            (
                _id(
                    conn.execute(
                        """
                        insert into web_chunks (
                            page_id, start_line, end_line, content, content_hash
                        )
                        values (%s, %s, %s, %s, %s)
                        returning id
                        """,
                        (
                            page_id,
                            chunk.start_line,
                            chunk.end_line,
                            chunk.content,
                            chunk.content_hash,
                        ),
                    ).fetchone()
                ),
                chunk.content_hash,
            )
        )
    fresh = embed(
        [(chunk.content_hash, chunk.content) for chunk in pieces],
        task="RETRIEVAL_DOCUMENT",
        already=known,
    )
    vectors = {item.content_hash: list(item.vector) for item in fresh}
    vectors.update(copied)
    model = _model()
    rows = []
    for chunk_id, content_hash in stored:
        vector = vectors.get(content_hash)
        if vector is None:
            continue
        rows.append((chunk_id, model, len(vector), Vector(vector)))
    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into web_chunk_embeddings (chunk_id, model, dims, embedding)
                values (%s, %s, %s, %s)
                on conflict (chunk_id, model) do nothing
                """,
                rows,
            )
    conn.commit()
    return _load(conn, page_id, url, score)


def _known(conn: psycopg.Connection[Any], hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    found = conn.execute(
        """
        select distinct web_chunks.content_hash
        from web_chunk_embeddings
        join web_chunks on web_chunks.id = web_chunk_embeddings.chunk_id
        where web_chunk_embeddings.model = %s
          and web_chunks.content_hash = any(%s)
        """,
        (_model(), hashes),
    ).fetchall()
    return {item[0] for item in found}


def _known_vectors(
    conn: psycopg.Connection[Any], hashes: list[str]
) -> dict[str, list[float]]:
    if not hashes:
        return {}
    found = conn.execute(
        """
        select distinct on (web_chunks.content_hash)
            web_chunks.content_hash, web_chunk_embeddings.embedding
        from web_chunk_embeddings
        join web_chunks on web_chunks.id = web_chunk_embeddings.chunk_id
        where web_chunk_embeddings.model = %s
          and web_chunks.content_hash = any(%s)
        order by web_chunks.content_hash
        """,
        (_model(), hashes),
    ).fetchall()
    vectors: dict[str, list[float]] = {}
    for content_hash, embedding in found:
        values = [float(item) for item in embedding]
        if len(values) == _DIMS:
            vectors[content_hash] = values
    return vectors


def _load(
    conn: psycopg.Connection[Any], page_id: UUID, url: str, score: float
) -> tuple[WebHit, ...]:
    rows = conn.execute(
        """
        select content, start_line, end_line
        from web_chunks
        where page_id = %s
        order by start_line
        """,
        (page_id,),
    ).fetchall()
    return tuple(
        WebHit(
            score=score,
            url=url,
            content=content,
            start_line=start_line,
            end_line=end_line,
        )
        for content, start_line, end_line in rows
    )


def _id(row: tuple[Any, ...] | None) -> UUID:
    if row is None:
        raise RuntimeError("web row was not stored")
    row_id = row[0]
    if not isinstance(row_id, UUID):
        raise TypeError("web row was not stored")
    return row_id


def _allowed(url: str) -> bool:
    host = _domain(url)
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in _allowlist())


def _allowlist() -> tuple[str, ...]:
    raw = os.environ.get("WEB_ALLOWLIST", "")
    return tuple(
        item.strip().lower().removeprefix("www.")
        for item in raw.split(",")
        if item.strip()
    )


def _domain(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.lower().removeprefix("www.")


def _model() -> str:
    model = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-2").strip()
    for prefix in ("models/", "google/"):
        if model.startswith(prefix):
            model = model.removeprefix(prefix)
    if not model:
        raise RuntimeError("EMBEDDING_MODEL is required")
    return model
