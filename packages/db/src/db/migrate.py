import os
import sys
from pathlib import Path

import psycopg
from psycopg import ClientCursor


def apply(database_url: str) -> list[str]:
    paths = sorted((Path(__file__).resolve().parent / "migrations").glob("*.sql"))
    applied: list[str] = []
    with psycopg.connect(database_url, cursor_factory=ClientCursor) as conn:
        conn.execute(
            """
            create table if not exists schema_migrations (
                filename text primary key,
                applied_at timestamptz not null default now()
            )
            """
        )
        seen = {
            row[0]
            for row in conn.execute("select filename from schema_migrations").fetchall()
        }
        conn.commit()
        for path in paths:
            if path.name in seen:
                continue
            with conn.transaction():
                conn.execute(path.read_bytes())
                conn.execute(
                    "insert into schema_migrations (filename) values (%s)",
                    (path.name,),
                )
            applied.append(path.name)
    return applied


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        raise SystemExit("DATABASE_URL is not set")
    applied = apply(database_url)
    if not applied:
        print("up to date")
        return
    for name in applied:
        print(f"applied {name}")


if __name__ == "__main__":
    main()
    sys.exit(0)
