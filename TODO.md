# TODO

Steps follow the build order in [README.md](README.md). Finish each section before starting the next. A section is done when its check passes.

## 1. Workspace and database

- Root `pyproject.toml`: Python 3.13, uv workspace members `packages/*` and `apps/*`, hatchling, ruff, pyright, pytest.
- One package per directory in the README tree. The git helper is `packages/repogit`. Each app declares only the workspace deps it calls. `apps/tui` depends on none of them.
- `docker-compose.yml`: `pgvector/pgvector:pg18`, a named volume mounted at `/var/lib/postgresql` (Postgres 18's volume path, not `/var/lib/postgresql/data`), a healthcheck, port published to the host.
- `justfile`: `up`, `down`, `migrate`, `lint`, `test`.
- `packages/db`: a `psycopg_pool` pool that calls `register_vector` on each connection, dataclasses with `class_row`, and an applier that runs `migrations/*.sql` in order and records them in `schema_migrations`.
- First migration: `vector` and `pg_trgm`, then `repos`, `commits`, `files`, `chunks`, `chunk_embeddings`, `jobs`, `conversations`, `messages`. Put `repo_id` on `chunks`. Add an HNSW index on `chunk_embeddings.embedding`. Leave web tables for section 4.
- `.github/workflows/code-quality.yml` has a `lint` job for `ruff check`, a `type` job for Pyright, and a `format` job for `ruff format --check`.
- Env: `DATABASE_URL`.

Done when `just up` is healthy, `just migrate` succeeds twice, and a tiny script inserts and selects a vector.

## 2. Jobs and indexer

- `packages/jobs` takes a psycopg connection from the caller.
- `enqueue` inserts a row and `NOTIFY jobs`. The payload stays in the row.
- `claim` runs `UPDATE ... FOR UPDATE SKIP LOCKED ... RETURNING *` in a short transaction.
- The worker holds a second connection, `autocommit=True`, outside the pool: `LISTEN jobs`, then `notifies(timeout=5)`.
- Failed jobs increment `attempts` and set `run_after` with backoff. A reaper requeues rows whose `locked_at` is stale.
- Tests against Compose Postgres: two workers cannot claim the same job, a notify wakes the listener, and a stale lock is requeued.
- `packages/repogit`: clone or fetch via the git binary, `git ls-files` for the file list, diff against `last_indexed_commit`, blob sha per file. Skip binaries and lockfiles.
- `packages/chunking`: fixed windows with overlap first. Then recursive splits, tree-sitter boundaries (function and class), and commit chunks (message plus a size-capped diff). Each chunk records kind `code`, `commit`, or `doc`, line range, and content hash.
- `packages/embeddings`: httpx client for the gateway embeddings endpoint. Batch requests. Skip the gateway call when `(content_hash, model)` already has a row.
- `apps/indexer`: claim loop. `clone_repo`, then `index_files` and `index_commits`, then `embed_chunks`. Upsert files, chunks, commits, and `chunk_embeddings`. Advance `repos.last_indexed_commit` and status.
- Env: `DATABASE_URL`, `AI_GATEWAY_BASE_URL`, `AI_GATEWAY_API_KEY`, `EMBEDDING_MODEL`.

Done when registering a local repo path indexes a pinned commit, a second run skips unchanged blobs, and commit chunks exist for that range.

## 3. Retrieval

- `packages/retrieval` stages are functions: query plus `repo_id` in, ranked hits out. A hit carries score, kind, file path and lines or commit sha, and the text.
- Symbol: `pg_trgm` over identifiers in chunk text.
- Semantic: embed the query, pgvector filtered by `repo_id`, plus `tsvector` full-text on chunk text. Keep an exact kNN query next to HNSW.
- History: search commit messages and `kind = commit` chunks. Do not mix these hits into the code ranking before fusion.
- `fuse` is reciprocal rank fusion over whatever lists the caller passes in, including lists that did not come from this package.
- Rerank asks the gateway to order the fused top results. Keep it as one function so a different ranker can replace the call.
- Router: one structured gateway call. Input is the question and a blurb (name, languages, top-level paths). Pydantic output is the set of repo sources, a web flag, and an optional web query. The router does not call `web`.
- In front of the selected stages: query rewrite, multi-query, and HyDE. HyDE writes a hypothetical passage, embeds it, and uses that vector only for semantic search.
- `pack` expands neighbor chunks, drops duplicates, and stops at a token budget.
- Env: `CHAT_MODEL` plus the embedding env vars from section 2. The score floor is a setting, default something you can change without a code edit.

Done when a script can route one repo question and one docs question, run only the selected stages, fuse, rerank, and print packed hits with locations.

## 4. Web

- Migration: `web_pages`, `web_chunks`, `web_chunk_embeddings`. No foreign key into repo `chunks`.
- `packages/web`: Brave search client as a single function. httpx fetch. Keep responses whose domain is on the allowlist (docs, changelogs, GitHub issues). trafilatura extracts the text. Chunk with `chunking`, embed with `embeddings`, upsert by content hash. Store `fetched_at`.
- Return hits shaped so the API can pass them to `retrieval.fuse`. This package does not import `retrieval`.
- A second fetch of the same url with the same content hash does not call the embedding API.
- Env: `BRAVE_API_KEY`, `WEB_ALLOWLIST`.

Done when a docs question returns cached web hits with urls, and a repo-only question does not call Brave.

## 5. API and TUI

- `apps/api`: FastAPI on uvicorn.
  - `POST /repos` inserts a repo and enqueues `clone_repo`.
  - `GET /repos/{id}` returns status and `last_indexed_commit`.
  - `GET /jobs` lists jobs, filterable by repo.
  - `GET /health` checks the database.
  - `POST /repos/{id}/chat` runs the query flow in the README and streams tokens, then citations, with sse-starlette. Persist `conversations` and `messages`, including the citations jsonb.
- The chat handler owns the web decision. Call `web` when the router sets the flag, or when repo hits are empty or under the score floor. Then `fuse`, rerank, `pack`, and one answer call. Include prior messages from that conversation in the answer call.
- Optional: a dedicated async listen connection that emits job status events. The chat stream does not depend on it.
- `apps/tui`: Textual. Repo list shows index status. Chat screen streams markdown, shows the router decision and the stages that ran, and lists citations. httpx only. No imports from `packages/*`.
- Env for the API adds nothing beyond sections 1–4. The TUI needs the API base URL.

Done when you can register a repo, watch it reach indexed, ask a repo question and a dependency-docs question, and see streamed answers with citations and a trace in the TUI.
