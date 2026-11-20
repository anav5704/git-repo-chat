import os
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

_DIMS = 1536
_BATCH = 64
_BASE = "https://generativelanguage.googleapis.com/v1beta"
_TASKS = frozenset(
    {
        "CLASSIFICATION",
        "CLUSTERING",
        "CODE_RETRIEVAL_QUERY",
        "FACT_VERIFICATION",
        "QUESTION_ANSWERING",
        "RETRIEVAL_DOCUMENT",
        "RETRIEVAL_QUERY",
        "SEMANTIC_SIMILARITY",
    }
)


@dataclass(frozen=True, slots=True)
class Embedded:
    content_hash: str
    vector: tuple[float, ...]


def embed(
    items: Sequence[tuple[str, str]],
    *,
    task: str,
    already: Collection[str] = (),
) -> tuple[Embedded, ...]:
    if task not in _TASKS:
        raise ValueError(f"unknown embedding task {task}")
    pending = _pending(items, already)
    if not pending:
        return ()
    key, model = _settings()
    embedded: list[Embedded] = []
    with httpx.Client(timeout=60) as client:
        for start in range(0, len(pending), _BATCH):
            batch = pending[start : start + _BATCH]
            vectors = _request(
                client,
                key,
                model,
                task,
                [text for _, text in batch],
            )
            embedded.extend(
                Embedded(content_hash, vector)
                for (content_hash, _), vector in zip(batch, vectors, strict=True)
            )
    return tuple(embedded)


def _pending(
    items: Sequence[tuple[str, str]], already: Collection[str]
) -> tuple[tuple[str, str], ...]:
    seen = set(already)
    chosen: list[tuple[str, str]] = []
    for content_hash, text in items:
        if not content_hash:
            raise ValueError("content hash is empty")
        if not text.strip():
            raise ValueError("text is empty")
        if content_hash in seen:
            continue
        seen.add(content_hash)
        chosen.append((content_hash, text))
    return tuple(chosen)


def _settings() -> tuple[str, str]:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-2").strip()
    for prefix in ("models/", "google/"):
        if model.startswith(prefix):
            model = model.removeprefix(prefix)
    if not key or not model:
        raise RuntimeError("GEMINI_API_KEY and EMBEDDING_MODEL are required")
    return key, model


def _request(
    client: httpx.Client,
    key: str,
    model: str,
    task: str,
    texts: list[str],
) -> tuple[tuple[float, ...], ...]:
    response = client.post(
        f"{_BASE}/models/{model}:batchEmbedContents",
        headers={"x-goog-api-key": key},
        json={
            "requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task,
                    "outputDimensionality": _DIMS,
                }
                for text in texts
            ]
        },
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"embeddings request failed: {response.status_code} {response.text[:500]}"
        )
    return _vectors(response.json(), len(texts))


def _vectors(payload: Any, count: int) -> tuple[tuple[float, ...], ...]:
    rows = payload.get("embeddings") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != count:
        got = len(rows) if isinstance(rows, list) else 0
        raise RuntimeError(f"embeddings returned {got} vectors for {count} texts")
    vectors: list[tuple[float, ...]] = []
    for row in rows:
        values = row.get("values") if isinstance(row, dict) else None
        if not isinstance(values, list) or len(values) != _DIMS:
            got = len(values) if isinstance(values, list) else 0
            raise RuntimeError(f"expected {_DIMS} dimensions, got {got}")
        vectors.append(tuple(float(value) for value in values))
    return tuple(vectors)
