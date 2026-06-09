"""Middleware for Permitlify (performance + security).

PublicCacheMiddleware adds a short ``Cache-Control: public, max-age=…,
stale-while-revalidate=…`` header to anonymous GET responses for our
marketing pages, so a returning visitor (or any CDN in front of the app
later) can serve the cached HTML for the next few minutes instead of
re-rendering on every hit. This pairs with the in-process settings cache
in ``core.whop`` — together they take the homepage TTFB from ~1.2s of DB
wait down to a few ms on cache hits.

SessionAbsoluteTimeoutMiddleware enforces a HARD 1-hour cap on every
logged-in session for security (independent of the 7-day cookie age).

Important:
  - Only applied to anonymous visitors (no ``user_id`` session, no auth
    headers). Logged-in users see personalised nav/badges/etc, so we
    keep their pages private.
  - Only applied to GET / HEAD requests on a strict whitelist of paths
    (or path prefixes for blog post slugs). Anything else falls through
    untouched.
  - Skipped if a cookie is being set on the response, since caching a
    response that mutates client state would be a correctness bug.
  - Vary: Cookie is added so a CDN keyed on cookies still distinguishes
    logged-in pages.
"""
from __future__ import annotations
from typing import Callable

# Exact paths that can safely be cached publicly. Blog post detail pages
# match by prefix below.
_PUBLIC_EXACT = {
    '/',
    '/pricing/',
    '/careers/',
    '/privacy/',
    '/terms/',
    '/blog/',
}
_PUBLIC_PREFIXES = (
    '/blog/',     # /blog/<slug>/
)
# 5 min fresh + 10 min stale-while-revalidate. Short enough that pricing
# edits in the admin show up quickly, long enough to absorb traffic spikes.
_MAX_AGE = 300
_SWR     = 600


def _is_anonymous(request) -> bool:
    """A request is 'anonymous' for caching purposes when there's no
    logged-in user in the session and no Authorization header. We treat
    *any* session user as logged in even if the row is gone, because the
    rendered nav still differs."""
    if request.session.get('user_id'):
        return False
    if request.META.get('HTTP_AUTHORIZATION'):
        return False
    return True


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    for pre in _PUBLIC_PREFIXES:
        # e.g. /blog/some-slug/ — but not /blog/ itself (already in EXACT)
        if path.startswith(pre) and len(path) > len(pre):
            return True
    return False


class PublicCacheMiddleware:
    def __init__(self, get_response: Callable):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method not in ('GET', 'HEAD'):
            return response
        if response.status_code != 200:
            return response
        if not _is_public_path(request.path):
            return response
        if not _is_anonymous(request):
            return response
        # Don't cache responses that set/clear cookies — they're carrying
        # client state, and caching would replay it for everyone.
        if response.cookies:
            return response
        # Don't override an explicit no-cache the view already set.
        existing = response.get('Cache-Control', '').lower()
        if 'no-store' in existing or 'private' in existing:
            return response
        response['Cache-Control'] = (
            f'public, max-age={_MAX_AGE}, '
            f'stale-while-revalidate={_SWR}'
        )
        # Cookie + Accept-Encoding so a CDN distinguishes logged-in vs
        # anonymous (different HTML) and gzip vs identity.
        existing_vary = response.get('Vary', '')
        vary_parts = {p.strip() for p in existing_vary.split(',') if p.strip()}
        vary_parts.update({'Cookie', 'Accept-Encoding'})
        response['Vary'] = ', '.join(sorted(vary_parts))
        return response


class SessionAbsoluteTimeoutMiddleware:
    """Enforce a hard absolute timeout on every logged-in session.

    Independent of ``SESSION_COOKIE_AGE`` (which is the *cookie* expiry,
    7 days). This middleware enforces the *server-side* absolute cap on
    any logged-in session — even if the user is actively clicking
    around, they get bounced to ``/login/?expired=1`` once
    ``login_at + SESSION_ABSOLUTE_TIMEOUT_SECONDS`` has passed. Pairs
    with the front-end countdown popup in ``templates/core/base.html``
    that warns the user 5 minutes before this fires.

    Why absolute (not idle): for sensitive admin work and contractor
    lead data, "you've been logged in for X hours" is a stronger
    security posture than "you've been idle for X minutes" — an
    attacker who hijacks an active session can't keep it alive
    indefinitely just by making periodic background requests.

    Graceful migration: sessions minted before this middleware existed
    have no ``login_at`` key. We stamp it on first sight rather than
    booting the user immediately — they'd still be capped at most
    TIMEOUT seconds later, just from "now" instead of from their true
    login time. That's an acceptable one-time deploy concession.

    Exempt paths: the login / signup / OAuth / password-recovery flows
    plus ``/api/session-status/`` (which the popup polls). Bouncing
    those to ``/login/`` would either redirect-loop the recovery flow
    or cause the polling JS to thrash the server with redirects.

    JSON / AJAX requests get a 401 with ``error: 'session_expired'``
    instead of a 302 so the front-end can render an "Your session
    ended" toast and stop polling, rather than chasing a redirect to
    HTML it can't render.
    """

    # Trailing slashes matter — without them `'/login'` would also
    # match a hypothetical future `/login-newsletter/` and silently
    # bypass the cap. Make every prefix end in `/` and add the bare
    # forms to EXEMPT_EXACT to cover the no-trailing-slash variant
    # (which can reach this middleware before CommonMiddleware's
    # APPEND_SLASH normalises it, since we run before Common).
    EXEMPT_PREFIXES = (
        '/login/',          # /login/, /login/2fa/, /login/verify-code/
        '/logout/',
        '/signup/',
        '/forgot-password/',
        '/reset-password/',
        '/auth/google/',    # google_oauth_start + google_oauth_callback
        '/static/',
        '/__replco/',       # Replit dev preview iframe
    )

    EXEMPT_EXACT = {
        '/login',
        '/logout',
        '/signup',
        '/forgot-password',
        '/reset-password',
        '/api/session-status/',
        '/api/session-status',
        '/favicon.ico',
        '/robots.txt',
        '/sitemap.xml',
    }

    def __init__(self, get_response):
        self.get_response = get_response
        from django.conf import settings as _s
        # Read once at startup; tweaks require a worker restart, which
        # is fine — this is a security-floor constant, not a knob.
        self.timeout = int(getattr(_s, 'SESSION_ABSOLUTE_TIMEOUT_SECONDS', 3600))

    @staticmethod
    def _wants_json(request, path):
        """Decide whether the caller is JS/JSON (→ 401) or a browser
        navigation (→ 302). The codebase's frontend uses ``fetch()``
        liberally without setting ``Accept`` or ``X-Requested-With``,
        so the original Accept-header check missed almost every
        in-app POST. We therefore treat any request whose own body is
        JSON, OR any non-GET method (which a browser navigation never
        is — POSTs originate from forms or fetch and either way
        prefer a status code over an HTML redirect they can't render),
        as a JSON caller. Cheap and matches reality.
        """
        if path.startswith('/api/'):
            return True
        ctype = (request.META.get('CONTENT_TYPE') or '').lower()
        if ctype.startswith('application/json'):
            return True
        if 'application/json' in (request.META.get('HTTP_ACCEPT', '') or ''):
            return True
        if request.META.get('HTTP_X_REQUESTED_WITH', '') == 'XMLHttpRequest':
            return True
        # Mutating verbs from a browser are ~always fetch/XHR; HTML
        # form submissions also mishandle 302→login (they replay the
        # original POST body to the login page). 401 is the right
        # signal for both.
        if request.method not in ('GET', 'HEAD'):
            return True
        return False

    def __call__(self, request):
        # Anonymous request: nothing to enforce.
        try:
            user_id = request.session.get('user_id') if hasattr(request, 'session') else None
        except Exception:
            user_id = None
        if not user_id:
            return self.get_response(request)

        path = request.path or ''
        if path in self.EXEMPT_EXACT or path.startswith(self.EXEMPT_PREFIXES):
            return self.get_response(request)

        import time as _time
        now = int(_time.time())
        login_at = request.session.get('login_at')

        # Graceful migration: pre-existing session with no stamp.
        # Stamp now and let the user continue — they'll still hit the
        # cap at most TIMEOUT seconds from this moment.
        if not isinstance(login_at, int):
            try:
                request.session['login_at'] = now
            except Exception:
                pass
            return self.get_response(request)

        # ── Sign-out-everywhere check (signed-cookie revocation) ────
        # Sessions are stored in signed cookies (no server-side store),
        # so a "sign out everywhere" action can't simply delete a row
        # to invalidate cookies on other devices. Instead the action
        # stamps ``sessions_revoked_at`` (unix-ts) on the user JSONB,
        # and every authed request compares its session's ``login_at``
        # against that timestamp. Any session whose login predates the
        # last revocation is dead — kicked here just like an expired
        # session. Cost: one indexed user lookup per authed request,
        # which the @login_required decorator already does for most
        # views anyway.
        revoked_at = 0
        try:
            from .db import get_user_by_id as _get_user_by_id
            _u = _get_user_by_id(user_id) or {}
            _ra = _u.get('sessions_revoked_at')
            if isinstance(_ra, (int, float)):
                revoked_at = int(_ra)
        except Exception:
            # DB hiccup must never let a revoked cookie back in OR
            # boot a healthy user — fail safe-and-open here, the next
            # request will check again. (Strictly fail-closed would
            # log everyone out on transient DB blips.)
            pass

        elapsed = now - login_at
        if elapsed < self.timeout and login_at >= revoked_at:
            return self.get_response(request)

        # ── Expired (or revoked) — purge and bounce ─────────────────
        session_key = getattr(request.session, 'session_key', None)
        try:
            from .pg import execute as _pg_execute
            if session_key:
                _pg_execute("DELETE FROM sessions WHERE session_key = %s",
                            (session_key,))
        except Exception:
            # Never block expiry on a DB hiccup — the cookie purge
            # below is what actually signs the user out.
            pass
        try:
            request.session.flush()
        except Exception:
            pass

        if self._wants_json(request, path):
            from django.http import JsonResponse
            return JsonResponse({
                'ok':      False,
                'error':   'session_expired',
                'message': 'Your session has ended for security. Please sign in again.',
            }, status=401)

        from django.shortcuts import redirect
        return redirect('/login/?expired=1')


class AdminHTMLCacheInvalidationMiddleware:
    """Drop the per-worker admin HTML cache after any admin write.

    Pairs with ``core.cache.cached_admin_html`` (the decorator that
    stores rendered admin GET responses for ~15 s). Whenever the admin
    does *anything* mutating under ``/admin-panel/*`` — delete a user,
    flip a Whop mode, save email settings, ban an email, etc. — this
    middleware flushes every cached admin page in this worker so the
    next GET re-renders from fresh DB state.

    Coarse-but-correct: the cache only ever holds ~10 admin pages
    × <100 KB each, so blowing the whole thing away on any write is
    free and immune to "I forgot to invalidate that one path" bugs.
    Place this AFTER CommonMiddleware so we only see the resolved
    request.path / request.method, and AFTER CsrfViewMiddleware so
    invalid POSTs (which return 403 before the view runs) don't
    needlessly invalidate.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Only invalidate on successful writes to /admin-panel/*.
        # GET/HEAD never mutate state, and a 4xx/5xx means the write
        # didn't happen so there's nothing to invalidate.
        if (request.method not in ('GET', 'HEAD')
                and request.path.startswith('/admin-panel/')
                and 200 <= getattr(response, 'status_code', 0) < 400):
            try:
                from .cache import invalidate_admin_html_cache
                invalidate_admin_html_cache()
            except Exception:
                # Cache is best-effort; never break the response on
                # an invalidation hiccup.
                pass
        return response
