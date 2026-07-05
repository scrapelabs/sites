"""Lifecycle email notifications for Permitlify.

Companion to ``core.auth_codes`` (which owns the security-critical login-code
and password-reset emails). Everything in this module is event-driven from
``core.views``:

  * ``send_welcome_email_async``           — fired on signup completion.
  * ``send_payment_success_email_async``   — fired after Whop checkout binds
                                             a paid membership to a user.
  * ``send_login_alert_email_async``       — fired when a session is minted
                                             for a device/IP combination not
                                             seen on the account before.
  * ``send_support_reply_email_async``     — fired when an admin replies to
                                             a support ticket.
  * ``send_support_status_email_async``    — fired when a ticket's status
                                             changes (open / in_progress /
                                             resolved / closed).

Design rules
------------
* Every public ``send_*_email_async`` is fire-and-forget. They spawn a
  daemon ``threading.Thread`` so the calling view can return its HTTP
  response immediately — same pattern PR #119 introduced for the login
  code email. A failed send must never block a redirect or a checkout.

* All template rendering and HTTP calls happen *inside* the worker — the
  caller passes plain strings/ints/bools so the worker doesn't need a
  live ``request`` object. URLs that depend on the host are pre-resolved
  by the caller via ``request.build_absolute_uri(...)``.

* All callers wrap us in ``try / except`` already (defense-in-depth) but
  we also wrap our worker bodies so a template error or transport hiccup
  can never bubble out and crash a background thread.

* No new third-party dependencies — same Django + stdlib surface as
  the rest of ``core/``.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from django.template.loader import render_to_string

from .email_service import send_email

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────
def _spawn(name: str, target):
    """Start a daemon thread with consistent naming. Failures inside the
    target are caught here so a worker crash logs cleanly instead of
    surfacing as an "Exception in thread" stderr line."""
    def _wrap():
        try:
            target()
        except Exception:
            log.exception("email worker %r crashed", name)
    threading.Thread(target=_wrap, name=name, daemon=True).start()


def _first_name(user: dict[str, Any]) -> str:
    """First word of the user's display name, falling back to "there"
    so we never email "Hi ,". Strips Whop / OAuth display oddities."""
    raw = (user.get('name') or '').strip()
    if raw:
        first = raw.split()[0]
        if first:
            return first
    email = (user.get('email') or '').strip()
    if email and '@' in email:
        return email.split('@', 1)[0].title() or 'there'
    return 'there'


def _now_pretty() -> str:
    """ISO-ish friendly timestamp for receipt + login-alert metadata."""
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%b')} {now.day}, {now:%Y · %H:%M UTC}"


def _year() -> int:
    return datetime.now(timezone.utc).year


# ──────────────────────────────────────────────────────────────────────
# DB notification recording
# ──────────────────────────────────────────────────────────────────────
# Every lifecycle email below ALSO writes a row to the ``notifications``
# table so /notifications/ shows the user a real history of what we sent
# them (welcome, payment receipt, login alert, support reply, support
# status). Previously this table was only seeded with demo rows for
# brand-new accounts; once those were dismissed or wiped the page sat
# empty even after real emails went out. Recording happens *after* the
# transport returns ``ok`` so failed sends don't pollute the timeline.
#
# Failure here must never break an email send or a request — the whole
# helper is wrapped in try/except and uses a late import so an unrelated
# DB outage can't take down the email pipeline.
def _record_db_notification(
    *,
    user_id: int | None,
    user_email: str,
    type_key: str,
    type_label: str,
    subject: str,
    preview: str,
    channel: str = 'Email',
    status_key: str = 'sent',
    status_label: str = 'Sent',
) -> None:
    try:
        from . import db as _db
        uid = user_id
        if not uid and user_email:
            row = _db.get_user_by_email(user_email)
            uid = row.get('id') if row else None
        if not uid:
            return
        _db.create_notification(
            user_id=int(uid),
            type_key=type_key, type_label=type_label,
            subject=subject, preview=preview,
            recipient=user_email, channel=channel,
            status_key=status_key, status_label=status_label,
            sent_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        log.exception(
            "notifications-table write failed for user_id=%s type=%s",
            user_id, type_key,
        )


# ──────────────────────────────────────────────────────────────────────
# Welcome — fired on signup
# ──────────────────────────────────────────────────────────────────────
def send_welcome_email_async(
    *,
    user: dict[str, Any],
    dashboard_url: str,
    pricing_url: str,
    support_url: str,
) -> None:
    """Send the post-signup welcome email in the background."""
    to_email = (user.get('email') or '').strip()
    if not to_email or '@' not in to_email:
        return

    ctx = {
        'first_name':    _first_name(user),
        'dashboard_url': dashboard_url,
        'pricing_url':   pricing_url,
        'support_url':   support_url,
        'joined_pretty': _now_pretty(),
        'year':          _year(),
    }

    def _go():
        try:
            html = render_to_string('core/emails/welcome.html', ctx)
        except Exception:
            log.exception("welcome.html render failed")
            html = None
        text = (
            f"Hi {ctx['first_name']},\n\n"
            f"Welcome to Permitlify — your account is set up.\n\n"
            f"Open your dashboard: {dashboard_url}\n\n"
            f"Pick the cities you want to track and we'll start delivering "
            f"AI-scored permit alerts to your inbox. Most contractors close "
            f"their first deal within 14 days.\n\n"
            f"Need a hand? Just reply to this email — a real human reads "
            f"every reply.\n\n"
            f"— The Permitlify team"
        )
        ok, info = send_email(
            'marketing', to_email,
            f"Welcome to Permitlify, {ctx['first_name']} — your account is ready",
            text, html,
        )
        if ok:
            log.info("welcome email dispatched (id=%s)", info)
            _record_db_notification(
                user_id=user.get('id'), user_email=to_email,
                type_key='system', type_label='System',
                subject=f"Welcome to Permitlify, {ctx['first_name']} — your account is ready",
                preview='Your account is set up. Pick the cities you want to track to start receiving AI-scored permit alerts.',
            )
        else:
            log.warning("welcome email send failed: %s", info)

    _spawn('welcome-email', _go)


# ──────────────────────────────────────────────────────────────────────
# Payment success — fired after a Whop membership binds successfully
# ──────────────────────────────────────────────────────────────────────
_PLAN_LABEL = {'starter': 'Starter', 'pro': 'Pro', 'agency': 'Agency'}


def send_payment_success_email_async(
    *,
    user: dict[str, Any],
    plan: str,
    membership_id: str,
    next_charge_pretty: str,
    billing_url: str,
) -> None:
    """Send the payment / activation receipt email in the background.

    ``plan`` is the lowercased internal plan key (``starter`` / ``pro`` /
    ``agency``). The displayed amount is pulled live from
    ``system_settings`` via ``wp.get_plan_price(plan, 'monthly', mode)``
    — the same source the pricing page and onboarding read — using the
    user's own ``whop_mode`` (``wp.mode_for_user``) so a dev-flagged
    tester sees the $1 dev price and a real customer sees the real
    monthly price the admin configured. Previously a hardcoded
    ``{starter:29, pro:99, agency:249}`` dict, which silently went out
    of sync with the admin-set prices on the pricing page.
    """
    to_email = (user.get('email') or '').strip()
    if not to_email or '@' not in to_email:
        return

    plan_key   = (plan or '').strip().lower()
    plan_label = _PLAN_LABEL.get(plan_key, plan_key.title() or 'Subscription')
    try:
        from . import whop as wp
        amount = wp.get_plan_price(plan_key, 'monthly', wp.mode_for_user(user))
    except Exception:
        log.exception("payment_success price lookup failed for plan=%s", plan_key)
        amount = 0
    amount_display = f'${amount}.00 / month' if amount else '—'

    ctx = {
        'first_name':         _first_name(user),
        'plan_key':           plan_key,
        'plan_label':         plan_label,
        'amount_display':     amount_display,
        'billing_email':      to_email,
        'paid_at_pretty':     _now_pretty(),
        'next_charge_pretty': next_charge_pretty or '—',
        'membership_id':      membership_id or '',
        'billing_url':        billing_url,
        'year':               _year(),
    }

    def _go():
        try:
            html = render_to_string('core/emails/payment_success.html', ctx)
        except Exception:
            log.exception("payment_success.html render failed")
            html = None
        text = (
            f"Hi {ctx['first_name']},\n\n"
            f"We've received your payment of {amount_display} for the "
            f"Permitlify {plan_label} plan. Your account is upgraded and "
            f"every feature on the plan is live.\n\n"
            f"Receipt:\n"
            f"  Plan:           Permitlify {plan_label}\n"
            f"  Amount charged: {amount_display}\n"
            f"  Billing email:  {to_email}\n"
            f"  Activated:      {ctx['paid_at_pretty']}\n"
            f"  Next charge:    {ctx['next_charge_pretty']}\n"
        )
        if membership_id:
            text += f"  Reference:      {membership_id}\n"
        text += (
            f"\nManage billing or download invoices: {billing_url}\n\n"
            f"Need an invoice with extra fields, a different billing address, "
            f"or a VAT/EIN number on the receipt? Just reply to this email "
            f"and Billing will handle it.\n\n"
            f"— Permitlify Billing"
        )
        ok, info = send_email(
            'billing', to_email,
            f"Payment received — your Permitlify {plan_label} plan is active",
            text, html,
        )
        if ok:
            log.info("payment_success email dispatched (plan=%s, id=%s)", plan_key, info)
            _record_db_notification(
                user_id=user.get('id'), user_email=to_email,
                type_key='system', type_label='System',
                subject=f"Payment received — your Permitlify {plan_label} plan is active",
                preview=f"{amount_display} charged · next charge {ctx['next_charge_pretty']}",
            )
        else:
            log.warning("payment_success email send failed (plan=%s): %s", plan_key, info)

    _spawn('payment-success-email', _go)


# ──────────────────────────────────────────────────────────────────────
# Login alert — fired only when device or IP is new for the account
# ──────────────────────────────────────────────────────────────────────
def send_login_alert_email_async(
    *,
    user: dict[str, Any],
    method_label: str,
    device: str,
    ip: str,
    ua: str,
    security_url: str,
) -> None:
    """Send the new-device sign-in alert email in the background.

    Callers MUST gate this on ``core.db.is_new_device_for_user(...)``
    so users don't get one of these for every sign-in from an already-
    known device. The check happens in the caller (synchronous DB hit)
    rather than here so we don't spawn a thread and immediately throw
    away its work for the common (recognized-device) path.
    """
    to_email = (user.get('email') or '').strip()
    if not to_email or '@' not in to_email:
        return

    ctx = {
        'first_name':   _first_name(user),
        'method_label': method_label or 'Email + password',
        'device':       device or 'Unknown device',
        'ip':           ip or 'Unknown',
        'ua':           ua or '',
        'when_pretty':  _now_pretty(),
        'security_url': security_url,
        'year':         _year(),
    }

    def _go():
        try:
            html = render_to_string('core/emails/login_alert.html', ctx)
        except Exception:
            log.exception("login_alert.html render failed")
            html = None
        text = (
            f"Hi {ctx['first_name']},\n\n"
            f"A new device just signed in to your Permitlify account.\n\n"
            f"  Device:         {ctx['device']}\n"
            f"  IP address:     {ctx['ip']}\n"
            f"  Sign-in method: {ctx['method_label']}\n"
            f"  When:           {ctx['when_pretty']}\n\n"
            f"Was this you? No action needed.\n\n"
            f"Don't recognize this sign-in? Lock other sessions and change "
            f"your password right away: {security_url}\n\n"
            f"— Permitlify Security"
        )
        ok, info = send_email(
            'system', to_email,
            "New sign-in to your Permitlify account",
            text, html,
        )
        if ok:
            log.info("login_alert email dispatched (id=%s)", info)
            _record_db_notification(
                user_id=user.get('id'), user_email=to_email,
                type_key='system', type_label='System',
                subject='New sign-in to your Permitlify account',
                preview=f"{ctx['device']} · {ctx['ip']} · {ctx['method_label']}",
            )
        else:
            log.warning("login_alert email send failed: %s", info)

    _spawn('login-alert-email', _go)


# ──────────────────────────────────────────────────────────────────────
# Support — replaces the inline-HTML _send_support_email_to_user emails
# ──────────────────────────────────────────────────────────────────────
_STATUS_PRETTY = {
    'open':        'Open',
    'in_progress': 'In Progress',
    'resolved':    'Resolved',
    'closed':      'Closed',
}


def send_support_reply_email_async(
    *,
    to_email: str,
    recipient: str,
    agent: str,
    ref: str,
    subject_topic: str,
    snippet: str,
    link: str,
) -> None:
    """Send the "agent replied" notification using the branded template."""
    to_email = (to_email or '').strip()
    if not to_email or '@' not in to_email:
        return

    ctx = {
        'recipient':     recipient or 'there',
        'agent':         agent or 'Permitlify Support',
        'ref':           ref,
        'subject_topic': subject_topic or 'your support ticket',
        'snippet':       (snippet or '').strip(),
        'link':          link,
        'year':          _year(),
    }

    def _go():
        try:
            html = render_to_string('core/emails/support_reply.html', ctx)
        except Exception:
            log.exception("support_reply.html render failed")
            html = None
        text = (
            f"Hi {ctx['recipient']},\n\n"
            f"You have a new reply from {ctx['agent']} on your support ticket "
            f"{ref} ({ctx['subject_topic']}):\n\n"
            f"\"{ctx['snippet']}\"\n\n"
            f"View the full conversation and reply here:\n{link}\n\n"
            f"— Permitlify Support"
        )
        ok, info = send_email(
            'support', to_email,
            f"[{ref}] Reply from Permitlify Support — {ctx['subject_topic']}",
            text, html,
        )
        if ok:
            _record_db_notification(
                user_id=None, user_email=to_email,
                type_key='system', type_label='Support',
                subject=f"[{ref}] Reply from Permitlify Support — {ctx['subject_topic']}",
                preview=(ctx['snippet'][:160] or f"New reply from {ctx['agent']}"),
            )
        else:
            log.warning("support_reply email send failed for ticket %s: %s", ref, info)

    _spawn('support-reply-email', _go)


def send_support_status_email_async(
    *,
    to_email: str,
    recipient: str,
    ref: str,
    subject_topic: str,
    new_status: str,
    link: str,
) -> None:
    """Send the "ticket status changed" notification."""
    to_email = (to_email or '').strip()
    if not to_email or '@' not in to_email:
        return

    status_key    = (new_status or '').strip().lower()
    status_pretty = _STATUS_PRETTY.get(status_key, status_key.title() or 'Updated')

    ctx = {
        'recipient':     recipient or 'there',
        'ref':           ref,
        'subject_topic': subject_topic or 'your support ticket',
        'status_key':    status_key or 'open',
        'status_pretty': status_pretty,
        'status_lower':  status_pretty.lower(),
        'link':          link,
        'year':          _year(),
    }

    def _go():
        try:
            html = render_to_string('core/emails/support_status.html', ctx)
        except Exception:
            log.exception("support_status.html render failed")
            html = None
        text = (
            f"Hi {ctx['recipient']},\n\n"
            f"Your support ticket {ref} ({ctx['subject_topic']}) is now: "
            f"{status_pretty}.\n\n"
            f"View the conversation here:\n{link}\n\n"
            f"— Permitlify Support"
        )
        ok, info = send_email(
            'support', to_email,
            f"[{ref}] Ticket {status_pretty.lower()} — {ctx['subject_topic']}",
            text, html,
        )
        if ok:
            _record_db_notification(
                user_id=None, user_email=to_email,
                type_key='system', type_label='Support',
                subject=f"[{ref}] Ticket {status_pretty.lower()} — {ctx['subject_topic']}",
                preview=f"Status changed to {status_pretty}",
            )
        else:
            log.warning("support_status email send failed for ticket %s: %s", ref, info)

    _spawn('support-status-email', _go)
