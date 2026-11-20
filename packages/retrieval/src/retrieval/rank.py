import os
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, Field

from llm import Turn, structured
from retrieval.hit import Blurb, Hit

_RRF_K = 60
_RERANK_CAP = 20
_ROUTE = (
    "Choose which searches to run for a question about one git repository. "
    "symbol is for exact identifiers and names. "
    "semantic is for how the code behaves. "
    "history is for why a change was made. "
    "web is for answers that are not in this repository, such as dependency "
    "docs, changelogs, and upstream errors. "
    "web_query is a short web search when web is true, and empty otherwise."
)
_REWRITE = (
    "Rewrite a question into search queries. "
    "queries has one to three short queries. "
    "hyde is a paragraph that could appear in the source if the question "
    "were already answered."
)
_RERANK = (
    "Order the passages so the one that best answers the question is first. "
    "order lists every passage number once."
)


class Route(BaseModel):
    symbol: bool
    semantic: bool
    history: bool
    web: bool
    web_query: str = ""


class Rewrite(BaseModel):
    queries: list[str] = Field(default_factory=list)
    hyde: str = ""


class _Order(BaseModel):
    order: list[int] = Field(default_factory=list)


def fuse(lists: Sequence[Sequence[Hit]]) -> tuple[Hit, ...]:
    scores: dict[tuple[Any, ...], float] = {}
    chosen: dict[tuple[Any, ...], Hit] = {}
    for ranked in lists:
        for rank, hit in enumerate(ranked, start=1):
            key = _key(hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            if key not in chosen:
                chosen[key] = hit
    ordered = sorted(scores, key=lambda key: scores[key], reverse=True)
    return tuple(replace(chosen[key], score=scores[key]) for key in ordered)


def route(question: str, blurb: Blurb) -> Route:
    question = _question(question)
    text = (
        f"Repository: {blurb.name}\n"
        f"Languages: {blurb.languages}\n"
        f"Paths:\n{blurb.paths}\n"
        f"Question: {question}"
    )
    return structured([Turn("user", text)], Route, system=_ROUTE)


def rewrite(question: str) -> Rewrite:
    question = _question(question)
    result = structured([Turn("user", question)], Rewrite, system=_REWRITE)
    queries = [item.strip() for item in result.queries if item.strip()][:3]
    if not queries:
        queries = [question]
    return Rewrite(queries=queries, hyde=result.hyde.strip())


def rerank(question: str, hits: Sequence[Hit]) -> tuple[Hit, ...]:
    question = _question(question)
    if not hits:
        return ()
    head = tuple(hits[:_RERANK_CAP])
    lines = [f"Question: {question}"]
    for index, hit in enumerate(head):
        snippet = " ".join(hit.content.split())[:400]
        lines.append(f"{index}. {snippet}")
    order = structured([Turn("user", "\n".join(lines))], _Order, system=_RERANK).order
    return (*_reorder(head, order), *hits[_RERANK_CAP:])


def score_floor() -> float:
    raw = os.environ.get("SCORE_FLOOR", "0").strip()
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError("SCORE_FLOOR must be a number") from None
    return value


def _reorder(hits: Sequence[Hit], order: Sequence[int]) -> tuple[Hit, ...]:
    chosen: list[Hit] = []
    seen: set[int] = set()
    for index in order:
        if index in seen or index < 0 or index >= len(hits):
            continue
        seen.add(index)
        chosen.append(hits[index])
    for index, hit in enumerate(hits):
        if index not in seen:
            chosen.append(hit)
    return tuple(chosen)


def _key(hit: Hit) -> tuple[Any, ...]:
    if hit.chunk_id is not None:
        return ("chunk", hit.chunk_id)
    if hit.url:
        return ("url", hit.url, hit.start_line, hit.end_line)
    if hit.sha and hit.kind == "commit":
        return ("commit", hit.sha, hit.content)
    return ("text", hit.kind, hit.path, hit.start_line, hit.content)


def _question(question: str) -> str:
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    return question
