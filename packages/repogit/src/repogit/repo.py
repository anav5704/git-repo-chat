import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_LOCK_NAMES = frozenset(
    {
        "bun.lock",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "package-lock.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)
_TEXT_MODES = frozenset({"100644", "100755"})
_BINARY_SNIFF = 8000


@dataclass(frozen=True, slots=True)
class Tip:
    commit: str
    branch: str | None


@dataclass(frozen=True, slots=True)
class Blob:
    path: str
    sha: str


def _git(repo: Path | None, *args: str) -> bytes:
    command = ["git"]
    if repo is not None:
        command.extend(["-C", os.fspath(repo)])
    command.extend(args)
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"git {args[0]} failed")
    return result.stdout


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _tip(repo: Path) -> Tip:
    try:
        commit = _decode(_git(repo, "rev-parse", "--verify", "origin/HEAD")).strip()
    except RuntimeError:
        commit = _decode(_git(repo, "rev-parse", "HEAD")).strip()
    branch: str | None = None
    try:
        ref = _decode(
            _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        ).strip()
    except RuntimeError:
        ref = ""
    if ref.startswith("origin/"):
        branch = ref.removeprefix("origin/")
    return Tip(commit, branch)


def clone(source: str, dest: Path) -> Tip:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(None, "clone", "--", source, os.fspath(dest))
    return _tip(dest)


def fetch(repo: Path) -> Tip:
    _git(repo, "fetch", "--prune", "origin")
    return _tip(repo)


def list_files(repo: Path, commit: str) -> tuple[Blob, ...]:
    raw = _git(repo, "ls-tree", "-r", "-z", commit)
    chosen: list[tuple[str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, path_raw = record.split(b"\t", 1)
        mode, kind, sha = _decode(meta).split(" ")
        if kind != "blob" or mode not in _TEXT_MODES:
            continue
        path = _decode(path_raw)
        if Path(path).name.casefold() in _LOCK_NAMES:
            continue
        chosen.append((path, sha))
    return _without_binaries(repo, chosen)


def changed_paths(repo: Path, old: str, new: str) -> tuple[str, ...]:
    raw = _git(repo, "diff", "--name-status", "-z", old, new)
    parts = raw.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(parts) and parts[index]:
        status = _decode(parts[index])
        if status.startswith(("R", "C")):
            paths.append(_decode(parts[index + 1]))
            paths.append(_decode(parts[index + 2]))
            index += 3
            continue
        paths.append(_decode(parts[index + 1]))
        index += 2
    return tuple(dict.fromkeys(paths))


def _without_binaries(repo: Path, entries: list[tuple[str, str]]) -> tuple[Blob, ...]:
    if not entries:
        return ()
    process = subprocess.Popen(
        ["git", "-C", os.fspath(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    stdin = process.stdin
    stdout = process.stdout
    if stdin is None or stdout is None:
        raise RuntimeError("git cat-file failed")
    kept: list[Blob] = []
    try:
        for path, sha in entries:
            stdin.write(f"{sha}\n".encode())
            stdin.flush()
            header = stdout.readline()
            if not header:
                raise RuntimeError("git cat-file failed")
            info = header.split()
            if len(info) < 3 or info[1] != b"blob":
                continue
            size = int(info[2])
            sample = stdout.read(min(size, _BINARY_SNIFF))
            remaining = size - len(sample)
            while remaining:
                chunk = stdout.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
            if stdout.read(1) != b"\n":
                raise RuntimeError("git cat-file failed")
            if b"\0" not in sample:
                kept.append(Blob(path, sha))
    finally:
        stdin.close()
        process.wait()
    if process.returncode != 0:
        raise RuntimeError("git cat-file failed")
    return tuple(kept)
