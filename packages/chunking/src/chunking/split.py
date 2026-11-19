import hashlib
from dataclasses import dataclass

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

_CODE_NODES = frozenset(
    {
        "class_declaration",
        "function_declaration",
        "method_definition",
    }
)


@dataclass(frozen=True, slots=True)
class Chunk:
    content: str
    kind: str
    start_line: int | None
    end_line: int | None
    content_hash: str


def windows(
    text: str,
    *,
    kind: str = "code",
    size: int = 80,
    overlap: int = 15,
) -> tuple[Chunk, ...]:
    if kind not in {"code", "doc"}:
        raise ValueError("kind must be code or doc")
    _check_window(size, overlap)
    return tuple(
        _from_lines(
            _lines(_normalize(text)), kind=kind, first=1, size=size, overlap=overlap
        )
    )


def split_text(
    text: str,
    *,
    path: str | None = None,
    kind: str = "code",
    size: int = 80,
    overlap: int = 15,
) -> tuple[Chunk, ...]:
    if kind not in {"code", "doc"}:
        raise ValueError("kind must be code or doc")
    _check_window(size, overlap)
    text = _normalize(text)
    if kind == "code" and path is not None and path.lower().endswith(".ts"):
        return _typescript(text, size=size, overlap=overlap)
    return tuple(
        _from_lines(_lines(text), kind=kind, first=1, size=size, overlap=overlap)
    )


def commit_chunks(
    message: str, diff: str, *, max_chars: int = 4000
) -> tuple[Chunk, ...]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    message = _normalize(message).strip("\n")
    diff = _normalize(diff).strip("\n")
    if not message and not diff:
        return ()
    if not diff:
        return (_hashed(message, "commit", None, None),)
    pieces = _cut(diff, max_chars)
    if not message:
        return tuple(_hashed(piece, "commit", None, None) for piece in pieces)
    return tuple(
        _hashed(f"{message}\n\n{piece}", "commit", None, None) for piece in pieces
    )


def _typescript(text: str, *, size: int, overlap: int) -> tuple[Chunk, ...]:
    lines = _lines(text)
    if not lines:
        return ()
    spans = _spans(text.encode())
    if not spans:
        return tuple(
            _from_lines(lines, kind="code", first=1, size=size, overlap=overlap)
        )
    covered = [False] * len(lines)
    chunks: list[Chunk] = []
    for start, end in spans:
        start = max(start, 1)
        end = min(end, len(lines))
        if end < start:
            continue
        for index in range(start - 1, end):
            covered[index] = True
        chunks.extend(
            _from_lines(
                lines[start - 1 : end],
                kind="code",
                first=start,
                size=size,
                overlap=overlap,
            )
        )
    chunks.extend(_gaps(lines, covered, size=size, overlap=overlap))
    chunks.sort(key=lambda chunk: (chunk.start_line or 0, chunk.end_line or 0))
    return tuple(chunks)


def _spans(source: bytes) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    _walk(get_parser("typescript").parse(source).root_node, True, found)
    return found


def _walk(node: Node, surface: bool, found: list[tuple[int, int]]) -> None:
    kind = node.type
    if kind in _CODE_NODES or (
        surface and kind == "lexical_declaration" and _arrow_const(node)
    ):
        found.append(_span(node))
    child_surface = kind == "program" or (surface and kind == "export_statement")
    for child in node.children:
        _walk(child, child_surface, found)


def _arrow_const(node: Node) -> bool:
    for child in node.children:
        if child.type != "variable_declarator":
            continue
        for part in child.children:
            if part.type == "arrow_function":
                return True
    return False


def _span(node: Node) -> tuple[int, int]:
    start = node.start_point.row + 1
    end = node.end_point.row if node.end_point.column == 0 else node.end_point.row + 1
    if end < start:
        return start, start
    return start, end


def _gaps(
    lines: list[str],
    covered: list[bool],
    *,
    size: int,
    overlap: int,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    start: int | None = None
    for index, flag in enumerate(covered):
        if not flag and start is None:
            start = index
        elif flag and start is not None:
            chunks.extend(
                _from_lines(
                    lines[start:index],
                    kind="code",
                    first=start + 1,
                    size=size,
                    overlap=overlap,
                )
            )
            start = None
    if start is not None:
        chunks.extend(
            _from_lines(
                lines[start:], kind="code", first=start + 1, size=size, overlap=overlap
            )
        )
    return chunks


def _from_lines(
    lines: list[str],
    *,
    kind: str,
    first: int,
    size: int,
    overlap: int,
) -> list[Chunk]:
    if not lines:
        return []
    step = size - overlap
    chunks: list[Chunk] = []
    offset = 0
    while offset < len(lines):
        stop = min(offset + size, len(lines))
        begin = offset
        end = stop
        while begin < end and lines[begin].strip() == "":
            begin += 1
        while end > begin and lines[end - 1].strip() == "":
            end -= 1
        if begin < end:
            chunks.append(
                _hashed(
                    "\n".join(lines[begin:end]),
                    kind,
                    first + begin,
                    first + end - 1,
                )
            )
        if stop == len(lines):
            break
        offset += step
    return chunks


def _cut(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        if len(line) > max_chars:
            if buf:
                pieces.append("\n".join(buf))
                buf = []
                size = 0
            for index in range(0, len(line), max_chars):
                pieces.append(line[index : index + max_chars])
            continue
        extra = len(line) + (1 if buf else 0)
        if buf and size + extra > max_chars:
            pieces.append("\n".join(buf))
            buf = [line]
            size = len(line)
            continue
        buf.append(line)
        size += extra
    if buf:
        pieces.append("\n".join(buf))
    return pieces


def _lines(text: str) -> list[str]:
    if text == "":
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
    return parts


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _check_window(size: int, overlap: int) -> None:
    if size < 1 or overlap < 0 or overlap >= size:
        raise ValueError("overlap must be smaller than size")


def _hashed(
    content: str,
    kind: str,
    start_line: int | None,
    end_line: int | None,
) -> Chunk:
    digest = hashlib.sha256(content.encode()).hexdigest()
    return Chunk(content, kind, start_line, end_line, digest)
