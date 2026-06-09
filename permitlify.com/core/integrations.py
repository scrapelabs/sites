"""
Outbound integration helpers (Slack, generic webhook, HubSpot OAuth, GoHighLevel
OAuth).

Kept dependency-free (uses urllib) so we don't add a new dependency just to POST
JSON. Each "send" function returns a tuple ``(ok: bool, detail: str)`` so callers
can log a result on the audit ``notifications`` table.

OAuth flows here cover the *server* side only — kicking off the authorize
redirect, exchanging the callback ``code`` for tokens, refreshing expired access
tokens, and a tiny "test connection" GET against each vendor's API. The view
layer is responsible for session/state CSRF protection and for persisting the
returned tokens on the user record.
"""
import ipaddress
import json
import os
import socket
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any


SLACK_TIMEOUT_SECONDS = 6
WEBHOOK_TIMEOUT_SECONDS = 6
OAUTH_TIMEOUT_SECONDS = 10


# ── OAuth provider config ──────────────────────────────────────────────────
#
# Each provider needs an OAuth app registered with the vendor (HubSpot dev
# portal, GoHighLevel marketplace). The owner of the Permitlify install puts
# the resulting client_id / client_secret into env vars below; until they do
# the views surface a friendly "OAuth not configured" state in the UI.

OAUTH_CONFIGS: dict[str, dict[str, Any]] = {
    'hubspot': {
        'name':              'HubSpot',
        'authorize_url':     'https://app.hubspot.com/oauth/authorize',
        'token_url':         'https://api.hubapi.com/oauth/v1/token',
        'scopes':            'crm.objects.contacts.write crm.objects.contacts.read oauth',
        'client_id_env':     'HUBSPOT_CLIENT_ID',
        'client_secret_env': 'HUBSPOT_CLIENT_SECRET',
    },
    'ghl': {
        'name':              'GoHighLevel',
        # GHL's chooselocation flow lets the installer pick which sub-account.
        'authorize_url':     'https://marketplace.gohighlevel.com/oauth/chooselocation',
        'token_url':         'https://services.leadconnectorhq.com/oauth/token',
        'scopes':            'contacts.write contacts.readonly locations.readonly',
        'client_id_env':     'GHL_CLIENT_ID',
        'client_secret_env': 'GHL_CLIENT_SECRET',
    },
}


def oauth_provider_configured(provider: str) -> bool:
    """True iff both client_id and client_secret env vars are set."""
    cfg = OAUTH_CONFIGS.get(provider)
    if not cfg:
        return False
    return bool(os.environ.get(cfg['client_id_env']) and os.environ.get(cfg['client_secret_env']))


def oauth_authorize_url(provider: str, redirect_uri: str, state: str) -> str | None:
    """Build the vendor 'authorize' URL the user is sent to."""
    cfg = OAUTH_CONFIGS.get(provider)
    if not cfg or not oauth_provider_configured(provider):
        return None
    params = {
        'client_id':     os.environ[cfg['client_id_env']],
        'redirect_uri':  redirect_uri,
        'scope':         cfg['scopes'],
        'response_type': 'code',
        'state':         state,
    }
    return cfg['authorize_url'] + '?' + urllib.parse.urlencode(params)


def _oauth_token_post(provider: str, form_fields: dict[str, str]) -> tuple[bool, dict | str]:
    cfg = OAUTH_CONFIGS.get(provider)
    if not cfg:
        return False, 'unknown provider'
    body = urllib.parse.urlencode(form_fields).encode('utf-8')
    req  = urllib.request.Request(
        cfg['token_url'],
        data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept':       'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=OAUTH_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode('utf-8', 'replace') or '{}')
        return True, payload
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:300] if hasattr(e, 'read') else str(e)
        return False, f'http {e.code}: {detail}'
    except urllib.error.URLError as e:
        return False, f'network error: {e.reason}'
    except Exception as e:  # noqa: BLE001
        return False, f'unexpected error: {e.__class__.__name__}: {e}'


def oauth_exchange_code(provider: str, code: str, redirect_uri: str) -> tuple[bool, dict | str]:
    """Exchange an authorization ``code`` for ``{access_token, refresh_token, expires_in, ...}``."""
    cfg = OAUTH_CONFIGS.get(provider)
    if not cfg or not oauth_provider_configured(provider):
        return False, 'OAuth not configured on the server'
    return _oauth_token_post(provider, {
        'grant_type':    'authorization_code',
        'client_id':     os.environ[cfg['client_id_env']],
        'client_secret': os.environ[cfg['client_secret_env']],
        'code':          code,
        'redirect_uri':  redirect_uri,
    })


def oauth_refresh(provider: str, refresh_token: str) -> tuple[bool, dict | str]:
    """Trade a refresh token for a fresh access token."""
    cfg = OAUTH_CONFIGS.get(provider)
    if not cfg or not oauth_provider_configured(provider):
        return False, 'OAuth not configured on the server'
    return _oauth_token_post(provider, {
        'grant_type':    'refresh_token',
        'client_id':     os.environ[cfg['client_id_env']],
        'client_secret': os.environ[cfg['client_secret_env']],
        'refresh_token': refresh_token,
    })


def normalize_token_response(payload: dict) -> dict:
    """Pick only the fields we persist, and compute an absolute expiry."""
    out: dict[str, Any] = {}
    for k in ('access_token', 'refresh_token', 'token_type', 'scope', 'hub_id', 'locationId'):
        if k in payload:
            out[k] = payload[k]
    expires_in = payload.get('expires_in')
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        # 30-second safety margin so we never use a token within seconds of expiring.
        out['expires_at'] = int(time.time()) + int(expires_in) - 30
    return out


def _api_get(url: str, headers: dict[str, str]) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=OAUTH_TIMEOUT_SECONDS) as resp:
            return (200 <= resp.status < 300), f'http {resp.status}'
    except urllib.error.HTTPError as e:
        return False, f'http {e.code}'
    except urllib.error.URLError as e:
        return False, f'network error: {e.reason}'
    except Exception as e:  # noqa: BLE001
        return False, f'unexpected error: {e.__class__.__name__}: {e}'


def hubspot_test_connection(access_token: str) -> tuple[bool, str]:
    """Lightweight HubSpot ping — list 1 owner."""
    if not access_token:
        return False, 'no access token'
    return _api_get('https://api.hubapi.com/crm/v3/owners?limit=1',
                    {'Authorization': f'Bearer {access_token}'})


def ghl_test_connection(access_token: str, location_id: str = '') -> tuple[bool, str]:
    """Lightweight GoHighLevel ping — list 1 contact in the location."""
    if not access_token:
        return False, 'no access token'
    url = 'https://services.leadconnectorhq.com/contacts/?limit=1'
    if location_id:
        url += f'&locationId={urllib.parse.quote(location_id)}'
    return _api_get(url, {
        'Authorization': f'Bearer {access_token}',
        'Version':       '2021-07-28',
        'Accept':        'application/json',
    })


def redact_token(token: str) -> str:
    """Show only the last 4 chars of a sensitive token."""
    if not token:
        return ''
    s = str(token)
    if len(s) <= 4:
        return '****'
    return '****' + s[-4:]


def _is_https_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith('https://')


# Allow the test suite (or a self-hosted ngrok-style relay) to permit a single
# extra hostname so localhost-style fixtures can still hit a webhook. Defaults
# to empty: production deployments only allow public IPs.
_WEBHOOK_HOST_ALLOWLIST = {
    h.strip().lower()
    for h in (os.environ.get('WEBHOOK_HOST_ALLOWLIST') or '').split(',')
    if h.strip()
}


def _public_url_or_error(url: str) -> tuple[bool, str]:
    """Validate that ``url`` is a safe public https endpoint.

    Blocks SSRF avenues users sometimes try (loopback, private/link-local
    ranges, 0.0.0.0, multicast, internal cloud metadata IPs, and unresolvable
    or non-IP hosts). Returns ``(True, '')`` on success or ``(False, reason)``.

    Known limitation — DNS rebinding: we resolve the host once here, then
    urllib resolves it again at connect time. A sufficiently motivated attacker
    can publish a short-TTL record that returns a public IP for the validation
    lookup and an internal IP a moment later. Closing this fully requires
    binding the TCP connection to the *validated* IP while keeping TLS SNI on
    the hostname, or routing all webhook egress through a network-layer
    allowlist proxy. The first practical fix here is the egress proxy at
    deploy time; this function is the application-layer line of defense.
    """
    if not _is_https_url(url):
        return False, 'invalid webhook URL (must be https://)'
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, 'invalid webhook URL'
    host = (parsed.hostname or '').lower()
    if not host:
        return False, 'invalid webhook URL'
    if host in _WEBHOOK_HOST_ALLOWLIST:
        return True, ''
    if host in {'localhost', 'localhost.localdomain', 'ip6-localhost', 'ip6-loopback'}:
        return False, 'webhook host is not publicly routable'
    # Reject ports we never want to dial out on (DB, redis, ssh, etc.).
    try:
        port = parsed.port
    except ValueError:
        return False, 'invalid webhook URL'
    if port is not None and port not in {80, 443} and port < 1024:
        return False, 'webhook port is not allowed'
    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, 'webhook host could not be resolved'
    seen = set()
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, 'webhook host has an invalid IP'
        if (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False, 'webhook host is not publicly routable'
    return True, ''


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that *blocks* all redirects.

    Prevents a redirect-based SSRF bypass: a public webhook endpoint could
    otherwise return ``302 Location: http://169.254.169.254/...`` and trick us
    into making the second request to an internal address (we only validate
    the *initial* URL). Treat any 30x as a hard failure so the caller sees a
    clear error and never re-dials.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(
            req.full_url, code, f'redirects are not followed (Location: {newurl})',
            headers, fp,
        )


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def redact_webhook_url(url: str) -> str:
    """Return a display-safe form of a webhook URL.

    Slack incoming webhooks are bearer credentials — anyone with the full URL
    can post to that channel. We only show the host plus the last 4 characters
    of the path so the user can recognise *which* webhook it was without
    exposing a usable secret in the audit log / CSV export / UI.
    """
    if not url:
        return ''
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.netloc or 'webhook'
        tail = (parsed.path or '').rstrip('/')[-4:]
        return f'{host}/****{tail}' if tail else host
    except Exception:
        return 'webhook (redacted)'


def post_slack_webhook(webhook_url: str, text: str, blocks: list | None = None) -> tuple[bool, str]:
    """
    POST a message to a Slack incoming webhook URL.

    Slack accepts ``{"text": "..."}`` for a plain message, or ``{"blocks": [...]}``
    for richer formatting (we always include ``text`` as a fallback for
    notifications/screen readers).
    """
    if not webhook_url or not webhook_url.startswith('https://hooks.slack.com/'):
        return False, 'URL is not a Slack incoming webhook'
    ok, why = _public_url_or_error(webhook_url)
    if not ok:
        return False, why

    payload: dict = {'text': text or 'Permitlify alert'}
    if blocks:
        payload['blocks'] = blocks

    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=SLACK_TIMEOUT_SECONDS) as resp:
            status = resp.status
            body_txt = resp.read().decode('utf-8', 'replace')[:200]
        if 200 <= status < 300 and body_txt.strip().lower() == 'ok':
            return True, 'ok'
        return False, f'slack returned status={status} body={body_txt!r}'
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:200] if hasattr(e, 'read') else str(e)
        return False, f'http {e.code}: {detail}'
    except urllib.error.URLError as e:
        return False, f'network error: {e.reason}'
    except Exception as e:  # noqa: BLE001 — last-resort guard so a bad webhook never breaks the request
        return False, f'unexpected error: {e.__class__.__name__}: {e}'


def post_generic_webhook(webhook_url: str, payload: dict) -> tuple[bool, str]:
    """POST a JSON body to an arbitrary user-supplied https endpoint."""
    ok, why = _public_url_or_error(webhook_url)
    if not ok:
        return False, why
    body = json.dumps(payload, default=str).encode('utf-8')
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'User-Agent':   'Permitlify-Webhook/1.0',
        },
        method='POST',
    )
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=WEBHOOK_TIMEOUT_SECONDS) as resp:
            status = resp.status
        # Treat redirects as a hard failure too — _NoRedirectHandler raises on
        # 30x, but be defensive in case a server returns 1xx or other oddity.
        if 200 <= status < 300:
            return True, f'http {status}'
        return False, f'http {status}'
    except urllib.error.HTTPError as e:
        return False, f'http {e.code}'
    except urllib.error.URLError as e:
        return False, f'network error: {e.reason}'
    except Exception as e:  # noqa: BLE001
        return False, f'unexpected error: {e.__class__.__name__}: {e}'


def build_permit_alert_blocks(permit: dict) -> tuple[str, list]:
    """
    Build a Slack ``(text, blocks)`` pair for a single permit alert.

    ``permit`` keys used (all optional):
        ai_score, ai_grade, address, project, owner_name, owner, trade,
        permit_type, type, valuation_cents, value, city, state, permit_number,
        id, issued_date, issued
    """
    score   = permit.get('ai_score') or permit.get('score') or '—'
    grade   = permit.get('ai_grade') or permit.get('grade') or ''
    address = permit.get('address') or permit.get('project') or 'Unknown address'
    owner   = permit.get('owner_name') or permit.get('owner') or '—'
    trade   = (permit.get('trade') or '').title() or '—'
    ptype   = permit.get('permit_type') or permit.get('type') or '—'
    city    = permit.get('city') or ''
    state   = permit.get('state') or ''
    pnum    = permit.get('permit_number') or permit.get('id') or '—'
    issued  = permit.get('issued_date') or permit.get('issued') or ''

    if permit.get('valuation_cents') is not None:
        try:
            value = f'${int(permit["valuation_cents"]) // 100:,}'
        except (TypeError, ValueError):
            value = '—'
    else:
        value = permit.get('value') or '—'

    location = f'{city}, {state}'.strip(', ')
    text = f'New {trade or "permit"} alert · score {score} · {address} ({location})'

    blocks = [
        {'type': 'header', 'text': {'type': 'plain_text', 'text': f'🔥 New permit · score {score} {grade}'.strip()}},
        {'type': 'section', 'fields': [
            {'type': 'mrkdwn', 'text': f'*Address*\n{address}'},
            {'type': 'mrkdwn', 'text': f'*Location*\n{location or "—"}'},
            {'type': 'mrkdwn', 'text': f'*Owner*\n{owner}'},
            {'type': 'mrkdwn', 'text': f'*Trade*\n{trade}'},
            {'type': 'mrkdwn', 'text': f'*Type*\n{ptype}'},
            {'type': 'mrkdwn', 'text': f'*Value*\n{value}'},
        ]},
        {'type': 'context', 'elements': [
            {'type': 'mrkdwn', 'text': f'Permit `{pnum}` · issued {issued or "—"} · via Permitlify'},
        ]},
    ]
    return text, blocks
