"""Transactional email sender for Permitlify.

Wraps the Resend HTTP API (https://resend.com/docs/api-reference/emails/send-email)
behind a tiny ``send_email`` helper that:

* Looks up the From address / display name / reply-to for the given service
  category (billing, support, alerts, marketing, system) from
  ``system_settings`` so admins can change them at runtime from the admin panel.
* Uses Resend if ``RESEND_API_KEY`` is set, falls back to Django's SMTP
  backend if SMTP env vars are configured, otherwise returns a clear error
  pointing the admin at the right secret.
* Returns ``(ok: bool, error: str)`` instead of raising — the test workflow
  in ``admin_email_settings`` surfaces the error string directly to the UI.

Pure stdlib + Django; no new third-party deps.
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from .db import get_system_setting

log = logging.getLogger(__name__)


_RESEND_ENDPOINT = 'https://api.resend.com/emails'
_RESEND_TIMEOUT  = 12  # seconds


# ---- Transport credential helpers -------------------------------------------
# Same pattern as core.whop._db_setting: prefer the value saved in
# ``system_settings`` (admin-editable from /admin-panel/email-settings/),
# fall back to the env var so existing deployments keep working.
def _transport_setting(db_key: str, env_key: str, default: str = '') -> str:
    val = get_system_setting(db_key, '')
    if val:
        return str(val)
    return (os.environ.get(env_key) or default or '').strip()


def get_resend_api_key() -> str:
    return _transport_setting('email_resend_api_key', 'RESEND_API_KEY')


def get_smtp_config() -> dict[str, Any]:
    """Return SMTP config — DB first, env fallback. Empty strings on absent."""
    return {
        'host':     _transport_setting('email_smtp_host',     'EMAIL_HOST'),
        'port':     _transport_setting('email_smtp_port',     'EMAIL_PORT', '587'),
        'user':     _transport_setting('email_smtp_user',     'EMAIL_HOST_USER'),
        'password': _transport_setting('email_smtp_password', 'EMAIL_HOST_PASSWORD'),
        'use_tls':  (get_system_setting('email_smtp_use_tls', '') or
                     os.environ.get('EMAIL_USE_TLS', 'true')).strip().lower() in ('1', 'true', 'yes', 'on'),
    }


def get_transport_status() -> dict[str, Any]:
    """Summarise which transport (if any) is currently active.

    Used by the admin page banner so the operator knows at a glance
    whether saving credentials made the page "live".
    """
    if get_resend_api_key():
        return {'active': 'resend', 'label': 'Resend (HTTP API)', 'configured': True}
    smtp = get_smtp_config()
    if smtp['user'] and smtp['host']:
        return {'active': 'smtp',
                'label': f"SMTP ({smtp['host']}:{smtp['port']} as {smtp['user']})",
                'configured': True}
    return {'active': '', 'label': 'None', 'configured': False}


# Service-key -> default From / Name / Reply-To, mirrors core.views._EMAIL_SERVICES.
# Duplicated minimally here so this module has no circular import on views.py.
_DEFAULTS = {
    'billing':   {'from_email': 'billing@permitlify.com',  'from_name': 'Permitlify Billing',  'reply_to': 'billing@permitlify.com'},
    'support':   {'from_email': 'support@permitlify.com',  'from_name': 'Permitlify Support',  'reply_to': 'support@permitlify.com'},
    'alerts':    {'from_email': 'alerts@permitlify.com',   'from_name': 'Permitlify Alerts',   'reply_to': 'noreply@permitlify.com'},
    'marketing': {'from_email': 'hello@permitlify.com',    'from_name': 'Permitlify Team',     'reply_to': 'hello@permitlify.com'},
    'system':    {'from_email': 'noreply@permitlify.com',  'from_name': 'Permitlify',          'reply_to': ''},
}


def get_service_sender(svc_key: str) -> dict[str, str]:
    """Return the resolved {from_email, from_name, reply_to} for a service.

    Falls back to the hardcoded defaults if no admin override is saved.
    Unknown service keys fall back to the ``system`` defaults so callers
    never crash on a typo.
    """
    defs = _DEFAULTS.get(svc_key) or _DEFAULTS['system']
    return {
        'from_email': get_system_setting(f'{svc_key}_from_email', defs['from_email']) or defs['from_email'],
        'from_name':  get_system_setting(f'{svc_key}_from_name',  defs['from_name'])  or defs['from_name'],
        'reply_to':   get_system_setting(f'{svc_key}_reply_to',   defs['reply_to'])   or defs['reply_to'],
    }


def _format_from(name: str, email: str) -> str:
    """Build an RFC 5322 ``From`` header value: ``"Name" <email>``."""
    name = (name or '').strip()
    email = (email or '').strip()
    if not email:
        return ''
    if not name:
        return email
    # Strip quotes that would break the header
    safe_name = name.replace('"', '')
    return f'"{safe_name}" <{email}>'


def _send_via_resend(api_key: str, payload: dict[str, Any]) -> tuple[bool, str]:
    """POST to the Resend API. Returns (ok, error_or_id)."""
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        _RESEND_ENDPOINT,
        data=body,
        method='POST',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type':  'application/json',
            'User-Agent':    'permitlify-admin',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_RESEND_TIMEOUT) as resp:
            data = json.loads(resp.read().decode('utf-8') or '{}')
            msg_id = data.get('id') or ''
            return True, msg_id
    except urllib.error.HTTPError as e:
        # Resend returns JSON {message, name} on errors — surface message.
        try:
            err_body = json.loads(e.read().decode('utf-8') or '{}')
            err_msg  = err_body.get('message') or err_body.get('name') or str(e)
        except Exception:
            err_msg = f'Resend API error (HTTP {e.code})'
        return False, err_msg
    except urllib.error.URLError as e:
        return False, f'Network error contacting Resend: {e.reason}'
    except Exception as e:
        return False, f'Unexpected Resend error: {e}'


def _send_via_smtp(
    *,
    from_header: str,
    reply_to: str,
    to: str,
    subject: str,
    text: str,
    html: str | None,
    headers: dict | None = None,
) -> tuple[bool, str]:
    """SMTP path using the credentials saved in admin → Email Settings
    (DB) with env-var fallback.

    We bypass Django's global EMAIL_BACKEND because the admin can change
    SMTP creds at runtime without restarting; building an EmailBackend
    per-call honours those overrides immediately. Returns (ok, error).
    """
    smtp = get_smtp_config()
    if not smtp['user'] or not smtp['host']:
        return False, (
            'No email transport configured. Add a Resend API key (recommended) '
            'or SMTP host + user + password in the Transport section above, '
            'or via the RESEND_API_KEY / EMAIL_HOST_* env vars.'
        )
    try:
        from django.core.mail.backends.smtp import EmailBackend
        from django.core.mail.message import EmailMultiAlternatives
        try:
            port_int = int(smtp['port'])
        except (TypeError, ValueError):
            port_int = 587
        backend = EmailBackend(
            host=smtp['host'],
            port=port_int,
            username=smtp['user'],
            password=smtp['password'],
            use_tls=bool(smtp['use_tls']),
            timeout=15,
        )
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=from_header,
            to=[to],
            reply_to=[reply_to] if reply_to else None,
            connection=backend,
            headers={str(k): str(v) for k, v in headers.items()} if headers else None,
        )
        if html:
            msg.attach_alternative(html, 'text/html')
        sent = msg.send(fail_silently=False)
        return (sent > 0, '' if sent > 0 else 'SMTP server accepted but rejected the message.')
    except Exception as e:
        return False, f'SMTP send failed: {e}'


def send_email(
    svc_key: str,
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    headers: dict | None = None,
) -> tuple[bool, str]:
    """Send a transactional email for the given service category.

    Returns ``(True, message_id_or_empty)`` on success, ``(False, error)``
    on failure. Never raises so callers (including the admin test endpoint)
    can show the error string directly.
    """
    to = (to or '').strip()
    if not to or '@' not in to:
        return False, 'Recipient email is required and must be valid.'
    subject = (subject or '').strip()
    if not subject:
        return False, 'Subject is required.'
    if not (body_text or '').strip() and not (body_html or '').strip():
        return False, 'Body is required.'

    sender = get_service_sender(svc_key)
    from_header = _format_from(sender['from_name'], sender['from_email'])
    if not from_header:
        return False, f'No From address configured for service "{svc_key}".'

    api_key = get_resend_api_key()
    if api_key:
        payload: dict[str, Any] = {
            'from':    from_header,
            'to':      [to],
            'subject': subject,
            'text':    body_text or '',
        }
        if body_html:
            payload['html'] = body_html
        if sender['reply_to']:
            payload['reply_to'] = sender['reply_to']
        if headers:
            payload['headers'] = {str(k): str(v) for k, v in headers.items()}
        return _send_via_resend(api_key, payload)

    return _send_via_smtp(
        from_header=from_header,
        reply_to=sender['reply_to'],
        to=to,
        subject=subject,
        text=body_text or '',
        html=body_html,
        headers=headers,
    )


def notify_admin_new_user(user: dict, source: str = 'email', ip: str = '') -> tuple[bool, str]:
    """Email the admin when a new user signs up.

    Reads the toggle + recipient from ``system_settings``
    (``notify_signup_enabled`` / ``notify_signup_to``), both editable from
    /admin-panel/email-settings/. Routes via the ``alerts`` service category
    so the From / Display Name / Reply-To come from the same admin override
    used for hot-lead alerts (default sender: alerts@permitlify.com).

    Never raises — signup must not fail because notification delivery did.
    Returns ``(False, 'disabled')`` when the toggle is off or no recipient
    is configured, otherwise ``(ok, info)`` from :func:`send_email`.
    """
    try:
        enabled = (get_system_setting('notify_signup_enabled', '') or '').strip().lower() in ('1', 'true', 'yes', 'on')
        to_addr = (get_system_setting('notify_signup_to', '') or '').strip()
    except Exception:
        log.exception("notify_admin_new_user: failed to read settings")
        return False, 'settings unavailable'
    if not enabled or not to_addr or '@' not in to_addr:
        return False, 'disabled'

    name  = (user.get('name')  or user.get('email') or 'New user').strip()
    email = (user.get('email') or '').strip()
    plan  = (user.get('plan')  or 'starter').strip()
    uid   = user.get('id') or '?'
    ip    = (ip or '').strip() or 'Unknown'
    src_label = 'Google Sign-In' if str(source).lower() == 'google' else 'Email + password'

    subject = f'New Permitlify signup · {email or "(unknown)"}'
    text = (
        'A new user just created an account on Permitlify.\n\n'
        f'Name:    {name}\n'
        f'Email:   {email}\n'
        f'IP:      {ip}\n'
        f'User ID: {uid}\n'
        f'Plan:    {plan}\n'
        f'Source:  {src_label}\n'
    )
    e = _html.escape
    # Email-client friendly: table layout, inline styles, no <style> blocks.
    # The "glowing orb" is a radial-gradient on a table cell with a thick
    # blurred box-shadow halo — degrades gracefully to a flat purple disc
    # in clients that strip box-shadow (Outlook).
    html = f"""\
<!doctype html>
<html><head><meta charset="utf-8"><title>New Permitlify signup</title></head>
<body style="margin:0;padding:0;background:#1e1145;background-image:radial-gradient(ellipse at top,#5b21b6 0%,#2e1065 45%,#1e1145 100%);font-family:-apple-system,'Segoe UI',Inter,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#1e1145;background-image:radial-gradient(ellipse at top,#5b21b6 0%,#2e1065 45%,#1e1145 100%);padding:48px 16px;">
  <tr><td align="center">
    <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;width:100%;">

      <!-- Glowing orb -->
      <tr><td align="center" style="padding:0 0 28px 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td width="92" height="92" align="center" valign="middle"
              style="width:92px;height:92px;border-radius:50%;
                     background:#a78bfa;
                     background-image:radial-gradient(circle at 32% 30%,#ffffff 0%,#e9d5ff 22%,#a78bfa 55%,#7c3aed 100%);
                     box-shadow:0 0 0 6px rgba(167,139,250,.18),0 0 40px 14px rgba(167,139,250,.55),0 0 90px 30px rgba(124,58,237,.45);
                     font-family:-apple-system,'Segoe UI',Inter,Arial,sans-serif;
                     font-size:34px;line-height:92px;color:#ffffff;font-weight:700;
                     text-shadow:0 1px 2px rgba(76,29,149,.5);">
            ✦
          </td>
        </tr></table>
      </td></tr>

      <!-- White bubble card -->
      <tr><td style="background:#ffffff;border-radius:20px;padding:36px 36px 32px 36px;box-shadow:0 24px 60px -12px rgba(45,12,90,.55),0 8px 20px -8px rgba(45,12,90,.35);">

        <div style="font-family:-apple-system,'Segoe UI',Inter,Arial,sans-serif;font-size:11px;letter-spacing:1.4px;text-transform:uppercase;color:#7c3aed;font-weight:700;margin:0 0 8px 0;">
          Permitlify · Admin alert
        </div>
        <h1 style="margin:0 0 6px 0;font-family:-apple-system,'Segoe UI',Inter,Arial,sans-serif;font-size:24px;line-height:1.25;color:#1f1147;font-weight:700;">
          New user just signed up
        </h1>
        <p style="margin:0 0 22px 0;font-family:-apple-system,'Segoe UI',Inter,Arial,sans-serif;font-size:14px;line-height:1.55;color:#5b6478;">
          A fresh account was created on Permitlify. Details below.
        </p>

        <!-- Highlight pill: email -->
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f5f3ff;border:1px solid #e9d5ff;border-radius:14px;margin:0 0 18px 0;">
          <tr>
            <td style="padding:14px 18px;">
              <div style="font-family:-apple-system,'Segoe UI',Inter,Arial,sans-serif;font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:#7c3aed;font-weight:700;margin:0 0 4px 0;">
                New user email
              </div>
              <a href="mailto:{e(email)}" style="font-family:-apple-system,'Segoe UI',Inter,Arial,sans-serif;font-size:18px;font-weight:600;color:#1f1147;text-decoration:none;word-break:break-all;">
                {e(email) or 'unknown@unknown'}
              </a>
            </td>
          </tr>
        </table>

        <!-- Details table -->
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="border-collapse:separate;border-spacing:0;font-family:-apple-system,'Segoe UI',Inter,Arial,sans-serif;font-size:14px;color:#1f1147;">
          <tr>
            <td style="padding:10px 0;border-bottom:1px solid #f1eef9;color:#7c8597;width:120px;">Name</td>
            <td style="padding:10px 0;border-bottom:1px solid #f1eef9;font-weight:600;">{e(name)}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;border-bottom:1px solid #f1eef9;color:#7c8597;">IP address</td>
            <td style="padding:10px 0;border-bottom:1px solid #f1eef9;font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-weight:600;color:#7c3aed;">{e(ip)}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;border-bottom:1px solid #f1eef9;color:#7c8597;">Source</td>
            <td style="padding:10px 0;border-bottom:1px solid #f1eef9;font-weight:600;">{e(src_label)}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;border-bottom:1px solid #f1eef9;color:#7c8597;">Plan</td>
            <td style="padding:10px 0;border-bottom:1px solid #f1eef9;font-weight:600;text-transform:capitalize;">{e(plan)}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;color:#7c8597;">User ID</td>
            <td style="padding:10px 0;font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-weight:600;">#{e(str(uid))}</td>
          </tr>
        </table>

      </td></tr>

      <!-- Footer -->
      <tr><td align="center" style="padding:22px 16px 0 16px;">
        <div style="font-family:-apple-system,'Segoe UI',Inter,Arial,sans-serif;font-size:12px;color:#c4b5fd;line-height:1.6;">
          You're receiving this because new-user signup notifications are enabled in
          <span style="color:#ffffff;">Admin → Email Settings</span>.
        </div>
      </td></tr>

    </table>
  </td></tr>
</table>
</body></html>"""
    try:
        return send_email('alerts', to_addr, subject, text, html)
    except Exception as ex:
        log.exception("notify_admin_new_user: send failed")
        return False, f'send failed: {ex}'


def get_service_health(svc_key: str) -> dict[str, Any]:
    """Return verification metadata for a service category.

    Stored in system_settings:
      - {svc}_verified_at      ISO 8601 timestamp of last admin "mark as working" click
      - {svc}_verified_by      Admin email who marked it
      - {svc}_last_test_at     ISO 8601 of most recent test send attempt
      - {svc}_last_test_to     Recipient of last test send
      - {svc}_last_test_status 'sent' | 'failed' | ''
      - {svc}_last_test_error  Error string if status == 'failed'

    Returns a flat dict suitable for direct template consumption.
    """
    return {
        'verified_at':      get_system_setting(f'{svc_key}_verified_at',      '') or '',
        'verified_by':      get_system_setting(f'{svc_key}_verified_by',      '') or '',
        'last_test_at':     get_system_setting(f'{svc_key}_last_test_at',     '') or '',
        'last_test_to':     get_system_setting(f'{svc_key}_last_test_to',     '') or '',
        'last_test_status': get_system_setting(f'{svc_key}_last_test_status', '') or '',
        'last_test_error':  get_system_setting(f'{svc_key}_last_test_error',  '') or '',
    }
