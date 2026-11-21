import os
from pathlib import Path


def load_env() -> None:
    seen: set[Path] = set()
    starts = [Path.cwd(), Path(__file__).resolve()]
    for start in starts:
        for directory in (start, *start.parents):
            if directory in seen:
                continue
            seen.add(directory)
            path = directory / ".env"
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
            return
