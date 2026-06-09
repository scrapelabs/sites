"""Google OAuth 2.0 sign-in helpers (server-side "Authorization Code" flow).

The flow at runtime is:

  1. User clicks "Continue with Google" -> we redirect them to
     ``https://accounts.google.com/o/oauth2/v2/auth`` with our client_id, the
     scopes we want, the callback URL, and a random ``state`` we also stash in
     the user's session for CSRF protection.
  2. Google authenticates the user, then redirects back to
     ``/auth/google/callback/?code=...&state=...``.
  3. We POST that ``code`` to ``https://oauth2.googleapis.com/token`` together
     with our client_id + secret. Google returns an access_token (and an
     id_token we don't need to verify here, since we only trust the userinfo
     endpoint over TLS).
  4. We GET ``https://www.googleapis.com/oauth2/v3/userinfo`` with the access
     token in the ``Authorization`` header. The response gives us the user's
     ``sub`` (Google's stable user-id), ``email``, ``email_verified`` flag and
     ``name``.

Configuration lives in the ``system_settings`` table (managed from the admin
dashboard at ``/admin-panel/google-settings/``); env vars
``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET`` act as a fallback so the same
helpers work in both deployments.
"""
import json
import os
import secrets as _secrets
import urllib.parse
import urllib.request

from .db import get_system_setting

GOOGLE_AUTHORIZE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL     = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL  = 'https://www.googleapis.com/oauth2/v3/userinfo'

OAUTH_TIMEOUT = 12  # seconds for Google API calls


def get_google_settings() -> dict:
    """Return the current Google OAuth configuration. Stored values in the
    ``system_settings`` table take priority; the environment variables are a
    fallback so the integration also works from a fresh deploy with secrets."""
    cid = (get_system_setting('google_client_id', '') or '').strip()
    sec = (get_system_setting('google_client_secret', '') or '').strip()
    if not cid:
        cid = (os.environ.get('GOOGLE_CLIENT_ID', '') or '').strip()
    if not sec:
        sec = (os.environ.get('GOOGLE_CLIENT_SECRET', '') or '').strip()
    return {
        'client_id':     cid,
        'client_secret': sec,
        'enabled':       bool(get_system_setting('google_oauth_enabled', False)),
    }


def is_google_oauth_ready() -> bool:
    """True when the admin has both flipped Google sign-in on AND saved a
    valid client id / secret pair (via DB or env vars)."""
    s = get_google_settings()
    return s['enabled'] and bool(s['client_id']) and bool(s['client_secret'])


def get_redirect_uri_override() -> str:
    """Admin-saved redirect URI override (empty string when not set)."""
    return (get_system_setting('google_redirect_uri', '') or '').strip()


def get_authorized_origin_override() -> str:
    """Admin-saved authorized JS origin override (empty string when not set)."""
    return (get_system_setting('google_authorized_origin', '') or '').strip()


def auto_redirect_uri(request) -> str:
    """The redirect URI computed from the current request host.

    Used as the default when the admin has not pinned an explicit override.
    """
    scheme = 'https' if request.is_secure() else request.scheme
    host = request.get_host()
    return f'{scheme}://{host}/auth/google/callback/'


def auto_authorized_origin(request) -> str:
    """The JS origin computed from the current request host."""
    scheme = 'https' if request.is_secure() else request.scheme
    host = request.get_host()
    return f'{scheme}://{host}'


def build_redirect_uri(request) -> str:
    """Produce the absolute redirect URI we hand to Google.

    Must match the URI registered in the Google Cloud Console for the OAuth
    client exactly. The admin can pin an explicit override on the Google
    Sign-In settings page (useful when the dev URL drifts, when the app is
    behind a proxy, or for production on a custom domain). When no override
    is saved, we compute it from the inbound request host.
    """
    override = get_redirect_uri_override()
    if override:
        return override
    return auto_redirect_uri(request)


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the authorization-endpoint URL the user is redirected to.

    Uses ``prompt=select_account`` so the user always sees a Google chooser
    rather than being silently logged in as whoever Google last remembered."""
    params = {
        'client_id':     client_id,
        'redirect_uri':  redirect_uri,
        'response_type': 'code',
        'scope':         'openid email profile',
        'state':         state,
        'access_type':   'online',
        'prompt':        'select_account',
        'include_granted_scopes': 'true',
    }
    return f'{GOOGLE_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}'


def exchange_code_for_token(code: str, client_id: str, client_secret: str,
                            redirect_uri: str) -> dict:
    """Exchange an authorization ``code`` for tokens.

    Returns the decoded JSON payload (``access_token``, ``id_token``,
    ``expires_in``, etc). Raises on HTTP / network / JSON errors."""
    body = urllib.parse.urlencode({
        'code':          code,
        'client_id':     client_id,
        'client_secret': client_secret,
        'redirect_uri':  redirect_uri,
        'grant_type':    'authorization_code',
    }).encode('utf-8')
    req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=body,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept':       'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=OAUTH_TIMEOUT) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
        return json.loads(raw)


def fetch_userinfo(access_token: str) -> dict:
    """Call the Google userinfo endpoint with a bearer token. Returns the
    decoded JSON profile (``sub``, ``email``, ``email_verified``, ``name``,
    etc). Raises on HTTP / network / JSON errors."""
    req = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={
            'Authorization': f'Bearer {access_token}',
            'Accept':        'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=OAUTH_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8', errors='replace'))


def new_state() -> str:
    """Generate an unguessable opaque ``state`` we round-trip through Google
    to defend the callback against CSRF."""
    return _secrets.token_urlsafe(24)
