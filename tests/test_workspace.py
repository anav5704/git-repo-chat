def test_import_names() -> None:
    """Workspace members import by project name."""
    import api
    import chunking
    import db
    import embeddings
    import indexer
    import llm
    import queue
    import repogit
    import retrieval
    import tui
    import web

    imported = (
        api,
        chunking,
        db,
        embeddings,
        indexer,
        llm,
        queue,
        repogit,
        retrieval,
        tui,
        web,
    )
    assert [module.__name__ for module in imported] == [
        "api",
        "chunking",
        "db",
        "embeddings",
        "indexer",
        "llm",
        "queue",
        "repogit",
        "retrieval",
        "tui",
        "web",
    ]
