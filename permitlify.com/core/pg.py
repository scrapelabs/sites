"""
Postgres connection helper for Supabase.

Uses psycopg3 with a small thread-safe connection pool. The Supabase pooler
endpoint (port 6543) is the *transaction* pooler, so we disable named prepared
statements via prepare_threshold=None.
"""
import os
import threading
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool = None
_lock = threading.Lock()


def _is_local(url: str) -> bool:
    """True if the DSN points at this machine's own Postgres.

    A local Postgres install isn't configured for TLS, so forcing
    ``sslmode=require`` (correct for Supabase) makes it reject every
    connection with "server does not support SSL, but SSL was required".
    """
    lowered = url.lower()
    return any(h in lowered for h in ('@127.0.0.1', '@localhost', '@[::1]', '@::1'))


def _dsn() -> str:
    url = os.environ.get('SUPABASE_DATABASE_URL')
    if not url:
        raise RuntimeError(
            "SUPABASE_DATABASE_URL is not set. Add it as a Replit secret "
            "with the Supabase Postgres connection string."
        )
    if 'sslmode=' not in url:
        # Remote (Supabase) requires TLS; a local Postgres doesn't speak it.
        mode = 'disable' if _is_local(url) else 'require'
        url += ('&' if '?' in url else '?') + 'sslmode=' + mode
    return url


def get_pool() -> ConnectionPool:
    global _pool
    # Self-heal: psycopg-pool (>=3.2) *closes the pool permanently* if its
    # initial open/wait can't reach the DB in time. A transient DB hiccup at
    # boot would otherwise brick every later request with PoolClosed. Rebuild
    # the pool whenever it's missing or has been closed, so the app recovers
    # the moment the DB is reachable again.
    if _pool is None or _pool.closed:
        with _lock:
            if _pool is None or _pool.closed:
                # min_size=2 so each gunicorn worker boots with two warm
                # connections in hand (see gunicorn.conf.py post_fork, which
                # warms the pool right after fork). Pre-PR-#112 this was 1,
                # which meant the worker's *second* concurrent request still
                # paid the ~290 ms cold-connection cost.
                #
                # open=False + .open() (wait=False) opens the pool WITHOUT the
                # self-closing "wait until ready or die" behaviour: connections
                # fill in the background and a momentarily-unreachable DB yields
                # a recoverable PoolTimeout on first use, never a permanent
                # PoolClosed.
                pool = ConnectionPool(
                    conninfo=_dsn(),
                    min_size=2,
                    max_size=8,
                    kwargs={
                        'prepare_threshold': None,
                        'row_factory': dict_row,
                    },
                    open=False,
                )
                pool.open()
                _pool = pool
    return _pool


def conn():
    """Context manager that yields a pooled connection (auto-commits on exit)."""
    return get_pool().connection()


def query(sql: str, params: tuple = ()) -> list[dict]:
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return list(cur.fetchall())


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_returning(sql: str, params: tuple = ()) -> dict | None:
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        row = cur.fetchone()
        return row
