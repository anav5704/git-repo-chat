import json
from collections.abc import Iterator, Sequence
from typing import Any
from uuid import UUID

import psycopg
import structlog
from psycopg.types.json import Jsonb

from llm import Turn, stream
from retrieval import (
    Blurb,
    Hit,
    Rewrite,
    Route,
    full_text,
    fuse,
    history,
    pack,
    rerank,
    rewrite,
    route,
    score_floor,
    symbol,
    vector_search,
)
from web import WebHit, search

log = structlog.get_logger()

_ANSWER = (
    "Answer the question using the passages. "
    "Mention the file and line range, the commit, or the URL you used. "
    "If the passages do not contain the answer, say so."
)


def events(
    pool: Any,
    repo_id: UUID,
    question: str,
    conversation_id: UUID | None,
) -> Iterator[dict[str, str]]:
    with pool.connection() as conn:
        repo = _repo(conn, repo_id)
        if repo is None:
            yield _event("error", "repo does not exist")
            return
        conversation_id = _conversation(conn, repo_id, conversation_id)
        _message(conn, conversation_id, "user", question, [])
        conn.commit()
    yield _event("meta", {"conversation_id": str(conversation_id)})
    try:
        decision, stages, packed = _retrieve(pool, repo, question)
        citations = [_citation(hit) for hit in packed]
        yield _event(
            "trace",
            {
                "route": decision.model_dump(),
                "stages": stages,
                "hits": citations,
            },
        )
        with pool.connection() as conn:
            turns = _turns(conn, conversation_id, packed)
        parts: list[str] = []
        for token in stream(turns, system=_ANSWER):
            parts.append(token)
            yield _event("token", token)
        with pool.connection() as conn:
            _message(conn, conversation_id, "assistant", "".join(parts), citations)
            conn.commit()
        yield _event("citations", citations)
    except Exception as exc:  # noqa: BLE001
        log.info("chat.failed", repo_id=str(repo_id), error=str(exc))
        yield _event("error", str(exc)[:500])


def _retrieve(
    pool: Any, repo: tuple[Any, ...], question: str
) -> tuple[Route, list[str], tuple[Hit, ...]]:
    repo_id = repo[0]
    with pool.connection() as conn:
        blurb = _blurb(conn, repo)
    decision = route(question, blurb)
    stages: list[str] = []
    lists: list[tuple[Hit, ...]] = []
    if decision.symbol or decision.semantic or decision.history:
        plan = rewrite(question)
    else:
        plan = Rewrite(queries=[question], hyde="")
    for query in plan.queries:
        with pool.connection() as conn:
            if decision.symbol:
                lists.append(symbol(conn, repo_id=repo_id, query=query))
            if decision.semantic:
                lists.append(full_text(conn, repo_id=repo_id, query=query))
            if decision.history:
                lists.append(history(conn, repo_id=repo_id, query=query))
        if decision.semantic:
            with pool.connection() as conn:
                lists.append(vector_search(conn, repo_id=repo_id, text=query))
    if decision.symbol:
        stages.append("symbol")
    if decision.semantic:
        stages.append("semantic")
        if plan.hyde:
            with pool.connection() as conn:
                lists.append(vector_search(conn, repo_id=repo_id, text=plan.hyde))
    if decision.history:
        stages.append("history")
    repo_hits = fuse(lists)
    web_hits: tuple[Hit, ...] = ()
    if (
        decision.web
        or not repo_hits
        or max(hit.score for hit in repo_hits) < score_floor()
    ):
        query = decision.web_query.strip() or question
        with pool.connection() as conn:
            web_hits = tuple(_from_web(hit) for hit in search(conn, query))
        stages.append("web")
    fused = fuse([repo_hits, web_hits]) if web_hits else repo_hits
    ranked = rerank(question, fused) if len(fused) > 1 else fused
    with pool.connection() as conn:
        return decision, stages, pack(conn, ranked)


def _blurb(conn: psycopg.Connection[Any], repo: tuple[Any, ...]) -> Blurb:
    repo_id, source = repo[0], str(repo[1])
    languages = conn.execute(
        """
        select distinct language
        from files
        where repo_id = %s and language is not null
        order by language
        """,
        (repo_id,),
    ).fetchall()
    paths = conn.execute(
        """
        select distinct split_part(path, '/', 1)
        from files
        where repo_id = %s
        order by 1
        limit 40
        """,
        (repo_id,),
    ).fetchall()
    name = source.rstrip("/").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return Blurb(
        name=name or source,
        languages=", ".join(row[0] for row in languages),
        paths="\n".join(row[0] for row in paths),
    )


def _turns(
    conn: psycopg.Connection[Any], conversation_id: UUID, hits: Sequence[Hit]
) -> list[Turn]:
    rows = conn.execute(
        """
        select role, content
        from messages
        where conversation_id = %s
        order by created_at
        """,
        (conversation_id,),
    ).fetchall()
    turns = [
        Turn("assistant" if role == "assistant" else "user", content)
        for role, content in rows
        if role in {"user", "assistant"}
    ]
    if not turns:
        turns = [Turn("user", "")]
    passages = _passages(hits)
    last = turns[-1]
    body = last.content if not passages else f"{last.content}\n\nPassages:\n{passages}"
    turns[-1] = Turn("user", body)
    return turns


def _passages(hits: Sequence[Hit]) -> str:
    blocks: list[str] = []
    for index, hit in enumerate(hits, start=1):
        if hit.path and hit.start_line and hit.end_line:
            where = f"{hit.path}:{hit.start_line}-{hit.end_line}"
        elif hit.url:
            where = hit.url
        elif hit.sha:
            where = hit.sha
        else:
            where = hit.kind
        blocks.append(f"[{index}] {where}\n{hit.content}")
    return "\n\n".join(blocks)


def _from_web(hit: WebHit) -> Hit:
    return Hit(
        score=hit.score,
        kind="doc",
        content=hit.content,
        start_line=hit.start_line,
        end_line=hit.end_line,
        url=hit.url,
    )


def _citation(hit: Hit) -> dict[str, Any]:
    return {
        "kind": hit.kind,
        "path": hit.path,
        "start_line": hit.start_line,
        "end_line": hit.end_line,
        "sha": hit.sha,
        "url": hit.url,
        "score": hit.score,
    }


def _repo(conn: psycopg.Connection[Any], repo_id: UUID) -> tuple[Any, ...] | None:
    return conn.execute(
        "select id, source from repos where id = %s",
        (repo_id,),
    ).fetchone()


def _conversation(
    conn: psycopg.Connection[Any], repo_id: UUID, conversation_id: UUID | None
) -> UUID:
    if conversation_id is not None:
        row = conn.execute(
            """
            select id from conversations
            where id = %s and repo_id = %s
            """,
            (conversation_id, repo_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("conversation does not exist")
        return row[0]
    row = conn.execute(
        "insert into conversations (repo_id) values (%s) returning id",
        (repo_id,),
    ).fetchone()
    if row is None or not isinstance(row[0], UUID):
        raise RuntimeError("conversation was not stored")
    return row[0]


def _message(
    conn: psycopg.Connection[Any],
    conversation_id: UUID,
    role: str,
    content: str,
    citations: list[dict[str, Any]],
) -> None:
    conn.execute(
        """
        insert into messages (conversation_id, role, content, citations)
        values (%s, %s, %s, %s)
        """,
        (conversation_id, role, content, Jsonb(citations)),
    )


def _event(name: str, data: Any) -> dict[str, str]:
    if isinstance(data, str):
        payload = data
    else:
        payload = json.dumps(data)
    return {"event": name, "data": payload}
