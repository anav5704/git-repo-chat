create table web_pages (
    id uuid primary key default gen_random_uuid(),
    url text not null unique,
    domain text not null,
    content_hash text not null,
    fetched_at timestamptz not null default now()
);

create table web_chunks (
    id uuid primary key default gen_random_uuid(),
    page_id uuid not null references web_pages (id) on delete cascade,
    start_line integer not null check (start_line > 0),
    end_line integer not null check (end_line >= start_line),
    content text not null,
    content_hash text not null
);

create index web_chunks_page_id_idx on web_chunks (page_id);

create index web_chunks_content_hash_idx on web_chunks (content_hash);

create table web_chunk_embeddings (
    chunk_id uuid not null references web_chunks (id) on delete cascade,
    model text not null,
    dims integer not null check (dims > 0),
    embedding vector not null,
    primary key (chunk_id, model),
    constraint web_chunk_embeddings_dims_match check (vector_dims(embedding) = dims)
);
