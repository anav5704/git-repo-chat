# Repo chat

Ask questions about a git repo. An indexer clones it, splits the files and commits into chunks, and stores embeddings in Postgres. Chat searches that index and, when the answer is outside the repo, the web. Citations point at a file and line range, a commit, or a URL.

## Run

Docker Desktop has to be running. From the repo root:

```powershell
just up
just migrate
uv run python -m indexer
uv run python -m api
uv run python -m tui
```

The indexer, API, and TUI each need their own terminal. They read `.env` themselves. Copy `.env.example` if you do not have one yet.

In the TUI, enter a local path or a git URL. Wait until the status is `indexed`, select the row, and press `c`. Escape leaves the chat.

A small repo to try: [octocat/Hello-World](https://github.com/octocat/Hello-World).

## Layout

Each package does one job. Apps depend only on what they call. There is no shared package.

```
packages/
  db/          pool, row types, SQL migrations
  jobs/        enqueue, claim, listen
  repogit/     clone, list files, diff, history
  chunking/    line windows, TypeScript tree-sitter, commit diffs
  embeddings/  Gemini embeddings
  llm/         Gemini chat
  retrieval/   symbol, full-text, vector, history, fusion, rerank, pack
  web/         Tavily search, fetch, extract, cache
apps/
  indexer/     claim jobs and store chunks
  api/         HTTP and the chat stream
  tui/         terminal client
```

`retrieval` does not import `web`, and `web` does not import `retrieval`. The API runs both and passes web hits into `retrieval.fuse`. Search SQL lives in `retrieval`. Job SQL lives in `jobs`. `db` is the pool, the row types, and the migrations.

The TUI talks to the API over HTTP and server-sent events. It imports none of the packages.

The git helper is `repogit`, so the `git` import stays free. The job package is `jobs`, so `queue` stays the standard library.

## Chat

A question does not clone or chunk anything. The repo is already indexed.

1. The router picks symbol search, semantic search, history search, and whether to call the web.
2. The query is rewritten. HyDE, when used, is embedded for vector search only.
3. Symbol search is trigram, for names like `getUserById`. Semantic search is pgvector plus full-text. History search hits commit messages and diffs, and stays in its own list until fusion.
4. The web runs when the router asked for it, or when the repo hits are empty or under `SCORE_FLOOR`. Tavily returns links. Allowlisted pages are fetched, extracted, chunked, and cached by content hash.
5. Reciprocal rank fusion merges the lists. One or more hits are reranked. Packing adds neighboring chunks and stops at a token budget.
6. One Gemini call writes the answer. The API streams tokens, then citations.

## Stack

- Python 3.13, uv, and Hatchling
- just and Docker Compose
- Postgres 18 with pgvector and pg_trgm. The data volume is mounted at `/var/lib/postgresql`
- psycopg 3
- FastAPI, Textual, and httpx
- Pydantic
- tree-sitter
- Gemini: `gemini-embedding-2` at 1536 dimensions, and `gemini-3-flash-preview` for chat
- Tavily and trafilatura
- ruff and Pyright
