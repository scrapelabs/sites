"""Production WSGI launcher for Windows (waitress).

gunicorn — and the tuned ``gunicorn.conf.py`` — cannot run on Windows because
it relies on ``os.fork()``. This launcher is the Windows-friendly equivalent:
it warms the template cache and the Postgres connection pool ONCE at startup
(the same two wins ``gunicorn.conf.py`` gets from ``on_starting`` + ``post_fork``,
but in a single process since waitress is threaded, not forked), then serves
``permitdaily.wsgi:application`` with waitress.

Static files are served by WhiteNoise (already wired into MIDDLEWARE), so no
separate web server is needed for ``/static/``. You still want a TLS terminator
(IIS, nginx, or Cloudflare) in front, because the app forces HTTPS in production.

Run:
    python serve_waitress.py

Environment:
    HOST              bind address (default 127.0.0.1)
    PORT              bind port    (default 8000)
    WAITRESS_THREADS  worker threads (default 8)
    TRUSTED_PROXY     proxy IP allowed to set X-Forwarded-* (default '*').
                      Cloudflare uses many IPs, so '*' trusts the forwarded
                      headers from any client. SAFE here only because the
                      origin should be firewalled to accept port 80 from
                      Cloudflare's IP ranges ONLY — otherwise someone could
                      hit the raw IP and spoof X-Forwarded-Proto.

Why this matters: waitress (>=2.0) strips X-Forwarded-* headers by default
(``clear_untrusted_proxy_headers=True``) unless a trusted proxy is configured.
Without the settings below, Cloudflare's ``X-Forwarded-Proto: https`` never
reaches Django, ``request.is_secure()`` is False, and SECURE_SSL_REDIRECT sends
an endless 301 -> https loop. Trusting the proxy lets waitress set
``wsgi.url_scheme`` from the forwarded header so Django knows the request is
HTTPS.
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'permitdaily.settings')

import django

django.setup()


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back on missing/garbage values."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return int(raw)
    except ValueError:
        print(f'[serve_waitress] {name}={raw!r} is not an int; using {default}')
        return default


def _prewarm_templates() -> None:
    """Parse every template once so the first request pays no cold-parse cost.

    Mirrors ``gunicorn.conf.py:on_starting``. Django's ``cached.Loader`` (auto
    enabled when DEBUG=False) keeps the compiled template in an in-process dict,
    so warming here means the first admin/marketing page render is instant.
    Any single template that won't parse is skipped — boot never fails on a
    template typo.
    """
    try:
        from pathlib import Path

        from django.template import engines

        engine = engines['django'].engine
        roots = [Path(d) for d in (engine.dirs or [])]
        try:
            from django.template.utils import get_app_template_dirs

            roots += [Path(d) for d in get_app_template_dirs('templates')]
        except Exception:
            pass

        seen: set[str] = set()
        ok = 0
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob('*.html'):
                # Django addresses templates with forward slashes ("core/x.html")
                # regardless of OS, so normalize to POSIX — otherwise on Windows
                # relative_to() yields "core\x.html" and warms a cache key that
                # never matches the real lookup, silently defeating the warm-up.
                rel = path.relative_to(root).as_posix()
                if rel in seen:
                    continue
                seen.add(rel)
                try:
                    engine.get_template(rel)
                    ok += 1
                except Exception:
                    pass
        print(f'[serve_waitress] templates pre-warmed: {ok} compiled')
    except Exception as exc:  # never block boot on warm-up trouble
        print(f'[serve_waitress] template pre-warm skipped: {exc}')


def _warm_db_pool() -> None:
    """Open the psycopg pool before taking traffic.

    Mirrors ``gunicorn.conf.py:post_fork`` — the first DB query then finds a
    warm connection instead of paying the cold-connect penalty. A transient DB
    hiccup is non-fatal: the server still boots and reconnects on demand.
    """
    try:
        from core.pg import get_pool

        pool = get_pool()
        # Warm by actually borrowing a connection (with its own timeout) rather
        # than pool.wait(): psycopg-pool (>=3.2) *closes the pool* if wait()
        # times out, which would brick every later request with PoolClosed.
        # A failed borrow here just raises PoolTimeout and leaves the pool open
        # to reconnect on demand.
        try:
            with pool.connection(timeout=5.0) as c, c.cursor() as cur:
                cur.execute('SELECT 1')
            print(f'[serve_waitress] DB pool warm: {pool.min_size} connection(s) ready')
        except Exception as exc:
            print(f'[serve_waitress] DB pool warm-up skipped (pool stays open): {exc}')
    except Exception as exc:
        print(f'[serve_waitress] DB pool warm-up skipped: {exc}')


def main() -> None:
    _prewarm_templates()
    _warm_db_pool()
    try:
        from core.server_cron import start_server_cron_scheduler
        start_server_cron_scheduler()
    except Exception as exc:
        print(f'[serve_waitress] server cron scheduler skipped: {exc}')

    from waitress import serve

    from permitdaily.wsgi import application

    host = os.environ.get('HOST', '127.0.0.1')
    port = _env_int('PORT', 8000)
    threads = _env_int('WAITRESS_THREADS', 8)
    trusted_proxy = os.environ.get('TRUSTED_PROXY', '*')

    print(f'[serve_waitress] serving on http://{host}:{port} ({threads} threads)')
    print(f'[serve_waitress] trusting X-Forwarded-* from proxy: {trusted_proxy}')
    serve(
        application,
        host=host,
        port=port,
        threads=threads,
        # Let Cloudflare's X-Forwarded-Proto/Host/For reach Django. Without this,
        # waitress strips them and the app redirect-loops behind HTTPS proxies.
        trusted_proxy=trusted_proxy,
        trusted_proxy_headers={
            'x-forwarded-for',
            'x-forwarded-proto',
            'x-forwarded-host',
        },
        # waitress applies X-Forwarded-Proto to wsgi.url_scheme itself, so Django
        # sees the real scheme regardless of SECURE_PROXY_SSL_HEADER.
        clear_untrusted_proxy_headers=True,
    )


if __name__ == '__main__':
    main()
