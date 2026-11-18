from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pgvector import Vector


@dataclass(frozen=True, slots=True)
class Repo:
    id: UUID
    source: str
    default_branch: str | None
    status: str
    last_indexed_commit: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Commit:
    id: UUID
    repo_id: UUID
    sha: str
    author: str
    committed_at: datetime
    message: str


@dataclass(frozen=True, slots=True)
class File:
    id: UUID
    repo_id: UUID
    path: str
    language: str | None
    blob_sha: str


@dataclass(frozen=True, slots=True)
class Chunk:
    id: UUID
    repo_id: UUID
    file_id: UUID | None
    commit_id: UUID | None
    start_line: int | None
    end_line: int | None
    content: str
    kind: str
    content_hash: str
    content_tsv: str


@dataclass(frozen=True, slots=True)
class ChunkEmbedding:
    chunk_id: UUID
    model: str
    dims: int
    embedding: Vector


@dataclass(frozen=True, slots=True)
class Job:
    id: UUID
    repo_id: UUID
    type: str
    payload: Any
    status: str
    attempts: int
    run_after: datetime
    locked_at: datetime | None
    locked_by: str | None
    error: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Conversation:
    id: UUID
    repo_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Message:
    id: UUID
    conversation_id: UUID
    role: str
    content: str
    citations: Any
    created_at: datetime
