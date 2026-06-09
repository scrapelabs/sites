"""
Process-local response cache for the read-only admin pages.

Why this exists
---------------
Even after the us-east-1 Supabase migration and the gunicorn preload PR,
each admin page render still costs ~200 ms warm (middleware chain +
template render of admin_base.html ~39 KB + view-specific body +
GZip of ~100 KB output + CDN round-trip). When an admin clicks around
``Overview → Users → Revenue → Cities → Banned → Users`` they re-render
the same pages over and over against the same data — every click is
another full ~200 ms.

This cache stores the *finished* HttpResponse body keyed on
``(path, user_id, query_string)`` for ``ttl_seconds`` (default 15 s)
so the second-and-onwards click in that window returns in <20 ms,
straight out of memory, never touching the template engine or the DB.

It's deliberately **process-local**: each gunicorn worker has its own
dict, no cross-worker invalidation. With our 4 workers + sticky-less
load balancer, the first hit of each page on each worker is uncached,
all subsequent hits are cached. That's the right trade-off — no
network round-trip to Redis, no shared-cache consistency bugs, and
invalidation on writes is dirt simple (clear the local dict).

Safety
------
- Cache is **only** consulted on GET with **no** query string.
  Anything with a ``?...`` (toast results like ``?deleted=ok``,
  filter inputs, etc.) skips the cache entirely so the user always
  sees fresh state for those interactions.
- Cache is **only** populated for 200 OK responses, never redirects
  or errors.
- The middleware in ``core.middleware.AdminHTMLCacheInvalidationMiddleware``
  flushes the entire cache on every non-GET request to ``/admin-panel/*``,
  so any admin write (delete user, toggle whop mode, change settings)
  drops every cached page across the whole worker. Coarse, but
  correct, and costs effectively nothing because the cache holds only
  ~10 pages × <100 KB each.

Memory bound
------------
Hard cap of 200 entries. When we hit the cap we evict the 50 oldest
entries by expiry time. At ~100 KB per entry that's ~20 MB worst case
per worker, well within the 2 GB instance budget.
"""

from __future__ import annotations

import threading
import time
from functools import wraps

from django.http import HttpResponse


# ── Storage ────────────────────────────────────────────────────────────────
# Process-local; each gunicorn worker keeps its own dict.
_html_cache: dict[str, dict] = {}
_html_lock  = threading.Lock()

_MAX_ENTRIES = 200    # hard cap so we can't OOM on a runaway cache
_EVICT_BATCH = 50     # how many oldest entries to drop when we hit the cap


def _make_key(request) -> str:
    """Cache key: user-scoped + path-scoped.

    Including ``user_id`` keeps two admins from ever seeing each
    other's cached output (defense-in-depth — admins in this app see
    the same data anyway, but cross-user response leaks are the kind
    of bug worth one extra string concat to prevent).
    """
    uid = (request.session.get('user_id') if hasattr(request, 'session') else None) or 0
    return f"{uid}|{request.path}"


def cached_admin_html(ttl_seconds: int = 15):
    """Decorator: cache the rendered HTML response for ``ttl_seconds``.

    Bypasses the cache when:
      - method is anything other than GET
      - any query string is present (toast results, filters, ?refresh=1)
      - response is non-2xx, a redirect, or streaming

    Usage::

        @admin_required
        @cached_admin_html(15)
        def admin_users_view(request):
            ...
    """
    def deco(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            # Only safe to cache plain GETs with no query string. Anything
            # else (POST, ?deleted=ok, ?q=foo, etc.) goes straight through.
            if request.method != 'GET' or request.META.get('QUERY_STRING'):
                return view_func(request, *args, **kwargs)

            key = _make_key(request)
            now = time.time()

            with _html_lock:
                entry = _html_cache.get(key)
                if entry is not None and entry['exp'] > now:
                    # Cache hit — rebuild a minimal HttpResponse from the
                    # stored bytes. We don't try to share the original
                    # response object because Django mutates response
                    # state (cookies, headers) per request.
                    resp = HttpResponse(
                        entry['body'],
                        content_type=entry['ct'],
                        status=200,
                    )
                    resp['X-Cache'] = 'HIT'
                    return resp

            # Cache miss — render the real view, then maybe store it.
            response = view_func(request, *args, **kwargs)

            cacheable = (
                getattr(response, 'status_code', 0) == 200
                and not getattr(response, 'streaming', False)
                and 'Set-Cookie' not in response  # don't cache personalized cookies
                # View can opt out of caching by setting Cache-Control: no-store.
                # Used by the admin revenue page to avoid pinning a degraded
                # Whop-API render (e.g. payments endpoint failed) for 15s.
                and 'no-store' not in response.get('Cache-Control', '').lower()
            )
            if cacheable:
                with _html_lock:
                    _html_cache[key] = {
                        'body': bytes(response.content),
                        'ct':   response.get('Content-Type', 'text/html; charset=utf-8'),
                        'exp':  now + ttl_seconds,
                    }
                    # Cheap LRU-by-expiry: when we cross the cap, drop the
                    # 50 entries that will expire soonest (oldest activity).
                    if len(_html_cache) > _MAX_ENTRIES:
                        for k, _ in sorted(
                            _html_cache.items(), key=lambda kv: kv[1]['exp']
                        )[:_EVICT_BATCH]:
                            _html_cache.pop(k, None)
                response['X-Cache'] = 'MISS'

            return response
        return wrapper
    return deco


def invalidate_admin_html_cache() -> int:
    """Drop every cached admin page in this worker.

    Called by ``AdminHTMLCacheInvalidationMiddleware`` after any non-GET
    request to ``/admin-panel/*``. Returns the number of entries
    dropped (handy for logging / tests).
    """
    with _html_lock:
        n = len(_html_cache)
        _html_cache.clear()
    return n


def _cache_size() -> int:
    """Current entry count — exposed for debugging / tests only."""
    with _html_lock:
        return len(_html_cache)
