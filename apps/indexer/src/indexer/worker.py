import os
from pathlib import Path
from uuid import UUID

import psycopg
import structlog
from psycopg_pool import ConnectionPool

from db import open_pool
from indexer.env import load_env
from indexer.index import run_job
from jobs import claim, complete, fail, listen, reap

log = structlog.get_logger()


def main() -> None:
    load_env()
    database_url = os.environ["DATABASE_URL"]
    index_dir = Path(os.environ.get("INDEX_DIR", "data/repos"))
    index_dir.mkdir(parents=True, exist_ok=True)
    worker_id = f"indexer-{os.getpid()}"
    pool = open_pool(database_url)
    pool.open()
    try:
        with psycopg.connect(database_url, autocommit=True) as listen_conn:
            while True:
                listen(listen_conn)
                _drain(pool, worker_id, index_dir)
    finally:
        pool.close()


def _drain(pool: ConnectionPool, worker_id: str, index_dir: Path) -> None:
    while True:
        with pool.connection() as conn:
            reap(conn)
            job = claim(conn, worker_id=worker_id)
            conn.commit()
        if job is None:
            return
        checkout = index_dir / str(job.repo_id)
        payload = job.payload if isinstance(job.payload, dict) else {}
        try:
            with pool.connection() as conn:
                run_job(
                    conn,
                    repo_id=job.repo_id,
                    job_type=job.type,
                    payload=payload,
                    checkout=checkout,
                )
                complete(conn, job.id)
                conn.commit()
            log.info("job.done", job_id=str(job.id), job_type=job.type)
        except Exception as exc:  # noqa: BLE001
            _record_failure(pool, job.id, job.type, exc)


def _record_failure(
    pool: ConnectionPool, job_id: UUID, job_type: str, exc: Exception
) -> None:
    log.info("job.failed", job_id=str(job_id), job_type=job_type, error=str(exc))
    try:
        with pool.connection() as conn:
            fail(conn, job_id, str(exc)[:4000])
            conn.commit()
    except Exception as record_exc:  # noqa: BLE001
        log.info(
            "job.fail_unrecorded",
            job_id=str(job_id),
            error=str(record_exc),
        )
