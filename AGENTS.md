# Repo chat

Chat with a git repo. The indexer stores code and history in Postgres. Answers come from retrieval over that index, plus live web search when the question is outside the repo. Citations point at files, lines, commits, or URLs.

This repo is for learning retrieval and search in depth: chunking, embeddings, vector and lexical search, fusion, reranking, query rewriting, and web search as its own cached corpus. Extra techniques belong here when they are another stage in `retrieval` or `web`. The chat path stays a fixed pipeline so a bad answer points at a stage.

## Decisions to keep

- Packages stay separate. Apps depend only on what they call. There is no shared `core` package.
- `retrieval` does not import `web`. `web` does not import `retrieval`. The API runs both and passes web hits into `retrieval.fuse`.
- Search SQL lives in `retrieval`. Job SQL lives in `jobs`. `db` is the pool, row types, and numbered SQL migrations.
- Every query uses psycopg 3. Rows are dataclasses. Web chunks stay out of the repo HNSW index.
- The TUI talks to the API over HTTP and SSE. It imports none of the packages.
- The git helper package is `repogit`. The job package is `jobs`, so `queue` stays the standard library.
- Chat is the router pipeline in [README.md](README.md). The model does not choose tools. A hand-written tool loop, if added later, is a second mode over the same search functions, with a step cap, and it does not replace the pipeline.

## Where to work

- Layout, schema, stack, and query flow: [README.md](README.md)
- Build steps: [TODO.md](TODO.md)
- Deep work goes in `packages/retrieval` and `packages/web`. The API composes them. The indexer only ingests.
