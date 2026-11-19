import shutil
import stat
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import class_row

from chunking import commit_chunks, split_text
from db.rows import Repo
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
    conn.execute(
        """
        update repos
        set status = 'indexed', last_indexed_commit = %s
        where id = %s
        """,
        (commit, repo.id),
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
