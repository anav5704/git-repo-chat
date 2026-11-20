create index chunks_content_trgm_idx
    on chunks
    using gin (content gin_trgm_ops);
