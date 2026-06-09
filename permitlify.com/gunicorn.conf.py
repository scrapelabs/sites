"""
Gunicorn config for the Permitlify Django app.

Tuned for the DigitalOcean App Platform 2 GB instance running in NYC1
in front of a Supabase Postgres in us-east-1 (post-migration; was
us-west-1 prior to PR #112-+1). Three big wins over the previous
inline ``--workers 2 --timeout 60`` flags:

1. ``preload_app = True`` — Django, all middleware, every imported
   template tag library and the entire ``core.views`` module are
   loaded ONCE in the master process before fork. Each worker then
   forks copy-on-write, so it's serving requests in a few milliseconds
   instead of the ~2.3 s cold-start hit we were paying on every worker
   recycle. Memory drops too because all the read-only bytecode is
   shared.

2. ``on_starting`` — runs in the master before any worker forks. We
   walk every ``.html`` template in ``templates/`` and call
   ``engine.get_template(rel_path)`` on it, which forces Django's
   ``cached.Loader`` (auto-enabled when DEBUG=False) to parse the
   template once and cache the compiled object in an in-memory dict.
   Because this happens BEFORE fork, every worker inherits the parsed
   templates via copy-on-write — so the very first admin page request
   each worker sees no longer pays the ~5 s cost to parse
   ``admin_base.html`` (39 KB) or ``base.html`` (133 KB) from disk.

3. ``post_fork`` warms the psycopg connection pool inside each child.
   psycopg pools cannot be inherited across ``fork()`` (the TCP
   sockets become invalid in the child), so we deliberately do *not*
   open the pool at preload time — we open it here, after fork,
   blocking up to 5 s for ``min_size`` connections to be ready before
   the worker takes traffic. This kills the ~290 ms first-query
   penalty every worker used to pay on its first request (now ~10 ms
   per query thanks to the us-east-1 migration anyway, but the
   warm-up is still worth it).

Worker math: 4 workers × 2 threads = 8 concurrent requests, well
within the pool's max_size=8. ``max_requests`` recycles a worker
every ~1,000 requests (with jitter so they don't all recycle at once)
to defend against any slow memory leak in long-running processes.
"""

import os
from pathlib import Path

# ── Process model ──────────────────────────────────────────────
preload_app = True
workers     = 4
threads     = 2

# ── Timeouts ───────────────────────────────────────────────────
# Bumped 60 → 300 in PR #193 so the admin Accela-finder endpoint can
# ride out an Anthropic 429-with-Retry-After backoff. Each call may
# now wait up to ~3 min total (4 retries × 45 s clamp + the underlying
# urlopen timeout) before surfacing an error to the admin. With 60 s
# the worker would be SIGKILL-reaped mid-backoff and the admin would
# see a 502 instead of either the late-success result or the precise
# "wait for input-token budget" message.
#
# Trade-off: a genuinely hung worker (e.g. blocked on a dead socket)
# now takes 5 min to detect instead of 1. Mitigated by:
#   - workers=4, threads=2 → 8 concurrent slots, one stuck slot still
#     leaves 7 healthy.
#   - max_requests=1000 worker recycling catches slow leaks.
#   - No non-admin endpoint has any code path that approaches even
#     30 s, so steady-state hang detection is unchanged in practice.
timeout     = 300         # hard kill if a worker hangs > 5 min
keepalive   = 5           # reuse TCP conns from CDN/clients for 5 s

# ── Worker recycling ───────────────────────────────────────────
# Restart each worker after ~1k requests (±100) to recover from any
# slow memory growth. Jitter prevents thundering-herd recycles.
max_requests          = 1000
max_requests_jitter   = 100

# ── Logging ────────────────────────────────────────────────────
accesslog = '-'
errorlog  = '-'
loglevel  = 'info'


def on_starting(server):
    """Pre-parse every template into the master's cached.Loader.

    Runs once in the master, BEFORE any worker is forked. Every
    template parsed here lives in Django's in-process template cache
    dict — and because workers are forked copy-on-write *after* this
    runs, every worker inherits the parsed templates for free.

    Net effect: the very first admin/marketing page request to each
    worker no longer pays the ~5 s cold-template-parse hit. Steady
    state is unaffected (templates were already cached after the first
    hit), but cold deploys, autoscale events, and ``max_requests``
    worker recycles all become invisible to users.

    Safe failure: any single template that won't parse (syntax error,
    missing tag library) is logged and skipped — boot continues so a
    template typo can never block a deploy.
    """
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'permitdaily.settings')
    try:
        import django
        django.setup()
        from django.template import engines

        engine = engines['django'].engine
        # Combine engine.dirs (DIRS=...) with each app's templates/ dir
        # (APP_DIRS=True) so we cover every template the loader can find.
        roots: list[Path] = [Path(d) for d in (engine.dirs or [])]
        try:
            from django.template.utils import get_app_template_dirs
            roots += [Path(d) for d in get_app_template_dirs('templates')]
        except Exception:
            pass

        seen: set[str] = set()
        ok = 0
        fail = 0
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob('*.html'):
                rel = str(path.relative_to(root))
                if rel in seen:
                    continue
                seen.add(rel)
                try:
                    engine.get_template(rel)
                    ok += 1
                except Exception as exc:
                    fail += 1
                    server.log.debug("template warm skip %s: %s", rel, exc)
        server.log.info(
            "Templates pre-warmed: %d compiled, %d skipped (workers will inherit)",
            ok, fail,
        )
    except Exception as exc:
        # Never fail boot on warm-up trouble — workers will just pay
        # the old per-worker cold-parse hit on first request.
        server.log.warning("Template pre-warm skipped entirely: %s", exc)


def post_fork(server, worker):
    """Open the DB pool inside this worker after fork.

    Cannot be done at preload/on_starting time because psycopg
    ``ConnectionPool`` holds open sockets that become invalid
    post-fork. Calling ``get_pool()`` here builds the pool in the
    child; we then warm it by borrowing one connection so the
    worker's very first request finds a warm pool instead of paying
    ~290 ms (us-west-1) / ~10 ms (us-east-1) for a cold connection.
    We borrow rather than ``wait()`` because a timed-out ``wait()``
    closes the pool permanently (psycopg-pool >=3.2).
    """
    try:
        from core.pg import get_pool
        pool = get_pool()
        # Warm by borrowing a connection (with its own timeout) rather than
        # pool.wait(): psycopg-pool (>=3.2) *closes the pool* if wait() times
        # out, bricking every later request with PoolClosed. A failed borrow
        # here just raises PoolTimeout and leaves the pool open to reconnect.
        with pool.connection(timeout=5.0) as c, c.cursor() as cur:
            cur.execute("SELECT 1")
        worker.log.info("DB pool warm: %d connection(s) ready", pool.min_size)
    except Exception as exc:
        # Never fail boot on a transient DB hiccup — the worker can
        # still serve cached / static / non-DB pages while reconnecting.
        worker.log.warning("DB pool warm-up skipped: %s", exc)
