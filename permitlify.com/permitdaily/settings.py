import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Load variables from a local .env file (if present). override=True makes .env
# the single source of truth: any key it defines wins over a pre-existing
# machine / system environment variable. .env only overrides the keys you
# actually put in it, so unrelated machine-level secrets stay untouched.
# Safe no-op if python-dotenv isn't installed or no .env file exists.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / '.env', override=True)
except ImportError:
    pass

# DEBUG defaults to False so accidental prod deploys never expose tracebacks.
# Local dev sets DJANGO_DEBUG=1 (see the Replit "Start application" workflow).
DEBUG = os.environ.get('DJANGO_DEBUG', '').lower() in ('1', 'true', 'yes')

# Build-time management commands like collectstatic and Django's own checks
# need to import settings but never sign anything, so a placeholder SECRET_KEY
# is safe for them. This lets DigitalOcean's buildpack run collectstatic
# during the build phase before runtime env vars are evaluated, while still
# refusing to BOOT a real server (runserver / gunicorn / wsgi / asgi) without
# a real key.
_BUILD_TIME_COMMANDS = {'collectstatic', 'compilemessages', 'makemigrations',
                        'migrate', 'check', 'test', 'shell', 'showmigrations'}
_is_build_time = len(sys.argv) >= 2 and sys.argv[1] in _BUILD_TIME_COMMANDS

# SECRET_KEY MUST come from the environment for any real server boot. A
# dev-only fallback covers local dev and build-time management commands; it
# is never honored for runserver/gunicorn/wsgi/asgi in production.
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if DEBUG or _is_build_time:
        SECRET_KEY = 'dev-only-DO-NOT-USE-IN-PRODUCTION-xj8k2mq9p'
    else:
        raise RuntimeError(
            'DJANGO_SECRET_KEY environment variable must be set when DEBUG=False.'
        )

ALLOWED_HOSTS = ['*']

# Replit's edge proxy terminates TLS and forwards via X-Forwarded-Proto.
# Without this, request.is_secure() returns False even when the user reached
# us over https — which breaks any code that builds an absolute URL from the
# request scheme (Google OAuth redirect_uri, password-reset emails, etc).
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

CSRF_TRUSTED_ORIGINS = [
    'https://*.replit.dev',
    'https://*.repl.co',
    'https://*.spock.replit.dev',
    'https://*.replit.app',
    'http://localhost:8000',
    'http://127.0.0.1:8000',
    'http://localhost:5000',
    'http://127.0.0.1:5000',
    # Production: behind Cloudflare → DigitalOcean App Platform the
    # ``Host`` header that Django sees may not match the public hostname
    # the browser sent ``Origin: https://permitlify.com`` for, so the
    # default same-origin shortcut isn't enough — we have to enumerate
    # the public origins explicitly.
    'https://permitlify.com',
    'https://www.permitlify.com',
]

# If ``SITE_ORIGIN`` is configured, also trust that origin (and its
# ``www.``/apex sibling) for CSRF. Keeps the allowlist in sync if the
# canonical domain ever changes without needing a settings edit.
_site_origin = os.environ.get('SITE_ORIGIN', '').rstrip('/')
if _site_origin:
    if _site_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_site_origin)
    # Pair apex ↔ www so it doesn't matter which one the email link
    # happens to use.
    if '://www.' in _site_origin:
        _alt = _site_origin.replace('://www.', '://', 1)
    else:
        _alt = _site_origin.replace('://', '://www.', 1)
    if _alt and _alt not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_alt)

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serves collected static files in production (right after
    # SecurityMiddleware as recommended by the WhiteNoise docs). It handles
    # its own gzip/brotli encoding for static assets, so GZipMiddleware below
    # never double-encodes them.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    # GZip every dynamic HTML/JSON response. Our templates are huge inline
    # CSS+JS blobs (base.html alone is ~2k lines / ~80 KB) so this typically
    # cuts transferred bytes by 70-80% and shaves real-world TTFB on slow
    # mobile connections. Safe to put after WhiteNoise — static files are
    # served by WhiteNoise's own pre-compressed pipeline before reaching here.
    'django.middleware.gzip.GZipMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    # Hard 1-hour absolute cap on every logged-in session — independent
    # of the 7-day cookie age. Must run AFTER SessionMiddleware (so
    # request.session is populated) and BEFORE CSRF / Common (so we
    # can short-circuit expired sessions before view dispatch). See
    # SESSION_ABSOLUTE_TIMEOUT_SECONDS above for the constant.
    'core.middleware.SessionAbsoluteTimeoutMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Drops the per-worker admin HTML response cache after any admin
    # POST so the next GET re-renders from fresh DB state. Pairs with
    # the @cached_admin_html(15) decorator on the read-only admin
    # views in core/views.py — together they take repeat admin
    # navigation (Overview → Users → Revenue → ...) from ~250 ms each
    # down to <20 ms once each page is in cache. Must come after CSRF
    # so failed POSTs (403) don't needlessly invalidate the cache.
    'core.middleware.AdminHTMLCacheInvalidationMiddleware',
    # Adds short public Cache-Control to anonymous GETs of marketing pages
    # (/, /blog/, /pricing/, etc.) so a returning visitor — or any
    # CDN we put in front of the app later — can serve the cached HTML
    # without re-rendering. Skips logged-in sessions and any response
    # that's setting cookies. Must run *after* SessionMiddleware so it
    # can inspect ``request.session``.
    'core.middleware.PublicCacheMiddleware',
]

ROOT_URLCONF = 'permitdaily.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'core.context_processors.pricing',
                'core.context_processors.user_session',
                'core.context_processors.site_origin',
                'core.context_processors.cron_schedule',
                'core.context_processors.marketing',
            ],
        },
    },
]

WSGI_APPLICATION = 'permitdaily.wsgi.application'

# Signed-cookie sessions: no server-side storage, so sessions survive every
# container restart, redeploy, and autoscale event. No DB migrations required.
# Trade-off: ~4 KB per session and sessions can't be invalidated server-side
# (which is fine because we only store user_id + a few flags).
SESSION_ENGINE = 'django.contrib.sessions.backends.signed_cookies'
SESSION_COOKIE_AGE = 86400 * 7  # 7 days (signed-cookie max lifetime)

# ── Absolute session-timeout cap (security) ────────────────────────
# Independent of SESSION_COOKIE_AGE (which only governs how long the
# cookie itself stays valid in the browser). Even if the cookie is
# valid for 7 days, the SessionAbsoluteTimeoutMiddleware will bounce
# any logged-in user back to /login/?expired=1 once this many seconds
# have passed since the login_at timestamp was stamped on the session.
#
# Purpose: a contractor leaving a workstation, or an admin with a
# tab that gets bookmarked-and-shared, should not have an indefinitely
# active session. The frontend popup in base.html shows a countdown
# warning 5 minutes before this cap fires so the user is never
# surprised mid-task.
#
# Pairs with: core/middleware.py SessionAbsoluteTimeoutMiddleware,
# core/views.py session_status() / sign_out_everywhere(),
# templates/core/base.html session-expiry modal.
SESSION_ABSOLUTE_TIMEOUT_SECONDS = 3600  # 1 hour

# Cookie + transport hardening — only enforced outside DEBUG so localhost dev
# over plain http keeps working.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
# Safe to enable: SECURE_PROXY_SSL_HEADER above already lets Django recognize
# proxied https traffic, so this won't redirect-loop behind Replit/DO's edge.
SECURE_SSL_REDIRECT = not DEBUG

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/Chicago'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
_STATIC_DIR = BASE_DIR / 'static'
os.makedirs(_STATIC_DIR, exist_ok=True)
STATICFILES_DIRS = [_STATIC_DIR]
# `collectstatic` writes here at deploy time; WhiteNoise serves from this dir.
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}
# Cache fingerprinted static files (logo, ads, etc.) for one year on the
# browser + any intermediate CDN. CompressedManifestStaticFilesStorage above
# rewrites every URL with a content hash, so updates are safe — the URL
# changes whenever the file changes.
WHITENOISE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year, immutable cache

# Canonical site origin used for absolute URLs in robots.txt, sitemap.xml,
# OG/Twitter image tags, and JSON-LD. Falls back to the request host when
# unset. Setting this in the environment locks SEO output to the production
# domain so a forged Host header can't poison the sitemap or canonical URLs.
SITE_ORIGIN = os.environ.get('SITE_ORIGIN', '').rstrip('/')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# App data lives in Supabase Postgres (see core/pg.py and core/db.py). The
# SQLite below is only used for Django internals (contenttypes/admin) — we
# never run migrations against it in this app.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'django_meta.sqlite3',
    }
}

