"""LangGraph conversation memory backed by PostgreSQL.

Uses LangGraph's built-in ``PostgresSaver`` checkpointer so each conversation
(``thread_id``) has a persisted message history. The connection is opened lazily
so the app can boot without a database; a module-level lock serializes access
because a single psycopg connection is not safe for concurrent use.
"""

import logging
import threading
from functools import lru_cache

from core.config import POSTGRES_URI

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def memory_lock() -> threading.Lock:
    """Serializes checkpointer access across FastAPI's threadpool."""
    return _lock


@lru_cache(maxsize=1)
def get_checkpointer():
    """Open the Postgres checkpointer once and run its schema setup."""
    if not POSTGRES_URI:
        raise RuntimeError(
            "POSTGRES_URI is not set — add it to .env to enable conversation memory."
        )
    import psycopg
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres import PostgresSaver

    try:
        conn = psycopg.connect(POSTGRES_URI, autocommit=True, row_factory=dict_row)
        checkpointer = PostgresSaver(conn)
        checkpointer.setup()  # idempotent: creates checkpoint tables if missing
    except Exception as e:
        # lru_cache does not cache exceptions, so this retries once the DB is reachable.
        raise RuntimeError(
            f"Could not connect to PostgreSQL for conversation memory: {e}. "
            f"Check POSTGRES_URI in .env and that the database exists and is running."
        ) from e
    logger.info("[Memory] PostgreSQL checkpointer ready.")
    return checkpointer
