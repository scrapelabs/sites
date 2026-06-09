"""
Email-based one-time code login flow.

After the user submits the right password on /login/, we generate a 6-digit
code, deliver it via the configured email transport (Resend → SMTP fallback),
and stash a hashed copy of it (plus the pending user identity) in the Django
session. The user is then redirected to /login/verify-code/ where they have
10 minutes and 5 attempts to enter it before having to start over.

Pure stdlib — no new dependencies. The HTML email itself lives at
``templates/core/emails/login_code.html`` so designers can iterate on it
without touching Python.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone

from django.template.loader import render_to_string

from .email_service import send_email, get_transport_status

log = logging.getLogger(__name__)

# Public knobs — kept here so they are easy to tweak from one place.
CODE_LENGTH       = 6     # digits
CODE_TTL_MINUTES  = 10    # how long a code is valid after generation
CODE_MAX_ATTEMPTS = 5     # wrong tries before the code is burned


# ──────────────────────────────────────────────────────────────────────
# Code primitives
# ──────────────────────────────────────────────────────────────────────
def generate_code() -> str:
    """Return a fresh ``CODE_LENGTH``-digit numeric code (zero-padded).

    Uses ``secrets`` for CSPRNG — never ``random``.
    """
    upper = 10 ** CODE_LENGTH
    return f"{secrets.randbelow(upper):0{CODE_LENGTH}d}"


def hash_code(code: str) -> str:
    """Return a hex SHA-256 of the code so we never store it in the clear.

    The session payload sits in the Django session table — hashing is a
    cheap defence-in-depth so a DB leak doesn't expose live login codes.
    """
    return hashlib.sha256((code or '').encode('utf-8')).hexdigest()


def verify_code(submitted: str, expected_hash: str) -> bool:
    """Constant-time equality check against the stored hash."""
    return hmac.compare_digest(hash_code(submitted), expected_hash or '')


def expiry_iso(now: datetime | None = None) -> str:
    """ISO-8601 UTC timestamp ``CODE_TTL_MINUTES`` from ``now``."""
    return ((now or datetime.now(timezone.utc)) + timedelta(minutes=CODE_TTL_MINUTES)).isoformat()


def is_expired(expires_at_iso: str) -> bool:
    """True iff the stored expiry timestamp is in the past."""
    if not expires_at_iso:
        return True
    try:
        exp = datetime.fromisoformat(expires_at_iso)
    except ValueError:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= exp


# ──────────────────────────────────────────────────────────────────────
# Email delivery
# ──────────────────────────────────────────────────────────────────────
def send_login_code_email(
    *,
    to_email: str,
    to_name: str,
    code: str,
    request_ip: str = '',
    request_ua: str = '',
) -> tuple[bool, str]:
    """Render the beautiful HTML login-code email and send it.

    Returns ``(True, message_id_or_empty)`` on success, ``(False, error)``
    otherwise. Falls back to a plain-text only message if the HTML render
    raises (defensive — should never happen).
    """
    # Split the code so the email can render each digit in its own pill,
    # matching the on-screen verify form's separated-digit feel.
    digits = list(code or '')
    ctx = {
        'name':           (to_name or '').split(' ')[0] or 'there',
        'code':           code,
        'code_digits':    digits,
        'expires_in_min': CODE_TTL_MINUTES,
        'request_ip':     request_ip or '',
        'request_ua':     request_ua or '',
        'year':           datetime.now(timezone.utc).year,
    }

    try:
        html = render_to_string('core/emails/login_code.html', ctx)
    except Exception:
        log.exception("Failed to render login_code.html — sending plaintext only")
        html = None

    text = (
        f"Hi {ctx['name']},\n\n"
        f"Your Permitlify sign-in code is:\n\n"
        f"    {code}\n\n"
        f"It expires in {CODE_TTL_MINUTES} minutes. If you didn't request this, "
        f"you can ignore this email — no one can sign in without the code.\n\n"
        f"— The Permitlify team"
    )

    # Code intentionally NOT in the subject line — email subjects can leak
    # via mail-server logs and lock-screen notifications, so we keep the
    # one-time code inside the (encrypted in transit) body only.
    return send_email(
        svc_key='system',
        to=to_email,
        subject="Your Permitlify sign-in code",
        body_text=text,
        body_html=html,
    )


def is_email_transport_configured() -> bool:
    """Cheap static check (one DB lookup, no network) used by the
    login view to decide between the async-email flow and the
    direct-login fallback BEFORE writing pending session state.

    Thin wrapper over ``email_service.get_transport_status()`` so
    callers don't have to know the dict shape.
    """
    return bool(get_transport_status().get('configured'))


def send_login_code_email_async(
    *,
    to_email: str,
    to_name: str,
    code: str,
    request_ip: str = '',
    request_ua: str = '',
) -> tuple[bool, str]:
    """Fire-and-forget version of ``send_login_code_email``.

    Why
    ---
    The synchronous version blocks the login response on the email
    provider's HTTP round-trip (300-2000 ms with Resend, longer with
    SMTP). Doing this in a daemon thread lets us redirect the user to
    the verify-code page immediately and finish the actual send in the
    background — the user then waits on their inbox, not on us.

    Pre-flight check
    ----------------
    Returns ``(False, reason)`` synchronously when no email transport
    is configured (no Resend API key, no SMTP host/user) so the caller
    can fall back to a direct, password-only login. This preserves the
    long-standing safety valve: an admin on a fresh deploy who hasn't
    wired up Resend yet must still be able to log in to the admin panel
    — they need to be able to fix transport in the first place.

    Returns ``(True, '')`` once the worker thread is spawned. Failures
    inside the thread are logged via ``log.warning`` / ``log.exception``;
    the user discovers them by not receiving the email and clicking
    "Resend code" on the verify page (which is still synchronous and
    surfaces the actual error string in its JSON response).

    Security note
    -------------
    The pre-fix synchronous code had a "transport down → bypass code"
    fallback that meant an attacker who could DoS our email provider
    could skip the second factor entirely. This version only falls
    back when transport is *not configured at all* (a static config
    check, not a network outcome), so a transient Resend hiccup no
    longer downgrades login security.
    """
    status = get_transport_status()
    if not status.get('configured'):
        return False, 'No email transport configured (set RESEND_API_KEY or SMTP env vars).'

    def _worker():
        try:
            ok, msg = send_login_code_email(
                to_email   = to_email,
                to_name    = to_name,
                code       = code,
                request_ip = request_ip,
                request_ua = request_ua,
            )
            if ok:
                # Don't log the recipient email — Resend's message id is
                # enough to follow up via their dashboard if needed.
                log.info("login-code email dispatched (resend id=%s)", msg)
            else:
                log.warning("login-code email send failed: %s", msg)
        except Exception:
            log.exception("login-code email worker crashed")

    threading.Thread(
        target=_worker,
        name='login-code-email',
        daemon=True,  # don't block process shutdown on a stuck send
    ).start()
    return True, ''


# ──────────────────────────────────────────────────────────────────────
# Password reset email
# ──────────────────────────────────────────────────────────────────────
RESET_LINK_TTL_HOURS = 1   # how long the reset link is valid


def send_reset_password_email(
    *,
    to_email: str,
    to_name: str,
    reset_link: str,
    request_ip: str = '',
    request_ua: str = '',
) -> tuple[bool, str]:
    """Render the beautiful HTML password-reset email and send it.

    Returns ``(True, message_id_or_empty)`` on success, ``(False, error)``
    otherwise. Falls back to plaintext only if HTML render raises.
    """
    ctx = {
        'name':           (to_name or '').split(' ')[0] or 'there',
        'reset_link':     reset_link,
        'expires_in_hr':  RESET_LINK_TTL_HOURS,
        'request_ip':     request_ip or '',
        'request_ua':     request_ua or '',
        'year':           datetime.now(timezone.utc).year,
    }

    try:
        html = render_to_string('core/emails/reset_password.html', ctx)
    except Exception:
        log.exception("Failed to render reset_password.html — sending plaintext only")
        html = None

    text = (
        f"Hi {ctx['name']},\n\n"
        f"Someone (hopefully you) asked to reset the password on your Permitlify "
        f"account. Click the link below to choose a new one:\n\n"
        f"    {reset_link}\n\n"
        f"This link expires in {RESET_LINK_TTL_HOURS} hour. If you didn't ask "
        f"for a reset, ignore this email — your password stays the same.\n\n"
        f"— The Permitlify team"
    )

    return send_email(
        svc_key='system',
        to=to_email,
        subject="Reset your Permitlify password",
        body_text=text,
        body_html=html,
    )
