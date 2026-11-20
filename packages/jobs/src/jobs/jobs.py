from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from db.rows import Job


def enqueue(
    conn: psycopg.Connection[Any],
    *,
    repo_id: UUID,
    job_type: str,
    payload: dict[str, Any] | None = None,
) -> Job:
    with conn.cursor(row_factory=class_row(Job)) as cur:
        cur.execute(
            """
            insert into jobs (repo_id, type, payload)
            values (%s, %s, %s)
            returning *
            """,
            (repo_id, job_type, Jsonb({} if payload is None else payload)),
        )
        job = cur.fetchone()
        cur.execute("notify jobs")
    if job is None:
        raise RuntimeError("enqueue returned no row")
    return job


def claim(conn: psycopg.Connection[Any], *, worker_id: str) -> Job | None:
    with conn.cursor(row_factory=class_row(Job)) as cur:
        cur.execute(
            """
            update jobs
            set status = 'running',
                locked_at = now(),
                locked_by = %s,
                attempts = attempts + 1,
                error = null
            where id = (
                select id
                from jobs
                where status = 'pending'
                  and run_after <= now()
                order by run_after, id
                for update skip locked
                limit 1
            )
            returning *
            """,
            (worker_id,),
        )
        return cur.fetchone()


def complete(conn: psycopg.Connection[Any], job_id: UUID) -> None:
    cur = conn.execute(
        """
        update jobs
        set status = 'succeeded',
            locked_at = null,
            locked_by = null,
            error = null
        where id = %s
          and status = 'running'
        """,
        (job_id,),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"job {job_id} is not running")


def fail(
    conn: psycopg.Connection[Any],
    job_id: UUID,
    error: str,
    *,
    max_attempts: int = 5,
    base_delay: int = 2,
) -> None:
    cur = conn.execute(
        """
        update jobs
        set error = %s,
            locked_at = null,
            locked_by = null,
            status = case
                when attempts >= %s then 'failed'
                else 'pending'
            end,
            run_after = case
                when attempts >= %s then run_after
                else now() + make_interval(secs => (%s * power(2.0, attempts))::float8)
            end
        where id = %s
          and status = 'running'
        """,
        (error, max_attempts, max_attempts, base_delay, job_id),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"job {job_id} is not running")


def reap(conn: psycopg.Connection[Any], *, stale_after: int = 900) -> int:
    cur = conn.execute(
        """
        update jobs
        set status = 'pending',
            locked_at = null,
            locked_by = null,
            run_after = now()
        where status = 'running'
          and locked_at < now() - make_interval(secs => %s)
        """,
        (stale_after,),
    )
    return cur.rowcount


def listen(conn: psycopg.Connection[Any], *, timeout: float = 5) -> None:
    if not conn.autocommit:
        raise RuntimeError("listen requires autocommit")
    conn.execute("listen jobs")
    for _ in conn.notifies(timeout=timeout, stop_after=1):
        return
