"""Generic Windows WSGI launcher (waitress) for ANY Django site.

This is the multi-site twin of Permitlify's own serve_waitress.py, but it is
project-agnostic: it does NOT hard-code a settings module or a wsgi import.
Drop this file into a Django project's root and run it (new_site.bat copies it
in automatically when the site doesn't already have one).

How it finds your Django project:
    1) DJANGO_SETTINGS_MODULE env var if set (the NSSM service can set it), else
    2) auto-detected from the project's manage.py (the standard
       ``os.environ.setdefault('DJANGO_SETTINGS_MODULE', '<proj>.settings')``).
The WSGI app is then loaded via django.core.wsgi.get_wsgi_application(), so the
project name never has to be typed anywhere.

Environment:
    HOST              bind address (default 127.0.0.1)
    PORT              bind port    (default 8000)
    WAITRESS_THREADS  worker threads (default 8)
    TRUSTED_PROXY     proxy allowed to set X-Forwarded-* (default '*').
                      SAFE only because the origin's port 80 is firewalled to
                      Cloudflare's IP ranges. Without trusting the proxy,
                      waitress strips X-Forwarded-Proto and an HTTPS-forcing
                      Django app redirect-loops forever behind Cloudflare.
"""

import os
import re
import sys


def _detect_settings_module() -> str:
    """Return the Django settings module, from env or the project's manage.py."""
    val = os.environ.get('DJANGO_SETTINGS_MODULE')
    if val:
        return val
    here = os.path.dirname(os.path.abspath(__file__))
    manage = os.path.join(here, 'manage.py')
    if os.path.exists(manage):
        with open(manage, 'r', encoding='utf-8') as fh:
            text = fh.read()
        m = re.search(
            r"""DJANGO_SETTINGS_MODULE['"]\s*,\s*['"]([^'"]+)['"]""", text
        )
        if m:
            return m.group(1)
    sys.exit(
        '[serve_waitress] Could not determine the Django settings module. '
        'Set the DJANGO_SETTINGS_MODULE env var (e.g. mysite.settings) and retry.'
    )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return int(raw)
    except ValueError:
        print(f'[serve_waitress] {name}={raw!r} is not an int; using {default}')
        return default


def main() -> None:
    # Make the project root importable (so "<proj>.settings" resolves) and set
    # the settings module BEFORE django.setup().
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', _detect_settings_module())

    import django

    django.setup()

    from django.core.wsgi import get_wsgi_application
    from waitress import serve

    application = get_wsgi_application()

    host = os.environ.get('HOST', '127.0.0.1')
    port = _env_int('PORT', 8000)
    threads = _env_int('WAITRESS_THREADS', 8)
    trusted_proxy = os.environ.get('TRUSTED_PROXY', '*')

    print(f'[serve_waitress] settings: {os.environ["DJANGO_SETTINGS_MODULE"]}')
    print(f'[serve_waitress] serving on http://{host}:{port} ({threads} threads)')
    print(f'[serve_waitress] trusting X-Forwarded-* from proxy: {trusted_proxy}')
    serve(
        application,
        host=host,
        port=port,
        threads=threads,
        # Let Cloudflare/Caddy's X-Forwarded-Proto/Host/For reach Django.
        # Without this, waitress strips them and an HTTPS-forcing app loops.
        trusted_proxy=trusted_proxy,
        trusted_proxy_headers={
            'x-forwarded-for',
            'x-forwarded-proto',
            'x-forwarded-host',
        },
        clear_untrusted_proxy_headers=True,
    )


if __name__ == '__main__':
    main()
