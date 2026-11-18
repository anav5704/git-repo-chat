create extension if not exists vector;
create extension if not exists pg_trgm;

create table repos (
    id uuid primary key default gen_random_uuid(),
    source text not null unique,
    default_branch text,
    status text not null default 'pending' check (
        status in ('pending', 'indexing', 'indexed', 'failed')
    ),
    last_indexed_commit text,
    created_at timestamptz not null default now()
);

create table commits (
    id uuid primary key default gen_random_uuid(),
    repo_id uuid not null references repos (id) on delete cascade,
    sha text not null,
    author text not null,
    committed_at timestamptz not null,
    message text not null,
    unique (repo_id, sha)
);

create table files (
    id uuid primary key default gen_random_uuid(),
    repo_id uuid not null references repos (id) on delete cascade,
    path text not null,
    language text,
    blob_sha text not null,
    unique (repo_id, path)
);

create table chunks (
    id uuid primary key default gen_random_uuid(),
    repo_id uuid not null references repos (id) on delete cascade,
    file_id uuid references files (id) on delete cascade,
    commit_id uuid references commits (id) on delete cascade,
    start_line integer,
    end_line integer,
    content text not null,
    kind text not null check (kind in ('code', 'commit', 'doc')),
    content_hash text not null,
    content_tsv tsvector generated always as (to_tsvector('simple', content)) stored,
    constraint chunks_source check (
        (
            kind = 'commit'
            and commit_id is not null
            and file_id is null
        )
        or (
            kind in ('code', 'doc')
            and file_id is not null
            and commit_id is null
            and start_line is not null
            and end_line is not null
            and start_line > 0
            and end_line >= start_line
        )
    )
);

create index chunks_repo_id_idx on chunks (repo_id);

create index chunks_file_lines_idx on chunks (file_id, start_line);

create index chunks_content_hash_idx on chunks (content_hash);

create index chunks_content_tsv_idx on chunks using gin (content_tsv);

create table chunk_embeddings (
    chunk_id uuid not null references chunks (id) on delete cascade,
    model text not null,
    dims integer not null check (dims > 0),
    embedding vector not null,
    primary key (chunk_id, model),
    constraint chunk_embeddings_dims_match check (vector_dims(embedding) = dims)
);

create index chunk_embeddings_embedding_hnsw
    on chunk_embeddings
    using hnsw ((embedding::vector(1536)) vector_cosine_ops)
    where dims = 1536;

create table jobs (
    id uuid primary key default gen_random_uuid(),
    repo_id uuid not null references repos (id) on delete cascade,
    type text not null check (
        type in ('clone_repo', 'index_files', 'index_commits', 'embed_chunks')
    ),
    payload jsonb not null default '{}',
    status text not null default 'pending' check (
        status in ('pending', 'running', 'succeeded', 'failed')
    ),
    attempts integer not null default 0 check (attempts >= 0),
    run_after timestamptz not null default now(),
    locked_at timestamptz,
    locked_by text,
    error text,
    created_at timestamptz not null default now(),
    constraint jobs_lock check (
        (status = 'running' and locked_at is not null)
        or (status <> 'running' and locked_at is null)
    )
);

create index jobs_claim_idx on jobs (run_after, id)
    where status = 'pending';

create index jobs_repo_id_idx on jobs (repo_id);

create table conversations (
    id uuid primary key default gen_random_uuid(),
    repo_id uuid not null references repos (id) on delete cascade,
    created_at timestamptz not null default now()
);

create table messages (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references conversations (id) on delete cascade,
    role text not null check (role in ('user', 'assistant', 'system')),
    content text not null,
    citations jsonb not null default '[]',
    created_at timestamptz not null default now()
);

create index messages_conversation_id_idx on messages (conversation_id, created_at);
