from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from api.chat import events
from db import open_pool
from jobs import enqueue


class RepoIn(BaseModel):
    source: str = Field(min_length=1)


class RepoOut(BaseModel):
    id: UUID
    source: str
    status: str
    default_branch: str | None
    last_indexed_commit: str | None


class JobOut(BaseModel):
    id: UUID
    repo_id: UUID
    type: str
    status: str
    attempts: int
    error: str | None


class ChatIn(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: UUID | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = open_pool()
    pool.open()
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(lifespan=_lifespan)


@app.get("/health")
def health(request: Request) -> dict[str, bool]:
    with request.app.state.pool.connection() as conn:
        conn.execute("select 1")
    return {"ok": True}


@app.post("/repos", status_code=201)
def create_repo(body: RepoIn, request: Request) -> RepoOut:
    with request.app.state.pool.connection() as conn:
        try:
            row = conn.execute(
                """
                insert into repos (source, status)
                values (%s, 'pending')
                returning *
                """,
                (body.source,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=500, detail="repo was not stored")
            repo = _repo_out(row)
            enqueue(conn, repo_id=repo.id, job_type="clone_repo")
            conn.commit()
        except UniqueViolation:
            raise HTTPException(status_code=409, detail="repo already exists") from None
    return repo


@app.get("/repos/{repo_id}")
def get_repo(repo_id: UUID, request: Request) -> RepoOut:
    with request.app.state.pool.connection() as conn:
        row = conn.execute("select * from repos where id = %s", (repo_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="repo does not exist")
    return _repo_out(row)


@app.get("/jobs")
def list_jobs(
    request: Request, repo_id: Annotated[UUID | None, Query()] = None
) -> list[JobOut]:
    with request.app.state.pool.connection() as conn:
        rows = conn.execute(
            """
            select id, repo_id, type, status, attempts, error
            from jobs
            where %s::uuid is null or repo_id = %s
            order by created_at desc
            limit 100
            """,
            (repo_id, repo_id),
        ).fetchall()
    return [
        JobOut(
            id=row[0],
            repo_id=row[1],
            type=row[2],
            status=row[3],
            attempts=row[4],
            error=row[5],
        )
        for row in rows
    ]


@app.post("/repos/{repo_id}/chat")
def chat(repo_id: UUID, body: ChatIn, request: Request) -> EventSourceResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        row = conn.execute("select 1 from repos where id = %s", (repo_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="repo does not exist")

    def generate() -> Iterator[dict[str, str]]:
        with pool.connection() as conn:
            yield from events(conn, repo_id, body.message, body.conversation_id)

    return EventSourceResponse(generate())


def _repo_out(row: tuple[Any, ...]) -> RepoOut:
    return RepoOut(
        id=row[0],
        source=row[1],
        default_branch=row[2],
        status=row[3],
        last_indexed_commit=row[4],
    )
