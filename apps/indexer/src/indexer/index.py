import os
import shutil
import stat
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from pgvector import Vector
from psycopg.rows import class_row

from chunking import commit_chunks, split_text
from db.rows import Repo
from embeddings import embed
from jobs import enqueue
from repogit import (
    Revision,
    changed_paths,
    clone,
    fetch,
    history,
    list_files,
    read_blob,
)

_DIMS = 1536
_EMBED_BATCH = 64
_DOC_SUFFIXES = frozenset({".adoc", ".md", ".mdx", ".rst", ".txt"})
_LANGUAGES = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".py": "python",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def run_job(
    conn: psycopg.Connection[Any],
    *,
    repo_id: UUID,
    job_type: str,
    payload: dict[str, Any],
    checkout: Path,
) -> None:
    repo = _repo(conn, repo_id)
    if job_type == "clone_repo":
        _clone_repo(conn, repo, checkout)
    elif job_type == "index_files":
        _index_files(conn, repo, checkout, payload)
    elif job_type == "index_commits":
        _index_commits(conn, repo, checkout, payload)
    elif job_type == "embed_chunks":
        _embed_chunks(conn, repo, payload)
    else:
        raise RuntimeError(f"unknown job type {job_type}")


def _clone_repo(conn: psycopg.Connection[Any], repo: Repo, checkout: Path) -> None:
    if (checkout / ".git").is_dir():
        tip = fetch(checkout)
    else:
        if checkout.exists():
            _remove(checkout)
        tip = clone(repo.source, checkout)
    conn.execute(
        """
        update repos
        set status = 'indexing', default_branch = %s
        where id = %s
        """,
        (tip.branch, repo.id),
    )
    enqueue(
        conn,
        repo_id=repo.id,
        job_type="index_files",
        payload={"commit": tip.commit, "previous": repo.last_indexed_commit},
    )


def _index_files(
    conn: psycopg.Connection[Any],
    repo: Repo,
    checkout: Path,
    payload: dict[str, Any],
) -> None:
    commit = str(payload["commit"])
    previous = payload.get("previous")
    blobs = list_files(checkout, commit)
    current = {blob.path: blob for blob in blobs}
    changed = None
    if isinstance(previous, str):
        changed = set(changed_paths(checkout, previous, commit))
    conn.execute(
        """
        delete from files
        where repo_id = %s
          and path <> all(%s::text[])
        """,
        (repo.id, list(current)),
    )
    for path, blob in current.items():
        if changed is not None and path not in changed:
            continue
        file_id = _upsert_file(conn, repo.id, path, blob.sha)
        conn.execute("delete from chunks where file_id = %s", (file_id,))
        kind = "doc" if Path(path).suffix.lower() in _DOC_SUFFIXES else "code"
        for chunk in split_text(read_blob(checkout, blob.sha), path=path, kind=kind):
            conn.execute(
                """
                insert into chunks (
                    repo_id, file_id, start_line, end_line, content, kind, content_hash
                )
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    repo.id,
                    file_id,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.content,
                    chunk.kind,
                    chunk.content_hash,
                ),
            )
    enqueue(
        conn,
        repo_id=repo.id,
        job_type="index_commits",
        payload={"commit": commit, "previous": previous},
    )


def _index_commits(
    conn: psycopg.Connection[Any],
    repo: Repo,
    checkout: Path,
    payload: dict[str, Any],
) -> None:
    commit = str(payload["commit"])
    previous = payload.get("previous")
    old = previous if isinstance(previous, str) else None
    for revision in history(checkout, old, commit):
        commit_id = _upsert_commit(conn, repo.id, revision)
        conn.execute("delete from chunks where commit_id = %s", (commit_id,))
        for chunk in commit_chunks(revision.message, revision.diff):
            conn.execute(
                """
                insert into chunks (repo_id, commit_id, content, kind, content_hash)
                values (%s, %s, %s, 'commit', %s)
                """,
                (repo.id, commit_id, chunk.content, chunk.content_hash),
            )
    enqueue(
        conn,
        repo_id=repo.id,
        job_type="embed_chunks",
        payload={"commit": commit},
    )


def _embed_chunks(
    conn: psycopg.Connection[Any], repo: Repo, payload: dict[str, Any]
) -> None:
    commit = str(payload["commit"])
    model = _embedding_model()
    missing = conn.execute(
        """
        select id, content_hash, content
        from chunks
        where repo_id = %s
          and not exists (
              select 1
              from chunk_embeddings
              where chunk_embeddings.chunk_id = chunks.id
                and chunk_embeddings.model = %s
          )
        order by id
        """,
        (repo.id, model),
    ).fetchall()
    by_hash: dict[str, list[UUID]] = {}
    texts: dict[str, str] = {}
    for chunk_id, content_hash, content in missing:
        if not isinstance(chunk_id, UUID) or not isinstance(content_hash, str):
            raise TypeError("chunk row has the wrong shape")
        if not isinstance(content, str):
            raise TypeError("chunk row has the wrong shape")
        by_hash.setdefault(content_hash, []).append(chunk_id)
        texts.setdefault(content_hash, content)
    known = _known_vectors(conn, model, list(texts))
    _store_vectors(conn, model, known, by_hash)
    conn.commit()
    pending = [
        (content_hash, texts[content_hash])
        for content_hash in texts
        if content_hash not in known
    ]
    for start in range(0, len(pending), _EMBED_BATCH):
        embedded = embed(
            pending[start : start + _EMBED_BATCH], task="RETRIEVAL_DOCUMENT"
        )
        _store_vectors(
            conn,
            model,
            {item.content_hash: list(item.vector) for item in embedded},
            by_hash,
        )
        conn.commit()
    conn.execute(
        """
        update repos
        set status = 'indexed', last_indexed_commit = %s
        where id = %s
        """,
        (commit, repo.id),
    )
    conn.commit()


def _embedding_model() -> str:
    model = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-2").strip()
    for prefix in ("models/", "google/"):
        if model.startswith(prefix):
            model = model.removeprefix(prefix)
    if not model:
        raise RuntimeError("EMBEDDING_MODEL is required")
    return model


def _known_vectors(
    conn: psycopg.Connection[Any], model: str, hashes: list[str]
) -> dict[str, list[float]]:
    if not hashes:
        return {}
    found = conn.execute(
        """
        select distinct on (chunks.content_hash)
            chunks.content_hash, chunk_embeddings.embedding
        from chunk_embeddings
        join chunks on chunks.id = chunk_embeddings.chunk_id
        where chunk_embeddings.model = %s
          and chunks.content_hash = any(%s)
        order by chunks.content_hash
        """,
        (model, hashes),
    ).fetchall()
    vectors: dict[str, list[float]] = {}
    for content_hash, embedding in found:
        if not isinstance(content_hash, str):
            raise TypeError("stored embedding has the wrong shape")
        values = [float(item) for item in embedding]
        if len(values) == _DIMS:
            vectors[content_hash] = values
    return vectors


def _store_vectors(
    conn: psycopg.Connection[Any],
    model: str,
    vectors: dict[str, list[float]],
    by_hash: dict[str, list[UUID]],
) -> None:
    rows = [
        (chunk_id, model, _DIMS, Vector(vector))
        for content_hash, vector in vectors.items()
        for chunk_id in by_hash[content_hash]
    ]
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into chunk_embeddings (chunk_id, model, dims, embedding)
            values (%s, %s, %s, %s)
            on conflict (chunk_id, model) do nothing
            """,
            rows,
        )


def _repo(conn: psycopg.Connection[Any], repo_id: UUID) -> Repo:
    with conn.cursor(row_factory=class_row(Repo)) as cur:
        cur.execute("select * from repos where id = %s", (repo_id,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"repo {repo_id} does not exist")
    return row


def _upsert_file(
    conn: psycopg.Connection[Any], repo_id: UUID, path: str, blob_sha: str
) -> UUID:
    row = conn.execute(
        """
        insert into files (repo_id, path, language, blob_sha)
        values (%s, %s, %s, %s)
        on conflict (repo_id, path) do update
        set language = excluded.language,
            blob_sha = excluded.blob_sha
        returning id
        """,
        (repo_id, path, _LANGUAGES.get(Path(path).suffix.lower()), blob_sha),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"file {path} was not stored")
    file_id = row[0]
    if not isinstance(file_id, UUID):
        raise TypeError(f"file {path} was not stored")
    return file_id


def _upsert_commit(
    conn: psycopg.Connection[Any], repo_id: UUID, revision: Revision
) -> UUID:
    row = conn.execute(
        """
        insert into commits (repo_id, sha, author, committed_at, message)
        values (%s, %s, %s, %s, %s)
        on conflict (repo_id, sha) do update
        set author = excluded.author,
            committed_at = excluded.committed_at,
            message = excluded.message
        returning id
        """,
        (
            repo_id,
            revision.sha,
            revision.author,
            revision.committed_at,
            revision.message,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"commit {revision.sha} was not stored")
    commit_id = row[0]
    if not isinstance(commit_id, UUID):
        raise TypeError(f"commit {revision.sha} was not stored")
    return commit_id


def _remove(path: Path) -> None:
    def unlock(function: Any, name: str, _exc: BaseException) -> None:
        Path(name).chmod(stat.S_IWRITE)
        function(name)

    shutil.rmtree(path, onexc=unlock)
