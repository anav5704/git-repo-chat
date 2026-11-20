from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Hit:
    score: float
    kind: str
    content: str
    chunk_id: UUID | None = None
    file_id: UUID | None = None
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    sha: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Blurb:
    name: str
    languages: str
    paths: str
