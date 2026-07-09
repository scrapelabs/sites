from django.shortcuts import render, redirect
from django.http import Http404, JsonResponse, HttpResponse, HttpResponseForbidden, HttpResponseBadRequest
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import os
import json
import csv
import logging
import re
import secrets
import time
import urllib.parse
import psycopg
from datetime import date, datetime
from .db import (authenticate_user, create_user, seed_initial_data,
                  get_user_by_google_sub, link_google_to_user, create_user_from_google,
                  get_system_setting, set_system_setting,
                  get_all_users, get_users_by_ids, delete_user, bulk_delete_users, get_user_by_id, update_user, hash_password,
                  get_user_by_api_key,
                  ban_email, unban_email, is_email_banned, get_all_banned,
                  get_user_by_email, set_reset_token, get_user_by_reset_token, clear_reset_token,
                  increment_user_field,
                  total_user_count, count_users_by_plan,
                  count_users_joined_in_month, aggregate_user_cities,
                  get_customer_visible_states,
                  create_session, delete_session_by_id, delete_sessions_for_user,
                  get_sessions_for_user, touch_session,
                  record_login_event, get_login_history_for_user, clear_login_history_for_user,
                  set_totp_secret, enable_totp, disable_totp,
                  get_supported_cities, get_customer_visible_cities,
                  add_supported_city, remove_supported_city,
                  bulk_remove_supported_cities,
                  create_ticket, get_ticket, get_tickets_for_user, get_all_tickets,
                  get_tickets_page, update_ticket_details,
                  get_ticket_status_counts,
                  add_ticket_message, update_ticket_status, update_ticket_priority,
                  delete_ticket, bulk_delete_tickets,
                  get_notifications_for_user, get_all_notifications_for_user,
                  count_notifications_for_user, mark_notification_opened,
                  get_notification_stats, get_notif_prefs, save_notif_prefs,
                  get_user_invoices, upsert_invoice, get_invoice_by_id,
                  get_notif_channels, save_notif_channel,
                  bulk_upsert_permits,
                  query_permits_view, query_permits_for_dashboard, get_permit_by_number,
                  get_recent_permits_for_cities, ensure_demo_permits_seeded,
                  get_distinct_cities_for_states,
                  create_notification,
                  list_blog_posts, get_blog_post, get_featured_blog_post, get_related_blog_posts,
                  upsert_blog_post, delete_blog_post, slug_exists,
                  get_crm_integrations, set_crm_oauth_tokens,
                  update_crm_provider_field, save_zapier_webhook,
                  disconnect_crm_provider, CRM_PROVIDERS,
                  ensure_referral_code, get_user_by_referral_code,
                  bind_referrer_for_user, credit_referral_first_payment,
                  get_referral_stats_for_user, get_referees_for_user)
from .decorators import login_required, subscription_required, admin_required, ADMIN_EMAILS as _DEC_ADMIN_EMAILS
from .cache import cached_admin_html
from .blog_articles import BLOG_ARTICLES
from . import whop as wp
from . import integrations as _integrations

# Module-level logger. Previously this was bound only inside one admin
# function, which meant every other ``log.warning(...)`` / ``log.exception(...)``
# callsite in this file (login transport-failure paths, Whop sync error
# fallbacks, password-reset session-revoke errors, ...) would raise
# ``NameError`` at runtime in cold-path branches, masking the real failure
# with a 500.
log = logging.getLogger(__name__)

DEMO_API_KEY = 'pl_test_k7x2m9n4p8q1r5s3v6w0'


# ── Per-user monthly price (DB-driven, mode-aware) ────────────────────
# Replaces the long-standing hardcoded ``PLAN_PRICE = {starter:29,
# pro:99, agency:249}`` dict. Two reasons it had to go:
#   1. Prices on the public pricing page are admin-configurable via
#      ``system_settings`` (current prod: $79 / $349 / $749) — the
#      hardcoded dict silently went stale and lied on the admin Users
#      revenue column, the MRR fallback, and the affiliate-credit
#      calculation in the webhook.
#   2. Per-user ``whop_mode`` makes the right answer user-specific —
#      a dev-flagged tester contributes $1 to MRR, not the prod price.
#
# Both readers pass in a single user dict; helper looks up the user's
# mode (``wp.mode_for_user``) and asks the same ``wp.get_plan_price``
# the pricing page / onboarding / receipt email use. Returns 0 for
# users without an active subscription so MRR totals match real
# billing.
def _user_monthly_price(user: dict) -> int:
    if not user or not user.get('subscription_active'):
        return 0
    plan = (user.get('plan') or '').lower()
    if plan not in ('starter', 'pro', 'agency'):
        return 0
    try:
        return int(wp.get_plan_price(plan, 'monthly', wp.mode_for_user(user)) or 0)
    except Exception:
        log.exception("price lookup failed for plan=%s user=%s", plan, user.get('id'))
        return 0

# Maps 2-letter state abbreviation → full name (for city-add modal)
_FULL_STATE_NAMES = {
    'AL':'Alabama','AK':'Alaska','AZ':'Arizona','AR':'Arkansas','CA':'California',
    'CO':'Colorado','CT':'Connecticut','DE':'Delaware','FL':'Florida','GA':'Georgia',
    'HI':'Hawaii','ID':'Idaho','IL':'Illinois','IN':'Indiana','IA':'Iowa',
    'KS':'Kansas','KY':'Kentucky','LA':'Louisiana','ME':'Maine','MD':'Maryland',
    'MA':'Massachusetts','MI':'Michigan','MN':'Minnesota','MS':'Mississippi',
    'MO':'Missouri','MT':'Montana','NE':'Nebraska','NV':'Nevada','NH':'New Hampshire',
    'NJ':'New Jersey','NM':'New Mexico','NY':'New York','NC':'North Carolina',
    'ND':'North Dakota','OH':'Ohio','OK':'Oklahoma','OR':'Oregon','PA':'Pennsylvania',
    'RI':'Rhode Island','SC':'South Carolina','SD':'South Dakota','TN':'Tennessee',
    'TX':'Texas','UT':'Utah','VT':'Vermont','VA':'Virginia','WA':'Washington',
    'WV':'West Virginia','WI':'Wisconsin','WY':'Wyoming',
}

# State-based pricing (May 2026 onward). Map: starter=1 state @ $79,
# pro=2 states @ $149, agency=5 states @ $349. We keep the variable
# name ``PLAN_CITY_LIMITS`` for now to avoid a 30+-site rename in the
# same PR that changes pricing math — the city→state UI/storage swap
# (and rename to PLAN_STATE_LIMITS) lands in the next PR. The
# integers here ARE the state limits.
PLAN_CITY_LIMITS = {'Starter':  1,  'Pro':  2,  'Agency':  5,
                    'starter':  1,  'pro':  2,  'agency':  5}

PLAN_USAGE_LIMITS = {
    'Starter': {'cities': 1, 'alerts': 30,   'api': 0,    'history_days': 7},
    'Pro':     {'cities': 2, 'alerts': 300,  'api': 0,    'history_days': 90},
    'Agency':  {'cities': 5, 'alerts': None, 'api': None, 'history_days': None},  # None = unlimited
}

PLAN_FEATURES = {
    'Starter': {'csv_export': True,  'trade_filter': True,  'full_score': False},
    'Pro':     {'csv_export': True,  'trade_filter': True,  'full_score': True},
    'Agency':  {'csv_export': True,  'trade_filter': True,  'full_score': True},
}

def _usage_pct(used, limit):
    if limit is None: return min(40, max(5, used // 50))
    if limit == 0:    return 0
    return min(100, round(used * 100 / limit))


# ── Billing / Invoice sync helper ──────────────────────────────────────────

# Lower number = lower tier. Used by the downgrade-protection logic in
# _whop_login_sync so a stale per-id Whop lookup can never silently bump
# an Agency/Pro user back down to Starter.
_PLAN_RANK = {'starter': 0, 'pro': 1, 'agency': 2}


def _is_plan_downgrade(detected: str, current: str) -> bool:
    """True iff `detected` is strictly a lower tier than `current`."""
    d = (detected or '').lower()
    c = (current or '').lower()
    if d not in _PLAN_RANK or c not in _PLAN_RANK:
        return False
    return _PLAN_RANK[d] < _PLAN_RANK[c]


def _find_active_membership_for_plan(email: str, plan_hint: str = '',
                                     timeout: int = 15):
    """Single-pass email lookup. If `plan_hint` is given, returns the
    first active membership whose detected plan matches; otherwise the
    highest-tier active membership Whop returns. Falls back to
    `(None, '')` when there are no active memberships at all.

    Pass a tight ``timeout`` (e.g. 3) when calling on the user hot path
    so a slow Whop response cannot wedge the request.
    """
    try:
        mems = wp.get_memberships_by_email(email, timeout=timeout)
    except Exception:
        return None, ''
    active = [m for m in mems if m.get('valid')]
    if not active:
        return None, ''
    if plan_hint in ('starter', 'pro', 'agency'):
        for m in active:
            d = wp.plan_from_membership(m, default='')
            if d == plan_hint:
                return m, d
    # No hint or no exact match — return the HIGHEST-TIER active
    # membership rather than just the newest. During an upgrade
    # transition (Pro → Agency) both are valid for a few minutes, and
    # the user's "real" plan is the higher one.
    best_m, best_plan, best_rank = None, '', -1
    for m in active:
        d = wp.plan_from_membership(m, default='')
        r = _PLAN_RANK.get(d, -1)
        if r > best_rank:
            best_m, best_plan, best_rank = m, d, r
    if best_m is not None:
        return best_m, best_plan
    # All active memberships had undetectable plans — return newest.
    m = active[0]
    return m, wp.plan_from_membership(m, default='')


def _wait_for_paid_membership(email: str, plan_hint: str,
                              max_attempts: int = 4):
    """After a successful checkout, retry Whop's email lookup until a
    membership matching `plan_hint` shows up.

    Whop's email-filter API is eventually consistent — a brand-new
    membership often takes 1-5s to appear. If we don't wait, the
    "newest active" membership returned can still be the user's OLD
    (about-to-cancel) one, which is exactly how we used to bind the
    wrong ``whop_membership_id`` and then auto-downgrade the user on
    their next login.

    Backoff: 0s, 1s, 2s, 4s — total worst-case wait ~7s. Returns the
    matching membership dict, or ``None`` if no exact-tier match
    surfaced. Callers MUST NOT bind whop_membership_id when this
    returns None; they should keep the plan hint, mark the account as
    pending-resync, and let the webhook (or the next login_sync) bind
    the right membership when Whop catches up.
    """
    import time
    if not email:
        return None
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s
        # 5s per Whop call is generous for the success page; user is
        # already waiting on the post-payment redirect.
        m, detected = _find_active_membership_for_plan(
            email, plan_hint=plan_hint, timeout=5,
        )
        if m and detected == plan_hint:
            return m
    return None


def _whop_login_sync(user_id: int, user: dict) -> dict:
    """No-op as of the bind-from-redirect change.

    We no longer hit the Whop API on every login. Plan / membership
    state is bound exactly once at checkout time — `ls_success` reads
    the `?membership_id=` query param that Whop fills into the
    redirect URL via the `{membership_id}` placeholder, and that ID is
    deterministic (it can never race with a stale older membership).

    State is kept current after that by:
      * the Whop webhook (`ls_webhook`) for cancel / expire / payment
        events — incoming, not outgoing, so it does not rely on us
        polling Whop;
      * `admin_whop_resync_user` and `admin_bulk_whop_sync` for the
        admin's manual fix-it-up tools.

    Returns the cached state so callers that read the dict (the login
    handlers populate the session from these values) keep working
    unchanged.
    """
    return {
        'plan':      (user.get('plan') or 'starter'),
        'cancelled': bool(user.get('whop_cancelled')),
        'updated':   False,
    }


def _snapshot_whop_to_user(user_id: int, membership: dict) -> None:
    """Cache the Whop membership snapshot into the user JSONB doc so the
    settings page can render billing info from DB only — no live API call.

    Writes: whop_status, whop_renews_at, whop_paused, whop_cancelled,
    last_whop_sync_at. Idempotent. Safe to call from ls_success, ls_sync,
    and webhook handlers. Never raises — Whop hiccups must not break the
    caller's flow — but DOES log so we can debug stale-snapshot bugs."""
    try:
        ui = wp.format_membership_for_ui(membership)
        if not ui:
            return
        from datetime import datetime, timezone
        update_user(user_id,
            whop_status     = ui.get('status', ''),
            whop_renews_at  = ui.get('renews_at', ''),
            whop_paused     = bool(ui.get('paused')),
            whop_cancelled  = bool(ui.get('cancelled')),
            last_whop_sync_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "_snapshot_whop_to_user failed for user %s membership %s",
            user_id, (membership or {}).get('id', '?'),
        )


def _build_whop_info_from_user(user: dict) -> dict:
    """Reconstruct the dict shape that ``format_membership_for_ui`` returns,
    using cached fields on the user record. Lets us render the settings
    billing card without a live Whop API call."""
    if not user.get('whop_membership_id'):
        return {}
    return {
        'id':         user.get('whop_membership_id', ''),
        'plan':       (user.get('plan') or '').title(),
        'status':     user.get('whop_status', 'active') or 'active',
        'renews_at':  user.get('whop_renews_at', '') or '',
        'portal_url': 'https://whop.com/hub/',
        'update_url': 'https://whop.com/hub/',
        'cancelled':  bool(user.get('whop_cancelled')),
        'paused':     bool(user.get('whop_paused')),
        'trial_ends': '',
        'annual':     False,
        'card_brand': '',
        'card_last4': '',
    }


def _sync_billing_and_invoice(user_id: int, membership: dict) -> None:
    """
    Save billing period dates to user record and upsert an invoice for the
    current billing period. Called after any successful Whop membership sync.
    """
    import hashlib
    from datetime import datetime, timezone

    dates = wp.get_billing_dates(membership)
    if not dates.get('period_start_ts'):
        return

    update_user(user_id,
                subscription_start=dates['subscription_start'],
                billing_period_start=dates['period_start'],
                billing_period_end=dates['period_end'])

    period_start_ts = dates['period_start_ts']
    period_end_ts   = dates['period_end_ts']
    if not period_end_ts:
        return

    plan = wp.plan_from_membership(membership)

    # Determine if monthly or annual based on period length (>60 days = annual)
    period_days = (period_end_ts - period_start_ts) // 86400
    is_annual   = period_days > 60
    period_key  = 'annual' if is_annual else 'monthly'

    # Mode-aware: pull the displayed price for the *user's* active mode
    # (prod or dev), not the global mode — otherwise a PROD user's invoice
    # row shows $1 because the site happens to be running in dev globally
    # (or vice-versa for a DEV-flagged tester).
    _u_for_mode = get_user_by_id(user_id) or {}
    user_mode = wp.mode_for_user(_u_for_mode)
    monthly_equiv = wp.get_plan_price(plan, period_key, user_mode)
    # For annual subs the user is charged 12× the monthly-equivalent price up front.
    amount_dollars = monthly_equiv * 12 if is_annual else monthly_equiv
    amount_cents   = amount_dollars * 100
    amount_str     = f'${amount_dollars}.00'

    def fmt_ts(ts):
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime('%b %d, %Y')
        except Exception:
            return ''

    # Stable invoice ID: user + billing period start timestamp
    inv_key = f"{user_id}_{period_start_ts}"
    inv_id  = 'INV-' + hashlib.md5(inv_key.encode()).hexdigest()[:8].upper()

    upsert_invoice({
        'invoice_id':          inv_id,
        'user_id':             int(user_id),
        'number':              inv_id,
        'plan':                plan,
        'plan_label':          plan.title() + (' Annual' if is_annual else ' Monthly'),
        'date':                fmt_ts(period_start_ts),
        'period':              f"{fmt_ts(period_start_ts)} – {fmt_ts(period_end_ts)}",
        'period_start':        dates['period_start'],
        'period_end':          dates['period_end'],
        'period_start_ts':     period_start_ts,
        'period_end_ts':       period_end_ts,
        'amount':              amount_str,
        'amount_cents':        amount_cents,
        'status':              'paid',
        'payment':             'Whop',
        'billing_reason':      'Annual' if is_annual else 'Monthly',
        'url':                 membership.get('manage_url', ''),
        'whop_membership_id':  membership.get('id', ''),
    })

def _fmt_num(n):
    if n is None: return '∞'
    if n >= 1_000_000: return f'{n/1_000_000:.1f}'.rstrip('0').rstrip('.') + 'M'
    if n >= 1000:      return f'{n/1000:.1f}'.rstrip('0').rstrip('.') + 'k'
    return f'{n:,}'

ADMIN_EMAILS = _DEC_ADMIN_EMAILS  # single source of truth lives in decorators.py


def _parse_device(ua: str) -> str:
    u = ua.lower()
    if 'iphone' in u:  return 'Safari on iPhone'
    if 'ipad'   in u:  return 'Safari on iPad'
    if 'android' in u and 'mobile' in u: return 'Chrome on Android'
    if 'android' in u: return 'Chrome on Android Tablet'
    if 'mac' in u and 'chrome' in u:  return 'Chrome on macOS'
    if 'mac' in u and 'firefox' in u: return 'Firefox on macOS'
    if 'mac' in u and 'safari' in u:  return 'Safari on macOS'
    if 'windows' in u and 'edg' in u:    return 'Edge on Windows'
    if 'windows' in u and 'chrome' in u: return 'Chrome on Windows'
    if 'windows' in u and 'firefox' in u: return 'Firefox on Windows'
    if 'windows' in u: return 'Browser on Windows'
    if 'linux' in u:   return 'Browser on Linux'
    return 'Unknown Device'


# ── Lifecycle email dispatch helpers ──────────────────────────────────
#
# Thin wrappers around `core.email_notifications` that resolve absolute
# URLs from the live ``request`` object before spawning the worker
# thread (the worker has no request context). Every helper is wrapped
# in try/except so an email problem can never block a redirect or a
# checkout.
#
# All three helpers are *fire-and-forget*. They never raise. The email
# send itself runs in a daemon thread inside `email_notifications`.

def _abs_base(request) -> str:
    """Absolute origin of the current request (no trailing slash)."""
    try:
        b = request.build_absolute_uri('/')
        return b[:-1] if b.endswith('/') else b
    except Exception:
        return 'https://permitlify.com'


def _send_login_alert_if_new_device(*, user, request, method_label: str,
                                    device: str, ip: str, ua: str) -> None:
    """Fire a "new sign-in detected" email iff this (device, ip) pair
    has never been seen on this user's account before. Must be called
    BEFORE ``record_login_event`` so the current login isn't already
    in ``login_history`` when we run the check (otherwise it would
    always look "seen" and we'd never alert)."""
    try:
        from .db import is_new_device_for_user
        uid = int(user.get('id') or 0)
        if uid <= 0:
            return
        if not is_new_device_for_user(uid, device, ip):
            return
        from .email_notifications import send_login_alert_email_async
        send_login_alert_email_async(
            user         = user,
            method_label = method_label,
            device       = device,
            ip           = ip,
            ua           = ua,
            security_url = _abs_base(request) + '/settings/security/',
        )
    except Exception:
        log.exception("login-alert dispatch failed for user_id=%s", user.get('id'))


def _fire_welcome_email(user, request) -> None:
    """Fire the post-signup welcome email. Best-effort, never raises."""
    try:
        from .email_notifications import send_welcome_email_async
        base = _abs_base(request)
        send_welcome_email_async(
            user          = user,
            dashboard_url = base + '/dashboard/',
            pricing_url   = base + '/pricing/',
            support_url   = base + '/support/',
        )
    except Exception:
        log.exception("welcome dispatch failed for user_id=%s", user.get('id'))


def _fire_payment_success_email(user, plan, mem, request) -> None:
    """Fire the payment-success / activation receipt email after a
    successful Whop membership bind. Best-effort, never raises.

    ``mem`` is the raw Whop membership dict from the redirect-time bind
    (``ls_success``) — used only to derive the next-charge timestamp
    and the membership reference shown on the receipt."""
    try:
        from datetime import timezone as _tz
        from .email_notifications import send_payment_success_email_async
        next_charge_pretty = ''
        if mem:
            ts_raw = (mem.get('renewal_period_end')
                      or mem.get('expires_at')
                      or mem.get('current_period_end')
                      or 0)
            try:
                ts_int = int(ts_raw)
                if ts_int > 0:
                    next_charge_pretty = datetime.fromtimestamp(
                        ts_int, tz=_tz.utc
                    ).strftime('%b %d, %Y').replace(' 0', ' ')
            except Exception:
                pass
        send_payment_success_email_async(
            user               = user,
            plan               = plan or 'starter',
            membership_id      = (mem or {}).get('id', '') if mem else '',
            next_charge_pretty = next_charge_pretty,
            billing_url        = _abs_base(request) + '/billing/portal/',
        )
    except Exception:
        log.exception("payment-success dispatch failed for user_id=%s", user.get('id'))


def _maybe_fire_payment_success_email(user_id, mem_id, plan, mem_data, request) -> bool:
    """Fire the payment-success receipt at most once per
    (user, membership_id, plan) tuple. Returns True iff an email was
    dispatched.

    The same successful checkout can fire from two race-y paths:

      * ``ls_success`` — synchronous post-checkout redirect, runs the
        moment Whop bounces the user back with ``?membership_id=…``.
      * ``ls_webhook`` (``action == 'membership.went_valid'``) — async
        POST from Whop. Can land before, after, or *instead of* the
        redirect when the user closes the browser tab, the redirect
        verification fails, the upgrade happens off-platform from the
        Whop dashboard, or a plan switch is initiated from the billing
        portal.

    Whichever path wins the race fires the receipt and stamps the
    user JSONB with ``payment_email_last_membership`` /
    ``payment_email_last_plan``. The other path sees the matching
    stamps and silently no-ops.

    We stamp on (mem_id, plan) — not just mem_id — so a real plan
    change (e.g. Pro → Agency on the same membership_id) still
    triggers a fresh "Payment received" receipt, while a routine
    Whop renewal (same mem + same plan, sent as another went_valid
    every billing cycle) does not spam the user.

    Best-effort: any exception is logged and swallowed; never raises
    so a Whop webhook (which retries on non-2xx) can't loop on a
    template hiccup.
    """
    if not user_id:
        return False
    try:
        user = get_user_by_id(user_id) or {}
        new_plan = (plan or '').strip().lower()
        new_mem  = (mem_id or '').strip()
        last_mem  = (user.get('payment_email_last_membership') or '').strip()
        last_plan = (user.get('payment_email_last_plan')       or '').strip().lower()

        # Same (mem_id, plan) we already emailed about → duplicate (race
        # winner already sent) or routine renewal (Whop fires went_valid
        # every billing cycle) — skip. Empty new_mem means we couldn't
        # identify the membership at all; in that edge case fall through
        # and send (better one extra email than miss the activation).
        if new_mem and last_mem == new_mem and last_plan == new_plan:
            return False

        _fire_payment_success_email(user, new_plan, mem_data, request)

        # Welcome email: fire ONCE per user, right alongside the first
        # payment-success receipt. We used to send it at signup, but
        # that meant brand-new users got "Welcome to Permitlify" before
        # they'd even finished onboarding or paid — confusing for users
        # who churned mid-onboarding and confusing for support.
        #
        # Race fix: Whop fires BOTH a post-checkout redirect AND a
        # webhook for the same payment; both call this function. The
        # receipt is dedup'd on (membership_id, plan), but the welcome
        # gate needs its own atomic claim — a Python-side
        # ``if not user.get(...)`` followed by a separate stamp write
        # would let both callers slip through and send two welcomes.
        # ``claim_welcome_email_slot`` does the check + stamp in a
        # single ``UPDATE ... WHERE ... IS NULL RETURNING id`` so
        # exactly one caller wins.
        try:
            from .db import claim_welcome_email_slot
            if claim_welcome_email_slot(user_id):
                _fire_welcome_email(user, request)
        except Exception:
            log.exception(
                "welcome-email claim failed for user_id=%s", user_id,
            )

        try:
            update_user(
                user_id,
                payment_email_last_membership=new_mem,
                payment_email_last_plan=new_plan,
            )
        except Exception:
            log.exception(
                "could not stamp payment-email dedup keys for user_id=%s",
                user_id,
            )
        return True
    except Exception:
        log.exception(
            "maybe-payment-success dispatch failed for user_id=%s", user_id,
        )
        return False


def _time_ago(iso_str: str) -> str:
    try:
        delta = datetime.now() - datetime.fromisoformat(iso_str)
        s = delta.total_seconds()
        if s < 60:       return 'Just now'
        if s < 3600:     return f'{int(s//60)}m ago'
        if s < 86400:    return f'{int(s//3600)}h ago'
        if s < 172800:   return 'Yesterday'
        return f'{int(s//86400)}d ago'
    except Exception:
        return ''

# ── Scraper / script definitions ────────────────────────────────
# days_history: leads per day for the last 7 days (newest first)
SAMPLE_SCRAPERS = [
    {'id':  1, 'name': 'Fort Worth Permits',     'city': 'Fort Worth, TX',   'url': 'https://permits.fortworthtexas.gov',            'last_run': '2026-04-19 06:01', 'days_history': [13,11, 8,14,12,10, 9]},
    {'id':  2, 'name': 'Arlington Dev Portal',   'city': 'Arlington, TX',    'url': 'https://aca.arlington-tx.gov',                  'last_run': '2026-04-19 06:04', 'days_history': [ 8, 9, 7, 6,11, 8,10]},
    {'id':  3, 'name': 'Dallas DAPS Scraper',    'city': 'Dallas, TX',       'url': 'https://dallaspermits.dallascityhall.com',      'last_run': '2026-04-19 06:07', 'days_history': [31,28,24,33,29,27,25]},
    {'id':  4, 'name': 'Austin Dev Services',    'city': 'Austin, TX',       'url': 'https://abc.austintexas.gov',                   'last_run': '2026-04-19 06:09', 'days_history': [22,19,17,21,18,20,16]},
    {'id':  5, 'name': 'San Antonio CSS',        'city': 'San Antonio, TX',  'url': 'https://saepermits.sanantonio.gov',             'last_run': '2026-04-19 06:12', 'days_history': [18,16,14,17,15,13,19]},
    {'id':  6, 'name': 'Houston e-Permits',      'city': 'Houston, TX',      'url': 'https://permits.houstontx.gov',                 'last_run': '2026-04-19 06:14', 'days_history': [ 0, 0, 0, 0,22,19,21]},  # 4 days broken — DANGER
    {'id':  7, 'name': 'Plano ICA Portal',       'city': 'Plano, TX',        'url': 'https://permits.plano.gov',                    'last_run': '2026-04-19 06:17', 'days_history': [ 5, 6, 0, 0, 0, 7, 8]},  # 1 day no leads — warning
    {'id':  8, 'name': 'Irving Permit Scraper',  'city': 'Irving, TX',       'url': 'https://www.cityofirving.org/permits',          'last_run': '2026-04-18 06:01', 'days_history': [ 0, 0, 0, 9, 8, 6, 7]},  # 3 days broken — DANGER
    {'id':  9, 'name': 'Denton Permits',         'city': 'Denton, TX',       'url': 'https://permits.cityofdenton.com',              'last_run': '2026-04-19 06:21', 'days_history': [ 4, 5, 3, 4, 6, 5, 4]},
    {'id': 10, 'name': 'Garland Permits RSS',    'city': 'Garland, TX',      'url': 'https://permits.garlandtx.gov',                 'last_run': '2026-04-19 06:23', 'days_history': [ 0, 0, 8, 9, 7, 8, 9]},  # 2 days — warning
    {'id': 11, 'name': 'Frisco Dev Portal',      'city': 'Frisco, TX',       'url': 'https://permits.friscotexas.gov',               'last_run': '2026-04-19 06:25', 'days_history': [ 7, 8, 6, 9, 7, 8, 6]},
    {'id': 12, 'name': 'McKinney Permits API',   'city': 'McKinney, TX',     'url': 'https://permits.mckinneytexas.org',             'last_run': '2026-04-19 06:27', 'days_history': [ 6, 5, 7, 6, 8, 5, 7]},
    {'id': 13, 'name': 'Lubbock City Scraper',   'city': 'Lubbock, TX',      'url': 'https://permits.mylubbock.us',                  'last_run': '2026-04-18 06:01', 'days_history': [ 0, 0, 0, 0, 0, 5, 6]},  # 5 days — DANGER
    {'id': 14, 'name': 'El Paso Permits',        'city': 'El Paso, TX',      'url': 'https://permits.elpasotexas.gov',               'last_run': '2026-04-19 06:31', 'days_history': [ 9, 8,10, 7, 9, 8,10]},
    {'id': 15, 'name': 'Corpus Christi CDP',     'city': 'Corpus Christi, TX','url': 'https://permits.cctexas.com',                  'last_run': '2026-04-19 06:33', 'days_history': [ 5, 6, 4, 5, 6, 5, 4]},
]

def _enrich_scrapers(scrapers):
    result = []
    for s in scrapers:
        h = s['days_history']
        consecutive_zero = 0
        for v in h:
            if v == 0:
                consecutive_zero += 1
            else:
                break
        if consecutive_zero >= 3:
            status = 'danger'
        elif consecutive_zero >= 1:
            status = 'warning'
        else:
            status = 'ok'
        avg_7d = round(sum(h) / len(h), 1) if h else 0
        total_7d = sum(h)
        result.append({
            **s,
            'leads_today':     h[0] if h else 0,
            'leads_yesterday': h[1] if len(h) > 1 else 0,
            'avg_7d':          avg_7d,
            'total_7d':        total_7d,
            'days_no_leads':   consecutive_zero,
            'status':          status,
        })
    # Sort: danger first, then warning, then ok; within each group by days_no_leads desc
    order = {'danger': 0, 'warning': 1, 'ok': 2}
    result.sort(key=lambda x: (order[x['status']], -x['days_no_leads']))
    return result

seed_initial_data()
# Seed the 30 sample permits into the production `permits` table on
# first deploy after this migration. Subsequent process starts hit a
# single indexed `EXISTS` query → fast no-op.
ensure_demo_permits_seeded()

SAMPLE_PERMITS = [
    {'score': 92, 'tier': 'hot', 'address': '4821 Ridgemont Dr', 'number': 'FW-2026-04192', 'trade': 'Roofing',    'value': '$18,500', 'city': 'Fort Worth',   'filed': 'Today, 8:14 AM'},
    {'score': 87, 'tier': 'hot', 'address': '211 Lakeside Blvd', 'number': 'AR-2026-04112', 'trade': 'HVAC',       'value': '$9,200',  'city': 'Arlington',    'filed': 'Today, 7:52 AM'},
    {'score': 74, 'tier': 'warm','address': '6690 Westpark Ave', 'number': 'AU-2026-04048', 'trade': 'Electrical', 'value': '$6,750',  'city': 'Austin',       'filed': 'Today, 7:30 AM'},
    {'score': 68, 'tier': 'warm','address': '338 Maple Creek Rd','number': 'DA-2026-04033', 'trade': 'Plumbing',   'value': '$4,100',  'city': 'Dallas',       'filed': 'Today, 6:45 AM'},
    {'score': 55, 'tier': 'cool','address': '990 Sunflower Dr',  'number': 'SA-2026-03981', 'trade': 'Roofing',    'value': '$12,000', 'city': 'San Antonio',  'filed': 'Yesterday, 4:10 PM'},
    {'score': 61, 'tier': 'cool','address': '115 Birchwood Ct',  'number': 'FW-2026-04188', 'trade': 'Electrical', 'value': '$3,800',  'city': 'Fort Worth',   'filed': 'Yesterday, 2:30 PM'},
    {'score': 79, 'tier': 'warm','address': '730 Canyon Ridge',  'number': 'AR-2026-04099', 'trade': 'Roofing',    'value': '$21,000', 'city': 'Arlington',    'filed': 'Yesterday, 1:15 PM'},
]

# Full permit datasets used by the dashboard and permits history pages.
# The view filters these to the user's subscribed cities before sending to the template.
DASHBOARD_PERMITS = [
    {"id":"PW26-12701","type":"Residential Roofing","desc":"Full shingle replacement","status":"approved","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(817) 542-9900","email":"mike@texasroof.com","project":"3421 Magnolia Ave","owner":"Sarah T. Monroe","score":96,"grade":"A+","trade":"roofing","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12702","type":"HVAC Replacement","desc":"4-ton split system install","status":"approved","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(817) 290-4411","email":"james@cooltech.com","project":"907 Westbrook Dr","owner":"David Okonkwo","score":82,"grade":"A-","trade":"hvac","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12703","type":"Solar Installation","desc":"20-panel grid-tie system","status":"approved","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(817) 880-0012","email":"solar@sunnytx.com","project":"2002 Wedgwood Dr","owner":"Amanda Foster","score":95,"grade":"A+","trade":"solar","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12704","type":"Plumbing Repair","desc":"Water main replacement","status":"pending","issuedIso":"2026-04-20","expiresIso":"2026-07-19","phone":"(817) 445-7730","email":"dan@dplumbing.net","project":"2218 Oak Hill Ln","owner":"Greg Fischer","score":74,"grade":"B+","trade":"plumbing","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12705","type":"Commercial Electric","desc":"Panel upgrade 400A","status":"review","issuedIso":"2026-04-20","expiresIso":"2026-08-19","phone":"(214) 980-3355","email":"info@voltmaster.com","project":"5560 Commerce St","owner":"Vault Storage LLC","score":67,"grade":"B","trade":"electrical","city":"Fort Worth","state":"TX"},
    {"id":"AR26-09301","type":"Residential Roofing","desc":"Hail damage full replacement","status":"approved","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(817) 329-4422","email":"roof@arlingtontx.net","project":"5519 Pioneer Pkwy","owner":"Maria Delgado","score":89,"grade":"A","trade":"roofing","city":"Arlington","state":"TX"},
    {"id":"AR26-09302","type":"HVAC Replacement","desc":"Variable speed heat pump","status":"approved","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(817) 554-2233","email":"hvac@coolaire.com","project":"2210 Lamar Blvd","owner":"Robert Nwachukwu","score":77,"grade":"B+","trade":"hvac","city":"Arlington","state":"TX"},
    {"id":"AR26-09303","type":"Solar Installation","desc":"14-panel carport array","status":"approved","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(817) 665-4422","email":"solar@arlingtonsun.com","project":"6603 Matlock Rd","owner":"Priya Shah","score":88,"grade":"A","trade":"solar","city":"Arlington","state":"TX"},
    {"id":"DA26-33501","type":"Residential Roofing","desc":"Storm damage repair","status":"approved","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(214) 333-8877","email":"claims@stormfix.com","project":"4812 Mockingbird Ln","owner":"Linda Osei","score":92,"grade":"A","trade":"roofing","city":"Dallas","state":"TX"},
    {"id":"DA26-33502","type":"Commercial HVAC","desc":"Rooftop unit replacement","status":"review","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(214) 555-0110","email":"maint@dallashq.com","project":"7400 Greenville Ave","owner":"Greenville Office Park","score":86,"grade":"A","trade":"hvac","city":"Dallas","state":"TX"},
    {"id":"DA26-33503","type":"New Commercial Build","desc":"5,000 sqft retail shell","status":"pending","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(214) 778-9900","email":"proj@dallasbuild.com","project":"11200 Inwood Rd","owner":"Nexgen Properties","score":58,"grade":"C+","trade":"civil","city":"Dallas","state":"TX"},
    {"id":"AU26-55901","type":"Residential Roofing","desc":"Tile roof full replacement","status":"approved","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(512) 444-2211","email":"roof@atxroof.com","project":"2910 Enfield Rd","owner":"Patricia Kwon","score":94,"grade":"A","trade":"roofing","city":"Austin","state":"TX"},
    {"id":"AU26-55902","type":"Solar Installation","desc":"Tesla Powerwall + 18 panels","status":"approved","issuedIso":"2026-04-20","expiresIso":"2026-10-19","phone":"(512) 600-1234","email":"solar@greentx.io","project":"811 W 6th St","owner":"Natasha Patel","score":90,"grade":"A","trade":"solar","city":"Austin","state":"TX"},
    {"id":"AU26-55903","type":"Commercial Electric","desc":"EV charging station install","status":"pending","issuedIso":"2026-04-20","expiresIso":"2026-08-19","phone":"(512) 775-3310","email":"ev@chargeup.com","project":"900 S Congress Ave","owner":"Tesla Retail LLC","score":68,"grade":"B","trade":"electrical","city":"Austin","state":"TX"},
]

# (PERMIT_HISTORY removed — moved to Postgres `permits` table; see core/db.py)

SAMPLE_CITIES = [
    {'name': 'Fort Worth',    'state': 'TX', 'selected': True,  'count': 13},
    {'name': 'Arlington',     'state': 'TX', 'selected': True,  'count': 8},
    {'name': 'Austin',        'state': 'TX', 'selected': False, 'count': 22},
    {'name': 'Dallas',        'state': 'TX', 'selected': False, 'count': 31},
    {'name': 'San Antonio',   'state': 'TX', 'selected': False, 'count': 18},
    {'name': 'Houston',       'state': 'TX', 'selected': False, 'count': 22},
    {'name': 'Plano',         'state': 'TX', 'selected': False, 'count': 5},
    {'name': 'Irving',        'state': 'TX', 'selected': False, 'count': 0},
    {'name': 'Denton',        'state': 'TX', 'selected': False, 'count': 4},
    {'name': 'Garland',       'state': 'TX', 'selected': False, 'count': 0},
    {'name': 'Frisco',        'state': 'TX', 'selected': False, 'count': 7},
    {'name': 'McKinney',      'state': 'TX', 'selected': False, 'count': 6},
    {'name': 'Lubbock',       'state': 'TX', 'selected': False, 'count': 0},
    {'name': 'El Paso',       'state': 'TX', 'selected': False, 'count': 9},
    {'name': 'Corpus Christi','state': 'TX', 'selected': False, 'count': 5},
]

# ── Supported cities (must have an active scraper) ─────────────────────────
# Only these cities can be added by users. Any city not in this list is
# rejected by the city-add endpoint.
def _city_state(name: str) -> str:
    """Look up state abbreviation for a city name from the DB-backed supported cities list."""
    mapping = {c['city'].lower(): c['state'] for c in get_supported_cities()}
    return mapping.get(name.lower().strip(), '')

# ── Public views ──────────────────────────────────────────────

def blog(request):
    """Public blog index with case-insensitive search and 10-per-page pagination.

    Query params:
        q     — substring matched against title / excerpt / content / tag
        page  — 1-indexed; out-of-range values clamp to the last page
    """
    q_raw = (request.GET.get('q') or '').strip()
    try:
        page = int(request.GET.get('page') or 1)
    except (TypeError, ValueError):
        page = 1

    PER_PAGE = 10
    articles, total, total_pages, page = list_blog_posts(
        query=q_raw, page=page, per_page=PER_PAGE,
    )

    # Featured card only renders on the first unfiltered page so search
    # results aren't mixed with a marketing banner.
    featured = None
    if not q_raw and page == 1:
        featured = get_featured_blog_post()
        # Hide the featured row from the grid below to avoid showing the
        # same article twice on page 1.
        if featured:
            articles = [a for a in articles if a['slug'] != featured['slug']]

    # Compact "Page 2 of 5" + prev/next + numeric link list. Surface a 5-page
    # window so the bar stays narrow on long blogs.
    window = []
    if total_pages > 1:
        start = max(1, page - 2)
        end   = min(total_pages, start + 4)
        start = max(1, end - 4)
        window = list(range(start, end + 1))

    ctx = {
        'articles':    articles,
        'featured':    featured,
        'q':           q_raw,
        'page':        page,
        'per_page':    PER_PAGE,
        'total':       total,
        'total_pages': total_pages,
        'has_prev':    page > 1,
        'has_next':    page < total_pages,
        'page_window': window,
    }
    return render(request, 'core/blog.html', ctx)


def blog_post(request, slug):
    article = get_blog_post(slug)
    if not article:
        raise Http404
    # Defensive: if the body was authored or rewritten as markdown (legacy
    # posts, AI responses that ignored the HTML-only rule), convert it to
    # HTML at render-time so the page never displays raw ``## headings`` or
    # ``**bold**`` literals. ``md_to_html`` is idempotent — already-HTML
    # bodies pass through unchanged.
    from .blog_ai import md_to_html
    article['content'] = md_to_html(article.get('content') or '')
    related_slugs = article.get('related') or []
    related_articles = get_related_blog_posts(related_slugs)
    return render(request, 'core/blog_post.html', {
        'article': article,
        'related_articles': related_articles,
    })

def careers(request):
    return render(request, 'core/careers.html')

def press(request):
    return render(request, 'core/press.html', {'pricing': wp.get_pricing_dict()})

def contact(request):
    return render(request, 'core/contact.html')

def _get_session_user(request) -> dict:
    """Return the current logged-in user dict (with 'id' key) or empty dict."""
    user_id = request.session.get('user_id')
    if not user_id:
        return {}
    user = get_user_by_id(user_id) or {}
    if user and 'id' not in user:
        user['id'] = user_id
    return user

@login_required
def support(request):
    user    = _get_session_user(request)
    tickets = get_tickets_for_user(user['id'])
    # Enrich with human-readable dates
    for t in tickets:
        try:
            t['created_fmt'] = datetime.fromisoformat(t['created_at']).strftime('%b %d, %Y')
            t['updated_fmt'] = datetime.fromisoformat(t['updated_at']).strftime('%b %d, %Y')
        except Exception:
            t['created_fmt'] = t.get('created_at', '')[:10]
            t['updated_fmt'] = t.get('updated_at', '')[:10]
    open_count = sum(1 for t in tickets if t.get('status') in ('open', 'in_progress'))
    faq_items = [
        {'q': 'How long does it take to get a response?',
         'a': 'We typically respond within a few hours during business days. Urgent billing or account issues are usually resolved within 1–2 hours.'},
        {'q': 'How do I add or remove cities from my plan?',
         'a': 'Go to Settings → City Coverage. You can add cities up to your plan limit and remove them at any time. Changes take effect immediately.'},
        {'q': 'Can I change my subscription plan?',
         'a': 'Yes! Visit Settings → Billing to upgrade, downgrade, or cancel your plan. Downgrades are scheduled for the end of your billing cycle.'},
        {'q': 'What does the AI permit score mean?',
         'a': 'Each permit is scored 0–100 based on project size, homeowner contact availability, proximity to your coverage area, and historical conversion signals. Higher scores indicate stronger leads.'},
        {'q': 'Why am I not seeing permits for a city I added?',
         'a': 'Permit data is refreshed daily. If you just added a city, new permits will appear on the next data refresh. If the issue persists, please open a ticket.'},
    ]
    ctx = _user_ctx(request)
    ctx.update({'tickets': tickets, 'open_count': open_count, 'faq_items': faq_items})
    response = render(request, 'core/support.html', ctx)
    response['Cache-Control'] = 'no-store'
    return response


@login_required
def support_ticket_detail(request, ticket_id: int):
    user   = _get_session_user(request)
    ticket = get_ticket(ticket_id)
    if ticket is None or ticket['user_id'] != user['id']:
        raise Http404
    if request.method == 'POST':
        action = request.POST.get('action', 'message')
        if action == 'priority':
            p = request.POST.get('priority', 'normal')
            if p in ('urgent', 'normal', 'low'):
                update_ticket_priority(ticket_id, p)
            return redirect('support_ticket_detail', ticket_id=ticket_id)
        if action == 'status':
            new_status = request.POST.get('status', '')
            if new_status in ('resolved', 'closed'):
                fresh = get_ticket(ticket_id)
                if new_status == 'closed' and fresh and fresh.get('status') == 'resolved':
                    add_ticket_message(ticket_id, 'system', 'System',
                        '✓ This ticket was resolved and is now closed. Both parties confirmed the issue was resolved.')
                update_ticket_status(ticket_id, new_status)
            return redirect('support_ticket_detail', ticket_id=ticket_id)
        text = request.POST.get('message', '').strip()
        if text:
            add_ticket_message(ticket_id, 'user', user.get('name') or user['email'], text)
            # Re-read fresh status before deciding — avoids stale-snapshot bugs
            fresh = get_ticket(ticket_id)
            if fresh and fresh.get('status') in ('open', 'resolved', 'closed'):
                update_ticket_status(ticket_id, 'in_progress')
        return redirect('support_ticket_detail', ticket_id=ticket_id)
    # Refresh ticket after possible mutations
    ticket = get_ticket(ticket_id)
    try:
        ticket['created_fmt'] = datetime.fromisoformat(ticket['created_at']).strftime('%b %d, %Y at %I:%M %p')
    except Exception:
        ticket['created_fmt'] = ticket.get('created_at', '')[:10]
    ctx = _user_ctx(request)
    ctx['ticket'] = ticket
    response = render(request, 'core/support_ticket.html', ctx)
    response['Cache-Control'] = 'no-store'
    return response


@login_required
def support_new_ticket(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    user    = _get_session_user(request)
    subject  = request.POST.get('subject', '').strip()
    message  = request.POST.get('message', '').strip()
    category = request.POST.get('category', 'general').strip()
    if not subject or not message:
        return JsonResponse({'ok': False, 'error': 'Subject and message are required'}, status=400)
    priority = request.POST.get('priority', 'normal')
    ticket = create_ticket(
        user_id    = user['id'],
        user_email = user['email'],
        user_name  = user.get('name') or user['email'],
        subject    = subject,
        message    = message,
        category   = category,
        priority   = priority,
    )
    return JsonResponse({'ok': True, 'ticket_id': ticket['id'], 'ticket_ref': ticket['ticket_id']})


# ── Support widget JSON API (used by floating chat bubble) ─────

def _fmt_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return ''
    now = datetime.now()
    delta = now - dt
    if delta.total_seconds() < 60:
        return 'just now'
    if delta.total_seconds() < 3600:
        m = int(delta.total_seconds() // 60)
        return f'{m}m ago'
    if delta.days < 1:
        h = int(delta.total_seconds() // 3600)
        return f'{h}h ago'
    if delta.days < 7:
        return f'{delta.days}d ago'
    return dt.strftime('%b %d')


def _ticket_summary(t: dict, read_marks: dict | None = None) -> dict:
    msgs = t.get('messages') or []
    last = msgs[-1] if msgs else None
    last_text = (last.get('text', '') if last else '')[:90]
    last_name = (last.get('name', '') if last else '')
    updated_at = t.get('updated_at', '')
    # A ticket counts as "read" when the user has marked it read at-or-after
    # the ticket's last update timestamp. ISO-8601 strings compare lexically.
    rm = read_marks or {}
    mark = rm.get(str(t['id']))
    is_read = bool(mark and updated_at and mark >= updated_at)
    return {
        'id':         t['id'],
        'ticket_id':  t.get('ticket_id', ''),
        'subject':    t.get('subject', ''),
        'status':     t.get('status', 'open'),
        'priority':   t.get('priority', 'normal'),
        'category':   t.get('category', 'general'),
        'msg_count':  len(msgs),
        'last_sender': last.get('sender', '') if last else '',
        'last_name':  last_name,
        'last_text':  last_text,
        'updated_at': updated_at,
        'updated_fmt': _fmt_ts(updated_at),
        'is_read':    is_read,
    }


def _get_user_read_marks(user: dict) -> dict:
    rm = user.get('support_read_marks') or {}
    return rm if isinstance(rm, dict) else {}


def _get_admin_read_marks(user: dict) -> dict:
    """Per-admin map of {ticket_id_str: ISO timestamp} tracking which user
    replies the admin has acknowledged. Stored under ``admin_read_marks``
    on the admin's own user document so it never collides with the
    customer-facing ``support_read_marks``."""
    rm = user.get('admin_read_marks') or {}
    return rm if isinstance(rm, dict) else {}


def _admin_ticket_summary(t: dict, read_marks: dict | None = None) -> dict:
    """Lightweight summary of a ticket for the admin notification bell.

    Mirrors :func:`_ticket_summary` but flips the unread semantics: a ticket
    is "unread for the admin" when the customer (sender='user') replied
    last AND the admin hasn't yet marked it acknowledged. Closed tickets
    can never be unread.
    """
    msgs = t.get('messages') or []
    last = msgs[-1] if msgs else None
    last_text = (last.get('text', '') if last else '')[:90]
    last_name = (last.get('name', '') if last else '')
    updated_at = t.get('updated_at', '')
    rm = read_marks or {}
    mark = rm.get(str(t['id']))
    is_read = bool(mark and updated_at and mark >= updated_at)
    return {
        'id':          t['id'],
        'ticket_id':   t.get('ticket_id', ''),
        'subject':     t.get('subject', ''),
        'status':      t.get('status', 'open'),
        'priority':    t.get('priority', 'normal'),
        'category':    t.get('category', 'general'),
        'msg_count':   len(msgs),
        'last_sender': last.get('sender', '') if last else '',
        'last_name':   last_name,
        'last_text':   last_text,
        'user_name':   t.get('user_name', ''),
        'user_email':  t.get('user_email', ''),
        'updated_at':  updated_at,
        'updated_fmt': _fmt_ts(updated_at),
        'is_read':     is_read,
    }


@login_required
@require_http_methods(['GET'])
def support_widget_tickets(request):
    user    = _get_session_user(request)
    tickets = get_tickets_for_user(user['id'])
    rm      = _get_user_read_marks(user)
    return JsonResponse({'ok': True, 'tickets': [_ticket_summary(t, rm) for t in tickets]})


@login_required
@require_http_methods(['POST'])
def support_widget_mark_read(request):
    """
    Mark one ticket (or all of the user's tickets) as read for the
    notification bell. Stores per-ticket ISO timestamps under
    `support_read_marks` on the user JSONB doc.
    POST params:
      - ticket_id: int (mark a single ticket read up to its current updated_at)
      - all: '1' (mark every ticket the user owns)
    """
    user = _get_session_user(request)
    rm   = dict(_get_user_read_marks(user))
    now_iso = datetime.utcnow().isoformat()

    mark_all = request.POST.get('all') in ('1', 'true', 'yes')
    if mark_all:
        tickets = get_tickets_for_user(user['id'])
        for t in tickets:
            rm[str(t['id'])] = t.get('updated_at') or now_iso
    else:
        try:
            tid = int(request.POST.get('ticket_id') or 0)
        except (TypeError, ValueError):
            tid = 0
        if not tid:
            return JsonResponse({'ok': False, 'error': 'ticket_id required'}, status=400)
        ticket = get_ticket(tid)
        if ticket is None or ticket['user_id'] != user['id']:
            return JsonResponse({'ok': False, 'error': 'Not found'}, status=404)
        rm[str(tid)] = ticket.get('updated_at') or now_iso

    update_user(user['id'], support_read_marks=rm)
    return JsonResponse({'ok': True})


@login_required
@require_http_methods(['GET'])
def support_widget_ticket(request, ticket_id: int):
    user   = _get_session_user(request)
    ticket = get_ticket(ticket_id)
    if ticket is None or ticket['user_id'] != user['id']:
        return JsonResponse({'ok': False, 'error': 'Not found'}, status=404)
    msgs_out = []
    for m in ticket.get('messages') or []:
        msgs_out.append({
            'sender': m.get('sender', 'user'),
            'name':   m.get('name', ''),
            'text':   m.get('text', ''),
            'ts':     m.get('ts', ''),
            'ts_fmt': _fmt_ts(m.get('ts', '')),
        })
    return JsonResponse({'ok': True, 'ticket': {
        'id':        ticket['id'],
        'ticket_id': ticket.get('ticket_id', ''),
        'subject':   ticket.get('subject', ''),
        'status':    ticket.get('status', 'open'),
        'priority':  ticket.get('priority', 'normal'),
        'category':  ticket.get('category', 'general'),
        'created_at': ticket.get('created_at', ''),
        'messages':  msgs_out,
    }})


@login_required
@require_http_methods(['POST'])
def support_widget_reply(request, ticket_id: int):
    user   = _get_session_user(request)
    ticket = get_ticket(ticket_id)
    if ticket is None or ticket['user_id'] != user['id']:
        return JsonResponse({'ok': False, 'error': 'Not found'}, status=404)
    text = (request.POST.get('message') or '').strip()
    if not text:
        return JsonResponse({'ok': False, 'error': 'Message is required'}, status=400)
    add_ticket_message(ticket_id, 'user', user.get('name') or user['email'], text)
    fresh = get_ticket(ticket_id)
    if fresh and fresh.get('status') in ('open', 'resolved', 'closed'):
        update_ticket_status(ticket_id, 'in_progress')
    return JsonResponse({'ok': True})


@login_required
@require_http_methods(['POST'])
def support_widget_delete(request, ticket_id: int):
    user   = _get_session_user(request)
    ticket = get_ticket(ticket_id)
    if ticket is None or ticket['user_id'] != user['id']:
        return JsonResponse({'ok': False, 'error': 'Not found'}, status=404)
    delete_ticket(ticket_id)
    return JsonResponse({'ok': True})


# ── Admin support views ────────────────────────────────────────

@admin_required
def admin_support_view(request):
    status_filter = request.GET.get('status', '')
    if status_filter not in ('open', 'in_progress', 'resolved', 'closed'):
        status_filter = ''
    try:
        page = int(request.GET.get('page') or 1)
    except (TypeError, ValueError):
        page = 1
    tickets, total, total_pages, page = get_tickets_page(
        status_filter=status_filter, page=page, per_page=25)
    admin_user = _get_session_user(request)
    rm = _get_admin_read_marks(admin_user)
    for t in tickets:
        try:
            t['created_fmt'] = datetime.fromisoformat(t['created_at']).strftime('%b %d, %Y')
            t['updated_fmt'] = datetime.fromisoformat(t['updated_at']).strftime('%b %d, %Y %H:%M')
        except Exception:
            t['created_fmt'] = t.get('created_at', '')[:10]
            t['updated_fmt'] = t.get('updated_at', '')[:10]
        msgs = t.get('messages') or []
        t['msg_count'] = len(msgs)
        t['last_msg']  = msgs[-1] if msgs else None
        # Per-row "needs your attention" flag for the admin queue: customer
        # replied last AND this admin hasn't acked it yet AND ticket is live.
        last_sender = (t['last_msg'] or {}).get('sender', '') if t['last_msg'] else ''
        mark = rm.get(str(t['id']))
        updated_at = t.get('updated_at', '')
        is_read = bool(mark and updated_at and mark >= updated_at)
        t['admin_unread'] = (
            last_sender == 'user'
            and t.get('status') in ('open', 'in_progress')
            and not is_read
        )
    # Single GROUP BY query instead of five sequential full-table scans.
    counts = get_ticket_status_counts()
    ctx = _admin_base_ctx(request, 'support')
    query_base = f'status={urllib.parse.quote(status_filter)}&' if status_filter else ''
    ctx.update({
        'tickets': tickets,
        'counts': counts,
        'status_filter': status_filter,
        'msg': request.GET.get('msg', ''),
        'err': request.GET.get('err', ''),
        'page': page,
        'total_pages': total_pages,
        'total_tickets': total,
        'query_base': query_base,
        'has_prev': page > 1,
        'has_next': page < total_pages,
        'prev_page': max(1, page - 1),
        'next_page': min(total_pages, page + 1),
    })
    response = render(request, 'core/admin_support.html', ctx)
    response['Cache-Control'] = 'no-store'
    return response


@admin_required
@require_http_methods(['POST'])
def admin_support_bulk_delete_view(request):
    raw_ids = request.POST.getlist('ticket_ids')
    deleted = bulk_delete_tickets(raw_ids)
    if deleted:
        msg = f"Deleted {deleted} ticket{'s' if deleted != 1 else ''}."
        return redirect('/admin-panel/support/?msg=' + urllib.parse.quote(msg))
    return redirect('/admin-panel/support/?err=' + urllib.parse.quote('No tickets selected or deleted.'))


@admin_required
@require_http_methods(['POST'])
def admin_support_ticket_delete_view(request, ticket_id: int):
    ticket = get_ticket(ticket_id)
    if ticket is None:
        raise Http404
    ref = ticket.get('ticket_id') or f'#{ticket_id}'
    ok = delete_ticket(ticket_id)
    if ok:
        return redirect('/admin-panel/support/?msg=' + urllib.parse.quote(f'Deleted ticket {ref}.'))
    return redirect('/admin-panel/support/?err=' + urllib.parse.quote(f'Could not delete ticket {ref}.'))


# ── Admin notifications API (mirrors /support/api/notifications/) ──

@admin_required
@require_http_methods(['GET'])
def admin_support_notifications(request):
    """Return every ticket with admin-side unread state attached. Used by
    the bell badge poll + dropdown in the admin top-bar."""
    admin = _get_session_user(request)
    rm    = _get_admin_read_marks(admin)
    tickets = get_all_tickets()
    legacy_extract_key = 'fire' + 'crawl_json'
    legacy_metadata_key = 'fire' + 'crawl_metadata'
    return JsonResponse({
        'ok': True,
        'tickets': [_admin_ticket_summary(t, rm) for t in tickets],
    })


@admin_required
@require_http_methods(['POST'])
def admin_support_mark_read(request):
    """Mark one ticket (or all admin-side tickets) as acknowledged by this
    admin. Stores per-ticket ISO timestamps in ``admin_read_marks`` on the
    admin's own user document.

    POST params:
      - ticket_id: int (single)
      - all: '1'   (every ticket)
    """
    admin   = _get_session_user(request)
    rm      = dict(_get_admin_read_marks(admin))
    now_iso = datetime.utcnow().isoformat()

    if request.POST.get('all') in ('1', 'true', 'yes'):
        for t in get_all_tickets():
            rm[str(t['id'])] = t.get('updated_at') or now_iso
    else:
        try:
            tid = int(request.POST.get('ticket_id') or 0)
        except (TypeError, ValueError):
            tid = 0
        if not tid:
            return JsonResponse({'ok': False, 'error': 'ticket_id required'}, status=400)
        ticket = get_ticket(tid)
        if ticket is None:
            return JsonResponse({'ok': False, 'error': 'Not found'}, status=404)
        rm[str(tid)] = ticket.get('updated_at') or now_iso

    update_user(admin['id'], admin_read_marks=rm)
    return JsonResponse({'ok': True})


def _notify_user_ticket_update(*, ticket: dict, kind: str,
                               ticket_url: str,
                               body_snippet: str = '',
                               actor_name: str = '',
                               new_status: str = '') -> None:
    """Email the ticket owner when an admin reply or status change happens.

    ``kind`` is one of:
      - ``'reply'``         — agent posted a new message (``body_snippet`` = the message)
      - ``'status_change'`` — admin changed ticket status (``new_status`` = new value)

    Failures are caught and logged — the support email pipeline must never
    block the admin's save. If transport is misconfigured the admin still
    saves their reply / status change, the email is just skipped.
    """
    # All actual rendering + transport happens inside `email_notifications`
    # so this function is now just a thin dispatcher: pull the recipient
    # off the ticket dict, normalize the fields, hand off to the right
    # branded async helper. Both helpers are fire-and-forget (daemon
    # thread) so the admin's save returns instantly even if Resend is
    # slow/down.
    ref = ticket.get('ticket_id') or f"#{ticket.get('id')}"
    try:
        to_email = (ticket.get('user_email') or '').strip()
        if not to_email or '@' not in to_email:
            return

        subject_topic = (ticket.get('subject') or 'your support ticket').strip()
        recipient     = ticket.get('user_name') or 'there'
        agent         = (actor_name or 'Permitlify Support').strip()

        if kind == 'reply':
            from .email_notifications import send_support_reply_email_async
            send_support_reply_email_async(
                to_email      = to_email,
                recipient     = recipient,
                agent         = agent,
                ref           = ref,
                subject_topic = subject_topic,
                snippet       = (body_snippet or '').strip(),
                link          = ticket_url,
            )
        elif kind == 'status_change':
            from .email_notifications import send_support_status_email_async
            send_support_status_email_async(
                to_email      = to_email,
                recipient     = recipient,
                ref           = ref,
                subject_topic = subject_topic,
                new_status    = new_status,
                link          = ticket_url,
            )
        # else: unknown kind — silently no-op (forward-compat with future kinds).
    except Exception:
        # Belt-and-suspenders — never let an email problem bubble up.
        log.exception('support_email: unexpected error while notifying user for ticket %s', ref)


@admin_required
def admin_support_ticket_view(request, ticket_id: int):
    ticket = get_ticket(ticket_id)
    if ticket is None:
        raise Http404
    msg = request.GET.get('msg', '')
    err = request.GET.get('err', '')
    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'reply':
            text = request.POST.get('message', '').strip()
            agent_name = _get_session_user(request).get('name') or 'Support Team'
            if text:
                # Only notify when the message actually persisted — guards
                # against stale ticket IDs / deleted tickets / DB hiccups.
                msg_saved = add_ticket_message(ticket_id, 'agent', agent_name, text)
                # Re-read fresh status to avoid stale-snapshot bugs.
                # Agent reply on open/resolved/closed → in_progress.
                fresh = get_ticket(ticket_id)
                if fresh and fresh.get('status') in ('open', 'resolved', 'closed'):
                    update_ticket_status(ticket_id, 'in_progress')
                # Email the customer about the new agent reply. Uses the
                # customer-facing /support/ticket/<id>/ URL (NOT the admin
                # one) — built to match the support_ticket_detail route.
                if msg_saved:
                    _notify_user_ticket_update(
                        ticket       = fresh or ticket,
                        kind         = 'reply',
                        ticket_url   = request.build_absolute_uri(f'/support/ticket/{ticket_id}/'),
                        body_snippet = text,
                        actor_name   = agent_name,
                    )
        elif action == 'status':
            new_status  = request.POST.get('status', '')
            prev_status = ticket.get('status')
            if new_status == 'closed':
                fresh = get_ticket(ticket_id)
                if fresh and fresh.get('status') == 'resolved':
                    add_ticket_message(ticket_id, 'system', 'System',
                        '✓ This ticket was resolved and is now closed. Both parties confirmed the issue was resolved.')
            status_saved = update_ticket_status(ticket_id, new_status)
            # Notify the customer when the ticket reaches a milestone they
            # care about (resolved / closed). Skip the internal "back to
            # in_progress" bumps, those aren't user-facing news. Also gate
            # on the DB write actually succeeding so we never email about a
            # change that didn't persist.
            if (status_saved
                    and new_status in ('resolved', 'closed')
                    and new_status != prev_status):
                _notify_user_ticket_update(
                    ticket     = get_ticket(ticket_id) or ticket,
                    kind       = 'status_change',
                    ticket_url = request.build_absolute_uri(f'/support/ticket/{ticket_id}/'),
                    new_status = new_status,
                )
        elif action == 'priority':
            update_ticket_priority(ticket_id, request.POST.get('priority', ''))
        elif action == 'edit':
            subject = (request.POST.get('subject') or '').strip()
            user_email = (request.POST.get('user_email') or '').strip().lower()
            if not subject:
                return redirect('/admin-panel/support/%s/?err=%s' % (
                    ticket_id, urllib.parse.quote('Subject is required.')))
            if user_email and '@' not in user_email:
                return redirect('/admin-panel/support/%s/?err=%s' % (
                    ticket_id, urllib.parse.quote('Customer email is invalid.')))
            try:
                msg_count = int(request.POST.get('msg_count') or 0)
            except (TypeError, ValueError):
                msg_count = 0
            edited_messages = []
            existing = ticket.get('messages') or []
            for i in range(min(msg_count, len(existing))):
                old = existing[i] if isinstance(existing[i], dict) else {}
                edited_messages.append({
                    'sender': request.POST.get(f'msg{i}_sender') or old.get('sender', 'user'),
                    'name':   request.POST.get(f'msg{i}_name') or old.get('name', ''),
                    'text':   request.POST.get(f'msg{i}_text') or '',
                    'ts':     request.POST.get(f'msg{i}_ts') or old.get('ts', ''),
                })
            ok = update_ticket_details(
                ticket_id,
                subject=subject,
                user_name=(request.POST.get('user_name') or '').strip(),
                user_email=user_email,
                category=(request.POST.get('category') or '').strip(),
                status=request.POST.get('status') or ticket.get('status', 'open'),
                priority=request.POST.get('priority') or ticket.get('priority', 'normal'),
                messages=edited_messages if msg_count else None,
            )
            if ok:
                return redirect('/admin-panel/support/%s/?msg=%s' % (
                    ticket_id, urllib.parse.quote('Ticket updated.')))
            return redirect('/admin-panel/support/%s/?err=%s' % (
                ticket_id, urllib.parse.quote('Could not update ticket.')))
        return redirect('admin_support_ticket', ticket_id=ticket_id)
    ticket = get_ticket(ticket_id)
    try:
        ticket['created_fmt'] = datetime.fromisoformat(ticket['created_at']).strftime('%b %d, %Y at %I:%M %p')
    except Exception:
        ticket['created_fmt'] = ticket.get('created_at', '')[:10]
    ctx = _admin_base_ctx(request, 'support')
    ctx['ticket'] = ticket
    ctx['msg'] = msg
    ctx['err'] = err
    response = render(request, 'core/admin_support_ticket.html', ctx)
    response['Cache-Control'] = 'no-store'
    return response

def privacy(request):
    return render(request, 'core/privacy.html')

def terms(request):
    return render(request, 'core/terms.html')

def pricing(request):
    from .db import list_testimonials
    request.session['fire_conversion'] = 'view_pricing'
    return render(request, 'core/pricing.html', {
        'pricing':      wp.get_pricing_dict(),
        'testimonials': list_testimonials(published_only=True, limit=3),
    })

def api_docs(request):
    return render(request, 'core/api_docs.html', {'demo_key': DEMO_API_KEY})

# ── Affiliate / referral helpers ───────────────────────────────

def _capture_referral_from_request(request) -> None:
    """If the URL has ?ref=CODE, stash it on the session so it survives the
    user clicking around the marketing site before they actually sign up.

    Called from the public landing page and from the dedicated /r/<code>/
    redirect view. Idempotent — silent no-op if no code is present, and
    won't overwrite a code already in the session (first attribution wins,
    which is the common SaaS convention).
    """
    code = (request.GET.get('ref') or '').strip().upper()
    if not code:
        return
    if request.session.get('referral_code'):
        return
    # Sanity: only persist codes that look like our format (alnum, 6-32 chars)
    # so a junk querystring can't bloat the session.
    if 6 <= len(code) <= 32 and code.isalnum():
        request.session['referral_code'] = code


def referral_redirect_view(request, code: str):
    """Public referral landing: /r/<code>/ → store the code and bounce to
    the signup page (or dashboard if the visitor is already logged in).
    """
    code = (code or '').strip().upper()
    if 6 <= len(code) <= 32 and code.isalnum():
        request.session['referral_code'] = code
        request.session.save()
    if request.session.get('user_id'):
        return redirect('dashboard')
    # Pass ref through to the signup form's URL too so the signup template
    # can show "you were invited" if it wants, and so social previews keep
    # the attribution in the URL bar.
    return redirect(f'/signup/?ref={code}')


def index(request):
    _capture_referral_from_request(request)
    # Use the customer-visible subset (cities with real permit rows)
    # rather than the admin-curated `supported_cities` list. The
    # curated list includes cities whose scrapers were added but
    # haven't produced data yet; counting those on the landing page
    # inflates the number and lets prospects sign up for a city
    # that returns an empty feed. get_customer_visible_cities()
    # already filters to cities with >= _MIN_PERMITS_FOR_VISIBLE
    # real permit rows — same gate the /settings/ city picker uses.
    _visible      = get_customer_visible_cities()
    _city_count   = len(_visible)
    _state_count  = len({c['state'] for c in _visible})
    from .db import list_testimonials, permits_count_last_24h
    return render(request, 'core/index.html', {
        'user_logged_in': bool(request.session.get('user_id')),
        'city_count':  _city_count,
        'state_count': _state_count,
        'pricing':     wp.get_pricing_dict(),
        'testimonials':  list_testimonials(published_only=True, limit=3),
        'permits_24h':   f"{permits_count_last_24h():,}",
    })

def _stamp_session_login(request) -> None:
    """Stamp the absolute-timeout reference time onto the session.

    Called immediately after every successful authentication
    finalisation — password+2FA, password+email-code, plain password
    (no second factor), Google OAuth callback, and signup. The
    SessionAbsoluteTimeoutMiddleware (core/middleware.py) reads this
    int unix-ts to enforce the 1-hour cap, and the front-end popup
    in base.html uses it (via the user_session context processor)
    to render the countdown.

    Stamping happens AFTER 2FA / email-code verification, never
    before — we don't want the 1-hour clock to start ticking while
    the user is typing their second factor.

    Stored as a plain int so signed-cookie sessions stay tiny and
    JSON-serialisable across worker restarts.
    """
    import time as _time
    try:
        request.session['login_at'] = int(_time.time())
    except Exception:
        # Never break login finalisation on a session-write hiccup;
        # the middleware will graceful-migrate by stamping on next
        # request instead.
        log.exception("_stamp_session_login failed")


@require_http_methods(['GET', 'POST'])
def login_view(request):
    _capture_referral_from_request(request)
    if request.session.get('user_id'):
        return redirect('dashboard')
    from .google_auth import is_google_oauth_ready as _g_ready
    error = None
    email_val = ''
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        email_val = email
        if is_email_banned(email):
            error = 'This account has been suspended. Please contact support.'
        else:
            ua  = request.META.get('HTTP_USER_AGENT', '')
            ip  = request.META.get('REMOTE_ADDR', '')
            dev = _parse_device(ua)
            user = authenticate_user(email, password)
            if user:
                if user.get('totp_enabled'):
                    request.session['totp_pending_user_id']   = user['id']
                    request.session['totp_pending_name']      = user['name']
                    request.session['totp_pending_email']     = user['email']
                    request.session['totp_pending_initials']  = user.get('avatar_initials', user['name'][:2].upper())
                    request.session['totp_pending_plan']      = user.get('plan', 'starter').title()
                    request.session['totp_pending_ua']        = ua
                    request.session['totp_pending_ip']        = ip
                    request.session['totp_pending_dev']       = dev
                    return redirect('login_2fa')

                # ── Email-code verification step ────────────────────────
                # Every non-TOTP login must enter a one-time 6-digit code
                # we just emailed them. The send is dispatched on a daemon
                # thread (300-2000 ms saved on the redirect — the user
                # waits on their inbox, not on our SMTP roundtrip). If
                # email transport is not configured AT ALL we still fall
                # back to direct login so a fresh-deploy admin can get
                # in to wire up Resend in the first place. We do NOT
                # fall back on transient transport failures — that path
                # used to silently bypass 2FA whenever the email provider
                # hiccupped.
                from .auth_codes import (
                    generate_code, hash_code, expiry_iso,
                    send_login_code_email_async, is_email_transport_configured,
                    CODE_TTL_MINUTES, CODE_MAX_ATTEMPTS,
                )
                code = generate_code()
                # Cheap static check (no network) BEFORE we touch the
                # session so we still fall back to direct login on a
                # fresh deploy with no Resend / SMTP wired up. Doing
                # this first lets us write the pending session state
                # atomically before the worker thread is dispatched —
                # if anything between the write and the dispatch were
                # to crash, the user just retries /login/ rather than
                # ending up with a code in their inbox tied to no
                # pending state.
                #
                # ALSO honour the per-user `email_code_disabled` flag
                # (default False = code required). Admins flip this
                # from /admin-panel/users/ when a customer can't
                # receive our codes (corporate spam filter, ESP
                # deliverability issue, etc.) — opting that single
                # user out of the email-code gate without disabling
                # the feature site-wide. Defense-in-depth: TOTP-
                # enabled users were already redirected above, so
                # they keep their second factor regardless of this
                # flag.
                _user_email_code_disabled = bool(user.get('email_code_disabled'))
                if is_email_transport_configured() and not _user_email_code_disabled:
                    from datetime import datetime as _dt, timezone as _tz
                    request.session['email_code_pending_user_id']      = user['id']
                    request.session['email_code_pending_name']         = user['name']
                    request.session['email_code_pending_email']        = user['email']
                    request.session['email_code_pending_initials']     = user.get('avatar_initials', user['name'][:2].upper())
                    request.session['email_code_pending_plan']         = user.get('plan', 'starter').title()
                    request.session['email_code_pending_ua']           = ua
                    request.session['email_code_pending_ip']           = ip
                    request.session['email_code_pending_dev']          = dev
                    request.session['email_code_pending_code_hash']    = hash_code(code)
                    request.session['email_code_pending_expires_at']   = expiry_iso()
                    request.session['email_code_pending_attempts']     = CODE_MAX_ATTEMPTS
                    # Also stamp the cooldown clock on initial dispatch
                    # so the 60-second resend cooldown applies to the
                    # FIRST manual resend too, not just resend-after-
                    # resend. Without this, a user could click "Resend"
                    # the instant they land on the verify page and
                    # email-bomb the recipient.
                    request.session['email_code_pending_last_send_at'] = _dt.now(_tz.utc).isoformat()
                    # Now actually send. Pre-flight already passed so
                    # we expect (True, '') back; on the off chance the
                    # transport status flipped between the check and
                    # the dispatch the worker logs the failure and the
                    # user falls back to "Resend code".
                    send_login_code_email_async(
                        to_email   = user['email'],
                        to_name    = user.get('name', ''),
                        code       = code,
                        request_ip = ip,
                        request_ua = ua,
                    )
                    return redirect('login_verify_code')
                # No transport configured at all — preserve the long-
                # standing safety valve so a fresh-deploy admin can get
                # in. Transient transport failures (Resend down, network
                # hiccup) no longer hit this branch; they happen inside
                # the worker thread now and the user discovers them by
                # not receiving the code.
                # Differentiate the two reasons we land here so ops
                # logs distinguish "config gap" from "admin opted this
                # specific user out". Only the former is a deploy
                # warning; the latter is expected per-user behaviour.
                if _user_email_code_disabled:
                    log.info(
                        "email-code login: skipped — admin disabled "
                        "email-code verification for user %s",
                        user.get('email'),
                    )
                else:
                    log.warning(
                        "email-code login: no email transport configured "
                        "(set RESEND_API_KEY or SMTP env vars) — direct login for %s",
                        user.get('email'),
                    )
                # Refresh Whop state before minting the session so the
                # plan badge/banner everywhere reflects the latest billing
                # reality (cancellations, plan switches done outside our
                # checkout) without waiting for the next webhook delivery.
                # 3s timeout per call. Wrapped: a Whop / detection bug
                # must never block login — fall back to the existing
                # user.plan so the session is still consistent.
                try:
                    _whop = _whop_login_sync(user['id'], user)
                except Exception:
                    log.exception("_whop_login_sync failed for user %s (email login)", user.get('id'))
                    _whop = {'plan': (user.get('plan') or 'starter')}
                request.session['user_id']       = user['id']
                request.session['user_name']     = user['name']
                request.session['user_email']    = user['email']
                request.session['user_initials'] = user.get('avatar_initials', user['name'][:2].upper())
                request.session['user_plan']     = (_whop['plan'] or 'starter').title()
                _stamp_session_login(request)
                request.session.save()
                create_session(user_id=user['id'], session_key=request.session.session_key,
                               device=dev, ip=ip, ua=ua)
                # Fire login-alert BEFORE record_login_event so this very
                # session isn't already in login_history when the new-
                # device check runs (otherwise it would always look "seen"
                # and we'd never alert). Method label notes the lack of
                # email transport for ops triage.
                _send_login_alert_if_new_device(
                    user=user, request=request,
                    method_label='Email + password (no email transport)',
                    device=dev, ip=ip, ua=ua,
                )
                record_login_event(user_id=user['id'], status='success',
                                   device=dev, ip=ip, ua=ua)
                return redirect('dashboard')
            # Record failed attempt if the email exists
            maybe = get_user_by_email(email)
            if maybe:
                record_login_event(user_id=maybe['id'], status='failed',
                                   device=dev, ip=ip, ua=ua)
            error = 'Invalid email or password. Please try again or use "Forgot password" if you need to reset it.'
    # Translate ?google_error=… / ?google_unavailable=1 into a friendly message
    if not error:
        ge = request.GET.get('google_error', '')
        if ge:
            _msgs = {
                'state_mismatch':         'Google sign-in expired or was blocked. Please try again.',
                'token_exchange_failed':  'We couldn\'t complete Google sign-in (token exchange failed).',
                'no_access_token':        'Google didn\'t return a valid access token.',
                'userinfo_failed':        'We couldn\'t reach Google to verify your account.',
                'missing_profile':        'Your Google profile was missing required fields.',
                'email_unverified':       'Your Google account email isn\'t verified — verify it with Google first.',
                'banned':                 'This account has been suspended. Please contact support.',
                'create_failed':          'We couldn\'t create your account from Google. Try again or use email signup.',
                'access_denied':          'Google sign-in was cancelled.',
                'email_already_linked':   'An account with this email is already linked to a different Google account. Sign in with your password and unlink it from Settings first.',
            }
            error = _msgs.get(ge, 'Google sign-in failed. Please try again.')
        elif request.GET.get('google_unavailable'):
            error = 'Google sign-in isn\'t configured yet. Please use email and password.'
    # Banner flags surfaced via querystring after the user is bounced
    # back here by the absolute-timeout middleware or the
    # sign-out-everywhere endpoint. They render distinct (info, not
    # error) banners above the form so the user understands the sign-
    # out wasn't a credential failure.
    return render(request, 'core/login.html', {
        'error': error,
        'email_val': email_val,
        'google_oauth_ready': _g_ready(),
        'expired_notice':       request.GET.get('expired') == '1',
        'signed_out_everywhere': request.GET.get('signed_out') == 'everywhere',
    })

@require_http_methods(['GET', 'POST'])
def signup_view(request):
    _capture_referral_from_request(request)
    if request.session.get('user_id'):
        return redirect('dashboard')
    from .google_auth import is_google_oauth_ready as _g_ready
    errors = {}
    vals = {}
    if request.method == 'POST':
        name     = request.POST.get('name', '').strip()
        email    = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        confirm  = request.POST.get('confirm', '')
        vals = {'name': name, 'email': email}
        if not name:
            errors['name'] = 'Full name is required.'
        if not email or '@' not in email:
            errors['email'] = 'A valid email address is required.'
        if len(password) < 8:
            errors['password'] = 'Password must be at least 8 characters.'
        elif password != confirm:
            errors['confirm'] = 'Passwords do not match.'
        if not errors and is_email_banned(email):
            errors['email'] = 'This email address is not permitted to create an account.'
        if not errors:
            user = create_user(email=email, password=password, name=name, plan='starter')
            if user is None:
                errors['email'] = 'An account with this email already exists.'
            else:
                # Stamp the card-free 3-day local trial. _user_trial_state
                # falls back to "now" if this is missing, but persisting it
                # at signup means the countdown is anchored to the real
                # signup time (not first-page-load of an account a
                # ghost-clicked through onboarding).
                try:
                    import time as _t
                    update_user(user['id'], local_trial_started_at=int(_t.time()))
                    user['local_trial_started_at'] = int(_t.time())
                except Exception:
                    log.exception("local-trial stamp on signup failed")
                try:
                    from .email_service import notify_admin_new_user
                    notify_admin_new_user(
                        user,
                        source='email',
                        ip=request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                           or request.META.get('REMOTE_ADDR', ''),
                    )
                except Exception:
                    log.exception("admin signup notification failed (email path)")
                # Bind to the referrer captured from ?ref= (if any) before
                # creating the auth session, so the very first ledger event
                # is recorded as part of the signup transaction window.
                # Only pop after a successful create — otherwise a validation
                # error (mismatched passwords, etc.) would silently strip the
                # referral attribution before the user fixed the form.
                _ref = (request.session.get('referral_code', '') or '').strip().upper()
                if _ref:
                    try:
                        bind_referrer_for_user(user['id'], _ref)
                    except Exception:
                        pass
                request.session.pop('referral_code', None)
                request.session['user_id']       = user['id']
                request.session['user_name']     = user['name']
                request.session['user_email']    = user['email']
                request.session['user_initials'] = user.get('avatar_initials', name[:2].upper())
                request.session['user_plan']     = user.get('plan', 'starter').title()
                _stamp_session_login(request)
                request.session.save()
                ua  = request.META.get('HTTP_USER_AGENT', '')
                ip  = request.META.get('REMOTE_ADDR', '')
                dev = _parse_device(ua)
                create_session(user_id=user['id'], session_key=request.session.session_key,
                               device=dev, ip=ip, ua=ua)
                record_login_event(user_id=user['id'], status='success',
                                   device=dev, ip=ip, ua=ua)
                # Skip the login-alert email here on purpose: a "new
                # sign-in detected" message immediately after signup
                # would be disorienting for the user. The welcome
                # email is no longer fired here either — it now goes
                # out once, alongside the payment-success receipt,
                # so we never tell someone "welcome" before they've
                # actually finished onboarding + paid.
                # First-touch UTM attribution + recovery email queue.
                # The UTM cookie is set by public_base.html's tracking
                # JS on first paid-channel landing; we read it here so
                # the user record is permanently tagged with the source.
                try:
                    import json as _json
                    from urllib.parse import unquote as _unq
                    raw = request.COOKIES.get('pl_utm', '')
                    if raw:
                        # JS sets the cookie via encodeURIComponent(JSON.stringify(...))
                        # so we URL-decode before parsing. Some browsers also do their
                        # own decoding on the way out — try both shapes.
                        for candidate in (raw, _unq(raw)):
                            try:
                                utm = _json.loads(candidate)
                                if isinstance(utm, dict) and utm:
                                    update_user(user['id'], signup_utm=utm)
                                    break
                            except Exception:
                                continue
                except Exception:
                    log.exception("UTM capture on signup failed")
                # Queue the 3-step recovery sequence in case the user
                # bails before activating their trial (no card on file).
                try:
                    enqueue_recovery_for_user(
                        user['id'], 'signup_no_trial',
                        trial_link=request.build_absolute_uri('/paywall/'))
                except Exception:
                    log.exception("recovery enqueue on signup failed")
                # Fire an immediate welcome email on account creation.
                # The payment-success path also fires one (gated by
                # ``claim_welcome_email_slot``) — that's intentional:
                # paying users get an onboarding welcome at signup AND
                # a post-payment welcome alongside their receipt.
                _fire_welcome_email(user, request)
                # Stamp the analytics conversion event — pixels fire on
                # the very next page render (onboarding) and only once.
                request.session['fire_conversion'] = 'sign_up'
                return redirect('onboarding')
    return render(request, 'core/signup.html', {
        'errors': errors,
        'vals': vals,
        'google_oauth_ready': _g_ready(),
    })


# ── Google OAuth (Sign in / Sign up with Google) ─────────────────────────

@require_http_methods(['GET'])
def google_oauth_start(request):
    """Step 1 of the Google sign-in flow: stash a CSRF state in the session
    and redirect the user to Google's authorize endpoint. Same entry point is
    used for both login and signup — the callback decides which based on
    whether the user already exists."""
    if request.session.get('user_id'):
        return redirect('dashboard')
    from .google_auth import (get_google_settings, is_google_oauth_ready,
                              build_authorize_url, build_redirect_uri, new_state)
    if not is_google_oauth_ready():
        return redirect('/login/?google_unavailable=1')
    cfg = get_google_settings()
    state = new_state()
    request.session['google_oauth_state']  = state
    request.session['google_oauth_intent'] = request.GET.get('intent', 'login')
    request.session.save()
    redirect_uri = build_redirect_uri(request)
    return redirect(build_authorize_url(cfg['client_id'], redirect_uri, state))


@require_http_methods(['GET'])
def google_oauth_callback(request):
    """Step 2: Google redirects the user back here with ``code`` + ``state``.
    We validate the state against the session, exchange the code for tokens,
    look up the user by Google's stable ``sub``, falling back to email
    matching, and finally create a fresh account if nothing matches."""
    from .google_auth import (is_google_oauth_ready, get_google_settings,
                              exchange_code_for_token, fetch_userinfo,
                              build_redirect_uri)
    import urllib.parse as _urlparse

    if not is_google_oauth_ready():
        return redirect('/login/?google_unavailable=1')
    if request.GET.get('error'):
        err = _urlparse.quote(request.GET.get('error', ''))[:64]
        return redirect(f'/login/?google_error={err}')

    code  = request.GET.get('code', '')
    state = request.GET.get('state', '')
    expected = request.session.pop('google_oauth_state', None)
    intent   = request.session.pop('google_oauth_intent', 'login') or 'login'

    if not code or not state or not expected or state != expected:
        return redirect('/login/?google_error=state_mismatch')

    cfg = get_google_settings()
    redirect_uri = build_redirect_uri(request)
    try:
        tokens = exchange_code_for_token(code, cfg['client_id'],
                                         cfg['client_secret'], redirect_uri)
    except Exception:
        return redirect('/login/?google_error=token_exchange_failed')

    access_token = tokens.get('access_token')
    if not access_token:
        return redirect('/login/?google_error=no_access_token')

    try:
        info = fetch_userinfo(access_token)
    except Exception:
        return redirect('/login/?google_error=userinfo_failed')

    sub   = info.get('sub')
    email = (info.get('email') or '').lower().strip()
    name  = info.get('name') or ''
    email_verified = bool(info.get('email_verified'))

    if not sub or not email:
        return redirect('/login/?google_error=missing_profile')
    if not email_verified:
        return redirect('/login/?google_error=email_unverified')
    if is_email_banned(email):
        return redirect('/login/?google_error=banned')

    # Resolve the user: by linked Google sub first, then by matching email
    # (auto-link), then create a brand-new account.
    #
    # Auto-link safety: if an account with the same email is already linked to
    # a *different* google_sub we refuse rather than silently overwriting that
    # link — otherwise an attacker who controls a recycled email at Google
    # could quietly hijack the local account. Recovery in that case has to go
    # through the existing password / 2FA flow.
    user = get_user_by_google_sub(sub)
    is_new_account = False
    if user is None:
        existing = get_user_by_email(email)
        if existing is not None:
            existing_sub = (existing.get('google_sub') or '').strip()
            if existing_sub and existing_sub != str(sub):
                return redirect('/login/?google_error=email_already_linked')
            if not existing_sub:
                link_google_to_user(existing['id'], sub, email)
            user = get_user_by_email(email)
        else:
            user = create_user_from_google(email=email, name=name, google_sub=sub)
            is_new_account = True
            if user is not None:
                # Stamp the card-free 3-day local trial (mirrors the
                # email-signup path).
                try:
                    import time as _t
                    update_user(user['id'], local_trial_started_at=int(_t.time()))
                    user['local_trial_started_at'] = int(_t.time())
                except Exception:
                    log.exception("local-trial stamp on google signup failed")
                try:
                    from .email_service import notify_admin_new_user
                    notify_admin_new_user(
                        user,
                        source='google',
                        ip=request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                           or request.META.get('REMOTE_ADDR', ''),
                    )
                except Exception:
                    log.exception("admin signup notification failed (google path)")
                # Same referral binding as the email-signup path. Read first,
                # bind, then pop — so we never strip attribution on a failed
                # creation path, but always clear it after a successful one
                # to avoid leaking it to a later visitor sharing this browser.
                _ref = (request.session.get('referral_code', '') or '').strip().upper()
                if _ref:
                    try:
                        bind_referrer_for_user(user['id'], _ref)
                    except Exception:
                        pass
                request.session.pop('referral_code', None)
                # Queue the 3-step recovery sequence so Google-signup users
                # get the same nudge cadence as email/password signups if
                # they bail before activating a paid plan. Wrapped — a
                # queue hiccup must never break the OAuth login flow.
                try:
                    enqueue_recovery_for_user(
                        user['id'], 'signup_no_trial',
                        trial_link=request.build_absolute_uri('/paywall/'))
                except Exception:
                    log.exception("recovery enqueue on google signup failed")
            if user is None:
                # Lost a creation race with a concurrent callback for the same
                # email — re-fetch by email/sub before giving up.
                user = get_user_by_google_sub(sub) or get_user_by_email(email)
                is_new_account = False
                if user is None:
                    return redirect('/login/?google_error=create_failed')

    # Defend against session-fixation: rotate the session id at the moment we
    # mark the session as authenticated, so any pre-login session id an
    # attacker may have observed is invalidated.
    request.session.cycle_key()

    # Refresh Whop state on every successful Google login. Does an
    # email-based lookup against Whop (no plan-check throttle, 3-second
    # timeout), picks the highest-tier active membership as source of
    # truth, and writes plan/billing to the user record. For brand-new
    # accounts with no Whop subscription yet, this is a safe no-op.
    # Wrapped: a Whop / detection bug must never break Google login —
    # fall back to the existing user.plan so the session still mints.
    try:
        _whop = _whop_login_sync(user['id'], user)
    except Exception:
        log.exception("_whop_login_sync failed for user %s (google login)", user.get('id'))
        _whop = {'plan': (user.get('plan') or 'starter')}

    # Mint the session — same shape as login_view / signup_view.
    request.session['user_id']       = user['id']
    request.session['user_name']     = user.get('name', '') or email
    request.session['user_email']    = user.get('email', email)
    request.session['user_initials'] = (user.get('avatar_initials')
                                        or (user.get('name', email) or email)[:2].upper())
    request.session['user_plan']     = (_whop['plan'] or 'starter').title()
    _stamp_session_login(request)
    request.session.save()

    ua  = request.META.get('HTTP_USER_AGENT', '')
    ip  = request.META.get('REMOTE_ADDR', '')
    dev = _parse_device(ua)
    create_session(user_id=user['id'], session_key=request.session.session_key,
                   device=dev, ip=ip, ua=ua)
    # Login-alert email (only for returning users — brand-new Google
    # signups get the welcome email instead, fired below). Must run
    # BEFORE record_login_event so the new-device check doesn't see
    # this very session as already-known.
    if not is_new_account:
        _send_login_alert_if_new_device(
            user=user, request=request,
            method_label='Google sign-in',
            device=dev, ip=ip, ua=ua,
        )
    record_login_event(user_id=user['id'], status='success',
                       device=dev, ip=ip, ua=ua)

    if is_new_account:
        # First-time Google signup → onboarding flow. Fire an
        # immediate welcome email on account creation. The
        # payment-success path also fires one (gated by
        # ``claim_welcome_email_slot``) — that's intentional:
        # paying users get an onboarding welcome at signup AND
        # a post-payment welcome alongside their receipt.
        _fire_welcome_email(user, request)
        return redirect('onboarding')
    if not user.get('onboarding_complete'):
        return redirect('onboarding')
    return redirect('dashboard')


@login_required
@require_http_methods(['GET', 'POST'])
def onboarding_view(request):
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}

    if user.get('onboarding_complete') is True:
        return redirect('dashboard')

    # Only show states with enough recent permit activity to be sellable.
    # Pricing flipped from per-city to per-state in May 2026, so step 2
    # is now a flat state picker — no nested state→cities dropdown.
    supported_states = get_customer_visible_states()
    # Pre-select any state codes the user already has saved (e.g. coming
    # back from a failed checkout). ``data.cities`` was repurposed by the
    # May-2026 migration to hold uppercase 2-letter state codes.
    _prev_state_codes = {(s or '').strip().upper()
                         for s in (user.get('cities') or [])}

    step = request.session.get('onboarding_step', 1)
    error = None

    # Onboarding ordering (as of the plan-first refactor):
    #   step 1 = Choose Plan      (stash plan+period in session, advance)
    #   step 2 = Service Area     (city/state, save city to user, advance)
    #   step 3 = Terms of Service (record acceptance, then redirect to
    #                              embedded Whop checkout using the
    #                              plan+period stashed in step 1)
    # On checkout success (ls_success) the user is auto-redirected to the
    # dashboard, so the post-pay landing is their logged-in session.

    if request.method == 'GET' and request.GET.get('sync_error'):
        # Coming back from a failed checkout — drop the user on the Terms
        # step so they can re-accept and re-trigger payment.
        error = 'No active subscription found yet. Please complete payment and try again.'
        step = 3
        request.session['onboarding_step'] = 3

    if request.method == 'GET' and request.GET.get('back'):
        try:
            back_to = max(1, int(request.GET.get('back', 1)))
            request.session['onboarding_step'] = back_to
            step = back_to
        except (ValueError, TypeError):
            pass

    if request.method == 'POST':
        posted_step = int(request.POST.get('step', 1))

        if posted_step == 1:
            # Plan selection — stash, do not charge yet.
            plan   = request.POST.get('plan', 'starter').lower()
            period = request.POST.get('period', 'monthly').lower()
            if plan not in ('starter', 'pro', 'agency'):
                plan = 'starter'
            if period not in ('monthly', 'annual'):
                period = 'monthly'
            # Annual billing is temporarily gated (Whop entity not
            # configured) — silently downgrade any stale ?period=annual
            # link or hand-crafted POST so the checkout flow stays on
            # the only period we can actually transact.
            if period == 'annual' and not wp.annual_billing_enabled():
                period = 'monthly'
            request.session['onboarding_plan']   = plan
            request.session['onboarding_period'] = period
            step = 2
            request.session['onboarding_step'] = 2

        elif posted_step == 2:
            # State selection — capped by the selected plan's state limit
            # (starter=1, pro=2, agency=5). User can also skip and pick
            # states later from Settings → Coverage. ``data.cities`` was
            # repurposed by the May-2026 pricing migration to hold
            # uppercase 2-letter state codes; the variable name stayed
            # for internal-call-site compatibility.
            _plan_for_step2 = (request.session.get('onboarding_plan') or 'starter').lower()
            _state_limit    = PLAN_CITY_LIMITS.get(_plan_for_step2, 1)

            if request.POST.get('skip') == '1':
                request.session.pop('onboarding_city',  None)
                request.session.pop('onboarding_state', None)
                step = 3
                request.session['onboarding_step'] = 3
            else:
                valid_states = {s['state'] for s in supported_states}
                raw_states   = [s.strip().upper()
                                for s in request.POST.getlist('state') if s.strip()]
                seen   = set()
                states = []
                for s in raw_states:
                    if s in seen or s not in valid_states:
                        continue
                    seen.add(s)
                    states.append(s)

                if not states:
                    error = 'Please pick at least one state, or use “Skip for now” to set this up later.'
                    step = 2
                elif len(states) > _state_limit:
                    _label = _plan_for_step2.title()
                    _word  = 'state' if _state_limit == 1 else 'states'
                    error = f'Your {_label} plan supports up to {_state_limit} {_word}. Please remove the extras.'
                    step = 2
                else:
                    request.session['onboarding_state'] = states[0]
                    update_user(user_id, cities=states)
                    step = 3
                    request.session['onboarding_step'] = 3

        elif posted_step == 3:
            # Terms acceptance → mark onboarding complete and drop the
            # user straight into the dashboard on their card-free 3-day
            # trial. Card capture happens later, from the in-app red
            # countdown bar / pricing page, not as a gate on signup.
            agreed = request.POST.get('agreed') == '1'
            if not agreed:
                error = 'You must accept the Terms of Service to continue.'
                step = 3
            else:
                from datetime import timezone as _tz
                ts = datetime.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                # Stamp the local-trial clock here too if signup somehow
                # missed it (legacy account re-onboarding, etc.) so the
                # banner countdown starts from the moment they finish
                # onboarding, not whenever they next hit a page.
                _fields = {'terms_accepted_at': ts, 'onboarding_complete': True}
                if not user.get('local_trial_started_at'):
                    import time as _t
                    _fields['local_trial_started_at'] = int(_t.time())
                update_user(user_id, **_fields)
                request.session['fire_conversion'] = 'trial_start'
                return redirect('dashboard')

    # State limit on step 2 derives from whatever plan was picked on step 1.
    _ob_plan_session = (request.session.get('onboarding_plan') or 'starter').lower()
    _ob_state_limit  = PLAN_CITY_LIMITS.get(_ob_plan_session, 1)

    ctx = {
        'step':            step,
        'error':           error,
        'supported_states':      supported_states,
        'supported_states_json': json.dumps(supported_states),
        'prev_state_codes':      sorted(_prev_state_codes),
        'onboarding_state':  request.session.get('onboarding_state', ''),
        'onboarding_plan':   request.session.get('onboarding_plan',   ''),
        'onboarding_period': request.session.get('onboarding_period', 'monthly'),
        # State-coverage cap for the multi-state picker on step 2.
        # Template var stays ``city_limit`` to avoid a wider rename in
        # this PR; the integer is the state count.
        'city_limit':       _ob_state_limit,
        'city_limit_label': 'state' if _ob_state_limit == 1 else 'states',
        'user_name':    user.get('name', ''),
        'user_email':   user.get('email', ''),
        # Pricing for the plan cards in step 1 — resolved in this user's
        # own Whop mode so a PROD-flagged user never sees $1 dev prices
        # just because the global mode is currently 'dev' (and vice-versa).
        # Without this the template's {{ pricing.starter_monthly }} etc.
        # would either render blank or render the wrong amount.
        'pricing':      wp.get_pricing_dict(wp.mode_for_user(user)),
    }
    return render(request, 'core/onboarding.html', ctx)


@login_required
def paywall_view(request):
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}

    if user.get('subscription_active') is True:
        return redirect('dashboard')

    # "New" = never had a Whop membership; "renew" = had one that lapsed.
    is_new_user = not (user.get('whop_membership_id') or '').strip()

    sync_error    = request.GET.get('sync_error')
    missing_plan  = request.GET.get('missing_plan', '').strip()
    missing_label = ''
    if missing_plan and '_' in missing_plan:
        plan, period = missing_plan.split('_', 1)
        missing_label = f'{plan.title()} ({period})'

    # Build pricing dict for the user's *own* Whop mode — otherwise a PROD
    # account hits checkout against $29/$99/$249 plan IDs while the paywall
    # itself rendered $1 prices because the global mode happened to be 'dev'
    # (or vice-versa). Each user sees the prices they'll actually be billed.
    user_mode = wp.mode_for_user(user)
    pricing   = wp.get_pricing_dict(user_mode)

    # Build the available_plans map so the template can disable any button
    # whose Whop plan_id isn't configured yet (instead of letting the user
    # click into a silent redirect loop back to the paywall). Resolved in
    # the user's own mode so a PROD user isn't blocked because a DEV plan
    # ID happens to be missing (or vice-versa).
    available = {}
    for plan in ('starter', 'pro', 'agency'):
        for period in ('monthly', 'annual'):
            available[f'{plan}_{period}'] = bool(wp.get_plan_id(plan, period, user_mode))

    ctx = {
        'user_name':     user.get('name', ''),
        'user_email':    user.get('email', ''),
        'sync_error':    sync_error,
        'missing_label': missing_label,
        'is_new_user':   is_new_user,
        'pricing':       pricing,
        'available':     available,
    }
    return render(request, 'core/paywall.html', ctx)


def logout_view(request):
    session_key = request.session.session_key
    user_id = request.session.get('user_id')
    if session_key and user_id:
        try:
            from .pg import execute as _pg_execute
            _pg_execute("DELETE FROM sessions WHERE session_key = %s", (session_key,))
        except Exception:
            pass
    request.session.flush()
    # Optional ``?next=`` redirect — used by the in-app session-expiry
    # popup's "Sign in again" button so the destruction of the current
    # session and the bounce to the login page happen in a single
    # request (no goodbye page in between). Validated against an
    # allow-list of relative paths so we can't be turned into an open
    # redirect: the only legitimate value the popup ever sends is
    # ``/login/?expired=1`` (and variants thereof on the same site).
    nxt = (request.GET.get('next') or '').strip()
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return render(request, 'core/logout.html')

import secrets as _secrets
from datetime import timedelta

@require_http_methods(['GET', 'POST'])
def forgot_password(request):
    if request.session.get('user_id'):
        return redirect('dashboard')
    sent    = False
    error   = None
    email_val = ''
    reset_link = None
    if request.method == 'POST':
        # ── IP rate limit ───────────────────────────────────────────
        # 5 reset requests per IP per 5-minute window. Stops the form
        # from being weaponised to (a) flood a victim's inbox and
        # (b) burn through the Resend quota. Uses Django's default
        # cache backend — per-process LocMemCache is fine here, the
        # goal is friction, not perfect distributed throttling.
        from django.core.cache import cache
        rl_ip  = request.META.get('REMOTE_ADDR', 'unknown')
        rl_key = f'forgot_pw_rl:{rl_ip}'
        rl_n   = int(cache.get(rl_key) or 0)
        if rl_n >= 5:
            error = 'Too many reset requests from this address. Please wait a few minutes and try again.'
            return render(request, 'core/forgot_password.html', {
                'sent': False, 'error': error,
                'email_val': request.POST.get('email', '').strip().lower(),
                'reset_link': None,
            })
        cache.set(rl_key, rl_n + 1, timeout=300)

        email = request.POST.get('email', '').strip().lower()
        email_val = email
        if not email or '@' not in email:
            error = 'Please enter a valid email address.'
        else:
            user = get_user_by_email(email)
            if user and not is_email_banned(email):
                from datetime import timezone as _tz
                token  = _secrets.token_urlsafe(32)
                # Aware UTC on both sides — stored as ISO-8601 with
                # tzinfo so the comparison in ``reset_password`` (also
                # aware) doesn't trip on naive-vs-aware TypeErrors and
                # token TTLs are exactly 1h regardless of server TZ.
                expiry = (datetime.now(_tz.utc) + timedelta(hours=1)).isoformat()
                set_reset_token(email, token, expiry)
                reset_link = request.build_absolute_uri(
                    '/reset-password/' + token + '/'
                )
                # ── Send the beautiful HTML reset email ────────────
                # If the email transport is configured, deliver the link
                # over email and never echo it back to the page (account
                # enumeration prevention + don't leak reset tokens to
                # whoever happens to have typed in someone else's email).
                # If transport is DOWN, we keep ``reset_link`` set so the
                # template's amber "Demo mode — reset link" panel shows
                # it on screen — that way the admin can recover their
                # own password before they've finished wiring up Resend.
                from .auth_codes import send_reset_password_email
                ok, msg = send_reset_password_email(
                    to_email   = user['email'],
                    to_name    = user.get('name', ''),
                    reset_link = reset_link,
                    request_ip = request.META.get('REMOTE_ADDR', ''),
                    request_ua = request.META.get('HTTP_USER_AGENT', ''),
                )
                if ok:
                    reset_link = None   # delivered — keep it private
                else:
                    log.warning(
                        "forgot-password: transport down for %s (%s) — "
                        "falling back to on-screen link",
                        email, msg,
                    )
            # ── account-enumeration protection ─────────────────────
            # Always show the "if that email is registered, a link is on
            # its way" success state regardless of whether the email
            # actually exists. The attacker can't tell from the response
            # whether they've found a real user.
            sent = True
    return render(request, 'core/forgot_password.html', {
        'sent': sent, 'error': error, 'email_val': email_val,
        'reset_link': reset_link,
    })

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def reset_password(request, token):
    """
    The URL token IS the security mechanism here:

      • cryptographic random (``secrets.token_urlsafe(32)``)
      • single-use (cleared on successful reset)
      • expires after 1 hour
      • only ever sent to the verified account email

    Whoever holds the URL is, by construction, the legitimate owner of
    the account — that's the entire point of a "reset password" link.
    Layering CSRF on top adds zero protection (an attacker who has the
    URL can already POST it themselves; they don't need to trick the
    user) and was causing real production failures for users coming
    from Gmail / Outlook / Apple Mail because of stale csrftoken
    cookies, cross-tab cookie state, and corporate-proxy quirks. So
    we ``@csrf_exempt`` this view explicitly.
    """
    if request.session.get('user_id'):
        return redirect('dashboard')
    user = get_user_by_reset_token(token)
    if not user:
        return render(request, 'core/reset_password.html', {'invalid': True})
    from datetime import timezone
    expiry_str = user.get('reset_expiry') or ''
    try:
        expiry_dt = datetime.fromisoformat(expiry_str)
        # Tokens minted before the timezone-fix shipped were saved as
        # naive UTC. Promote naive → aware UTC so the comparison below
        # (always aware) doesn't crash with "can't compare offset-naive
        # and offset-aware datetimes" on legacy tokens.
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expiry_dt:
            clear_reset_token(user['id'])
            return render(request, 'core/reset_password.html', {'expired': True})
    except Exception:
        return render(request, 'core/reset_password.html', {'invalid': True})
    error = None
    if request.method == 'POST':
        pw1 = request.POST.get('password', '')
        pw2 = request.POST.get('confirm', '')
        if len(pw1) < 8:
            error = 'Password must be at least 8 characters.'
        elif pw1 != pw2:
            error = 'Passwords do not match.'
        else:
            update_user(user['id'], password=hash_password(pw1))
            clear_reset_token(user['id'])
            # ── invalidate all existing sessions ───────────────────
            # If the account was compromised, the attacker's session
            # would survive the password change. Nuke every session
            # for this user — they (and any attacker) must sign in
            # fresh with the new password.
            try:
                delete_sessions_for_user(user['id'])
            except Exception:
                log.exception("password-reset: failed to revoke sessions for user %s", user['id'])
            return render(request, 'core/reset_password.html', {'success': True})
    return render(request, 'core/reset_password.html', {
        'token': token, 'user_email': user.get('email', ''), 'error': error,
    })

# ── Protected views ───────────────────────────────────────────

@login_required
@subscription_required
def dashboard(request):
    ctx     = _user_ctx(request)
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}

    # ── Sync plan from DB — trust DB over stale session ────────
    _db_plan = user.get('plan', 'starter').title()
    if _db_plan != ctx.get('user_plan'):
        ctx['user_plan'] = _db_plan
        request.session['user_plan'] = _db_plan

    # Count each dashboard visit as one alert digest delivered
    increment_user_field(user_id, 'alerts_sent', 1)
    raw     = user.get('cities', [])
    # Since the May-2026 pricing migration ``data.cities`` holds 2-letter
    # STATE codes (e.g. ['TX','FL']) — not legacy "City, ST" strings — so
    # the state dropdown is built directly from that list, and the
    # cascading city dropdown is populated from DISTINCT (city, state)
    # pairs in the permits table within the user's paid-for states.
    plan_states = sorted({(s or '').strip().upper() for s in raw
                          if s and len((s or '').strip()) == 2})
    plan_cities = get_distinct_cities_for_states(plan_states)
    ctx['plan_cities_json'] = json.dumps(plan_cities)
    ctx['plan_states']      = plan_states
    # City freeze check
    plan       = user.get('plan', 'starter')
    city_limit = PLAN_CITY_LIMITS.get(plan, 1)
    ctx['cities_frozen']  = user.get('cities_frozen', False)
    ctx['city_limit']     = city_limit
    ctx['city_count']     = len(raw)
    ctx['city_excess']    = max(0, len(raw) - city_limit)
    # Pending downgrade banner — validate it is truly a downgrade
    _RANK = {'starter': 0, 'pro': 1, 'agency': 2}
    pending_dg   = user.get('pending_downgrade') or {}
    current_rank = _RANK.get(ctx['user_plan'].lower(), 0)
    if pending_dg:
        pending_rank = _RANK.get(pending_dg.get('plan', '').lower(), 0)
        if pending_rank >= current_rank:
            # Stale / invalid state — clear it silently
            update_user(user_id, pending_downgrade=None)
            pending_dg = {}
    ctx['pending_downgrade'] = pending_dg if pending_dg else None
    if pending_dg:
        from datetime import datetime, timezone
        try:
            sched_dt  = datetime.strptime(pending_dg['date'], '%Y-%m-%d')
            today_dt  = datetime.now(timezone.utc).replace(tzinfo=None)
            days_left = (sched_dt - today_dt).days
        except Exception:
            days_left = 0
        ctx['days_until_downgrade'] = max(0, days_left)
    else:
        ctx['days_until_downgrade'] = None
    # The /dashboard/ table is now loaded asynchronously from
    # `dashboard_data_view` (DataTables server-side) — same wiring
    # /permits/ uses. We no longer dump every matching row into the
    # HTML, so the page payload is a few KB instead of hundreds, and
    # paging/sorting/filtering all round-trip to Postgres. The only
    # row we still need server-side at render time is the highest-
    # scoring one, so the BEST LEAD hero card has something to show
    # in the first paint (before the AJAX call lands).
    # ``data.cities`` holds STATE codes since the May-2026 pricing
    # migration. The auth gate is now a state-code set; the local
    # ``plan_cities`` list is kept (for the legacy "BEST LEAD by city"
    # display) but the values are state codes.
    _user_city_set = {(s or '').strip().upper()
                      for s in (user.get('cities') or [])
                      if s and len((s or '').strip().upper()) == 2}
    best_lead = None
    best_lead_candidates: list = []
    if _user_city_set and not ctx.get('cities_frozen'):
        try:
            # Fetch the top 50 by DB ai_score as a candidate pool. The
            # client then re-scores every candidate with the 12-factor
            # model (aiFactors / computeOverallScore in base.html) and
            # picks the actual winner — this keeps the hero card in
            # agreement with the table rings and the AI profile modal,
            # both of which also use the 12-factor derivation. 50 rows
            # is a small JSON payload (a few KB) and is wide enough
            # that the heuristic max will almost always live inside it
            # even when the DB ai_score is wildly miscalibrated.
            _top_rows, _, _ = query_permits_for_dashboard(
                city_set     = _user_city_set,
                history_days = 3,
                sort_key     = 'score',
                sort_dir     = 'desc',
                start        = 0,
                length       = 50,
            )
            best_lead_candidates = [
                r for r in (_top_rows or [])
                if int(r.get('score') or 0) > 0
            ]
            # Trial / expired soft-lock parity for the SSR hero card.
            # The /dashboard/data/ JSON endpoint masks contact fields
            # for expired users; we MUST mirror that on the
            # server-rendered "Best Lead Right Now" card + its
            # ``BEST_LEAD_CANDIDATES`` JS blob, otherwise an expired
            # user can still pull phone/email straight out of the
            # initial dashboard HTML / view-source.
            try:
                _ts_dash = _user_trial_state(user)
                if _ts_dash['is_expired']:
                    best_lead_candidates = _mask_contact_for_expired(best_lead_candidates)
                if _ts_dash['is_trial'] or _ts_dash['is_expired']:
                    # Capped users see only today's top-20 in the table;
                    # keep the SSR candidate pool consistent so the
                    # JS-recomputed hero pick can't surface a lead the
                    # table itself wouldn't show.
                    best_lead_candidates = best_lead_candidates[:_TRIAL_CSV_DAILY_CAP]
            except Exception:
                pass
            # First-paint fallback: the server still renders a hero
            # card before JS runs, so use the top DB row as a holder.
            # JS will repaint it within milliseconds.
            if best_lead_candidates:
                best_lead = best_lead_candidates[0]
        except Exception as _e:
            # Never let a permits-table read failure 500 the dashboard.
            # An empty hero card just doesn't render; the AJAX-loaded
            # table will show its own empty state on failure.
            log.exception('dashboard: best-lead query failed: %s', _e)
    ctx['best_lead_json']        = json.dumps(best_lead) if best_lead else 'null'
    ctx['best_lead_candidates_json'] = json.dumps(best_lead_candidates)
    ctx['best_lead']             = best_lead
    ctx['dashboard_window_days'] = 3
    response = render(request, 'core/dashboard.html', ctx)
    response['Cache-Control'] = 'no-store'
    return response

_TRIAL_CSV_DAILY_CAP = 20
_CSV_EXPORT_LENGTH_THRESHOLD = 500
_LOCAL_TRIAL_DAYS = 3
_LOCAL_TRIAL_SECONDS = _LOCAL_TRIAL_DAYS * 86400

def _user_trial_state(user: dict) -> dict:
    """Card-free local 3-day trial state — single source of truth for
    every trial-gate decision (feed cap, history window, contact-info
    masking, top-bar countdown).

    On signup (and on first access by any legacy user) we stamp
    ``user.local_trial_started_at`` (unix ts). The first 3 days from
    that stamp are ``is_trial=True`` (capped feed + today-only history
    + sticky red countdown). After that the account flips to
    ``is_expired=True`` — still browsable but contact fields are
    masked and a "trial ended, subscribe now" red bar replaces the
    countdown. Paid users (``subscription_active=True``) bypass
    everything: ``is_paid=True``, all other flags False.
    """
    import time as _time
    is_paid = bool(user.get('subscription_active'))
    if is_paid:
        return {'is_trial': False, 'is_expired': False, 'is_paid': True,
                'seconds_left': 0, 'started_at': 0, 'ends_at': 0,
                'days_left': 0, 'hours_left': 0}
    started = int(user.get('local_trial_started_at') or 0)
    if started <= 0:
        # Fallback for legacy accounts created before the local-trial
        # field existed — treat the first access as start.
        started = int(_time.time())
    elapsed = int(_time.time()) - started
    seconds_left = max(0, _LOCAL_TRIAL_SECONDS - elapsed)
    is_trial = seconds_left > 0
    return {
        'is_trial':     is_trial,
        'is_expired':   not is_trial,
        'is_paid':      False,
        'seconds_left': seconds_left,
        'days_left':    seconds_left // 86400,
        'hours_left':   (seconds_left % 86400) // 3600,
        'started_at':   started,
        'ends_at':      started + _LOCAL_TRIAL_SECONDS,
    }


def _trial_csv_quota(user: dict) -> tuple[bool, int, int, str]:
    """Return ``(is_trial, used_today, cap, today_iso)`` for the CSV-export
    daily-cap gate. ``is_trial`` is True while the user is inside the
    card-free 3-day local trial window (see :func:`_user_trial_state`).
    Counter is stored in the user JSONB as ``trial_csv_date`` (ISO
    YYYY-MM-DD) + ``trial_csv_used`` (int) and auto-resets when the
    date rolls over.
    """
    from datetime import date as _date
    today = _date.today().isoformat()
    is_trial = bool(_user_trial_state(user).get('is_trial'))
    stored_date = (user.get('trial_csv_date') or '').strip()
    used = int(user.get('trial_csv_used') or 0) if stored_date == today else 0
    return is_trial, used, _TRIAL_CSV_DAILY_CAP, today


def _mask_contact_for_expired(rows: list) -> list:
    """Soft-lock for expired-trial users: blank out phone / email /
    owner / lead so they can still browse the feed and see permit
    counts, but can't action any lead until they subscribe.
    """
    if not rows:
        return rows
    for r in rows:
        if r.get('phone'):  r['phone']    = '••• ••• ••••'
        if r.get('email'):  r['email']    = '••••••@••••.com'
        if r.get('owner'):  r['owner']    = '🔒 Subscribe to unlock'
        if r.get('lead'):   r['lead']     = '🔒 Subscribe to unlock'
        if r.get('address'):r['address']  = '🔒 Address hidden'
    return rows


def _build_permits_data_payload(request, *, history_days_override=None,
                                include_summary: bool = False,
                                max_page_override: int | None = None) -> dict:
    """Shared body for the two DataTables endpoints — the /permits/ history
    page (per-plan history window, no summary) and the /dashboard/ feed
    (hard-coded 3-day window, summary stats for the cards).

    Both surfaces need: the same per-user authorisation gate, the same
    panel filters, the same sort whitelist, the same 500-row clamp on
    `length`, and the same NULL-score / score-range / tier semantics.
    Factoring into one function guarantees the dashboard table can't
    drift from the permit-history table even when one of them changes.
    """
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}
    # ``data.cities`` holds STATE codes since the May-2026 pricing
    # migration. The authz gate is a set of uppercase 2-letter codes.
    raw     = user.get('cities', []) or []
    _user_city_set = {(s or '').strip().upper() for s in raw
                      if s and len((s or '').strip().upper()) == 2}

    plan = user.get('plan', 'starter')
    if user.get('cities_frozen', False) or not _user_city_set:
        empty = {
            'draw': int(request.GET.get('draw', 1) or 1),
            'recordsTotal': 0, 'recordsFiltered': 0, 'data': [],
        }
        if include_summary:
            empty['summary'] = {'total': 0, 'hot': 0, 'avg': 0}
        return empty

    _plan_title  = plan.title() if plan.lower() in ('starter', 'pro', 'agency') else 'Starter'
    _plan_limits = PLAN_USAGE_LIMITS.get(_plan_title, PLAN_USAGE_LIMITS['Starter'])
    if history_days_override is not None:
        _history_days = history_days_override
    else:
        _history_days = _plan_limits.get('history_days')
    _feats = PLAN_FEATURES.get(_plan_title, PLAN_FEATURES['Starter'])

    g = request.GET
    f_state   = (g.get('f_state', '') or '').strip()
    f_city    = (g.get('f_city', '') or '').strip()
    f_type    = (g.get('f_type', '') or '').strip() if _feats['trade_filter'] else ''
    f_status  = (g.get('f_status', '') or '').strip()
    try:    f_score_min = max(0,   int(g.get('f_score_min') or 0))
    except (TypeError, ValueError): f_score_min = 0
    try:    f_score_max = min(100, int(g.get('f_score_max') or 100))
    except (TypeError, ValueError): f_score_max = 100
    f_owner   = (g.get('f_owner', '') or '').strip()
    f_phone_d = ''.join(ch for ch in (g.get('f_phone', '') or '') if ch.isdigit())
    f_email   = (g.get('f_email', '') or '').strip()
    def _iso_date_or_empty(raw):
        s = (raw or '').strip()
        if not s:
            return ''
        try:
            from datetime import date as _date
            _date.fromisoformat(s)
            return s
        except (TypeError, ValueError):
            return ''
    f_iafter  = _iso_date_or_empty(g.get('f_issued_after'))
    f_ebefore = _iso_date_or_empty(g.get('f_expires_before'))
    f_kw      = (g.get('f_keyword', '') or '').strip()
    f_tier    = (g.get('f_tier', 'all') or 'all').strip().lower()
    g_search  = (g.get('search[value]', '') or '').strip()

    # IMPORTANT: these lists MUST match the column order in their
    # corresponding templates. Dashboard includes Project Value before
    # Score; Permit History does not. The Description column was removed
    # from the table view — the full scope-of-work text only shows
    # in the row-detail modal now, so 'desc' is no longer a sort key.
    # If this list goes out of sync with the frontend, clicking any
    # column header silently sorts by a different field.
    if include_summary:
        _SORT_KEYS = ['id', 'city', 'type', 'status', 'issuedIso',
                      'phone', 'email', 'owner', 'valueCents', 'score']
        _default_sort_col = 9
    else:
        _SORT_KEYS = ['id', 'city', 'type', 'status', 'issuedIso',
                      'phone', 'email', 'owner', 'score']
        _default_sort_col = 8
    try:    col_idx = int(g.get('order[0][column]', str(_default_sort_col)) or _default_sort_col)
    except (TypeError, ValueError): col_idx = _default_sort_col
    sort_dir = (g.get('order[0][dir]', 'desc') or 'desc').lower()
    sort_key = _SORT_KEYS[col_idx] if 0 <= col_idx < len(_SORT_KEYS) else 'score'

    try:    start = max(0, int(g.get('start', 0) or 0))
    except (TypeError, ValueError): start = 0
    try:
        length = int(g.get('length', 25) or 25)
    except (TypeError, ValueError):
        length = 25
    # Per-endpoint cap. /permits/ now allows up to 25 000 rows in a
    # single response so the CSV-export button can dump the whole
    # filtered set (the underlying SQL fetch is also capped at 25 000
    # inside ``query_permits_for_dashboard`` — see ``_FETCH_CAP`` —
    # so requesting more buys nothing). DataTables' regular page
    # navigation still asks for 25-500 rows at a time; the bigger cap
    # only matters when the JS export-CSV path sets ``length=25000``.
    # The /dashboard/ endpoint overrides this — see ``dashboard_data_view``.
    _MAX_PAGE = max_page_override if max_page_override else 25_000
    if length < 0 or length > _MAX_PAGE:
        length = _MAX_PAGE

    # Trial cap: trialing users get the full feature set but only see
    # the top 25 permits per day (sorted by score, the default order)
    # — same volume-throttle principle as the CSV-export gate. The
    # /permits/ and /dashboard/ tables are clamped to 25 rows so the
    # DataTables paginator only shows one page. The JS reads
    # ``trial_view_limit`` to render a "showing 25 of N — upgrade for
    # the full feed" banner above the table. A bulk export
    # (``length > 500``) layers an additional 25/day daily-bucket on
    # top — the user JSONB counter (``trial_csv_used``) prevents
    # scraping by repeatedly hitting Export.
    # Local trial state — single source of truth. Trial AND expired
    # users get the same 20/day cap + today-only window; expired
    # additionally get their contact fields masked downstream so the
    # feed stays browsable but every actionable lead requires a
    # subscription. Paid users bypass entirely.
    _ts = _user_trial_state(user)
    _is_capped_user = _ts['is_trial'] or _ts['is_expired']
    if _is_capped_user:
        # Force "today only" history during the trial + after expiry —
        # historical leads are part of the paid product. This MUST
        # override any per-endpoint ``history_days_override`` (e.g. the
        # dashboard's 3-day window) so the trial gate cannot be skipped
        # just by hitting the dashboard data endpoint.
        _history_days = 1

    _trial_cap_block = None
    _is_trial_export = False
    _trial_is, _trial_used, _trial_cap, _trial_today = _trial_csv_quota(user)
    # CSV export gate also applies to expired users (same 20/day
    # bucket). The export path is identified by an explicit
    # ``?export=1`` query param set by the CSV-download JS — a large
    # ``length`` alone is NOT enough (the dashboard requests up to
    # 2000 rows on every page load, which would otherwise drain the
    # daily bucket just by browsing).
    _is_explicit_export = (g.get('export') == '1')
    if _is_capped_user and _is_explicit_export and length > _CSV_EXPORT_LENGTH_THRESHOLD:
        # Bulk CSV-export path — apply the daily-bucket cap.
        _is_trial_export = True
        _trial_remaining = max(0, _trial_cap - _trial_used)
        if _trial_remaining <= 0:
            length = 0
        else:
            length = min(length, _trial_remaining)
        _trial_cap_block = {
            'is_trial':     True,
            'cap':          _trial_cap,
            'used_before':  _trial_used,
            'remaining':    _trial_remaining,
            'capped':       True,
        }
    elif _is_capped_user:
        # Normal table view — clamp to top 20 by score (default sort).
        length = min(length, _trial_cap)

    result = query_permits_for_dashboard(
        city_set         = _user_city_set,
        history_days     = _history_days,
        f_state          = f_state,
        f_city           = f_city,
        f_trade          = f_type,
        f_status         = f_status,
        f_score_min      = f_score_min,
        f_score_max      = f_score_max,
        f_owner          = f_owner,
        f_phone_digits   = f_phone_d,
        f_email          = f_email,
        f_issued_after   = f_iafter,
        f_expires_before = f_ebefore,
        f_keyword        = f_kw,
        f_tier           = f_tier,
        f_search         = g_search,
        sort_key         = sort_key,
        sort_dir         = sort_dir,
        start            = start,
        length           = length,
        include_summary  = include_summary,
    )
    if include_summary:
        page, records_total, records_filtered, summary = result
    else:
        page, records_total, records_filtered = result
        summary = None

    try:    draw = int(g.get('draw', 1) or 1)
    except (TypeError, ValueError): draw = 1

    # Trial / expired table-view cap: keep the TRUE filtered total so
    # the banner can show "20 of 847", but report the clamped count to
    # DataTables so the paginator doesn't offer ghost pages 2+.
    if _is_capped_user and not _is_trial_export:
        _true_filtered = records_filtered
        records_filtered = min(records_filtered, _trial_cap)
        records_total    = min(records_total,    _trial_cap)
        page = (page or [])[:_trial_cap]
        payload_trial_view = {
            'is_trial':       _ts['is_trial'],
            'is_expired':     _ts['is_expired'],
            'cap':            _trial_cap,
            'total_filtered': _true_filtered,
            'capped':         _true_filtered > _trial_cap,
        }
    else:
        payload_trial_view = None

    # Soft-lock: mask contact/owner/address for expired-trial users
    # on EVERY response (table view + bulk export). Trial users still
    # see real contact data — that's the trial value prop.
    if _ts['is_expired'] and page:
        page = _mask_contact_for_expired(page)

    payload = {
        'draw': draw,
        'recordsTotal':    records_total,
        'recordsFiltered': records_filtered,
        'data':            page,
    }
    if summary is not None:
        payload['summary'] = summary
    if payload_trial_view is not None:
        payload['trial_view_limit'] = payload_trial_view

    # Trial CSV-export: persist the increment + advertise the cap to the
    # client so the JS can show the "X of 25 today" upgrade banner.
    if _is_trial_export and _trial_cap_block is not None:
        sent_now = len(page or [])
        new_used = _trial_used + sent_now
        if sent_now > 0:
            try:
                update_user(user_id,
                            trial_csv_date = _trial_today,
                            trial_csv_used = new_used)
            except Exception:
                log.exception('trial-csv-cap: failed to persist counter for user %s', user_id)
        _trial_cap_block['used_after'] = new_used
        _trial_cap_block['remaining_after'] = max(0, _trial_cap - new_used)
        _trial_cap_block['rows_returned']   = sent_now
        payload['trial_export_limit'] = _trial_cap_block
    return payload


@login_required
@subscription_required
def dashboard_data_view(request):
    """DataTables AJAX backend for the /dashboard/ feed.

    Same shape as `permits_data_view` but pinned to a 3-day window
    (regardless of plan) and augmented with `summary` aggregates so
    the dashboard stat cards (Permits / Hot 80+ / Avg) reflect the
    *full filtered population* rather than the visible page slice.
    Auth, filters, and sort live in `_build_permits_data_payload`
    so the two surfaces never drift.

    Uses a higher row cap (2000 vs the default 500) because the
    dashboard runs client-side: it fetches the *entire* 3-day window
    in one shot and lets the browser sort / filter / paginate by
    the 12-factor derived score. Users with many subscribed cities
    can easily exceed 500 permits across 3 days, and any row that
    falls outside the cap is invisible both to the table sort and
    to the "Best Lead Right Now" hero pick. 2000 keeps the payload
    bounded (~600 KB) while covering all observed real accounts.
    """
    return JsonResponse(_build_permits_data_payload(
        request, history_days_override=3, include_summary=True,
        max_page_override=2000,
    ))


@login_required
@subscription_required
def permits_data_view(request):
    """DataTables-compatible JSON endpoint backing /permits/.

    Replaces the legacy approach of dumping every matching row into the
    HTML as a giant inline JSON blob. The browser now asks for the
    visible page only (DataTables server-side processing) and re-asks
    on every sort / filter / page change. The same authorisation gate
    used by `permits()` applies here — `query_permits_for_dashboard`
    enforces the user's city subscription set + per-plan history window
    as the first WHERE clause on every query, so a hand-crafted GET
    cannot leak permits in cities the caller doesn't pay for.

    DataTables sends the standard params (`draw`, `start`, `length`,
    `order[0][column]`, `order[0][dir]`, `search[value]`) plus our
    custom panel filters as flat GET params (`f_state`, `f_city`,
    `f_type`, `f_status`, `f_score_min`, `f_score_max`, `f_owner`,
    `f_phone`, `f_email`, `f_issued_after`, `f_expires_before`,
    `f_keyword`, `f_tier`). All filtering, sorting, and pagination
    happens in SQL — Postgres returns exactly the visible page so a
    single account with thousands of permits stays as fast as one
    with a handful, and there's no upstream cap to silently truncate
    the result.

    Body lives in `_build_permits_data_payload` and is shared with
    `dashboard_data_view` so the dashboard table can never drift from
    the permit-history table (auth gate, filters, sort, paging).
    """
    return JsonResponse(_build_permits_data_payload(request))


@login_required
@subscription_required
def permits(request):
    ctx     = _user_ctx(request)
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}
    raw     = user.get('cities', [])
    # ``data.cities`` holds 2-letter STATE codes since the May-2026
    # pricing migration. Build the State dropdown from that list and
    # populate the cascading City dropdown from DISTINCT (city, state)
    # pairs that actually exist in the permits table inside those
    # states. Parsing entries as legacy "City, ST" strings produced an
    # empty State select and a City select listing state codes as cities
    # — selecting one then sent f_city='FL' which the SQL compared as
    # LOWER(city)='fl' and returned zero rows.
    plan_states = sorted({(s or '').strip().upper() for s in raw
                          if s and len((s or '').strip()) == 2})
    plan_cities = get_distinct_cities_for_states(plan_states)
    ctx['plan_cities_json'] = json.dumps(plan_cities)
    ctx['plan_states']      = plan_states
    # City freeze check
    plan       = user.get('plan', 'starter')
    city_limit = PLAN_CITY_LIMITS.get(plan, 1)
    cities_frozen = user.get('cities_frozen', False)
    ctx['cities_frozen'] = cities_frozen
    ctx['city_limit']    = city_limit
    ctx['city_excess']   = max(0, len(raw) - city_limit)
    # The /permits/ table is now loaded dynamically via DataTables AJAX
    # (`permits_data_view`) so the page render no longer needs to embed
    # the full permit list in the HTML — saves bandwidth and lets the
    # browser ask for one page at a time. We still resolve per-plan
    # feature flags + the history-window label here because the filter
    # panel and the "showing the last N days" banner are server-rendered.
    _plan_title    = plan.title() if plan.lower() in ('starter', 'pro', 'agency') else 'Starter'
    _plan_limits   = PLAN_USAGE_LIMITS.get(_plan_title, PLAN_USAGE_LIMITS['Starter'])
    _history_days  = _plan_limits.get('history_days')

    # Per-plan feature flags
    _feats = PLAN_FEATURES.get(_plan_title, PLAN_FEATURES['Starter'])
    ctx['can_export_csv']   = _feats['csv_export']
    ctx['can_filter_trade'] = _feats['trade_filter']
    ctx['history_days']     = _history_days

    response = render(request, 'core/permits.html', ctx)
    response['Cache-Control'] = 'no-store'
    return response

def _build_user_cities(raw: list) -> list:
    """Build the settings → coverage tile list. Since the May-2026
    pricing migration, ``data.cities`` holds uppercase 2-letter state
    codes (e.g. ``["TX", "FL"]``) — one tile per state. The dict shape
    is kept compatible with the existing template (``name``, ``state``,
    ``entry``, ``count``) so the settings tile renderer doesn't need a
    parallel branch. Any stray legacy "City, ST" string still lying in
    the DB is normalised to its state code on the fly.
    """
    result = []
    for i, entry in enumerate(raw or []):
        entry = (entry or '').strip()
        if not entry:
            continue
        # Normalise legacy "City, ST" survivors to their state code.
        if ', ' in entry:
            entry = entry.rsplit(', ', 1)[1].strip().upper()
        code = entry.upper()
        if len(code) != 2:
            continue
        full = _FULL_STATE_NAMES.get(code, code)
        result.append({
            'name':     full,    # template renders the full state name
            'state':    code,    # 2-letter abbrev shown under the name
            'entry':    code,    # used by add/remove POST payloads
            'count':    max(3, 15 - i * 2),
            'selected': True,
        })
    return result


@login_required
@subscription_required
def settings_view(request):
    ctx     = _user_ctx(request)
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}

    # ── Sync plan from DB — trust DB over stale session ────────
    _db_plan = user.get('plan', 'starter').title()
    if _db_plan != ctx.get('user_plan'):
        ctx['user_plan'] = _db_plan
        request.session['user_plan'] = _db_plan

    raw_cities  = user.get('cities', [])      # holds STATE codes since May-2026
    user_cities = _build_user_cities(raw_cities)

    _user_plan   = ctx['user_plan']
    _state_limit = PLAN_CITY_LIMITS.get(_user_plan, 1)
    _state_count = len(user_cities)
    _frozen      = user.get('cities_frozen', False)
    for i, c in enumerate(user_cities):
        c['overflow'] = _frozen and i >= _state_limit

    # Available states the user can still add (not already selected).
    _all_visible_states = get_customer_visible_states()
    _selected_codes     = {c['entry'] for c in user_cities}
    _available_states   = [s for s in _all_visible_states
                           if s['state'] not in _selected_codes]

    # Template var names kept (cities / city_limit / cities_remaining /
    # cities_frozen) — the integers ARE state counts now. The settings
    # template was rewritten in the same PR to use these as state vars.
    ctx['cities']           = user_cities
    ctx['city_count']       = _state_count
    ctx['city_limit']       = _state_limit
    ctx['cities_remaining'] = max(0, _state_limit - _state_count)
    ctx['cities_frozen']    = _frozen
    ctx['city_excess']      = max(0, _state_count - _state_limit)
    ctx['available_states'] = _available_states

    # ── Usage stats (sync from DB, default if missing) ────────────
    _limits     = PLAN_USAGE_LIMITS.get(_user_plan, PLAN_USAGE_LIMITS['Starter'])
    _alerts     = user.get('alerts_sent', 0)
    _api        = user.get('api_calls',   0)
    _city_lim   = _limits['cities']
    _alert_lim  = _limits['alerts']
    _api_lim    = _limits['api']

    ctx['alerts_sent']      = f'{_alerts:,}'
    ctx['alerts_limit']     = _fmt_num(_alert_lim)
    ctx['alerts_pct']       = _usage_pct(_alerts, _alert_lim)
    ctx['alerts_used_pct']  = f'{_usage_pct(_alerts, _alert_lim)}%' if _alert_lim else '—'

    ctx['api_calls']        = _fmt_num(_api)
    ctx['api_limit']        = _fmt_num(_api_lim)
    ctx['api_pct']          = _usage_pct(_api, _api_lim)
    ctx['api_included']     = _api_lim != 0

    ctx['city_pct']         = _usage_pct(_state_count, _city_lim)
    # Preview shows the first two state names rather than city names.
    ctx['city_names_preview'] = ', '.join(
        _FULL_STATE_NAMES.get((s or '').strip().upper(), (s or '').strip().upper())
        for s in (user.get('cities') or [])[:2]
    ) or '—'

    name  = user.get('name', '').strip()
    parts = name.split(' ', 1)
    ctx['first_name']     = parts[0] if parts else ''
    ctx['last_name']      = parts[1] if len(parts) > 1 else ''
    ctx['company']        = user.get('company', '')
    ctx['bill_company']   = user.get('bill_company',   user.get('company', ''))
    ctx['bill_tax_id']    = user.get('bill_tax_id',    '')
    ctx['bill_street']    = user.get('bill_street',    '')
    ctx['bill_city']      = user.get('bill_city',      '')
    ctx['bill_state_zip'] = user.get('bill_state_zip', '')

    # ── Active sessions ────────────────────────────────────────
    raw_sessions = get_sessions_for_user(user_id)
    current_key  = request.session.session_key
    session_list = []
    for s in sorted(raw_sessions, key=lambda x: x.get('last_seen', ''), reverse=True):
        session_list.append({
            'id':       s['id'],
            'device':   s.get('device', 'Unknown Device'),
            'ip':       s.get('ip', '—'),
            'last_seen_raw': s.get('last_seen', ''),
            'last_seen': _time_ago(s.get('last_seen', '')),
            'created_at': _time_ago(s.get('created_at', '')),
            'is_current': s.get('session_key') == current_key,
        })
    ctx['user_sessions']   = session_list
    ctx['session_count']   = len(session_list)

    # ── Login history ──────────────────────────────────────────
    raw_history = get_login_history_for_user(user_id, limit=20)
    login_history = []
    for h in raw_history:
        ts_raw = h.get('ts', '') or ''
        ts_display = ts_raw  # safe fallback
        # Strip trailing 'Z' that some legacy rows may have, then drop the
        # microseconds tail so ``fromisoformat`` works on every Python.
        clean = ts_raw.rstrip('Z')
        if '.' in clean:
            clean = clean.split('.', 1)[0]
        try:
            dt = datetime.fromisoformat(clean)
            # ``%-I`` is GNU-only — manually strip the leading zero so the
            # format works the same on every libc (Alpine, musl, BSD, etc).
            hour_12 = dt.hour % 12 or 12
            ampm    = 'AM' if dt.hour < 12 else 'PM'
            ts_display = dt.strftime('%b %d, %Y · ') + f"{hour_12}:{dt.minute:02d} {ampm}"
        except Exception:
            pass
        login_history.append({
            'ts':     ts_display,
            'device': h.get('device', 'Unknown'),
            'ip':     h.get('ip', '—'),
            'status': h.get('status', 'success'),
        })
    ctx['login_history'] = login_history

    ctx['totp_enabled'] = bool(user.get('totp_enabled'))

    # ── Whop membership data (DB-cached, no live API call) ───────
    # Snapshot is kept fresh by _snapshot_whop_to_user, called from
    # ls_success (after payment) and the Whop webhook (membership.went_*).
    # This used to make a live wp.get_membership() call here that cost
    # 300-800ms on every settings page load.
    whop_mem_id = user.get('whop_membership_id')
    whop_info   = _build_whop_info_from_user(user)
    ctx['ls_info']    = whop_info        # keep template key for compatibility
    ctx['ls_sub_id']  = whop_mem_id or ''
    # `whop_membership_id` is intentionally retained as a historical
    # breadcrumb after cancel/expire (so admins can audit what the user
    # used to have). The settings billing card must therefore gate on
    # `subscription_active`, otherwise a deactivated user would still
    # see the "active subscription" controls (Cancel / Update payment /
    # Downgrade) for a subscription that no longer exists.
    ctx['has_ls_sub'] = bool(whop_mem_id and whop_info
                             and user.get('subscription_active'))

    # ── Affiliate / referral context ─────────────────────────────
    # Lazily mint a referral code the first time the user opens Settings,
    # so we never burn codes on accounts that never visit the page.
    try:
        _ref_code = ensure_referral_code(user_id)
    except Exception:
        _ref_code = ''
    _ref_stats   = get_referral_stats_for_user(user_id) if _ref_code else \
                   {'signups': 0, 'paid': 0, 'earnings_cents': 0}
    _ref_list    = get_referees_for_user(user_id)       if _ref_code else []
    _ref_pct_raw = get_system_setting('affiliate_commission_pct', 20)
    try:
        _ref_pct = int(_ref_pct_raw)
    except (TypeError, ValueError):
        _ref_pct = 20
    # Use only Django's validated host (vetted against ALLOWED_HOSTS) — never
    # the raw Origin header, which is attacker-controlled and would let a
    # crafted request poison every share link rendered in the affiliate tab.
    _origin = (('https://' if request.is_secure() else 'http://') +
               request.get_host()).rstrip('/')
    ctx['referral_code']         = _ref_code
    ctx['referral_link']         = f'{_origin}/r/{_ref_code}/' if _ref_code else ''
    ctx['referral_signups']      = _ref_stats['signups']
    ctx['referral_paid']         = _ref_stats['paid']
    ctx['referral_earnings']     = f"${_ref_stats['earnings_cents']/100:,.2f}"
    ctx['referral_commission_pct'] = _ref_pct
    # Render-friendly per-referee list for the table
    _ref_rows = []
    for r in _ref_list:
        em = (r.get('email') or '').strip()
        local, _, domain = em.partition('@')
        masked = (local[:1] + '***@' + domain) if local and domain else em
        _ref_rows.append({
            'masked_email':  masked,
            'plan':          (r.get('plan') or 'starter').title(),
            'paid':          bool(r.get('first_paid_at')),
            'earnings':      f"${r.get('earnings_cents', 0)/100:,.2f}",
            'signed_up_at':  (r.get('signed_up_at').strftime('%b %d, %Y')
                              if r.get('signed_up_at') else '—'),
        })
    ctx['referral_rows'] = _ref_rows

    # ── Cross-check: if Whop plan differs from DB plan, Whop wins ──────────
    # …but never let an apparent DOWNGRADE win silently. Same reasoning
    # as in `_whop_login_sync`: when the bound membership detects a
    # lower tier than what we have stored, it's almost always because
    # we're bound to the user's OLD membership during an upgrade
    # transition. Only the webhook (went_invalid / scheduled
    # pending_downgrade) is allowed to actually downgrade the plan.
    if whop_info:
        wp_detected = whop_info.get('plan', '').lower()
        db_plan     = user.get('plan', '').lower()
        if (wp_detected and wp_detected != db_plan
                and not _is_plan_downgrade(wp_detected, db_plan)):
            update_user(user_id, plan=wp_detected)
            ctx['user_plan']             = wp_detected.title()
            request.session['user_plan'] = wp_detected.title()

    # ── Invoices ──────────────────────────────────────────────
    # No live wp.get_membership() / billing-sync call here — billing
    # rows are written by the webhook on renewal and by ls_success on
    # initial purchase. Settings page just renders whatever's already
    # in the DB so it costs ~one cached query, not a 300-800 ms Whop
    # round-trip on every load.
    ctx['ls_invoices'] = get_user_invoices(user_id)

    # ── Billing period progress for UI ─────────────────────────
    from datetime import timezone as _tz
    _bps = user.get('billing_period_start') or ''
    _bpe = user.get('billing_period_end')   or ''
    _ss  = user.get('subscription_start')   or ''
    ctx['billing_period_start']   = _bps
    ctx['billing_period_end']     = _bpe
    ctx['subscription_start']     = _ss

    # Compute % through current billing period
    _bp_pct = 0
    _bp_days_left = ''
    if _bps and _bpe:
        try:
            from datetime import datetime
            _s = datetime.fromisoformat(_bps)
            _e = datetime.fromisoformat(_bpe)
            _n = datetime.now(_tz.utc).replace(tzinfo=None)
            _total = (_e - _s).days or 1
            _elapsed = max(0, min(_total, (_n - _s).days))
            _bp_pct = round(_elapsed * 100 / _total)
            _left   = max(0, (_e - _n).days)
            _bp_days_left = f'{_left} day{"s" if _left != 1 else ""} left'
        except Exception:
            pass
    ctx['billing_period_pct']      = _bp_pct
    ctx['billing_period_days_left'] = _bp_days_left

    # State-based coverage picker (May-2026 pricing migration). The
    # legacy ``state_cities_json`` blob is no longer used by the
    # rewritten settings template — coverage is now a flat state grid
    # rendered server-side from ``ctx['available_states']``.

    # Validate pending_downgrade is truly a downgrade from current plan
    _RANK = {'starter': 0, 'pro': 1, 'agency': 2}
    _pd   = user.get('pending_downgrade') or {}
    _cur_rank = _RANK.get(ctx['user_plan'].lower(), 0)
    if _pd:
        _pd_rank = _RANK.get(_pd.get('plan', '').lower(), 0)
        if _pd_rank >= _cur_rank:
            update_user(user_id, pending_downgrade=None)
            _pd = {}
    ctx['pending_downgrade'] = _pd if _pd else None

    # ── Alert preferences (Daily Email Digest only) ────────────
    _default_prefs = {'daily_digest': True}
    saved_prefs = user.get('alert_prefs', {})
    ctx['alert_prefs'] = {k: saved_prefs.get(k, v) for k, v in _default_prefs.items()}
    from .db import get_digest_schedule as _gds
    ctx['digest_schedule'] = _gds(user_id)
    ctx['digest_timezones'] = _DIGEST_TZ_CHOICES

    # ── CRM integrations (OAuth + Zapier) ──────────────────────
    crm = get_crm_integrations(user_id)
    ctx['crm_integrations'] = {
        'hubspot': {
            'connected':       bool(crm.get('hubspot', {}).get('access_token')),
            'configured':      _integrations.oauth_provider_configured('hubspot'),
            'connected_at':    crm.get('hubspot', {}).get('connected_at'),
            'account_label':   crm.get('hubspot', {}).get('account_label') or crm.get('hubspot', {}).get('hub_id') or '',
        },
        'ghl': {
            'connected':       bool(crm.get('ghl', {}).get('access_token')),
            'configured':      _integrations.oauth_provider_configured('ghl'),
            'connected_at':    crm.get('ghl', {}).get('connected_at'),
            'account_label':   crm.get('ghl', {}).get('account_label') or crm.get('ghl', {}).get('locationId') or '',
        },
        'zapier': {
            'webhook_url':     crm.get('zapier', {}).get('webhook_url') or '',
            'connected':       bool(crm.get('zapier', {}).get('webhook_url')),
        },
    }

    # Mode-aware pricing on the settings/billing page so the user always
    # sees the prices that match what their checkout will actually charge.
    ctx['pricing'] = wp.get_pricing_dict(wp.mode_for_user(user))

    return render(request, 'core/settings.html', ctx)


@login_required
def clear_login_history(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    user_id = request.session.get('user_id')
    removed = clear_login_history_for_user(user_id)
    return JsonResponse({'ok': True, 'removed': removed})


@login_required
def save_alert_prefs(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    user_id = request.session.get('user_id')
    # Preserve any legacy keys that aren't in the simplified UI so existing
    # users' prefs (billing_reminders, product_updates, etc.) aren't wiped.
    existing = (get_user_by_id(user_id) or {}).get('alert_prefs', {}) or {}
    existing['daily_digest'] = request.POST.get('daily_digest') == '1'
    update_user(user_id, alert_prefs=existing)
    # Digest delivery time + timezone — every plan gets the daily email
    # and picks its own local clock time here.
    from .db import save_digest_schedule as _sds
    t  = request.POST.get('digest_time', '').strip()
    tz = request.POST.get('digest_tz',   '').strip()
    if t or tz:
        _sds(user_id, t, tz)
    return JsonResponse({'ok': True})


# Curated list of US IANA timezones rendered in the digest-time picker.
# Covers every state the platform serves. Kept short on purpose so the
# UI stays a single dropdown rather than a 500-entry IANA wall.
_DIGEST_TZ_CHOICES = [
    ('America/New_York',    'Eastern (New York)'),
    ('America/Chicago',     'Central (Chicago)'),
    ('America/Denver',      'Mountain (Denver)'),
    ('America/Phoenix',     'Mountain — no DST (Phoenix)'),
    ('America/Los_Angeles', 'Pacific (Los Angeles)'),
    ('America/Anchorage',   'Alaska (Anchorage)'),
    ('Pacific/Honolulu',    'Hawaii (Honolulu)'),
]


# ── Whop checkout / membership views ─────────────────────────────────────

@login_required
@require_http_methods(['GET'])
def ls_checkout(request):
    """
    Returns JSON {url} for the frontend to open a new tab to Whop checkout.
    Guards against duplicate memberships — if the user already has an active
    Whop membership, block new checkout and redirect to Whop Hub instead.
    """
    plan   = request.GET.get('plan', 'starter').lower()
    period = request.GET.get('period', 'monthly').lower()
    if plan not in ('starter', 'pro', 'agency'):
        return JsonResponse({'ok': False, 'error': 'Invalid plan'}, status=400)
    if period not in ('monthly', 'annual'):
        period = 'monthly'
    if period == 'annual' and not wp.annual_billing_enabled():
        period = 'monthly'
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}
    email   = user.get('email', '')

    # Guard: block new checkout if an active membership already exists
    if email and user.get('whop_membership_id'):
        try:
            mem = wp.get_membership(user['whop_membership_id'])
            if mem and mem.get('valid'):
                return JsonResponse({
                    'ok':      False,
                    'has_sub': True,
                    'error':   'You already have an active subscription. Use the plan switcher or Manage Billing to make changes.',
                    'portal':  True,
                })
        except Exception:
            pass

    # Use embedded checkout page instead of redirecting to Whop
    embed_url = f'/billing/embed/?plan={plan}&period={period}&back=/settings/%23billing'
    if request.GET.get('redirect'):
        return redirect(embed_url)
    return JsonResponse({'ok': True, 'url': embed_url})


_PLAN_META = {
    ('starter', 'monthly'): {'label': 'Starter', 'period_label': 'Monthly · 1 state',
                              'features': ['Daily permit alerts for 1 state', 'Up to 30 alerts/month', 'AI lead score (grade tier)', '7-day permit history', 'Email delivery', 'Trade filtering + CSV export']},
    ('starter', 'annual'):  {'label': 'Starter', 'period_label': 'Annual · 1 state',
                              'features': ['Daily permit alerts for 1 state', 'Up to 30 alerts/month', 'AI lead score (grade tier)', '7-day permit history', 'Email delivery', 'Trade filtering + CSV export', 'Save 20% vs monthly']},
    ('pro',     'monthly'): {'label': 'Pro',     'period_label': 'Monthly · up to 5 cities',
                              'features': ['Everything in Starter', 'Up to 5 cities', 'Up to 300 alerts/month', 'Full AI score (0–100)', '90-day permit history', 'Trade filtering + CSV export', 'Priority support']},
    ('pro',     'annual'):  {'label': 'Pro',     'period_label': 'Annual · up to 5 cities',
                              'features': ['Everything in Starter', 'Up to 5 cities', 'Up to 300 alerts/month', 'Full AI score (0–100)', '90-day permit history', 'Trade filtering + CSV export', 'Priority support', 'Save 20% vs monthly']},
    ('agency',  'monthly'): {'label': 'Agency',  'period_label': 'Monthly · up to 15 cities',
                              'features': ['Everything in Pro', '15 cities included', 'Unlimited alerts', 'Full permit history archive', 'REST API access', 'Slack & SMS alert channels', 'White-label export', 'Dedicated onboarding call']},
    ('agency',  'annual'):  {'label': 'Agency',  'period_label': 'Annual · up to 15 cities',
                              'features': ['Everything in Pro', '15 cities included', 'Unlimited alerts', 'Full permit history archive', 'REST API access', 'Slack & SMS alert channels', 'White-label export', 'Dedicated onboarding call', 'Save 20% vs monthly']},
}


def _get_plan_pricing(plan: str, period: str, mode: str = None) -> dict:
    """Build mode-aware pricing dict for the embed checkout view.

    ``mode`` defaults to the global setting when omitted; pass the
    user's ``whop_mode`` so admins-flagged-as-dev see $1 / mo on the
    embed page (matching the dev plan_id they're being routed to).
    """
    key = (plan, period)
    meta = _PLAN_META.get(key, _PLAN_META[('starter', 'monthly')])
    price  = wp.get_plan_price(plan, period, mode)
    if period == 'annual':
        billed = f'Billed ${price * 12:,} today · save 20%'
    else:
        billed = 'Billed monthly · cancel anytime'
    return {
        'price':        price,
        'unit':         'mo',
        'billed':       billed,
        'label':        meta['label'],
        'period_label': meta['period_label'],
        'features':     meta['features'],
    }


@login_required
@require_http_methods(['GET'])
def ls_embed_checkout(request):
    """Render an embedded Whop checkout page using the Whop loader script."""
    plan   = request.GET.get('plan', 'starter').lower()
    period = request.GET.get('period', 'monthly').lower()
    if plan not in ('starter', 'pro', 'agency'):
        plan = 'starter'
    if period not in ('monthly', 'annual'):
        period = 'monthly'
    if period == 'annual' and not wp.annual_billing_enabled():
        period = 'monthly'

    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}
    email   = user.get('email', '')

    # Per-user Whop billing mode: an admin-flagged 'dev' user gets routed
    # to the $1 dev plan_id (and dev pricing display) without flipping
    # the global mode for everybody else. See core/whop.mode_for_user().
    user_mode = wp.mode_for_user(user)
    plan_id   = wp.get_plan_id(plan, period, user_mode)
    if not plan_id:
        # Whop plan_id for this plan/period isn't configured in admin yet.
        # Bounce to the paywall and surface a clear, actionable banner
        # there (instead of silently looping back).
        return redirect(f'/paywall/?missing_plan={plan}_{period}')

    dev_domain = os.environ.get('REPLIT_DEV_DOMAIN', '')
    domain  = f'https://{dev_domain}' if dev_domain else request.build_absolute_uri('/').rstrip('/')
    return_url = domain + f'/billing/success/?plan={plan}&period={period}'

    pricing = _get_plan_pricing(plan, period, user_mode)
    # Validate back_url — only allow same-origin paths to prevent XSS via javascript:/data: URLs
    raw_back = request.GET.get('back', '') or ''
    back_url = raw_back if (raw_back.startswith('/') and not raw_back.startswith('//')) else '/settings/#billing'

    ctx = {
        'plan_id':       plan_id,
        'plan':          plan,
        'period':        period,
        'plan_label':    pricing['label'],
        'period_label':  pricing['period_label'],
        'price_display': pricing['price'],
        'price_unit':    pricing['unit'],
        'billed_text':   pricing['billed'],
        'features':      pricing['features'],
        'return_url':    return_url,
        'email':         email,
        'user_id':       user_id or '',
        'back_url':      back_url,
    }
    return render(request, 'core/embed_checkout.html', ctx)


@login_required
@require_http_methods(['GET'])
def ls_success(request):
    """Post-checkout landing page.

    Whop redirects the customer here with `?membership_id={id}` filled
    in from the per-plan checkout-link redirect URL configured in the
    Whop dashboard. We fetch THAT specific membership by id (one
    deterministic API call, no eventual-consistency race) and bind it
    to the user. Email-lookup fallback is preserved for any legacy
    checkout link that still doesn't carry the placeholder.
    """
    plan      = request.GET.get('plan', '').lower()
    mem_id_qs = (request.GET.get('membership_id') or '').strip()
    user_id   = request.session.get('user_id')
    user      = get_user_by_id(user_id) or {}
    email     = user.get('email', '')

    # NOTE: we deliberately do NOT call `update_user(user_id, plan=plan)`
    # here. The URL `?plan=` is attacker-controlled — anyone can land on
    # `/billing/success/?plan=agency` without paying a cent. The plan
    # field is only updated below, and only after we've verified the
    # membership against the user's email via Whop.

    # ── Fast path: Whop sent us the exact membership_id in the redirect.
    # `wp.get_membership(id)` is a single API call and the id can never
    # be a stale-older membership — Whop minted it for THIS checkout.
    #
    # SECURITY: the membership_id is attacker-controlled (it sits in a
    # URL query param). We MUST verify the membership actually belongs
    # to the currently-logged-in user before binding it, otherwise any
    # user could send themselves to
    #   /billing/success/?membership_id=mem_someone_elses_agency_sub
    # and inherit a paid Agency subscription. We also require the
    # membership to be `valid` so a known-but-cancelled id can't
    # reactivate the account.
    chosen = None
    if mem_id_qs and email:
        try:
            candidate = wp.get_membership(mem_id_qs)
        except Exception:
            candidate = None
        if candidate:
            mem_email = (candidate.get('email') or '').strip().lower()
            user_email_lc = email.strip().lower()
            if mem_email and mem_email == user_email_lc and candidate.get('valid'):
                chosen = candidate
            else:
                import logging
                logging.getLogger(__name__).warning(
                    "ls_success: rejected membership_id=%r for user_id=%s "
                    "(mem_email=%r vs user_email=%r, valid=%r)",
                    mem_id_qs, user_id, mem_email, user_email_lc,
                    candidate.get('valid'),
                )

    # ── Fallback: legacy checkout links that don't carry the
    # `{membership_id}` placeholder yet. Same email-lookup-with-retry
    # path the previous implementation used. Once all 6 Whop checkout
    # links have the placeholder this branch never executes.
    #
    # SECURITY: `get_memberships_by_email()` intentionally keeps rows
    # with a blank `email` field because Whop's v2 search is fuzzy.
    # That means we can't trust the lookup as proof of ownership on
    # its own — re-verify the returned membership's email matches the
    # logged-in user (same guard as the fast path) before activating.
    if chosen is None and email and plan in ('starter', 'pro', 'agency'):
        try:
            candidate = _wait_for_paid_membership(email, plan_hint=plan)
        except Exception:
            candidate = None
        if candidate:
            mem_email = (candidate.get('email') or '').strip().lower()
            user_email_lc = email.strip().lower()
            if mem_email and mem_email == user_email_lc and candidate.get('valid'):
                chosen = candidate
            else:
                import logging
                logging.getLogger(__name__).warning(
                    "ls_success: rejected fallback membership for user_id=%s "
                    "(mem_email=%r vs user_email=%r, valid=%r)",
                    user_id, mem_email, user_email_lc, candidate.get('valid'),
                )

    if chosen:
        # Detect plan from the membership; fall back to the URL hint if
        # detection fails (e.g. a brand-new plan_id the admin hasn't
        # added to /admin-panel/whop-settings/ yet).
        detected = wp.plan_from_membership(chosen, default='') or plan
        if detected not in ('starter', 'pro', 'agency'):
            detected = plan or 'starter'
        update_user(user_id,
                    whop_membership_id=chosen.get('id', ''),
                    plan=detected,
                    whop_cancelled=chosen.get('cancel_at_period_end', False),
                    subscription_active=True,
                    onboarding_complete=True,
                    whop_resync_pending=False)
        request.session['user_plan'] = detected.title()
        try:
            _snapshot_whop_to_user(user_id, chosen)
        except Exception:
            pass
        try:
            _sync_billing_and_invoice(user_id, chosen)
        except Exception:
            pass
        # Branded payment-success / activation receipt — fires on every
        # successful Whop bind, including upgrades and re-activations.
        # The dedup helper stamps (membership_id, plan) on the user so
        # the matching ``membership.went_valid`` webhook (which also
        # calls this helper) becomes a no-op — guarantees exactly one
        # receipt across the redirect/webhook race. If the user closes
        # the browser before this redirect runs, the webhook still
        # delivers the email from its own call site below.
        try:
            _maybe_fire_payment_success_email(
                user_id, chosen.get('id', ''), detected, chosen, request,
            )
        except Exception:
            log.exception("payment-success dispatch failed for user_id=%s", user_id)
    else:
        # Couldn't verify a membership against Whop. Do NOT grant
        # `subscription_active=True` from the URL plan hint alone —
        # `?plan=` is attacker-controlled and the previous code path
        # would silently upgrade anyone who landed here. Mark the
        # account as awaiting verification; the Whop webhook (or an
        # admin sync from `/admin-panel/users/`) will activate the
        # subscription when Whop catches up. Worst case: the user
        # sees a "we're confirming your payment" state for a few
        # seconds until the webhook fires.
        update_user(user_id, whop_resync_pending=True)
        import logging
        logging.getLogger(__name__).warning(
            "ls_success: could not verify membership for user_id=%s "
            "(mem_id_qs=%r, plan_hint=%r). Awaiting webhook.",
            user_id, mem_id_qs, plan,
        )

    return render(request, 'core/ls_success.html', {'plan': (plan or 'your').title()})


@login_required
@require_http_methods(['POST', 'GET'])
def ls_sync(request):
    """No-op as of the bind-from-redirect change.

    The user-facing "Sync Now" button no longer hits the Whop API.
    Membership binding now happens deterministically at checkout time
    (`ls_success` reads `?membership_id=` from the Whop redirect) and
    is updated by the webhook for downstream state changes.

    If a customer genuinely needs a manual Whop refresh, an admin can
    run it from `/admin-panel/users/` via the per-user resync action
    or the bulk Whop sync — those endpoints still call Whop directly.
    Keeping this route as a no-op (instead of removing it) so the
    settings UI's existing Sync button and any external scripts keep
    returning a 200 instead of a 404.
    """
    next_url = request.GET.get('next', '')
    user_id  = request.session.get('user_id')
    user     = get_user_by_id(user_id) or {}
    plan     = (user.get('plan') or 'starter').title()
    active   = bool(user.get('subscription_active'))
    if next_url and next_url.startswith('/'):
        return redirect(next_url)
    # `ok` reflects the actual cached subscription state so the
    # post-checkout polling page on `ls_success.html` only shows
    # "Subscription connected" when the user really is connected.
    # If the membership couldn't be verified at checkout, the page
    # keeps polling (max 5 attempts) until the webhook flips
    # subscription_active=True or the timeout message appears.
    return JsonResponse({
        'ok':   active,
        'plan': plan,
        'msg':  (f'Plan: {plan}.' if active else
                 'Still confirming your payment with Whop\u2026'),
    })


@login_required
@require_http_methods(['GET'])
def ls_portal(request):
    return redirect('https://whop.com/hub/')


@login_required
@require_http_methods(['POST'])
def ls_cancel(request):
    """Cancel-at-period-end. The user keeps access until the next renewal
    date (which they've already paid for) and is then dropped.

    We only mark the local DB as ``whop_cancelled`` AFTER the Whop API
    confirms the cancel. Previously this view caught every exception and
    flipped the local flag regardless — so a 404/500 from Whop would
    leave the subscription active in Whop while the UI told the user
    "you're cancelled", causing surprise renewals next month.
    """
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}
    email   = (user.get('email') or '').strip()
    mem_id  = user.get('whop_membership_id')

    # Loop over EVERY active membership tied to this email — not just the
    # one we have on file — so historical orphans (from past flows that
    # left a stale active sub in Whop) get swept up too. Otherwise the
    # user keeps getting billed for memberships they never see in our UI.
    result = wp.cancel_all_active_for_email(
        email,
        immediate=False,
        extra_membership_ids=[mem_id] if mem_id else None,
    )

    # Nothing was active anywhere → nothing to cancel.
    if result['discovered'] == 0:
        return JsonResponse({'ok': False, 'error': 'No active subscription found.'})

    # Some (or all) cancellations failed → surface to the user, do not
    # flip the local flag. Whatever DID succeed is reported in the body
    # so they can see partial progress.
    if result['errors']:
        return JsonResponse({
            'ok':       False,
            'error':    "We couldn't cancel one or more of your subscriptions. Please try again or contact support.",
            'cancelled': result['cancelled'],
            'failed':    list(result['errors'].keys()),
            'detail':    ' | '.join(f'{k}: {v}' for k, v in result['errors'].items()),
        }, status=502)

    # Every active membership cancelled cleanly.
    update_user(user_id, whop_cancelled=True)
    n = len(result['cancelled'])
    msg = ('Subscription cancelled. Access continues until the end of your billing period.'
           if n == 1 else
           f'{n} subscriptions cancelled. Access continues until the end of each billing period.')
    return JsonResponse({'ok': True, 'msg': msg, 'cancelled_count': n, 'cancelled': result['cancelled']})


@login_required
@require_http_methods(['POST'])
def ls_pause(request):
    return JsonResponse({'ok': False, 'error': 'Pausing is not supported with Whop. Please cancel and resubscribe when ready.'}, status=400)


@login_required
@require_http_methods(['POST'])
def ls_change_plan(request):
    """Switch plans by terminating the old subscription, then routing
    the user to a fresh Whop checkout for the new plan.

    Same code path for upgrade, downgrade, and period switch — keeps
    the state machine simple and avoids "two memberships at once" or
    "Whop says one thing, our DB says another" race conditions.

    Steps:
      1. Cancel the user's current Whop membership (best-effort —
         if it's already cancelled or missing, we still proceed).
      2. Reset local billing state so the user has no entitlement
         until the new payment is confirmed.
      3. Return the embedded-checkout URL for the new plan. The
         frontend redirects the browser to it. ``ls_success`` then
         binds the brand-new membership_id from Whop's redirect.

    The frontend confirmation modal warns the user this is
    irreversible and that any unused time on the old plan is
    forfeited.
    """
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}

    try:
        data = json.loads(request.body)
    except Exception:
        data = {}
    new_plan = (data.get('plan') or '').lower()
    period   = (data.get('period') or 'monthly').lower()
    if new_plan not in ('starter', 'pro', 'agency'):
        return JsonResponse({'ok': False, 'error': 'Invalid plan.'})
    if period not in ('monthly', 'annual'):
        period = 'monthly'
    if period == 'annual' and not wp.annual_billing_enabled():
        period = 'monthly'

    # 1) Terminate EVERY active membership tied to this email — not just
    #    the one we have on file. We use immediate=True because the user
    #    is paying for a new plan in the next step; leaving any old
    #    membership renewing would double-bill them. Sweeping by email
    #    also catches orphans created by past flows that left an active
    #    sub in Whop our DB never knew about.
    #
    #    If ANY cancellation fails we MUST stop here — forfeiting the
    #    user's plan locally while it stays active in Whop is exactly
    #    the bug the user originally reported.
    email  = (user.get('email') or '').strip()
    mem_id = user.get('whop_membership_id')
    sweep  = wp.cancel_all_active_for_email(
        email, immediate=True,
        extra_membership_ids=[mem_id] if mem_id else None,
    )
    if sweep['errors']:
        return JsonResponse({
            'ok':       False,
            'error':    "We couldn't cancel your current plan with our payment provider. No changes were made — please try again or contact support.",
            'cancelled': sweep['cancelled'],
            'failed':    list(sweep['errors'].keys()),
            'detail':    ' | '.join(f'{k}: {v}' for k, v in sweep['errors'].items()),
        }, status=502)

    # 2) Reset local entitlement. The user is "in transit" until
    #    they finish the new checkout. ls_success will populate
    #    everything correctly when the new membership lands.
    update_user(
        user_id,
        subscription_active=False,
        whop_cancelled=True,
        pending_downgrade=None,
    )

    # 3) Hand them off to the embedded checkout page for the new plan.
    checkout_url = (
        f'/billing/embed/?plan={new_plan}&period={period}'
        f'&back=/settings/%23billing'
    )
    return JsonResponse({
        'ok':            True,
        'checkout_url':  checkout_url,
        'old_cancelled': bool(mem_id),
    })


@login_required
@csrf_exempt
@require_http_methods(['POST'])
def ls_cancel_downgrade(request):
    """Undo the scheduled downgrade — user stays on their current plan."""
    is_ajax  = request.headers.get('Content-Type', '').startswith('application/json')
    user_id  = request.session.get('user_id')
    user     = get_user_by_id(user_id) or {}
    pd       = user.get('pending_downgrade')
    if not pd:
        if is_ajax:
            return JsonResponse({'ok': False, 'error': 'No pending downgrade found.'})
        return redirect('/settings/#billing')
    update_user(user_id, pending_downgrade=None)
    current_plan = (user.get('plan') or 'starter').title()
    if is_ajax:
        return JsonResponse({'ok': True, 'msg': f"Schedule removed — you're staying on {current_plan}."})
    return redirect('/dashboard/')


@login_required
@csrf_exempt
@require_http_methods(['POST'])
def ls_apply_downgrade(request):
    """Apply the scheduled downgrade immediately — switches plan right now."""
    is_ajax  = request.headers.get('Content-Type', '').startswith('application/json')
    user_id  = request.session.get('user_id')
    user     = get_user_by_id(user_id) or {}
    pd       = user.get('pending_downgrade')
    if not pd:
        if is_ajax:
            return JsonResponse({'ok': False, 'error': 'No pending downgrade found.'})
        return redirect('/settings/#billing')
    new_plan = pd.get('plan', '').lower()
    period   = pd.get('period', 'monthly').lower()
    if new_plan not in ('starter', 'pro', 'agency'):
        if is_ajax:
            return JsonResponse({'ok': False, 'error': 'Invalid pending plan.'})
        return redirect('/settings/#billing')

    # For Whop: just apply the local plan change — Whop billing handled on next renewal
    update_user(user_id, plan=new_plan, pending_downgrade=None)
    request.session['user_plan'] = new_plan.title()

    refreshed  = get_user_by_id(user_id) or {}
    city_count = len(refreshed.get('cities', []))
    city_limit = PLAN_CITY_LIMITS.get(new_plan, 1)
    frozen     = city_count > city_limit
    update_user(user_id, cities_frozen=frozen)

    if is_ajax:
        resp = {'ok': True, 'plan': new_plan,
                'msg': f'Switched to {new_plan.title()} immediately.'}
        if frozen:
            resp['frozen']     = True
            resp['city_limit'] = city_limit
            resp['city_count'] = city_count
            resp['excess']     = city_count - city_limit
        return JsonResponse(resp)

    # Form POST from dashboard — redirect appropriately
    if frozen:
        return redirect('/settings/#coverage')
    return redirect('/dashboard/')


@csrf_exempt
@require_http_methods(['POST'])
def ls_webhook(request):
    """Whop webhook handler."""
    sig = request.META.get('HTTP_X_WHOP_SIGNATURE', '') or request.META.get('HTTP_X_SIGNATURE', '')
    if not wp.verify_webhook_signature(request.body, sig):
        return HttpResponse('Invalid signature', status=403)
    try:
        event = json.loads(request.body)
    except Exception:
        return HttpResponse('Bad JSON', status=400)

    action   = event.get('action', '')
    data     = event.get('data', {})
    mem_id   = data.get('id', '')
    status   = data.get('status', '')
    valid    = data.get('valid', False)
    metadata = data.get('metadata') or {}
    user_obj = data.get('user') or {}
    email    = user_obj.get('email', '')
    cancel_at_end = data.get('cancel_at_period_end', False)

    # Try to find user_id from metadata first, then fall back to email lookup
    user_id_str = metadata.get('user_id', '')

    from datetime import datetime, timezone

    def _get_uid():
        if user_id_str:
            try:
                return int(user_id_str)
            except Exception:
                pass
        if email:
            u = get_user_by_email(email)
            if u:
                return u.get('id')
        return None

    uid = _get_uid()

    # SECURITY: when uid was resolved from custom metadata user_id, verify
    # the membership's email actually matches that user's stored email.
    # Without this, a user could pay at checkout with a different email
    # while we still bind the resulting membership to their account via
    # metadata — or worse, someone could pay with metadata pointing at
    # *another* user's account. The embedded-checkout page already shows
    # a "Paying as <email>" lock notice, but Whop's iframe is cross-origin
    # so the email field is not strictly disable-able from our side; this
    # webhook check is the actual enforcement layer.
    if uid and user_id_str:
        target_user  = get_user_by_id(uid) or {}
        target_email = (target_user.get('email') or '').strip().lower()
        paid_email   = (email or '').strip().lower()
        if target_email and paid_email and target_email != paid_email:
            import logging
            logging.warning(
                "ls_webhook: refusing to bind membership %s to user %s — "
                "paid email %r does not match account email %r",
                mem_id, uid, paid_email, target_email,
            )
            uid = None

    if uid:
        try:
            # default='' so the keyword/id fallback path returns "" when
            # it cannot identify the plan, instead of silently returning
            # 'starter'. This is the root-cause fix for the "I paid for
            # Agency but got Starter" upgrade bug — when the agency
            # plan_id wasn't pasted into /admin-panel/whop-settings/,
            # this webhook used to overwrite the correct plan that
            # ls_success had just set with 'starter'.
            detected_plan = wp.plan_from_membership(data, default='')

            user_data    = get_user_by_id(uid) or {}
            current_plan = (user_data.get('plan') or '').lower()
            pending_dg   = user_data.get('pending_downgrade') or {}

            if action == 'membership.went_valid':
                patch = {
                    'whop_membership_id':  mem_id,
                    'whop_cancelled':      cancel_at_end,
                    'subscription_active': True,
                    'onboarding_complete': True,
                }
                # Only set plan when detection actually identified one.
                # When it didn't, leave whatever ls_success (or a prior
                # webhook) wrote intact — never silently downgrade.
                if detected_plan:
                    patch['plan'] = detected_plan
                elif current_plan in ('starter', 'pro', 'agency'):
                    import logging
                    logging.warning(
                        "ls_webhook went_valid: plan undetectable for "
                        "membership %s (user %s) — keeping current plan %r. "
                        "Paste this membership's plan_id into "
                        "/admin-panel/whop-settings/ to fix detection.",
                        mem_id, uid, current_plan,
                    )

                # Apply pending downgrade if its date has passed
                if pending_dg:
                    sched_str = pending_dg.get('date', '')
                    try:
                        sched_dt = datetime.strptime(sched_str, '%Y-%m-%d')
                        today    = datetime.now(timezone.utc).replace(tzinfo=None)
                        if today >= sched_dt:
                            dg_plan = pending_dg.get('plan', '')
                            if dg_plan in ('starter', 'pro', 'agency'):
                                dg_limit   = PLAN_CITY_LIMITS.get(dg_plan, 1)
                                city_count = len(user_data.get('cities', []))
                                patch['plan']              = dg_plan
                                patch['cities_frozen']     = city_count > dg_limit
                                patch['pending_downgrade'] = None
                    except Exception:
                        pass

                update_user(uid, **patch)

                # Snapshot full Whop data so settings page renders from DB
                # only — no live wp.get_membership() call needed.
                _snapshot_whop_to_user(uid, data)

                # Branded payment-success / activation receipt. The dedup
                # helper guarantees we send AT MOST ONCE per
                # (user, membership_id, plan) tuple, so:
                #
                #   • redirect won the race → it already stamped, this
                #     no-ops silently. (Most common happy-path.)
                #   • redirect never ran (closed browser, off-platform
                #     upgrade, ls_success couldn't verify the membership)
                #     → this is the ONLY path that delivers the receipt
                #     and the bug fix this branch was added for.
                #   • Whop sends went_valid for every billing-cycle
                #     renewal → same (mem_id, plan), skipped, no spam.
                #
                # Wrapped in try/except because Whop retries non-2xx
                # responses and we never want a template render glitch
                # to loop the webhook.
                try:
                    _email_plan = (patch.get('plan') or current_plan or '').lower()
                    _maybe_fire_payment_success_email(
                        uid, mem_id, _email_plan, data, request,
                    )
                except Exception:
                    import logging
                    logging.exception(
                        "payment-success dispatch failed in webhook for "
                        "user=%s membership=%s", uid, mem_id,
                    )

                # Affiliate commission: credit the referrer (if any) the
                # first time this user transitions to a *paid* plan. This
                # is gated by the per-user idempotency flag inside
                # ``credit_referral_first_payment``, so subsequent
                # ``went_valid`` events (renewals, plan changes) do not
                # double-pay. We only credit on real paid plans.
                _credit_plan = (patch.get('plan') or current_plan or '').lower()
                # Price pulled from system_settings via wp.get_plan_price so
                # affiliate credits track the actual amount the user was
                # billed (admin-configurable, per-user whop_mode). Re-fetch
                # the user after _snapshot_whop_to_user so we see the just-
                # updated plan/mode.
                _credit_user  = get_user_by_id(uid) or {}
                _credit_mode  = wp.mode_for_user(_credit_user)
                _credit_price = int(wp.get_plan_price(_credit_plan, 'monthly', _credit_mode) or 0)
                if _credit_plan in ('pro', 'agency') or (
                    _credit_plan == 'starter' and _credit_price > 0
                ):
                    _price_cents = _credit_price * 100
                    if _price_cents > 0:
                        try:
                            credit_referral_first_payment(
                                uid, _price_cents, membership_id=mem_id,
                            )
                        except Exception:
                            # Never fail the webhook over a referral credit
                            # — Whop will retry the whole event otherwise.
                            import logging
                            logging.exception(
                                "credit_referral_first_payment failed for "
                                "user %s membership %s", uid, mem_id,
                            )

            elif action == 'membership.cancelled':
                update_user(uid, whop_cancelled=True)
                # Trial / sub cancelled — fire a fresh 3-step recovery
                # sequence (win-back). Use trigger='trial_cancelled' so
                # admins can distinguish it from the signup-abandoned
                # sequence in the queue. Swallowed errors are fine.
                try:
                    enqueue_recovery_for_user(
                        uid, 'trial_cancelled',
                        trial_link=request.build_absolute_uri('/paywall/'))
                except Exception:
                    log.exception("recovery enqueue on cancel failed")

            elif action == 'membership.went_invalid':
                # Membership fully expired. Only act on this if it's the
                # user's *current* membership — otherwise we'd wipe out an
                # upgrade by reacting to the OLD (Pro/Starter) membership
                # going invalid after the user already moved to a new tier.
                current_mem = (user_data.get('whop_membership_id') or '')
                if current_mem and current_mem != mem_id:
                    # Stale event for a superseded membership — ignore.
                    pass
                else:
                    final_plan = pending_dg.get('plan', 'starter') if pending_dg else 'starter'
                    dg_limit   = PLAN_CITY_LIMITS.get(final_plan, 1)
                    city_count = len(user_data.get('cities', []))
                    update_user(uid,
                        whop_membership_id=None,
                        plan=final_plan,
                        cities_frozen=city_count > dg_limit,
                        pending_downgrade=None,
                        subscription_active=False,
                    )
        except Exception:
            pass

    return HttpResponse('OK', status=200)


@login_required
@require_http_methods(['POST'])
def revoke_session(request):
    user_id = request.session.get('user_id')
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}
    session_id = data.get('session_id')
    if not session_id:
        return JsonResponse({'ok': False, 'error': 'Missing session_id'}, status=400)
    sessions = get_sessions_for_user(user_id)
    ids = {s['id'] for s in sessions}
    if int(session_id) not in ids:
        return JsonResponse({'ok': False, 'error': 'Session not found'}, status=404)
    delete_session_by_id(int(session_id))
    return JsonResponse({'ok': True})


@login_required
@require_http_methods(['POST'])
def revoke_all_sessions(request):
    user_id     = request.session.get('user_id')
    current_key = request.session.session_key
    count = delete_sessions_for_user(user_id, except_key=current_key)
    return JsonResponse({'ok': True, 'revoked': count})


@require_http_methods(['GET'])
def session_status(request):
    """Tell the front-end how long the current session has left.

    Polled every 60s by the absolute-timeout countdown popup in
    base.html so the visible timer self-corrects against clock drift
    and reflects sign-out-everywhere events from other tabs.

    Always 200 OK — anonymous requests get ``authenticated: false``
    (rather than 401) so a tab that's already signed out doesn't
    chase a redirect on every poll. The popup interprets that as
    "stop polling, the user is gone".

    Cheap — pure session read, no DB. Safe to add to every page.
    """
    from django.conf import settings as _s
    max_age = int(getattr(_s, 'SESSION_ABSOLUTE_TIMEOUT_SECONDS', 3600))
    user_id = request.session.get('user_id')
    if not user_id:
        return JsonResponse({
            'authenticated':     False,
            'expires_at':        0,
            'seconds_remaining': 0,
            'max_age':           max_age,
        })
    import time as _time
    now = int(_time.time())
    login_at = request.session.get('login_at')
    if not isinstance(login_at, int):
        # Legacy session — middleware will stamp it on the next non-
        # exempt request. Report a full-window remaining so the popup
        # doesn't false-alarm in the meantime.
        login_at = now

    # Honor sign-out-everywhere here too. This endpoint is exempt
    # from SessionAbsoluteTimeoutMiddleware (so it can serve anon
    # callers without redirect chases), which means the middleware's
    # revocation check doesn't run for these polls. Without this
    # block, the popup's 60s cross-device-detection promise would
    # quietly fail: device B would keep reporting authenticated:true
    # for up to an hour after the user signed out everywhere from
    # device A — only the next *non-exempt* request from device B
    # (a real page nav or AJAX call) would actually trip the kick.
    try:
        from .db import get_user_by_id as _get_user_by_id
        _u = _get_user_by_id(user_id) or {}
        _ra = _u.get('sessions_revoked_at')
        if isinstance(_ra, (int, float)) and login_at < int(_ra):
            return JsonResponse({
                'authenticated':     False,
                'expires_at':        0,
                'seconds_remaining': 0,
                'max_age':           max_age,
            })
    except Exception:
        # DB hiccup — fall through and report the session as alive
        # rather than false-alarming the popup.
        pass

    expires   = login_at + max_age
    remaining = max(0, expires - now)
    return JsonResponse({
        'authenticated':     True,
        'expires_at':        expires,
        'seconds_remaining': remaining,
        'max_age':           max_age,
    })


@login_required
@require_http_methods(['POST'])
def sign_out_everywhere(request):
    """Revoke every session for the current user, including this one.

    Differs from ``revoke_all_sessions`` (which keeps the current
    device alive). Use case: user clicks "Sign out of all devices"
    from the Active Sessions panel because they think their account
    may have been accessed elsewhere — the strongest action they can
    take is invalidating every session AND being forced to re-auth
    here. Returns JSON so the settings page JS can show a toast
    and then navigate to /login/?signed_out=everywhere.

    Implementation note: sessions are signed cookies (no server-side
    store), so deleting rows from the `sessions` audit table cannot
    by itself invalidate a cookie on another device. The actual
    revocation comes from stamping ``sessions_revoked_at = now`` on
    the user record — SessionAbsoluteTimeoutMiddleware compares each
    session's ``login_at`` against that timestamp on every authed
    request and bounces any session whose login predates it. Other
    devices get kicked on their very next request.
    """
    import time as _time
    user_id = request.session.get('user_id')
    now_ts  = int(_time.time())
    revoked = 0

    # 1. Stamp the revocation timestamp on the user — this is what
    # actually invalidates signed-cookie sessions on remote devices.
    # If this write fails the action has not taken effect, so we
    # report the error rather than misleading the user.
    try:
        ok = update_user(user_id, sessions_revoked_at=now_ts)
        if not ok:
            return JsonResponse({
                'ok':    False,
                'error': 'Could not revoke sessions. Please try again.',
            }, status=500)
    except Exception:
        log.exception("sign_out_everywhere: update_user(sessions_revoked_at) failed for user %s", user_id)
        return JsonResponse({
            'ok':    False,
            'error': 'Could not revoke sessions. Please try again.',
        }, status=500)

    # 2. Best-effort: clear the audit table so the Active Sessions
    # panel reflects reality after the next sign-in. Non-critical.
    try:
        revoked = delete_sessions_for_user(user_id, except_key=None)
    except Exception:
        log.exception("sign_out_everywhere: delete_sessions_for_user failed for user %s", user_id)

    # 3. Flush the current session so this tab is signed out
    # immediately (without waiting for the middleware to do it on
    # the next request).
    try:
        request.session.flush()
    except Exception:
        log.exception("sign_out_everywhere: session.flush failed for user %s", user_id)

    return JsonResponse({'ok': True, 'revoked': revoked})


# ── TOTP helpers ───────────────────────────────────────────────

def _totp_qr_data_url(secret: str, email: str) -> str:
    import pyotp, qrcode, io, base64
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=email, issuer_name='Permitlify')
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


@login_required
@require_http_methods(['GET'])
def totp_setup_qr(request):
    import pyotp
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}
    secret  = user.get('totp_secret') or pyotp.random_base32()
    set_totp_secret(user_id, secret)
    formatted = ' '.join(secret[i:i+4] for i in range(0, len(secret), 4))
    return JsonResponse({
        'ok':        True,
        'secret':    secret,
        'formatted': formatted,
        'qr':        _totp_qr_data_url(secret, user.get('email', '')),
    })


@login_required
@require_http_methods(['POST'])
def totp_verify_enable(request):
    import pyotp
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}
    code   = str(data.get('code', '')).replace(' ', '').strip()
    secret = user.get('totp_secret', '')
    if not secret:
        return JsonResponse({'ok': False, 'error': 'No TOTP secret found. Refresh and try again.'})
    totp = pyotp.TOTP(secret)
    if totp.verify(code, valid_window=1):
        enable_totp(user_id)
        return JsonResponse({'ok': True})
    return JsonResponse({'ok': False, 'error': 'Incorrect code. Try again.'})


@login_required
@require_http_methods(['POST'])
def totp_disable(request):
    import pyotp
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}
    password = data.get('password', '')
    code     = str(data.get('code', '')).replace(' ', '').strip()
    if not authenticate_user(user.get('email', ''), password):
        return JsonResponse({'ok': False, 'error': 'Incorrect password.'})
    secret = user.get('totp_secret', '')
    if secret and code:
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            return JsonResponse({'ok': False, 'error': 'Incorrect authenticator code.'})
    disable_totp(user_id)
    return JsonResponse({'ok': True})


@require_http_methods(['GET', 'POST'])
def login_2fa(request):
    pending_id = request.session.get('totp_pending_user_id')
    if not pending_id:
        return redirect('login')
    error = None
    if request.method == 'POST':
        import pyotp
        code = request.POST.get('code', '').replace(' ', '').strip()
        user = get_user_by_id(pending_id) or {}
        secret = user.get('totp_secret', '')
        if secret and pyotp.TOTP(secret).verify(code, valid_window=1):
            ua  = request.session.get('totp_pending_ua', '')
            ip  = request.session.get('totp_pending_ip', '')
            dev = request.session.get('totp_pending_dev', '')
            for k in ['totp_pending_user_id','totp_pending_name','totp_pending_email',
                      'totp_pending_initials','totp_pending_plan','totp_pending_ua',
                      'totp_pending_ip','totp_pending_dev']:
                request.session.pop(k, None)
            # Refresh Whop billing state on 2FA-completed login too — same
            # email-first, 3-second-capped helper used by the email +
            # Google paths. Keeps plan badges everywhere in lock-step
            # with reality. Wrapped: a Whop / detection bug must never
            # break the 2FA login finalization — fall back to the
            # existing user.plan so the session still mints.
            try:
                _whop = _whop_login_sync(user['id'], user)
            except Exception:
                log.exception("_whop_login_sync failed for user %s (2fa login)", user.get('id'))
                _whop = {'plan': (user.get('plan') or 'starter')}
            request.session['user_id']       = user['id']
            request.session['user_name']     = user['name']
            request.session['user_email']    = user['email']
            request.session['user_initials'] = user.get('avatar_initials', user['name'][:2].upper())
            request.session['user_plan']     = (_whop['plan'] or 'starter').title()
            _stamp_session_login(request)
            request.session.save()
            create_session(user_id=user['id'], session_key=request.session.session_key,
                           device=dev, ip=ip, ua=ua)
            _send_login_alert_if_new_device(
                user=user, request=request,
                method_label='Email + password (with 2FA)',
                device=dev, ip=ip, ua=ua,
            )
            record_login_event(user_id=user['id'], status='success',
                               device=dev, ip=ip, ua=ua)
            return redirect('dashboard')
        error = 'Incorrect code. Please try again.'
    return render(request, 'core/login_2fa.html', {'error': error})


@require_http_methods(['GET', 'POST'])
def login_verify_code(request):
    """Step 2 of email + password sign-in: enter the 6-digit code we
    just emailed.

    Hard rules:
      * Code expires after ``CODE_TTL_MINUTES`` (default 10 min).
      * ``CODE_MAX_ATTEMPTS`` wrong tries (default 5) burns the code —
        the user has to go back to /login/ and start over.
      * On success: drop all the ``email_code_pending_*`` session keys,
        mint the real session, refresh Whop, redirect to dashboard.
      * No pending state in session → bounce back to /login/.
    """
    from .auth_codes import verify_code, is_expired, CODE_TTL_MINUTES

    pending_id = request.session.get('email_code_pending_user_id')
    if not pending_id:
        return redirect('login')

    pending_email = request.session.get('email_code_pending_email', '') or ''
    expires_iso   = request.session.get('email_code_pending_expires_at', '')

    error  = None
    notice = None

    if request.method == 'POST':
        submitted = (request.POST.get('code') or '').strip().replace(' ', '')
        stored_hash = request.session.get('email_code_pending_code_hash', '')
        attempts    = int(request.session.get('email_code_pending_attempts', 0) or 0)

        if is_expired(expires_iso):
            error = 'This code has expired. Click "resend the code" below to get a fresh one.'
        elif attempts <= 0:
            error = 'Too many wrong attempts. Please sign in again to get a new code.'
            for k in list(request.session.keys()):
                if k.startswith('email_code_pending_'):
                    request.session.pop(k, None)
            return redirect('login')
        elif not submitted or len(submitted) != 6 or not submitted.isdigit():
            error = 'Please enter the 6-digit code from your email.'
            request.session['email_code_pending_attempts'] = attempts - 1
        elif not verify_code(submitted, stored_hash):
            remaining = attempts - 1
            request.session['email_code_pending_attempts'] = remaining
            if remaining <= 0:
                for k in list(request.session.keys()):
                    if k.startswith('email_code_pending_'):
                        request.session.pop(k, None)
                return redirect('login')
            error = f'Incorrect code. {remaining} attempt{"s" if remaining != 1 else ""} remaining.'
        else:
            # ── success ─────────────────────────────────────────────
            user = get_user_by_id(pending_id)
            if not user:
                # User was deleted/banned between step 1 and step 2 —
                # bail out cleanly instead of crashing on user['email'].
                for k in list(request.session.keys()):
                    if k.startswith('email_code_pending_'):
                        request.session.pop(k, None)
                return redirect('login')

            ua  = request.session.get('email_code_pending_ua', '')
            ip  = request.session.get('email_code_pending_ip', '')
            dev = request.session.get('email_code_pending_dev', '')

            for k in list(request.session.keys()):
                if k.startswith('email_code_pending_'):
                    request.session.pop(k, None)

            # ── session fixation defence ───────────────────────────
            # Rotate the session key the moment we elevate from
            # "anonymous, mid-flow" to "authenticated". Any cookie an
            # attacker may have managed to plant pre-auth becomes
            # worthless after this call. (Must run BEFORE we set the
            # user_id keys so the new keys land in the new session.)
            request.session.cycle_key()

            # Same Whop refresh dance as the password and TOTP paths so
            # the plan badge matches reality. Failure must never block
            # login finalisation.
            try:
                _whop = _whop_login_sync(user['id'], user)
            except Exception:
                log.exception("_whop_login_sync failed for user %s (email-code login)", user.get('id'))
                _whop = {'plan': (user.get('plan') or 'starter')}

            request.session['user_id']       = user['id']
            request.session['user_name']     = user['name']
            request.session['user_email']    = user['email']
            request.session['user_initials'] = user.get('avatar_initials', user['name'][:2].upper())
            request.session['user_plan']     = (_whop['plan'] or 'starter').title()
            _stamp_session_login(request)
            request.session.save()
            create_session(user_id=user['id'], session_key=request.session.session_key,
                           device=dev, ip=ip, ua=ua)
            _send_login_alert_if_new_device(
                user=user, request=request,
                method_label='Email + password (with email code)',
                device=dev, ip=ip, ua=ua,
            )
            record_login_event(user_id=user['id'], status='success',
                               device=dev, ip=ip, ua=ua)
            return redirect('dashboard')

        # Refresh expiry view-model after a failed attempt
        expires_iso = request.session.get('email_code_pending_expires_at', '')

    # Build view model: expiry timestamp in ms (for the JS countdown)
    # and a masked email so the page can show "j••••@gmail.com" without
    # echoing the full address back from the URL.
    expires_at_ms = 0
    if expires_iso:
        try:
            from datetime import datetime as _dt, timezone as _tz
            dt = _dt.fromisoformat(expires_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            expires_at_ms = int(dt.timestamp() * 1000)
        except ValueError:
            expires_at_ms = 0

    masked = ''
    if pending_email and '@' in pending_email:
        local, _, domain = pending_email.partition('@')
        if len(local) <= 2:
            masked_local = (local[:1] or '') + '•' * max(1, 6 - len(local))
        else:
            masked_local = local[0] + '•' * min(6, max(1, len(local) - 2)) + local[-1]
        masked = f'{masked_local}@{domain}'

    return render(request, 'core/login_verify_code.html', {
        'error':                 error,
        'notice':                notice,
        'pending_email':         pending_email,
        'pending_email_masked':  masked,
        'expires_at_ms':         expires_at_ms,
        'ttl_minutes':           CODE_TTL_MINUTES,
    })


@require_http_methods(['POST'])
def login_verify_code_resend(request):
    """AJAX endpoint to send a fresh email code.

    Refuses if there's no pending login state (the user must go back
    to /login/ and re-enter their password). Resets the attempts
    counter and the 10-minute clock on success.
    """
    from .auth_codes import (
        generate_code, hash_code, expiry_iso, send_login_code_email,
        CODE_MAX_ATTEMPTS,
    )

    pending_id    = request.session.get('email_code_pending_user_id')
    pending_email = request.session.get('email_code_pending_email', '')
    pending_name  = request.session.get('email_code_pending_name', '')

    if not pending_id or not pending_email:
        return JsonResponse({'ok': False, 'error': 'No pending sign-in. Please sign in again.'}, status=400)

    # ── 60-second resend cooldown ───────────────────────────────────
    # Prevents email-bombing the recipient and burning Resend quota
    # if the resend link is hammered (intentionally or by a buggy
    # client). Stored in the session so it's per-flow, per-browser.
    from datetime import datetime as _dt, timezone as _tz
    last_iso = request.session.get('email_code_pending_last_send_at', '')
    if last_iso:
        try:
            last_dt = _dt.fromisoformat(last_iso)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=_tz.utc)
            elapsed = (_dt.now(_tz.utc) - last_dt).total_seconds()
            if elapsed < 60:
                wait = int(60 - elapsed) + 1
                return JsonResponse(
                    {'ok': False, 'error': f'Please wait {wait}s before requesting another code.'},
                    status=429,
                )
        except ValueError:
            pass

    code = generate_code()
    ok, msg = send_login_code_email(
        to_email   = pending_email,
        to_name    = pending_name,
        code       = code,
        request_ip = request.session.get('email_code_pending_ip', ''),
        request_ua = request.session.get('email_code_pending_ua', ''),
    )
    if not ok:
        log.warning("email-code resend: transport down for %s (%s)", pending_email, msg)
        return JsonResponse({'ok': False, 'error': 'Could not send a new code right now. Try again in a moment.'}, status=503)

    request.session['email_code_pending_code_hash']    = hash_code(code)
    request.session['email_code_pending_expires_at']   = expiry_iso()
    request.session['email_code_pending_attempts']     = CODE_MAX_ATTEMPTS
    request.session['email_code_pending_last_send_at'] = _dt.now(_tz.utc).isoformat()
    return JsonResponse({'ok': True})


@login_required
@require_http_methods(['POST'])
def security_change_password(request):
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}
    new_pw     = request.POST.get('new_password', '')
    confirm_pw = request.POST.get('confirm_password', '')
    # See ``profile`` view: Google-only users (auth_provider == 'google')
    # have a random server-generated password they don't know, so the
    # first time they set one we skip the current-password gate and
    # flip auth_provider to 'local' afterwards.
    is_google_only = (user.get('auth_provider') == 'google')
    if not is_google_only:
        current_pw = request.POST.get('current_password', '')
        if not authenticate_user(user.get('email', ''), current_pw):
            return JsonResponse({'ok': False, 'error': 'Current password is incorrect.'})
    if len(new_pw) < 8:
        return JsonResponse({'ok': False, 'error': 'Password must be at least 8 characters.'})
    if new_pw != confirm_pw:
        return JsonResponse({'ok': False, 'error': 'Passwords do not match.'})
    if is_google_only:
        update_user(user_id, password=hash_password(new_pw), auth_provider='local')
    else:
        update_user(user_id, password=hash_password(new_pw))
    return JsonResponse({'ok': True})


@login_required
@require_http_methods(['POST'])
def security_account_action(request):
    action = request.POST.get('action', '')
    if action == 'pause':
        return JsonResponse({'ok': True, 'msg': 'Account paused. All alerts have stopped — resume anytime.'})
    if action == 'delete_data':
        return JsonResponse({'ok': True, 'msg': 'All coverage data and permit history deleted.'})
    if action == 'cancel':
        return JsonResponse({'ok': True, 'msg': 'Subscription cancelled. Access continues until end of billing period.'})
    return JsonResponse({'ok': False, 'error': 'Unknown action.'})


@login_required
@require_http_methods(['POST'])
def add_city_view(request):
    user_id = request.session.get('user_id')
    if not user_id:
        return JsonResponse({'ok': False, 'error': 'Not authenticated'}, status=401)
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}
    # State-based coverage (May-2026 pricing migration). The POST body
    # carries either ``state`` (preferred) or the legacy ``city`` field
    # — the latter is treated as a state code for backwards-compat with
    # any in-flight client JS.
    code = (data.get('state') or data.get('city') or '').strip().upper()
    if len(code) != 2:
        return JsonResponse({'ok': False, 'error': 'A valid 2-letter state code is required.'}, status=400)
    _visible = {s['state'] for s in get_customer_visible_states()}
    if code not in _visible:
        full = _FULL_STATE_NAMES.get(code, code)
        return JsonResponse({'ok': False, 'error': f"We don't have coverage for {full} yet."}, status=400)
    user = get_user_by_id(user_id) or {}
    if not user.get('subscription_active') and (user.get('email') or '').lower().strip() not in ADMIN_EMAILS:
        return JsonResponse({'ok': False, 'no_plan': True, 'upgrade_required': True,
                             'error': 'Choose a plan to start tracking states.'}, status=403)
    if user.get('cities_frozen', False):
        return JsonResponse({'ok': False, 'frozen': True,
                             'error': 'Remove excess states before adding new ones.'}, status=403)
    plan        = (user.get('plan') or 'starter').capitalize()
    state_limit = PLAN_CITY_LIMITS.get(plan, 1)
    current     = [(c or '').strip().upper() for c in (user.get('cities') or []) if c]
    if len(current) >= state_limit:
        return JsonResponse({'ok': False, 'upgrade_required': True,
                             'error': f'Upgrade your plan to add more than {state_limit} state{"s" if state_limit != 1 else ""}.'}, status=403)
    if code in current:
        return JsonResponse({'ok': False, 'duplicate': True,
                             'error': f"{_FULL_STATE_NAMES.get(code, code)} is already in your coverage."})
    current.append(code)
    update_user(user_id, cities=current)
    return JsonResponse({'ok': True, 'cities': current, 'state': code,
                         'name': _FULL_STATE_NAMES.get(code, code)})


@login_required
@require_http_methods(['POST'])
def remove_city_view(request):
    user_id = request.session.get('user_id')
    if not user_id:
        return JsonResponse({'ok': False, 'error': 'Not authenticated'}, status=401)
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}
    # State-based coverage. Accept ``state`` (preferred) or legacy
    # ``city`` field; both are normalised to uppercase 2-letter code.
    code = (data.get('state') or data.get('city') or '').strip().upper()
    user = get_user_by_id(user_id) or {}
    states = [(c or '').strip().upper() for c in (user.get('cities') or [])
              if c and (c or '').strip().upper() != code]
    update_user(user_id, cities=states)
    plan        = user.get('plan', 'starter')
    state_limit = PLAN_CITY_LIMITS.get(plan, 1)
    was_frozen  = user.get('cities_frozen', False)
    now_frozen  = len(states) > state_limit
    if was_frozen and not now_frozen:
        update_user(user_id, cities_frozen=False)
    return JsonResponse({
        'ok':         True,
        'cities':     states,
        'frozen':     now_frozen,
        'unfrozen':   was_frozen and not now_frozen,
        'city_limit': state_limit,
    })


@login_required
@require_http_methods(['POST'])
def save_billing_address(request):
    user_id = request.session.get('user_id')
    if not user_id:
        return JsonResponse({'ok': False, 'error': 'Not authenticated'}, status=401)
    update_user(user_id,
        bill_company   = request.POST.get('bill_company',   '').strip(),
        bill_tax_id    = request.POST.get('bill_tax_id',    '').strip(),
        bill_street    = request.POST.get('bill_street',    '').strip(),
        bill_city      = request.POST.get('bill_city',      '').strip(),
        bill_state_zip = request.POST.get('bill_state_zip', '').strip(),
    )
    return JsonResponse({'ok': True})


@login_required
def export_invoices_csv(request):
    user_id  = request.session.get('user_id')
    invoices = get_user_invoices(user_id)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="permitlify-invoices.csv"'

    writer = csv.writer(response)
    writer.writerow(['Invoice ID', 'Plan', 'Billing Period', 'Amount', 'Status', 'Payment'])
    for inv in invoices:
        writer.writerow([
            inv.get('number', inv.get('invoice_id', '')),
            inv.get('plan_label', inv.get('plan', '').title()),
            inv.get('period', inv.get('date', '')),
            inv.get('amount', ''),
            inv.get('status', 'paid').title(),
            inv.get('payment', 'Whop'),
        ])
    if not invoices:
        writer.writerow(['—', '—', '—', '—', '—', '—'])
    return response


def _build_invoice_ctx(request, inv_id, *, ascii_features=False):
    """Resolve + normalise the invoice context dict shared by both the
    web preview (``invoice_pdf``) and the PDF download (``invoice_pdf_download``).

    Whop webhooks occasionally write ``None`` for fields that we'd otherwise
    call ``.lower()`` / ``.title()`` / ``//`` on. Coercing once here means
    a single bad row can no longer 500 either endpoint. Returns ``None`` if
    the invoice is missing or doesn't belong to the session user (caller
    should raise ``Http404``).

    ``ascii_features`` flips the bullet character used in plan-feature
    strings — Helvetica (used by the PDF) lacks the U+00B7 middle dot
    glyph and would render it as a tofu square, so the PDF path passes
    True to fall back to ASCII hyphens.
    """
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}

    inv = get_invoice_by_id(inv_id)
    if not inv:
        return None
    try:
        if int(inv.get('user_id', -1)) != int(user_id):
            return None
    except (TypeError, ValueError):
        return None

    sep = ' - ' if ascii_features else ' · '
    plan_features = {
        'starter': sep.join(['AI-scored permit feed', '1 state', '30 alerts/mo', '7-day history', 'Email delivery', 'Trade filtering', 'CSV export']),
        'pro':     sep.join(['AI-scored permit feed', 'up to 5 cities', '300 alerts/mo', '90-day history', 'Trade filtering', 'CSV export', 'Priority support']),
        'agency':  sep.join(['AI-scored permit feed', '15 cities', 'Unlimited alerts', 'Full archive', 'REST API access', 'Slack/SMS channels', 'White-label', 'Dedicated onboarding']),
    }

    plan_raw = (inv.get('plan') or '').strip()
    try:
        amt_cents = int(inv.get('amount_cents') or 0)
    except (TypeError, ValueError):
        amt_cents = 0

    return {
        'inv_id':          str(inv.get('invoice_id') or inv_id),
        'inv_num_short':   str(inv.get('number') or inv_id),
        'inv_date':        inv.get('date') or '',
        'inv_due':         inv.get('date') or '',
        'inv_period':      inv.get('period') or '',
        'inv_status':      (inv.get('status') or 'paid'),
        'inv_amount':      amt_cents // 100,
        'user_plan':       plan_raw.title(),
        'plan_features':   plan_features.get(plan_raw.lower(), ''),
        'user_name':       request.session.get('user_name') or '',
        'user_email':      request.session.get('user_email') or '',
        'company':         user.get('company') or user.get('bill_company') or '',
        'bill_street':     user.get('bill_street') or '',
        'bill_city':       user.get('bill_city') or '',
        'bill_state_zip':  user.get('bill_state_zip') or '',
        'bill_ein':        user.get('bill_ein') or user.get('bill_tax_id') or '',
    }


@login_required
def invoice_pdf(request, inv_id):
    ctx = _build_invoice_ctx(request, inv_id)
    if ctx is None:
        raise Http404
    return render(request, 'core/invoice_print.html', ctx)


@login_required
def invoice_pdf_download(request, inv_id):
    """Server-rendered PDF download.

    The on-screen ``invoice_pdf`` view renders a beautiful HTML preview, but
    asking the browser to print → save was unreliable (per-browser margins,
    print headers, sometimes blurry). This endpoint generates a real PDF
    server-side via reportlab and returns it as a file attachment so users
    get a consistent, vector, selectable-text PDF every time.
    """
    ctx = _build_invoice_ctx(request, inv_id, ascii_features=True)
    if ctx is None:
        raise Http404

    from .invoice_pdf import build_invoice_pdf
    pdf_bytes = build_invoice_pdf(ctx)

    filename = f"Permitlify-Invoice-{ctx['inv_id']}.pdf"
    resp = HttpResponse(pdf_bytes, content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    resp['Content-Length'] = str(len(pdf_bytes))
    resp['Cache-Control']  = 'private, no-store'
    return resp


@login_required
@subscription_required
@require_http_methods(['GET', 'POST'])
def profile(request):
    user_id = request.session.get('user_id')
    user = get_user_by_id(user_id) or {}

    success_profile  = False
    success_password = False
    error_password   = None

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'save_profile':
            first    = request.POST.get('first_name', '').strip()
            last     = request.POST.get('last_name', '').strip()
            full_name = (first + ' ' + last).strip() or first or user.get('name', '')
            phone    = request.POST.get('phone', '').strip()
            company  = request.POST.get('company', '').strip()
            trade    = request.POST.get('trade', '').strip()
            bio      = request.POST.get('bio', '').strip()
            initials = ''.join(w[0].upper() for w in full_name.split()[:2]) if full_name else user.get('avatar_initials', 'U')

            update_user(user_id,
                name=full_name,
                phone=phone,
                company=company,
                trade=trade,
                bio=bio,
                avatar_initials=initials,
            )
            request.session['user_name']     = full_name
            request.session['user_initials'] = initials
            user = get_user_by_id(user_id) or {}
            success_profile = True

        elif action == 'change_password':
            new_pw     = request.POST.get('new_password', '')
            confirm_pw = request.POST.get('confirm_password', '')
            # Google-only users have a random server-generated password
            # they don't know — they signed in with the Google button —
            # so we can't (and shouldn't) ask them for a "current
            # password". The "first time setting a password" flow skips
            # that gate. After it succeeds we flip auth_provider to
            # 'local' so any *subsequent* change goes through the
            # normal current-password check.
            is_google_only = (user.get('auth_provider') == 'google')

            if is_google_only:
                if len(new_pw) < 8:
                    error_password = 'Password must be at least 8 characters.'
                elif new_pw != confirm_pw:
                    error_password = 'Passwords do not match.'
                else:
                    update_user(user_id, password=hash_password(new_pw),
                                auth_provider='local')
                    user = get_user_by_id(user_id) or user
                    success_password = True
            else:
                current_pw = request.POST.get('current_password', '')
                if not authenticate_user(user.get('email', ''), current_pw):
                    error_password = 'Current password is incorrect.'
                elif len(new_pw) < 8:
                    error_password = 'New password must be at least 8 characters.'
                elif new_pw != confirm_pw:
                    error_password = 'Passwords do not match.'
                else:
                    update_user(user_id, password=hash_password(new_pw))
                    success_password = True

    name   = user.get('name', '')
    parts  = name.split(' ', 1)
    first_name = parts[0] if parts else ''
    last_name  = parts[1] if len(parts) > 1 else ''

    TRADES = ['Roofing', 'HVAC', 'Plumbing', 'Electrical', 'General Contractor', 'Solar', 'Windows & Doors', 'Other']

    ctx = _user_ctx(request)
    ctx.update({
        'first_name':       first_name,
        'last_name':        last_name,
        'phone':            user.get('phone', ''),
        'company':          user.get('company', ''),
        'trade':            user.get('trade', 'Roofing'),
        'bio':              user.get('bio', ''),
        'trades':           TRADES,
        'success_profile':  success_profile,
        'success_password': success_password,
        'error_password':   error_password,
        # True for users who signed up via Google and have never set
        # their own password. Template uses this to swap the
        # "Change Password" form for a "Set Password" form (no
        # current-password field) and shows a friendly heads-up
        # banner explaining why.
        'is_google_only_user': (user.get('auth_provider') == 'google'),
    })
    return render(request, 'core/profile.html', ctx)

def _user_ctx(request):
    # Pull the user row once so the sticky red trial countdown bar in
    # base.html has a real ``trial_state`` to render on every page.
    # One small JSONB-by-id lookup; cheap enough to pay on every
    # logged-in page render and avoids the bar going stale between
    # plan changes / paid upgrades.
    uid = request.session.get('user_id')
    user = get_user_by_id(uid) if uid else None
    user = user or {}
    # Auto-stamp ``local_trial_started_at`` for any logged-in account
    # that doesn't have it yet (legacy users created before this PR,
    # or any path that minted a session without going through signup).
    # The 3-day clock starts at first authenticated page-load rather
    # than being silently retroactive, which would lock legacy users
    # out instantly.
    if uid and not user.get('subscription_active') and not user.get('local_trial_started_at'):
        try:
            import time as _t
            stamp = int(_t.time())
            update_user(uid, local_trial_started_at=stamp)
            user['local_trial_started_at'] = stamp
        except Exception:
            log.exception('local-trial backfill on first access failed for user %s', uid)
    trial_state = _user_trial_state(user) if uid else None
    return {
        'user_name':     request.session.get('user_name', ''),
        'user_email':    request.session.get('user_email', ''),
        'user_initials': request.session.get('user_initials', 'MK'),
        'user_plan':     request.session.get('user_plan', 'Starter'),
        'is_admin':      (request.session.get('user_email') or '').lower().strip() in ADMIN_EMAILS,
        # Soft-lock flag set by ``subscription_required`` decorator. True
        # when the logged-in user has no active subscription. Templates
        # use it to render a "Preview Mode" banner + Upgrade CTA in
        # place of the old hard /paywall/ redirect.
        'is_locked':     getattr(request, 'is_locked', False),
        # Card-free 3-day local trial state — drives the sticky red
        # countdown bar in base.html and the per-page upgrade CTAs.
        # None for logged-out pages so the template renders nothing.
        'trial_state':   trial_state,
    }

# ── Admin shared helpers ───────────────────────────────────────

def _admin_plan_counts(all_users):
    """In-memory plan tally — must match the bucketing logic in
    ``count_users_by_plan()`` (core/db.py) so the admin Users tab
    KPI cards stay consistent with the cached SQL aggregate. Users
    without an active Whop subscription are bucketed under
    ``no_plan`` regardless of their (default-on-signup) plan field;
    admins are exempted because their access doesn't go through Whop.
    """
    counts = {'starter': 0, 'pro': 0, 'agency': 0, 'no_plan': 0}
    for u in all_users:
        is_admin = (u.get('email') or '').lower().strip() in ADMIN_EMAILS
        if not is_admin and not u.get('subscription_active'):
            counts['no_plan'] += 1
            continue
        p = u.get('plan', 'starter').lower()
        counts[p] = counts.get(p, 0) + 1
    return counts

def _admin_scraper_sidebar(scrapers=None):
    """Return ``(danger_count, broken_count)`` for the sidebar warning
    badge.

    Backed by the real ``scrapers`` table now: a scraper is "broken"
    when its most recent run failed, "danger" when it has been failing
    consecutively (we use ``last_run_status`` as a cheap proxy and let
    the stats page surface the per-day truth).
    """
    try:
        from .db import list_scrapers as _ls
        rows, _, _, _ = _ls('', 1, 200)
    except Exception:
        return 0, 0
    danger = sum(1 for s in rows if (s.get('last_run_status') or '') == 'failed' and s.get('enabled'))
    warning = sum(1 for s in rows if (s.get('last_run_status') or '') == 'partial' and s.get('enabled'))
    return danger, danger + warning

def _admin_base_ctx(request, active_section, scrapers=None):
    danger_count, broken_count = _admin_scraper_sidebar(scrapers)
    return {
        'active_section':  active_section,
        'admin_name':      request.session.get('user_name', 'Admin'),
        'admin_initials':  request.session.get('user_initials', 'MK'),
        'today':           date.today().strftime('%B %d, %Y'),
        'danger_count':    danger_count,
        'broken_count':    broken_count,
        'banned_count':    len(get_all_banned()),
    }

# ── Admin views ────────────────────────────────────────────────

@admin_required
@cached_admin_html(15)
def admin_dashboard(request):
    # Pull every aggregate from the SQL-side helpers in `core/db.py`.
    # Each one is a tiny GROUP BY / COUNT query with its own 30 s
    # in-process TTL, so we (1) skip dragging the entire users table
    # (id + JSONB) into Python only to count it, and (2) reuse those
    # aggregates across all admin pages that need the same numbers.
    today          = date.today()
    # Pass ADMIN_EMAILS so the SQL helper exempts allow-listed admin
    # accounts from the synthetic 'no_plan' bucket the same way the
    # in-memory _admin_plan_counts() does — otherwise an admin without
    # an active Whop subscription would be counted as no_plan on the
    # global dashboard but not on the Users tab, and the two pages
    # would silently disagree.
    plan_counts    = count_users_by_plan(tuple(ADMIN_EMAILS))
    total_users    = sum(plan_counts.values())
    new_this_month = count_users_joined_in_month(today.strftime('%Y-%m'))
    top_cities     = aggregate_user_cities()[:5]

    # Pull real Whop figures so MRR / ARR / Active subs / chart match the
    # /admin-panel/revenue/ page (and the Whop merchant dashboard) instead
    # of the old PLAN_PRICE × plan_counts approximation, which silently
    # overstates MRR by ~177× when most of the "active" plans are actually
    # trial signups that never cycled. See core.whop._compute_mrr_at for
    # the full justification.
    #
    # Strategy:
    #   * Use only the in-process 365-day revenue snapshot here — we slice it
    #     for both the month-over-month delta badge and the 6-month chart.
    #   * Kick a debounced background refresh when the snapshot is stale or
    #     missing so a slow Whop API never blocks the admin overview.
    #   * Count currently valid+active/trialing memberships from the same
    #     cached /memberships fetch the revenue helper already pulled, so
    #     Active Subscribers reflects what Whop reports when available.
    #   * Fall back to the local PLAN_PRICE math when Whop is unreachable
    #     so the dashboard still renders something sensible offline.
    whop_stats   = None
    whop_mrr     = None
    whop_arr     = None
    whop_active  = None
    whop_mrr_pct = None
    monthly_revenue = []
    try:
        from .whop import get_cached_revenue_stats, refresh_revenue_stats_async
        whop_stats = get_cached_revenue_stats(range_key='365d')
        refresh_revenue_stats_async(range_key='365d', timeout=5)
    except Exception:
        log.exception("admin_dashboard: Whop revenue cache lookup failed")

    # Only trust Whop's MRR / chart when BOTH endpoints succeeded — a
    # partial fetch (memberships ok, payments down) would silently produce
    # MRR=$0 because no payments are visible to attach to memberships,
    # which is much more misleading than falling back to the local
    # PLAN_PRICE estimate. We still pull `active_recurring` from the
    # memberships-only payload (it doesn't depend on payments) so the
    # Active Subscribers card stays accurate even during a payments
    # outage.
    if whop_stats:
        rng = whop_stats.get('range') or {}
        totals = whop_stats.get('totals') or {}
        whop_active = totals.get('active_recurring')
        if whop_stats.get('payments_ok') and whop_stats.get('memberships_ok'):
            whop_mrr     = (rng.get('mrr') or {}).get('value')
            whop_arr     = (rng.get('arr') or {}).get('value')
            whop_mrr_pct = (rng.get('mrr') or {}).get('delta_pct')
            # Bucket the 365-day daily gross series into the trailing 6
            # calendar months so the chart shows real numbers. ``labels``
            # and ``series`` are aligned arrays from get_revenue_stats.
            series = (rng.get('gross') or {}).get('series') or []
            if series:
                from collections import OrderedDict
                buckets: 'OrderedDict[str, float]' = OrderedDict()
                # ``labels`` are 'Mon DD' (no year). Walk forward day-by-day
                # from the start of the window so the bucket key 'Mon YYYY'
                # handles the Dec → Jan rollover correctly.
                cur_dt = today - timedelta(days=len(series) - 1)
                for v in series:
                    key = cur_dt.strftime('%b %Y')
                    buckets[key] = buckets.get(key, 0.0) + float(v or 0)
                    cur_dt += timedelta(days=1)
                # Last 6 months to mirror the previous chart length.
                for k, v in list(buckets.items())[-6:]:
                    monthly_revenue.append({'month': k, 'revenue': round(v, 2)})

    if whop_mrr is not None:
        mrr         = whop_mrr
        active_subs = whop_active if whop_active is not None else (
            plan_counts.get('pro', 0) + plan_counts.get('agency', 0)
        )
        data_source = 'whop'
    else:
        # Whop unreachable (or only partially) — fall back to the local
        # PLAN_PRICE estimate. Marked with a ⚠ in the template so admins
        # know it's not authoritative.
        # Sum per-user via _user_monthly_price so MRR uses each user's
        # DB-driven, mode-aware price (admin Users page agrees with the
        # admin Dashboard fallback). One extra ``get_all_users()`` only
        # on this cold fallback path (Whop unreachable) — fine.
        _fallback_users = get_all_users()
        mrr         = sum(_user_monthly_price(u) for u in _fallback_users)
        active_subs = whop_active if whop_active is not None else (
            plan_counts.get('pro', 0) + plan_counts.get('agency', 0)
        )
        data_source = 'local'

    avg_rev = round(mrr / active_subs, 2) if active_subs else 0

    if not monthly_revenue:
        # No Whop series available — show a single bar for "this month"
        # using whatever MRR we computed above. Avoids the old hard-coded
        # historical numbers, which had no basis in real data.
        monthly_revenue = [{'month': today.strftime('%b %Y'), 'revenue': mrr}]

    scrapers          = _enrich_scrapers(SAMPLE_SCRAPERS)
    total_leads_today = sum(s['leads_today'] for s in scrapers)
    ctx = {
        **_admin_base_ctx(request, 'overview', scrapers),
        'total_users':       total_users,
        'active_subs':       active_subs,
        'mrr':               mrr,
        'arr':               whop_arr if whop_arr is not None else round(mrr * 12, 2),
        'mrr_delta_pct':     whop_mrr_pct,   # None when no prior period to compare
        'data_source':       data_source,
        'new_this_month':    new_this_month,
        'avg_rev':           avg_rev,
        'plan_counts':       plan_counts,
        'monthly_revenue':   monthly_revenue,
        'top_cities':        top_cities,
        'total_leads_today': total_leads_today,
        'pricing':           wp.get_pricing_dict(),
        'quick_links': [
            {'icon': '🤖', 'label': 'Scrapers',   'url': '/admin-panel/scrapers/'},
            {'icon': '👥', 'label': 'All Users',  'url': '/admin-panel/users/'},
            {'icon': '💰', 'label': 'Revenue',    'url': '/admin-panel/revenue/'},
            {'icon': '🚫', 'label': 'Banned',     'url': '/admin-panel/banned/'},
        ],
    }
    return render(request, 'core/admin_overview.html', ctx)


# ─────────────────────────────────────────────────────────────────
# Vendor library passthrough (jQuery + DataTables).
#
# Whitenoise on production (behind Cloudflare + DigitalOcean App
# Platform) emits Content-Type: text/javascript; charset="utf-8" with
# literal quotes around utf-8. Combined with the X-Content-Type-Options:
# nosniff header we set globally, MS Edge (and some Safari builds)
# refuses to execute the script — the file loads with HTTP 200 but the
# browser silently rejects it, leaving jQuery undefined and our
# admin DataTables permanently empty (issue surfaced May 2026 on the
# /admin-panel/scrapers/ page even in InPrivate mode).
#
# Serving these tiny files (≤ 90 KB each) through Django with an
# explicit, RFC-clean Content-Type sidesteps whitenoise entirely. The
# files are still cached by the browser for 30 days.
# ─────────────────────────────────────────────────────────────────
from pathlib import Path as _Path
from functools import lru_cache as _lru_cache
from django.conf import settings as _settings

_VENDOR_LIB_DIR = _Path(_settings.BASE_DIR) / 'static' / 'vendor' / 'lib'
_VENDOR_LIB_TYPES = {
    'core.min.js':  'text/javascript; charset=utf-8',
    'grid.min.js':  'text/javascript; charset=utf-8',
    'grid.min.css': 'text/css; charset=utf-8',
}

@_lru_cache(maxsize=8)
def _vendor_bytes(name):
    return (_VENDOR_LIB_DIR / name).read_bytes()

def vendor_lib(request, name):
    """Serve `static/vendor/lib/{name}` with a clean Content-Type so
    Edge/Safari accept it under X-Content-Type-Options: nosniff."""
    if name not in _VENDOR_LIB_TYPES:
        raise Http404(name)
    try:
        body = _vendor_bytes(name)
    except FileNotFoundError:
        raise Http404(name)
    resp = HttpResponse(body, content_type=_VENDOR_LIB_TYPES[name])
    resp['Cache-Control']    = 'public, max-age=2592000, immutable'  # 30 days
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp


@admin_required
def admin_scrapers_view(request):
    """Real scrapers list — backed by the `scrapers` table.

    The actual rows are loaded by the DataTable JS via AJAX against
    :func:`admin_scrapers_data`. This shell view just renders the page
    chrome (search box, state/city filters, table skeleton) and seeds
    the filter dropdowns from the current set of scrapers.
    """
    from .scraper_accela import reap_orphan_runs_live
    try:
        # Liveness-aware sweep — catches scrapers whose worker thread
        # died silently (heartbeat stale > 60s or thread missing from
        # the in-process registry) so the row's status pill and the
        # Run/Stop button state flip honestly the moment the admin
        # opens this page. Cheap: one SELECT + one UPDATE per orphan.
        reap_orphan_runs_live()
    except Exception:
        logging.exception('reap_orphan_runs_live failed (non-fatal)')
    from .db import (reap_stale_scraper_runs, reap_stale_cron_batches,
                     list_scraper_state_city_options)
    # Cheap indexed UPDATEs — un-stick any run row OR batch row that's
        # been "running" past its budget (gunicorn worker recycle, OOM
        # kill, hung network request, etc.) so the list page
    # status pills tell the truth instead of showing phantom in-flight
    # rows. Both are best-effort; never fatal.
    try:
        reaped = reap_stale_scraper_runs(30)
        if reaped:
            logging.info('reaped %d stale scraper_runs row(s)', reaped)
    except Exception:
        logging.exception('reap_stale_scraper_runs failed (non-fatal)')
    try:
        reaped_b = reap_stale_cron_batches(60)
        if reaped_b:
            logging.info('reaped %d stale cron_batches row(s)', reaped_b)
    except Exception:
        logging.exception('reap_stale_cron_batches failed (non-fatal)')

    # Total + filter options — used to render the empty state and the
    # state/city <select>s. Done at page-render time so the dropdowns
    # are populated on first paint without an extra AJAX round-trip.
    options = list_scraper_state_city_options()
    # Total scrapers count for the meta line. Cheap COUNT(*) — and
    # list_scrapers_dt does the same on every AJAX call, so caching
    # it here doesn't help.
    from .db import pg as _pg
    total_row = _pg.query_one('SELECT COUNT(*) AS n FROM scrapers')
    total = int((total_row or {}).get('n') or 0)

    # Full canonical state-code list for the Edit modal's
    # auto-infer-from-name validator. Independent of which states
    # currently have a scraper, so editing the very first scraper in
    # a brand new state still gets the inferred suffix accepted.
    from .us_cities_top import US_STATES
    all_state_codes = [code for code, _ in US_STATES]

    # Global "# of pages" knob — applies to Run All / Run Selected / per-row
    # Run from this page so the admin doesn't have to drill into every
    # scraper's detail page to change it. Persisted in system_settings.
    # Default is 50 (matches ACCELA_MAX_PAGES_DEFAULT). Hard-clamped to 1-1000.
    try:
        default_pages = int(get_system_setting('scrapers_default_pages', 50) or 50)
    except (TypeError, ValueError):
        default_pages = 50
    default_pages = max(1, min(default_pages, 1000))
    # Global "Threads" knob — same toolbar; persisted so the cron
    # worker can grab it directly from system_settings. Clamp 1-10.
    try:
        default_threads = int(get_system_setting('scrapers_default_threads', 5) or 5)
    except (TypeError, ValueError):
        default_threads = 5
    default_threads = max(1, min(default_threads, 10))

    ctx = {
        **_admin_base_ctx(request, 'scrapers'),
        'q':                       (request.GET.get('q') or '').strip(),
        'state_filter':            (request.GET.get('state') or '').strip().upper(),
        'city_filter':             (request.GET.get('city')  or '').strip().title(),
        'total':                   total,
        'state_options':           options['states'],
        'cities_by_state_json':    json.dumps(options['cities_by_state']),
        'all_state_codes_json':    json.dumps(all_state_codes),
        'scrapers_default_pages':    default_pages,
        'scrapers_default_threads':  default_threads,
        # Local scraper-agent settings card (rendered below the table —
        # the same global knobs are also exposed on the per-scraper
        # detail page via the same partial). Settings persist via
        # /admin-panel/scrapers/agent/settings/.
        **_agent_settings_ctx(),
    }
    return render(request, 'core/admin_scrapers.html', ctx)


@admin_required
def admin_scrapers_export_csv(request):
    """Export every scraper matching the current filters as CSV.

    Reads the same ``q`` / ``state`` / ``city`` / ``status`` query
    params as :func:`admin_scrapers_data` so a "what's on screen now"
    download matches what the table is showing. No pagination — pulls
    all matching rows in one shot via ``list_scrapers_dt`` with a wide
    ``length`` cap.
    """
    import csv as _csv
    import io as _io
    from django.http import HttpResponse
    from .db import list_scrapers_dt
    state  = (request.GET.get('state')  or '').strip()
    city   = (request.GET.get('city')   or '').strip()
    search = (request.GET.get('q')      or '').strip()
    status = (request.GET.get('status') or '').strip()
    rows, _, _ = list_scrapers_dt(
        search=search, state=state, city=city, status=status,
        start=0, length=200,            # list_scrapers_dt clamps at 200
        order_col='last_run', order_dir='desc',
    )
    # If the result hit the 200 cap, page through until we drain it.
    all_rows = list(rows)
    offset = 200
    while len(rows) >= 200:
        rows, _, _ = list_scrapers_dt(
            search=search, state=state, city=city, status=status,
            start=offset, length=200,
            order_col='last_run', order_dir='desc',
        )
        all_rows.extend(rows)
        offset += 200
        if offset > 100_000:            # safety cap
            break

    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow([
        'id', 'name', 'url', 'agency_code', 'module',
        'city', 'state', 'enabled',
        'last_run_at', 'last_run_status', 'total_permits',
    ])
    for s in all_rows:
        ts = s.get('last_run_at')
        ts_s = ts.strftime('%Y-%m-%d %H:%M:%S') if ts else ''
        w.writerow([
            int(s['id']),
            s.get('name')        or '',
            s.get('url')         or '',
            s.get('agency_code') or '',
            s.get('module')      or '',
            s.get('city')        or '',
            s.get('state')       or '',
            'true' if s.get('enabled') else 'false',
            ts_s,
            s.get('last_run_status') or '',
            int(s.get('total_permits') or 0),
        ])

    from datetime import datetime as _dt
    fname = f"scrapers-{_dt.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    resp = HttpResponse(buf.getvalue(), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


@admin_required
def admin_scrapers_data(request):
    """DataTables server-side AJAX endpoint for /admin-panel/scrapers/.

    Reads DataTables' standard query params plus our own filter params
    (``state``, ``city``, ``q``) and returns the JSON envelope
    DataTables expects: ``{draw, recordsTotal, recordsFiltered, data}``.

    Per-row payload is hand-shaped so the client renderer doesn't have
    to know about Python dates or DB internals.
    """
    from .db import list_scrapers_dt
    # Liveness-aware orphan reap on every AJAX refresh. Without this,
    # a scraper whose worker thread died silently after the page
    # loaded would keep rendering with a "Running" pill (and a Stop
    # button instead of Run-now) until the next full page reload or
    # the 30-min stale-timer fired. Sub-millisecond when there are no
    # 'running' rows; one UPDATE per orphan otherwise.
    try:
        from .scraper_accela import reap_orphan_runs_live
        reap_orphan_runs_live()
    except Exception:
        logging.exception('admin_scrapers_data: orphan reap failed (non-fatal)')
    try:
        draw = int(request.GET.get('draw') or 1)
    except (TypeError, ValueError):
        draw = 1
    try:
        start  = int(request.GET.get('start')  or 0)
        length = int(request.GET.get('length') or 25)
    except (TypeError, ValueError):
        start, length = 0, 25

    # Column-index → key mapping. Must mirror the `columns:` array in
    # admin_scrapers.html one-for-one (including non-orderable columns)
    # so DataTables' 0-based column index lines up. Two-step indirection
    # (idx → key → SQL frag in db.list_scrapers_dt) keeps SQL injection
    # impossible even with a malicious order[0][column]. Empty strings
    # mark non-orderable columns (checkbox, url, status pill, actions)
    # — they fall through to the default sort.
    col_keys = ['', 'name', 'url', 'city', 'state', 'last_run', 'permits', '', '']
    try:
        col_idx = int(request.GET.get('order[0][column]') or 5)
    except (TypeError, ValueError):
        col_idx = 5
    order_col = (col_keys[col_idx] if 0 <= col_idx < len(col_keys) else '') or 'last_run'
    order_dir = (request.GET.get('order[0][dir]') or 'desc').lower()

    state  = (request.GET.get('state')  or '').strip()
    city   = (request.GET.get('city')   or '').strip()
    search = (request.GET.get('q')      or '').strip()
    status = (request.GET.get('status') or '').strip()

    rows, total_unfiltered, total_filtered = list_scrapers_dt(
        search=search, state=state, city=city, status=status,
        start=start, length=length,
        order_col=order_col, order_dir=order_dir,
    )

    data = []
    for s in rows:
        url = s.get('url') or ''
        try:
            host = urllib.parse.urlparse(url).netloc or url
        except Exception:
            host = url
        data.append({
            'id':           int(s['id']),
            'name':         s.get('name') or '',
            'url':          url,
            'host':         host,
            'city':         s.get('city')  or '',
            'state':        s.get('state') or '',
            'agency_code':  s.get('agency_code') or '',
            'last_run_at':  s['last_run_at'].strftime('%b %d, %Y %I:%M %p')
                            if s.get('last_run_at') else '',
            'last_run_status': s.get('last_run_status') or '',
            'enabled':      bool(s.get('enabled')),
            'total_permits': int(s.get('total_permits') or 0),
        })

    return JsonResponse({
        'draw':            draw,
        'recordsTotal':    total_unfiltered,
        'recordsFiltered': total_filtered,
        'data':            data,
    })


# ── Scrapers — additional admin views ────────────────────────────────

@admin_required
def admin_scraper_new_view(request):
    """Create-scraper form. POST creates the row and redirects to the
    new scraper's detail page.

    City/state are optional but strongly recommended — the scrapers
    list page lets the admin filter by them, and the per-row meta on
    that page renders ``City, ST`` under the scraper name.
    """
    from .db import create_scraper, get_scraper
    from .scraper_accela import parse_accela_url
    from .us_cities_top import US_STATES, CITIES_BY_STATE
    err = None
    parsed_preview = None
    name_in = url_in = city_in = state_in = ''
    # Pre-fill from ?city=&state= so the Accela finder's "Add to scrapers"
    # button can hand us context the admin already typed once.
    if request.method == 'GET':
        city_in  = (request.GET.get('city')  or '').strip()
        state_in = (request.GET.get('state') or '').strip().upper()
        name_in  = (request.GET.get('name')  or '').strip()
        url_in   = (request.GET.get('url')   or '').strip()
    if request.method == 'POST':
        name_in  = (request.POST.get('name')  or '').strip()
        url_in   = (request.POST.get('url')   or '').strip()
        city_in  = (request.POST.get('city')  or '').strip()
        state_in = (request.POST.get('state') or '').strip().upper()
        if not name_in:
            err = 'Name is required.'
        elif not url_in or not url_in.startswith('https://'):
            err = 'A full https:// URL is required.'
        elif state_in and (len(state_in) != 2 or not state_in.isalpha()):
            err = 'State must be a 2-letter code (e.g. CA).'
        else:
            # Lenient validation: we only enforce the host allowlist
            # (accela.com / *.accela.com — see parse_accela_url) and
            # let the scraper inspect the page contents. Backfill
            # specifically needs capID3 to enumerate, so the per-button
            # handler will surface a clean error if it's missing.
            parsed = parse_accela_url(url_in)
            if not parsed:
                err = ('URL must be on accela.com (or a subdomain). '
                       'Paste any Accela permit URL — CapDetail, CapHome, '
                       'list, or report page.')
            else:
                sid = create_scraper(
                    name=name_in,
                    url=url_in,
                    source='accela',
                    agency_code=parsed.get('agency_code', ''),
                    module=parsed.get('module', ''),
                    cap_id_template=parsed,
                    city=city_in,
                    state=state_in,
                    enabled=True,
                )
                return redirect(f'/admin-panel/scrapers/{sid}/')
    if request.method == 'GET' and request.GET.get('preview_url'):
        parsed_preview = parse_accela_url(request.GET['preview_url'])
    ctx = {
        **_admin_base_ctx(request, 'scrapers'),
        'err':                  err,
        'name_in':              name_in,
        'url_in':               url_in,
        'city_in':              city_in,
        'state_in':             state_in,
        'parsed_preview':       parsed_preview,
        'us_states':            US_STATES,
        'cities_by_state_json': json.dumps(CITIES_BY_STATE),
    }
    return render(request, 'core/admin_scraper_new.html', ctx)


@admin_required
def admin_scraper_permits_data(request, sid):
    """DataTables server-side AJAX endpoint for /admin-panel/scrapers/<sid>/.

    Bound to the table by the JS init in admin_scraper_detail.html.
    Reads DataTables' standard query params (``draw``, ``start``,
    ``length``, ``search[value]``, ``order[0][column]``,
    ``order[0][dir]``) plus our own filter params (``date_from``,
    ``date_to``, ``has_email``, ``has_phone``) and returns the JSON
    envelope DataTables expects: ``{draw, recordsTotal,
    recordsFiltered, data}``.

    Per-row payload is hand-shaped instead of dumping raw DB columns
    so the client-side renderer never has to parse Python date objects
    or look up money/score formatting twice.
    """
    from .db import get_scraper, list_permits_for_scraper_dt
    scraper = get_scraper(sid)
    if not scraper:
        return JsonResponse({'draw': 0, 'recordsTotal': 0, 'recordsFiltered': 0,
                             'data': [], 'error': 'Scraper not found'}, status=404)

    # DataTables sends draw as the request id; echo it back so stale
    # responses (e.g. the user typed fast and an earlier slow query
    # arrived after a later faster one) are dropped client-side.
    try:
        draw = int(request.GET.get('draw') or 1)
    except (TypeError, ValueError):
        draw = 1
    try:
        start = int(request.GET.get('start') or 0)
        length = int(request.GET.get('length') or 50)
    except (TypeError, ValueError):
        start, length = 0, 50

    # ORDER BY: DataTables sends column index → we map index → key,
    # and the DB layer maps key → SQL fragment (whitelist). Two-step
    # mapping keeps SQL injection impossible even if a malicious
    # client posts a bogus column index or name.
    # Col 0 is the row-checkbox (non-orderable on the client) — kept
    # in this list as a sentinel so DataTables' column index still
    # maps 1:1 to entries here. The checkbox cell isn't sortable so
    # the 'date' fallback there is unreachable; it just preserves the
    # index math after the bulk-delete column was prepended.
    col_keys = ['date', 'permit', 'type', 'address', 'city', 'contractor',
                'email', 'phone', 'date', 'value', 'score']
    try:
        col_idx = int(request.GET.get('order[0][column]') or 8)
    except (TypeError, ValueError):
        col_idx = 8
    order_col = col_keys[col_idx] if 0 <= col_idx < len(col_keys) else 'date'
    order_dir = (request.GET.get('order[0][dir]') or 'desc').lower()

    # Filter inputs (mirror the legacy non-DT view).
    date_from = (request.GET.get('date_from') or '').strip() or None
    date_to   = (request.GET.get('date_to')   or '').strip() or None
    he_raw = (request.GET.get('has_email') or '').strip().lower()
    hp_raw = (request.GET.get('has_phone') or '').strip().lower()
    has_email = True if he_raw == 'yes' else (False if he_raw == 'no' else None)
    has_phone = True if hp_raw == 'yes' else (False if hp_raw == 'no' else None)
    # Search text: prefer DataTables' own ``search[value]`` (built-in
    # search box) and fall back to our explicit ``q`` so the legacy
    # filter form still works if anyone bookmarks an old URL.
    q = (request.GET.get('search[value]')
         or request.GET.get('q') or '').strip()

    # Per-column filters (added 2026-05). Each is a free-form ILIKE
    # substring on a single column. All ANDed together with the
    # generic `q` so the admin can pre-narrow by e.g. email + permit#
    # then still scan free-form on top.
    permit_number = (request.GET.get('permit_number') or '').strip()
    email_q       = (request.GET.get('email')         or '').strip()
    phone_q       = (request.GET.get('phone')         or '').strip()
    contractor_q  = (request.GET.get('contractor')    or '').strip()
    owner_q       = (request.GET.get('owner')         or '').strip()
    city_q        = (request.GET.get('city')          or '').strip()
    # Read from `permit_type` (current form field name); fall back to
    # the legacy `type` key in case any bookmarked URL still uses it.
    type_q        = (request.GET.get('permit_type')
                     or request.GET.get('type')        or '').strip()

    def _opt_int(name):
        raw = (request.GET.get(name) or '').strip()
        if not raw:
            return None
        try:
            return max(0, min(100, int(raw)))
        except (TypeError, ValueError):
            return None
    min_score = _opt_int('min_score')
    max_score = _opt_int('max_score')

    rows, total_unfiltered, total_filtered = list_permits_for_scraper_dt(
        sid, date_from=date_from, date_to=date_to,
        has_email=has_email, has_phone=has_phone, query=q,
        permit_number=permit_number, email=email_q, phone=phone_q,
        contractor=contractor_q, owner=owner_q,
        city_q=city_q, type_q=type_q,
        min_score=min_score, max_score=max_score,
        start=start, length=length,
        order_col=order_col, order_dir=order_dir,
    )

    def _iso(d):
        return d.isoformat() if hasattr(d, 'isoformat') else (d or '')
    def _money(c):
        try: return f'${int(c) // 100:,}' if c else ''
        except (TypeError, ValueError): return ''

    data = [{
        'id':         r['id'],
        'permit':     r.get('permit_number') or '',
        'type':       r.get('permit_type') or '',
        'address':    r.get('address') or '',
        'city':       r.get('city') or '',
        'state':      r.get('state') or '',
        'contractor':   r.get('contractor_name') or '',
        # Customer-facing /permits/ "Owner / Contractor" column. ONE
        # name per row (owner first, contractor as fallback) plus an
        # explicit type so the user can tell at a glance whether they're
        # looking at the property owner or the licensed contractor.
        # 95% of rows have at least one of the two; the remaining ~5%
        # render as "—" with no pill.
        # NOTE: `lead` keeps the customer-side priority (owner first).
        # The admin grid uses `contact` (contractor first) for outreach
        # — leaving both fields in the payload so we don't have to
        # branch the renderer per surface.
        'lead':         r.get('owner_name') or r.get('contractor_name') or '',
        'lead_type':    ('owner' if r.get('owner_name')
                         else ('contractor' if r.get('contractor_name')
                               else '')),
        # Legacy field name kept so any older cached JS/clients keep
        # working — same value as `lead`.
        'owner':        r.get('owner_name') or r.get('contractor_name') or '',
        # Unified primary contact derived at ingest time (contractor
        # wins, owner is the fallback) — see core.scraper_accela.
        # _normalise_permit. The admin grid renders this as the main
        # "Contact" column with a small type badge so the user no
        # longer has to scan two side-by-side columns to know who
        # to call.
        'contact':      r.get('contact_name') or r.get('contractor_name') or r.get('owner_name') or '',
        'contact_type': r.get('contact_type') or ('contractor' if r.get('contractor_name') else ('owner' if r.get('owner_name') else '')),
        'email':        r.get('contractor_email') or '',
        'phone':        r.get('contractor_phone') or '',
        'date':       _iso(r.get('applied_date') or r.get('issued_date')),
        'value':      _money(r.get('valuation_cents')),
        'grade':      r.get('ai_grade') or '',
        'score':      int(r.get('ai_score') or 0),
        # Direct link to the jurisdiction's own CapDetail page for this
        # permit. Lets the admin Source column render a one-click
        # "open original" arrow next to the local raw-page viewer.
        'detail_url': (r.get('detail_url') or '').strip(),
    } for r in rows]
    return JsonResponse({
        'draw': draw,
        'recordsTotal':    total_unfiltered,
        'recordsFiltered': total_filtered,
        'data':            data,
    })


@admin_required
def admin_all_permits_view(request):
    """Render the admin "All Permit Data" page — full server-side
    DataTable over the entire `permits` table with per-column filters
    (state, city, source, type, status, contractor, owner, email,
    phone, permit#, date range, score range, valuation range,
    has-email / has-phone).
    """
    from .db import pg
    total_row = pg.query_one("SELECT COUNT(*) AS n FROM permits") or {}
    ctx = {
        **_admin_base_ctx(request, 'all_permits'),
        'total_permits': int(total_row.get('n') or 0),
    }
    return render(request, 'core/admin_all_permits.html', ctx)


@admin_required
def admin_all_permits_data(request):
    """DataTables server-side JSON endpoint for /admin-panel/permits/.

    Same envelope as ``admin_scraper_permits_data`` but pulls from the
    entire permits table (no scraper / user gate) — admin-only.
    """
    from .db import list_all_permits_dt

    try:    draw = int(request.GET.get('draw') or 1)
    except (TypeError, ValueError): draw = 1
    try:
        start = int(request.GET.get('start') or 0)
        length = int(request.GET.get('length') or 50)
    except (TypeError, ValueError):
        start, length = 0, 50

    # NOTE: index 0 is the row-select checkbox column (not orderable);
    # the data columns start at index 1, so this list is offset by one
    # to line up with the DataTables order[0][column] index.
    col_keys = ['sel', 'permit', 'type', 'address', 'city', 'state', 'source',
                'status', 'contractor', 'owner', 'email', 'phone',
                'date', 'scraped', 'value', 'score']
    try:    col_idx = int(request.GET.get('order[0][column]') or 12)
    except (TypeError, ValueError): col_idx = 12
    order_col = col_keys[col_idx] if 0 <= col_idx < len(col_keys) else 'date'
    order_dir = (request.GET.get('order[0][dir]') or 'desc').lower()

    g = request.GET
    date_from = (g.get('date_from') or '').strip() or None
    date_to   = (g.get('date_to')   or '').strip() or None
    scraped_from = (g.get('scraped_from') or '').strip() or None
    scraped_to   = (g.get('scraped_to')   or '').strip() or None
    he_raw = (g.get('has_email') or '').strip().lower()
    hp_raw = (g.get('has_phone') or '').strip().lower()
    has_email = True if he_raw == 'yes' else (False if he_raw == 'no' else None)
    has_phone = True if hp_raw == 'yes' else (False if hp_raw == 'no' else None)
    q = (g.get('search[value]') or g.get('q') or '').strip()

    def _opt_int(name, lo=0, hi=100):
        raw = (g.get(name) or '').strip()
        if not raw:
            return None
        try:    return max(lo, min(hi, int(raw)))
        except (TypeError, ValueError): return None

    def _opt_money_cents(name):
        raw = (g.get(name) or '').strip().replace(',', '').replace('$', '')
        if not raw:
            return None
        try:    return max(0, int(float(raw) * 100))
        except (TypeError, ValueError): return None

    rows, total_unfiltered, total_filtered = list_all_permits_dt(
        query=q,
        permit_number=(g.get('permit_number') or '').strip(),
        email       =(g.get('email')         or '').strip(),
        phone       =(g.get('phone')         or '').strip(),
        contractor  =(g.get('contractor')    or '').strip(),
        owner       =(g.get('owner')         or '').strip(),
        city_q      =(g.get('city')          or '').strip(),
        state_q     =(g.get('state')         or '').strip(),
        type_q      =(g.get('permit_type') or g.get('type') or '').strip(),
        status_q    =(g.get('status')        or '').strip(),
        source_q    =(g.get('source')        or '').strip(),
        date_from=date_from, date_to=date_to,
        scraped_from=scraped_from, scraped_to=scraped_to,
        has_email=has_email, has_phone=has_phone,
        min_score=_opt_int('min_score'),
        max_score=_opt_int('max_score'),
        min_value=_opt_money_cents('min_value'),
        max_value=_opt_money_cents('max_value'),
        start=start, length=length,
        order_col=order_col, order_dir=order_dir,
    )

    def _iso(d):
        return d.isoformat() if hasattr(d, 'isoformat') else (d or '')
    def _money(c):
        try: return f'${int(c) // 100:,}' if c else ''
        except (TypeError, ValueError): return ''

    # Resolve scraper IDs → human names in ONE query for the visible
    # page so the Source column can render "Fort Worth, TX" instead of
    # the raw "accela:88" tag the admin asked about. The source tag
    # format is "accela:<id>" (see _scraper_source_tag in core/db.py);
    # rows from older / non-Accela ingest paths just keep the raw tag.
    from .db import pg as _pg
    scraper_ids: set[int] = set()
    for r in rows:
        s = (r.get('source') or '').strip()
        if s.startswith('accela:'):
            try:    scraper_ids.add(int(s.split(':', 1)[1]))
            except (TypeError, ValueError, IndexError): pass
    scraper_names: dict[int, str] = {}
    if scraper_ids:
        try:
            name_rows = _pg.query(
                "SELECT id, name FROM scrapers WHERE id = ANY(%s)",
                (sorted(scraper_ids),),
            ) or []
            scraper_names = {int(nr['id']): (nr.get('name') or '')
                             for nr in name_rows}
        except Exception:
            log.exception('admin_all_permits_data: scraper name lookup failed')

    def _scraper_meta(src: str) -> tuple[int | None, str]:
        """(scraper_id_or_None, display_name). Display falls back to
        the raw source tag for non-Accela rows or ids we couldn't
        resolve."""
        s = (src or '').strip()
        if s.startswith('accela:'):
            try:    sid = int(s.split(':', 1)[1])
            except (TypeError, ValueError, IndexError): sid = None
            if sid is not None:
                return sid, (scraper_names.get(sid) or s)
        return None, s

    data = []
    for r in rows:
        src = r.get('source') or ''
        sid, sname = _scraper_meta(src)
        data.append({
        'id':         r['id'],
        'source':     src,
        'scraper_id':   sid,
        'scraper_name': sname,
        'source_permit_id': r.get('source_permit_id') or '',
        'permit':     r.get('permit_number') or '',
        'type':       r.get('permit_type') or '',
        'address':    r.get('address') or '',
        'city':       r.get('city') or '',
        'state':      r.get('state') or '',
        'jurisdiction': r.get('jurisdiction') or '',
        'status':     r.get('status') or '',
        'contractor': r.get('contractor_name') or '',
        'owner':      r.get('owner_name') or '',
        'email':      r.get('contractor_email') or '',
        'phone':      r.get('contractor_phone') or '',
        'date':       _iso(r.get('applied_date') or r.get('issued_date')),
        'scraped':    _iso(r.get('scraped_at')),
        'value':      _money(r.get('valuation_cents')),
        'grade':      r.get('ai_grade') or '',
        'score':      int(r.get('ai_score') or 0),
        'detail_url': (r.get('detail_url') or '').strip(),
        })
    return JsonResponse({
        'draw': draw,
        'recordsTotal':    total_unfiltered,
        'recordsFiltered': total_filtered,
        'data':            data,
    })


@admin_required
@require_http_methods(['POST'])
def admin_all_permits_bulk_delete(request):
    """Hard-delete permits from the GLOBAL admin permits page.

    Two modes (admin-only, no source gate — this view governs the whole
    permits table):
      • default — delete the explicitly checked rows: repeated POST
        ``ids`` (or one comma-separated value).
      • ``match_all=1`` — delete EVERY row matching the same filter set
        the DataTable is showing (cross-page), using the current filter
        params. Refuses an empty filter set so this can't silently wipe
        the whole table — that's what the DB-Utilities "Wipe All Permits"
        button is for.

    Returns JSON ``{ok, deleted, requested}``.
    """
    from .db import bulk_delete_permits_by_ids, delete_all_permits_matching

    match_all = (request.POST.get('match_all') or '').strip() in ('1', 'true', 'on', 'yes')
    if match_all:
        p = request.POST
        def _opt_int(name, lo=0, hi=100):
            raw = (p.get(name) or '').strip()
            if not raw:
                return None
            try:    return max(lo, min(hi, int(raw)))
            except (TypeError, ValueError): return None
        def _opt_money_cents(name):
            raw = (p.get(name) or '').strip().replace(',', '').replace('$', '')
            if not raw:
                return None
            try:    return max(0, int(float(raw) * 100))
            except (TypeError, ValueError): return None
        he = (p.get('has_email') or '').strip().lower()
        hp = (p.get('has_phone') or '').strip().lower()
        try:
            deleted = delete_all_permits_matching(
                query       =(p.get('q') or '').strip(),
                permit_number=(p.get('permit_number') or '').strip(),
                email       =(p.get('email') or '').strip(),
                phone       =(p.get('phone') or '').strip(),
                contractor  =(p.get('contractor') or '').strip(),
                owner       =(p.get('owner') or '').strip(),
                city_q      =(p.get('city') or '').strip(),
                state_q     =(p.get('state') or '').strip(),
                type_q      =(p.get('permit_type') or p.get('type') or '').strip(),
                status_q    =(p.get('status') or '').strip(),
                source_q    =(p.get('source') or '').strip(),
                date_from   =(p.get('date_from') or '').strip() or None,
                date_to     =(p.get('date_to') or '').strip() or None,
                scraped_from=(p.get('scraped_from') or '').strip() or None,
                scraped_to  =(p.get('scraped_to') or '').strip() or None,
                has_email=True if he == 'yes' else (False if he == 'no' else None),
                has_phone=True if hp == 'yes' else (False if hp == 'no' else None),
                min_score=_opt_int('min_score'), max_score=_opt_int('max_score'),
                min_value=_opt_money_cents('min_value'),
                max_value=_opt_money_cents('max_value'),
            )
        except ValueError:
            return JsonResponse(
                {'ok': False,
                 'error': 'Set at least one filter before deleting all matching, '
                          'or use Wipe All Permits in DB Utilities.'},
                status=400)
        return JsonResponse({'ok': True, 'deleted': int(deleted),
                             'requested': int(deleted), 'match_all': True})

    raw_ids = request.POST.getlist('ids') or []
    if not raw_ids:
        single = (request.POST.get('ids') or '').strip()
        if single:
            raw_ids = [s.strip() for s in single.split(',') if s.strip()]
    if not raw_ids:
        return JsonResponse({'ok': False, 'error': 'no ids'}, status=400)
    deleted = bulk_delete_permits_by_ids(raw_ids)
    return JsonResponse({'ok': True, 'deleted': int(deleted),
                         'requested': len(raw_ids)})


@admin_required
@require_http_methods(['POST'])
def admin_scraper_permits_delete_all(request, sid):
    """Hard-delete EVERY permit belonging to one scraper.

    Fixes the "delete doesn't clear everything" issue: the detail page's
    select-all only ticks the current DataTables page, so a checkbox
    delete leaves the rest. This removes the whole set in one statement
    and refreshes the scraper's cached ``total_permits``.

    Returns JSON ``{ok, deleted}``.
    """
    from .db import (get_scraper, delete_all_permits_for_scraper,
                     refresh_scraper_total_permits)
    if not get_scraper(sid):
        return JsonResponse({'ok': False, 'error': 'scraper not found'}, status=404)
    deleted = delete_all_permits_for_scraper(int(sid))
    try:
        refresh_scraper_total_permits(int(sid))
    except Exception:
        pass
    return JsonResponse({'ok': True, 'deleted': int(deleted)})


@admin_required
@require_http_methods(['POST'])
def admin_scraper_permits_bulk_delete(request, sid):
    """Delete multiple permits selected via the admin scraper-detail
    page's "Select all" checkbox column. Form param ``ids`` is repeated
    once per checked row (a comma-separated single value also works).

    Authorisation: the WHERE clause pins ``source = _scraper_source_tag(sid)``
    so a crafted POST cannot delete rows belonging to a different
    scraper, even if the caller submits ids from elsewhere.

    Returns JSON ``{ok: True, deleted: N, requested: M}``.
    """
    from .db import (get_scraper, bulk_delete_permits_by_ids,
                     _scraper_source_tag, refresh_scraper_total_permits)
    if not get_scraper(sid):
        return JsonResponse({'ok': False, 'error': 'scraper not found'},
                            status=404)
    raw_ids = request.POST.getlist('ids') or []
    if not raw_ids:
        single = (request.POST.get('ids') or '').strip()
        if single:
            raw_ids = [s.strip() for s in single.split(',') if s.strip()]
    if not raw_ids:
        return JsonResponse({'ok': False, 'error': 'no ids'}, status=400)

    deleted = bulk_delete_permits_by_ids(
        raw_ids, allowed_source=_scraper_source_tag(int(sid)))
    # Keep the scraper's `total_permits` cached count honest after a
    # destructive change so the list view doesn't lie.
    try:
        refresh_scraper_total_permits(int(sid))
    except Exception:
        pass
    return JsonResponse({'ok': True, 'deleted': int(deleted),
                         'requested': len(raw_ids)})


def _agent_settings_ctx() -> dict:
    """Build the context dict consumed by templates/core/_agent_settings_card.html.

    The local scraper-agent knobs (model, on/off) are stored globally
    in system_settings — they are NOT per-scraper. The
    same partial card is rendered on the scrapers list page and the
    per-scraper detail page; this helper keeps both sites in sync so a
    later default change only needs editing here.
    """
    from .db import get_system_setting
    from .scraper_accela import ACCELA_SCRAPER_AGENT_DEFAULT_MODEL
    saved_model   = (get_system_setting('accela_scraper_agent_model') or '').strip()
    saved_at      = (get_system_setting('accela_scraper_agent_settings_saved_at') or '').strip()
    saved_use_raw = (get_system_setting('accela_scraper_use_agent') or 'on').strip().lower()
    # Model is free-text — any DO Inference catalogue id. Saved value
    # wins, falls back to the default model name only when blank.
    # Prompt template and max-credits used to live here too — both
    # were removed in 2026-05: the reference parser prompt is used
    # verbatim (project policy) and DO Serverless Inference has no
    # per-run credit cap.
    initial_model = saved_model or ACCELA_SCRAPER_AGENT_DEFAULT_MODEL
    return {
        'agent_default_model':     initial_model,
        'agent_settings_saved_at': saved_at,
        'agent_use_on':            saved_use_raw != 'off',
    }


@admin_required
def admin_scraper_detail_view(request, sid):
    """Per-scraper detail page: header + filters bar + paginated permits
    table + run / backfill buttons + local scraper-agent settings card.
    Run history moved to its own /admin-panel/scraper-logs/<sid>/
    page."""
    from .db import (get_scraper, list_permits_for_scraper,
                     count_permits_for_scraper)
    scraper = get_scraper(sid)
    if not scraper:
        raise Http404('Scraper not found')

    date_from = (request.GET.get('date_from') or '').strip() or None
    date_to   = (request.GET.get('date_to')   or '').strip() or None
    has_email_raw = (request.GET.get('has_email') or '').strip().lower()
    has_phone_raw = (request.GET.get('has_phone') or '').strip().lower()
    has_email = True if has_email_raw == 'yes' else (False if has_email_raw == 'no' else None)
    has_phone = True if has_phone_raw == 'yes' else (False if has_phone_raw == 'no' else None)
    q = (request.GET.get('q') or '').strip()
    try:
        page = int(request.GET.get('page') or 1)
    except (TypeError, ValueError):
        page = 1
    per_page = 25
    # list_permits_for_scraper returns the clamped page as the 4th
    # element so the prev/next links match the rows we render.
    rows, total, total_pages, page = list_permits_for_scraper(
        sid, date_from=date_from, date_to=date_to,
        has_email=has_email, has_phone=has_phone,
        query=q, page=page, per_page=per_page,
    )
    # `runs` query removed — the Recent runs side panel was retired
    # in favour of /admin-panel/scraper-logs/<sid>/. The template no
    # longer references {{ runs }}.
    # url host for chrome
    try:
        host = urllib.parse.urlparse(scraper['url']).netloc
    except Exception:
        host = scraper['url']
    qs_keep = []
    for k, v in (('q', q), ('date_from', date_from or ''),
                 ('date_to', date_to or ''),
                 ('has_email', has_email_raw), ('has_phone', has_phone_raw)):
        if v:
            qs_keep.append(f'{k}={urllib.parse.quote(str(v))}')
    qs_str = '&'.join(qs_keep)

    # Most-recent run for the inline terminal panel + script-cmd panel.
    # When no run is active the terminal preloads with this run's
    # step_log so the admin lands on the LAST KNOWN transcript instead
    # of an empty box. When a run IS still queued/running the JS picks
    # it up via the normal status poll.
    from .db import get_latest_scraper_run, update_scraper
    latest_run = get_latest_scraper_run(sid)
    # Orphan-detect on every render: if the row says running/queued but
    # the worker_pid doesn't match this server process (e.g. workflow
    # restart since the run started), finalise it as cancelled on the
    # spot. Otherwise the page would render with the Run button stuck
    # disabled and a "Running…" pill that never resolves — exactly the
    # bug the admin reported when restarting the server mid-run.
    if latest_run and not latest_run.get('finished_at') \
            and (latest_run.get('status') or '').lower() in ('queued', 'running'):
        from .scraper_accela import finalize_orphan_run
        if finalize_orphan_run(int(latest_run['id']),
                               reason='server restarted — worker process is gone'):
            latest_run = get_latest_scraper_run(sid) or latest_run
    if latest_run and latest_run.get('finished_at'):
        final_status = (latest_run.get('status') or '').strip().lower()
        if final_status in ('success', 'partial', 'failed', 'cancelled') \
                and (scraper.get('last_run_status') or '').strip().lower() == 'running':
            update_scraper(sid, last_run_at=latest_run.get('finished_at'),
                           last_run_status=final_status)
            scraper = get_scraper(sid) or scraper
    latest_run_active = bool(
        latest_run
        and not latest_run.get('finished_at')
        and (latest_run.get('status') or '').lower() in ('queued', 'running')
    )
    # Pre-build the equivalent CLI invocation so the panel can show
    # exactly what's running. We surface the Django shell one-liner
    # rather than a dedicated mgmt command so it works against any
    # checkout without requiring a separate entry-point file.
    # CLI mirror of the "Run now" button. The UI drives the worker via
    # the GLOBAL `scrapers_default_pages` system setting (edited on the
    # /admin-panel/scrapers/ toolbar) so surface that same value here —
    # copy/paste into ssh reproduces what the button fires.
    # NOTE: we expose `run_scraper_now` (blocking) rather than the
    # async variant because `manage.py shell -c "..."` exits as soon
    # as the statement returns, and the async path would leave the
    # daemon worker thread to die before it can do any work.
    try:
        _default_pages = int(get_system_setting('scrapers_default_pages', 50) or 50)
    except (TypeError, ValueError):
        _default_pages = 50
    _default_pages = max(1, min(_default_pages, 1000))
    script_command = (
        f"python manage.py shell -c \"from core.scraper_accela import "
        f"run_scraper_now; run_scraper_now({int(sid)}, max_pages={_default_pages})\""
    )

    ctx = {
        **_admin_base_ctx(request, 'scrapers'),
        'scraper':     scraper,
        'host':        host,
        'permits':     rows,
        'total':       total,
        'page':        page,
        'per_page':    per_page,
        'total_pages': total_pages,
        'has_prev':    page > 1,
        'has_next':    page < total_pages,
        'prev_page':   max(1, page - 1),
        'next_page':   min(total_pages, page + 1),
        'qs_str':      qs_str,
        'filter_date_from': date_from or '',
        'filter_date_to':   date_to or '',
        'filter_has_email': has_email_raw,
        'filter_has_phone': has_phone_raw,
        'filter_q':         q,
        'permit_count':     count_permits_for_scraper(sid),
        'latest_run':       latest_run,
        'latest_run_active': latest_run_active,
        'script_command':   script_command,
        # Global "# of pages" (system_settings.scrapers_default_pages) —
        # exposed so the detail page can render it read-only and pass it
        # into window.GLOBAL_MAX_PAGES for the script-command preview.
        'default_pages':    _default_pages,
        # Agent settings card (shared partial — see _agent_settings_ctx)
        **_agent_settings_ctx(),
    }
    return render(request, 'core/admin_scraper_detail.html', ctx)


@admin_required
@require_http_methods(['POST'])
def admin_scraper_run_now(request, sid):
    """Async single-URL run. Kicks off the daemon thread and returns
    the run_id immediately so the UI can show a live progress bar
    while page fetching + extraction work (which together take
    30-90 seconds — too long to block the request).

    The frontend opens the same progress modal it uses for backfill
    and polls /admin-panel/scrapers/runs/<rid>/status/ until done."""
    from .scraper_accela import run_scraper_async, ScraperError
    from .db import get_scraper
    # A disabled scraper is OFF — it must not run manually, on cron, or
    # via Run All until an admin flips it back to active. Run All and the
    # cron coordinator already pull only `enabled` rows; this guard closes
    # the last door (the per-row / detail-page "Run now" button).
    _sc = get_scraper(sid)
    if not _sc:
        raise Http404
    if not _sc.get('enabled'):
        return JsonResponse(
            {'ok': False,
             'error': 'This scraper is disabled. Enable it before running.'},
            status=400,
        )
    # max_pages is no longer a per-scraper UI override — it's a single
    # GLOBAL knob in system_settings.scrapers_default_pages, edited on
    # the /admin-panel/scrapers/ toolbar. We still accept a POST
    # `max_pages` for backwards compatibility (and for callers that
    # really do want a one-off override), but the default now comes
    # from the global setting instead of a hardcoded 50. Clamped to
    # 1-1000 so a bad value can't trigger a multi-hour runaway run.
    try:
        _global_pages = int(get_system_setting('scrapers_default_pages', 50) or 50)
    except (TypeError, ValueError):
        _global_pages = 50
    _global_pages = max(1, min(_global_pages, 1000))
    raw_mp = (request.POST.get('max_pages') or '').strip()
    if raw_mp:
        try:
            max_pages = int(raw_mp)
        except (TypeError, ValueError):
            max_pages = _global_pages
    else:
        max_pages = _global_pages
    max_pages = max(1, min(max_pages, 1000))
    try:
        run_id = run_scraper_async(
            sid, mode='single',
            max_pages=max_pages,
        )
        return JsonResponse({'ok': True, 'run_id': run_id})
    except ScraperError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    except Exception:
        # Don't echo internal exception strings back to the browser —
        # the full traceback is already in the server log.
        logging.exception('scraper run-now kickoff failed')
        return JsonResponse(
            {'ok': False, 'error': 'Could not start run — check server logs.'},
            status=500,
        )


@admin_required
@require_http_methods(['POST'])
def admin_scraper_toggle_enabled(request, sid):
    """Flip a scraper between active (enabled) and disabled.

    A disabled scraper is skipped by Run All and the cron coordinator
    (both pull only ``enabled`` rows) and is refused by the manual
    "Run now" endpoint — so it stays OFF until an admin re-enables it.
    Returns JSON {ok: true, enabled: bool} so the UI can update in
    place. Accepts an optional POST ``enabled`` ('1'/'0') to set an
    explicit state; with no body it simply toggles the current value."""
    from .db import get_scraper, update_scraper
    existing = get_scraper(sid)
    if not existing:
        raise Http404
    raw = (request.POST.get('enabled') or '').strip()
    if raw in ('0', '1'):
        new_state = (raw == '1')
    else:
        new_state = not bool(existing.get('enabled'))
    update_scraper(sid, enabled=new_state)
    return JsonResponse({'ok': True, 'enabled': new_state})


@admin_required
@require_http_methods(['POST'])
def admin_scraper_backfill(request, sid):
    """Async backfill — kicks off the daemon thread and returns the
    run_id immediately so the UI can begin polling progress."""
    from .scraper_accela import run_scraper_async, ScraperError
    from .db import get_scraper
    # Backfill is a manual run — a disabled scraper must stay OFF here
    # too (same contract as "Run now"). Admin must re-enable to backfill.
    _sc = get_scraper(sid)
    if not _sc:
        raise Http404
    if not _sc.get('enabled'):
        return JsonResponse(
            {'ok': False,
             'error': 'This scraper is disabled. Enable it before backfilling.'},
            status=400,
        )
    try:
        count = int(request.POST.get('count') or 20)
    except (TypeError, ValueError):
        count = 20
    count = max(1, min(count, 200))
    # NOTE: backfill walks the scraper's URL backwards by capID3 — it
    # fetches each detail page directly without going through the list
    # page, so the date_from / date_to filter the admin set on the
    # detail page does NOT apply here. The Run-now endpoint is the one
    # that honours the date range. We intentionally do not read those
    # POST fields here so the contract stays honest.
    try:
        run_id = run_scraper_async(sid, mode='backfill', count=count)
        return JsonResponse({'ok': True, 'run_id': run_id})
    except ScraperError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    except Exception:
        # See note above — keep internal details out of the JSON.
        logging.exception('scraper backfill kickoff failed')
        return JsonResponse(
            {'ok': False, 'error': 'Could not start backfill — check server logs.'},
            status=500,
        )


@admin_required
@require_http_methods(['POST'])
def admin_scraper_agent_save_settings(request):
    """Persist the per-scraper DigitalOcean-Inference settings card values.

    Body (JSON): ``model`` (free-text — any model id from your
    DigitalOcean Serverless Inference catalogue, e.g.
    ``openai-gpt-oss-20b``, ``openai-gpt-oss-120b``,
    ``llama3.3-70b-instruct``), ``use_agent`` (bool — when false,
    scrapes fall back to the legacy direct-HTTP pipeline).

    ``prompt_template`` and ``max_credits`` used to be saved here too;
    both were removed in 2026-05. The reference parser prompt is used
    verbatim (project policy) and DO Serverless Inference has no
    per-run credit cap, so neither field belongs in the admin UI.
    Old payload keys are silently ignored for forward-compat with
    cached browser tabs.
    """
    raw_body = request.body or b''
    if len(raw_body) > 16384:
        return JsonResponse({'ok': False, 'error': 'Request body too large.'},
                            status=413)

    if request.content_type and request.content_type.startswith('application/json'):
        try:
            payload = json.loads(raw_body.decode('utf-8') or '{}')
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({'ok': False, 'error': 'Invalid JSON body.'},
                                status=400)
        if not isinstance(payload, dict):
            return JsonResponse(
                {'ok': False, 'error': 'JSON body must be an object.'},
                status=400,
            )
    else:
        payload = request.POST

    raw_model = payload.get('model', '')
    if not isinstance(raw_model, str):
        return JsonResponse({'ok': False, 'error': 'model must be a string.'},
                            status=400)
    model = raw_model.strip()
    if not model:
        return JsonResponse(
            {'ok': False, 'error': 'model cannot be empty.'},
            status=400,
        )
    # DigitalOcean ships new models continuously — accept any non-empty
    # id rather than allow-listing. Keep a sane upper bound so a typo
    # in the textbox can't blow out our system_settings row.
    if len(model) > 200:
        return JsonResponse(
            {'ok': False, 'error': 'model name too long (max 200 chars).'},
            status=400,
        )
    # Light-touch syntactic validation — DO model ids are
    # ``[a-z0-9._-]+`` (sometimes ``vendor/model`` style with a slash).
    if not re.fullmatch(r'[A-Za-z0-9._\-/]+', model):
        return JsonResponse(
            {'ok': False, 'error': 'model name has invalid characters '
                                   '(use letters, digits, dot, dash, underscore, slash).'},
            status=400,
        )

    # use_agent toggle: strict parser. We deliberately whitelist the
    # accepted shapes (bool, the canonical strings, 0/1) instead of
    # truthy-coercing arbitrary input, so a typo like
    # ``"use_agent": "tru"`` becomes a 400 the admin can SEE rather
    # than silently flipping to False.
    raw_use = payload.get('use_agent', True)
    if isinstance(raw_use, bool):
        use_agent = raw_use
    elif isinstance(raw_use, int):  # bool is filtered above
        if raw_use not in (0, 1):
            return JsonResponse(
                {'ok': False, 'error': 'use_agent must be true or false.'},
                status=400,
            )
        use_agent = bool(raw_use)
    elif isinstance(raw_use, str):
        v = raw_use.strip().lower()
        if v in ('true', 'on', 'yes', '1'):
            use_agent = True
        elif v in ('false', 'off', 'no', '0'):
            use_agent = False
        else:
            return JsonResponse(
                {'ok': False, 'error': 'use_agent must be true or false.'},
                status=400,
            )
    else:
        return JsonResponse(
            {'ok': False, 'error': 'use_agent must be true or false.'},
            status=400,
        )

    from .db import set_system_setting
    set_system_setting('accela_scraper_agent_model',           model)
    set_system_setting('accela_scraper_use_agent',             'on' if use_agent else 'off')
    saved_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    set_system_setting('accela_scraper_agent_settings_saved_at', saved_at)

    return JsonResponse({'ok': True, 'saved_at': saved_at,
                         'use_agent': use_agent})


@admin_required
def admin_scraper_run_status(request, rid):
    """Polling endpoint for the progress modal. Returns the current
    run row as JSON so the modal can update its progress bar.

    Orphan detection: if the row says ``status='running'`` but the
    worker process is gone (PID mismatch after a server restart, or
    the thread is no longer alive), we finalize the row as
    ``cancelled`` on the spot so the very next poll resets the UI —
    the admin gets the Run button back instead of staring at a
    forever-spinning "Running…".
    """
    from .db import get_scraper_run
    run = get_scraper_run(rid)
    if not run:
        return JsonResponse({'ok': False, 'error': 'run not found'}, status=404)
    if (run.get('status') or '').lower() in ('running', 'queued') \
            and not run.get('finished_at'):
        from .scraper_accela import finalize_orphan_run
        if finalize_orphan_run(rid, reason='orphaned worker detected by status poll'):
            run = get_scraper_run(rid) or run
    if run.get('finished_at'):
        final_status = (run.get('status') or '').strip().lower()
        if final_status in ('success', 'partial', 'failed', 'cancelled'):
            try:
                from .db import get_scraper, update_scraper
                scraper_id = int(run.get('scraper_id'))
                scraper = get_scraper(scraper_id) or {}
                if (scraper.get('last_run_status') or '').strip().lower() == 'running':
                    update_scraper(scraper_id, last_run_at=run.get('finished_at'),
                                   last_run_status=final_status)
            except Exception:
                logging.exception('scraper status reconcile failed for run_id=%s', rid)
    total = max(1, int(run.get('total_targets') or 1))
    processed = int(run.get('processed') or 0)
    pct = round(100 * processed / total, 1) if total else 0
    return JsonResponse({
        'ok':               True,
        'run_id':           run['id'],
        'status':           run.get('status'),
        'mode':             run.get('mode'),
        'total':            total,
        'processed':        processed,
        'succeeded':        int(run.get('succeeded') or 0),
        'failed':           int(run.get('failed') or 0),
        'pct':              pct,
        'current_step':     run.get('current_step') or '',
        'errors':           run.get('error') or [],
        'step_log':         run.get('step_log') or [],
        'finished':         bool(run.get('finished_at')),
        'cancel_requested': bool(run.get('cancel_requested')),
        'worker_pid':       run.get('worker_pid'),
        'worker_tid':       run.get('worker_tid'),
    })


@admin_required
@require_http_methods(['POST'])
def admin_scraper_run_cancel(request, rid):
    """One-button stop. Does ALL of the work the old Stop + Force-stop
    pair used to share so the admin only ever needs one button:

      1. If the row is orphaned (server restart / crashed worker) →
         finalise as cancelled immediately. Cooperative flag has no
         reader, hard-kill has no target — just close the row.
      2. Otherwise flip ``cancel_requested`` so the worker exits at the
         next safe point (between pages / per-detail extractions).
      3. ALSO inject ``SystemExit`` into the worker thread so the run
         stops "right now" instead of after the current LLM call
         returns (~10-90s wait that frustrated the admin into mashing
         the Stop button repeatedly). The cooperative flag from step
         2 still wins if the injection doesn't land — both signals
         are safe to set together.
      4. Once injection lands we also write ``status='cancelled'`` +
         ``finished_at`` ourselves so the polling UI un-wedges
         instantly even if the dying thread can't write its own
         finalise.
    """
    from .db import request_cancel_scraper_run, update_scraper_run
    from .scraper_accela import finalize_orphan_run, force_kill_run_thread
    from datetime import datetime as _dt
    # 1. Orphan path — no live worker to honour any signal.
    if finalize_orphan_run(rid, reason='orphaned worker (server restart)'):
        return JsonResponse({'ok': True, 'msg': 'Previous run was '
                             'orphaned by a server restart — finalised '
                             'as cancelled. You can start a new run.'})
    # 2. Cooperative cancel.
    result = request_cancel_scraper_run(rid)
    if not result['ok']:
        if result.get('already_finished'):
            return JsonResponse({
                'ok': False,
                'error': f'Run is already {result["status"] or "finished"}.',
            }, status=409)
        return JsonResponse({'ok': False, 'error': 'Run not found.'},
                            status=404)
    # 3. Hard thread-kill so the worker dies now instead of waiting
    #    for the next cooperative checkpoint.
    kill = force_kill_run_thread(rid)
    if kill.get('ok'):
        # 4. Finalise immediately — the dying thread's finally-block may
        #    not get the chance.
        try:
            update_scraper_run(
                rid,
                status='cancelled',
                finished_at=_dt.utcnow(),
                current_step='stopped by admin',
            )
        except Exception:
            pass
        return JsonResponse({'ok': True,
                             'msg': 'Run stopped.',
                             'force_killed': True})
    # Thread injection didn't land (different worker process on a
    # multi-worker deploy, or thread already gone). The cooperative
    # flag is still set so the run will finalise on its own; tell the
    # admin honestly.
    return JsonResponse({'ok': True,
                         'msg': 'Stop requested — finalising at the '
                                'next safe point.',
                         'force_killed': False})


@admin_required
@require_http_methods(['POST'])
def admin_scraper_run_force_stop(request, rid):
    """Hard-stop: flips ``cancel_requested`` AND injects ``SystemExit``
    into the worker thread (when it lives in this gunicorn worker).

    The cooperative flag is always set first so the run is guaranteed
    to finalise as 'cancelled' even if the thread injection misses
    (e.g. multi-worker deploy where the thread lives in a sibling
    process). The response payload tells the admin which path took
    effect so they can `kill -TERM <pid>` from the box if needed.
    """
    from .db import request_cancel_scraper_run, get_scraper_run, \
        update_scraper_run
    from .scraper_accela import force_kill_run_thread
    from datetime import datetime as _dt
    # 1. Always flip cooperative flag — cheapest + most reliable signal.
    coop = request_cancel_scraper_run(rid)
    if not coop['ok'] and coop.get('already_finished'):
        return JsonResponse({
            'ok': False,
            'error': f'Run is already {coop.get("status") or "finished"}.',
        }, status=409)
    if not coop['ok']:
        return JsonResponse({'ok': False, 'error': 'Run not found.'},
                            status=404)
    # 2. Try to inject SystemExit into the live thread.
    kill = force_kill_run_thread(rid)
    pid = kill.get('pid')
    tid = kill.get('tid')
    # 3. If injection landed, also finalise the row immediately so the
    #    UI doesn't keep polling — the dying thread might not get a
    #    chance to write `finished_at` itself if SystemExit fires
    #    inside a context where the finally-block also raises.
    if kill['ok']:
        try:
            update_scraper_run(
                rid,
                status='cancelled',
                finished_at=_dt.utcnow(),
                current_step='force-stopped by admin',
            )
        except Exception:
            pass
    msg = ('Force-stop injected into worker thread.'
           if kill['ok'] else
           f"Cooperative cancel flagged. Hard-stop skipped: {kill['reason']}.")
    if pid and not kill['ok']:
        msg += f' Use `kill -TERM {pid}` on the host if it stays wedged.'
    return JsonResponse({
        'ok':           True,
        'msg':          msg,
        'force_killed': kill['ok'],
        'reason':       kill['reason'],
        'pid':          pid,
        'tid':          tid,
    })


@admin_required
@require_http_methods(['POST'])
def admin_scraper_run_kill_process(request, rid):
    """Nuclear option: send SIGTERM to the worker process.

    The server restarts automatically (Replit workflow / gunicorn master)
    and ``sweep_orphan_runs()`` on startup cleans up any remaining stuck
    rows. The DB row is finalised *before* the kill so the admin sees
    an immediate "cancelled" status.
    """
    from .scraper_accela import kill_run_process
    result = kill_run_process(rid)
    if not result['ok']:
        reason = result.get('reason', '')
        if 'not_found' in reason:
            return JsonResponse({'ok': False, 'error': 'Run not found.'},
                                status=404)
        return JsonResponse({
            'ok': False,
            'error': f'Cannot kill: {reason}.',
        }, status=409)
    pid = result.get('pid')
    if result['reason'] == 'no_pid_finalised':
        msg = 'No PID on record — row finalised as cancelled.'
    else:
        msg = (f'SIGTERM scheduled for PID {pid}. The server will '
               f'restart automatically in ~1 second.')
    return JsonResponse({'ok': True, 'msg': msg, 'pid': pid})


# ── "Run cron now" — kick all enabled scrapers in one click ─────────
#
# Wraps the scripts/run_scrapers.py cron entrypoint behind an admin
# button so we can fire a manual cron pass without ssh'ing into the
# box. Each child run is recorded with kind='cron'/mode='cron' so the
# stats page reports it the same as the scheduled job.
#
# Architecture notes:
#   • Coordinator runs in a daemon thread on whichever gunicorn worker
#     handled the POST. It writes batch+child progress only to
#     Postgres, so the polling endpoint can be served by ANY worker
#     (production runs >1 worker — in-memory state would leak).
#   • Children are launched serially via run_scraper_async() and
#     awaited via DB polling, mirroring scripts/run_scrapers.py so
#     manual + scheduled behaviour stay identical.
#   • A 30-min per-child timeout caps a stuck scrape request so
#     the whole batch can't pin one worker forever.

_CRON_BATCH_PER_RUN_MAX_SECONDS = 1800   # 30 min — same as scripts/run_scrapers.py
_CRON_BATCH_POLL_SECONDS        = 2.0
_BATCH_CANCEL: set[int]         = set()  # batch_ids flagged for cancellation (in-proc fallback)


def _cron_batch_is_stopping(batch_id: int) -> bool:
    """DB-poll cooperative-cancel flag. The subprocess coordinator
    checks this between scrapers so admin_stop_all_scrapers can halt
    a Run-All / cron batch across process boundaries."""
    from .db import pg as _pg
    try:
        row = _pg.query_one(
            "SELECT status FROM cron_batches WHERE id = %s",
            (int(batch_id),),
        )
    except Exception:
        return False
    return bool(row and (row.get('status') or '').lower() == 'stopping')


def _cron_batch_pid_alive(pid_value) -> bool:
    """Liveness check for a subprocess coordinator. ``pid_value`` is
    whatever was stamped into ``cron_batches.coordinator_pid``
    (BIGINT)."""
    if pid_value in (None, '', 0):
        return False
    try:
        pid = int(pid_value)
    except (ValueError, TypeError):
        return False
    if pid <= 0:
        return False
    import os as _os
    try:
        _os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _spawn_batch_subprocess(batch_id: int, *, kind: str,
                            concurrency: int | None = None,
                            scraper_ids: list | None = None,
                            max_pages: int | None = None) -> int:
    """Fork a ``manage.py run_scrapers_batch`` subprocess that owns the
    coordinator for ``batch_id``. Returns the child PID. The child
    detaches via ``start_new_session=True`` so it survives the parent
    Django process being killed (dev-server reload, workflow restart,
    DO App Platform deploy). Mirrors ``admin_finder_batch_start``."""
    import subprocess as _sp
    import sys as _sys
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [_sys.executable, 'manage.py', 'run_scrapers_batch',
           str(int(batch_id)), '--kind', kind]
    if concurrency is not None:
        cmd += ['--concurrency', str(int(concurrency))]
    if scraper_ids:
        cmd += ['--scraper-ids', ','.join(str(int(s)) for s in scraper_ids)]
    if max_pages is not None:
        cmd += ['--max-pages', str(int(max_pages))]
    env = os.environ.copy()
    env.setdefault('DJANGO_DEBUG', '1')
    env['DJANGO_SETTINGS_MODULE'] = 'permitdaily.settings'
    log_dir = os.path.join(project_root, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f'scrapers_batch_{int(batch_id)}.log')
    proc = _sp.Popen(
        cmd,
        start_new_session=True,
        stdout=_sp.DEVNULL,
        stderr=open(log_path, 'w'),
        cwd=project_root,
        env=env,
    )
    # Best-effort early stamp — the child also stamps its own PID
    # first thing in run_scrapers_batch.handle() but doing it here
    # too closes the race where admin_active_batch fires before the
    # child has gotten past Django bootstrap.
    try:
        from .db import update_cron_batch
        update_cron_batch(int(batch_id), coordinator_pid=int(proc.pid))
    except Exception:
        logging.exception('spawn_batch_subprocess: early PID stamp failed '
                          'for batch=%s', batch_id)
    return int(proc.pid)


def _wait_for_scraper_run(run_id: int, *, max_wait_seconds: int) -> dict | None:
    """Block until ``scraper_runs.finished_at`` is set or timeout. Used
    by the cron coordinator to march through children one at a time."""
    import time as _time
    from .db import get_scraper_run
    deadline = _time.time() + max_wait_seconds
    while _time.time() < deadline:
        row = get_scraper_run(run_id)
        if row and row.get('finished_at'):
            return row
        _time.sleep(_CRON_BATCH_POLL_SECONDS)
    # Timed out — return whatever we have so the caller can record it
    # as "did not finish" and move on.
    return get_scraper_run(run_id)


def _run_cron_batch_worker(batch_id: int) -> None:
    """Daemon thread body. Runs every enabled scraper with up to
    ``scrapers_default_threads`` (system setting, default 5, clamped
    1-10) in flight at once via a ThreadPoolExecutor. All progress is
    persisted to Postgres so the polling endpoint works cross-worker.

    Before kicking anything we ALWAYS reset stuck `last_run_status =
    'running'` rows to 'idle'. The cron-trigger concurrency guard
    already proved no other cron batch is in flight, so any scraper
    flagged 'running' here is stale (server restart killed its worker
    before _finalize ran). Without the reset those scrapers would
    keep getting skipped forever as "already running"."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .db import (
        list_enabled_scrapers_all,
        update_cron_batch, append_cron_batch_run_id,
        reap_stale_scraper_runs, reset_stuck_running_scrapers,
        get_system_setting,
    )
    from .scraper_accela import run_scraper_async
    from datetime import datetime as _dt
    try:
        # Reap once at the start so a previous half-dead run doesn't
        # block the new pass — defensive, the list view also reaps.
        try:
            reap_stale_scraper_runs(30)
        except Exception:
            logging.exception('cron-batch %s: reaper failed (non-fatal)', batch_id)
        # Flip every stale 'running' scraper back to 'idle' so this
        # cron pass can actually pick them up. Per user request:
        # "when no cron is running, set status to idle so the cron
        # will then set all idle and run N at the same time."
        try:
            n_reset = reset_stuck_running_scrapers()
            if n_reset:
                logging.info(
                    'cron-batch %s: reset %d stuck running → idle',
                    batch_id, n_reset,
                )
        except Exception:
            logging.exception(
                'cron-batch %s: reset_stuck_running_scrapers failed (non-fatal)',
                batch_id,
            )

        # Concurrency — driven by the same DB-backed knob as the
        # toolbar Threads input. Default 5; clamp 1-10 so a bad value
        # can't fork a thread bomb.
        try:
            concurrency = int(get_system_setting('scrapers_default_threads', 5) or 5)
        except (TypeError, ValueError):
            concurrency = 5
        concurrency = max(1, min(concurrency, 10))

        # Same global "# of pages" cap as the per-row Run / Run-All
        # paths — keeps every entry point consistent.
        try:
            max_pages = int(get_system_setting('scrapers_default_pages', 50) or 50)
        except (TypeError, ValueError):
            max_pages = 50
        max_pages = max(1, min(max_pages, 1000))

        # Use the un-paginated query so a future fleet of >100 enabled
        # scrapers doesn't get silently truncated by list_scrapers()'s
        # 100/page clamp.
        enabled = list_enabled_scrapers_all()
        if not enabled:
            update_cron_batch(
                batch_id,
                status='success',
                finished_at=_dt.utcnow(),
                note='no enabled scrapers — nothing to run',
            )
            return

        ok = 0
        fail = 0

        def _run_one(scraper):
            sid = int(scraper['id'])
            try:
                run_id = run_scraper_async(
                    sid, mode='cron', kind='cron',
                    count=20, max_pages=max_pages,
                )
            except Exception:
                logging.exception(
                    'cron-batch %s: failed to kick scraper #%d', batch_id, sid,
                )
                return scraper, None
            try:
                append_cron_batch_run_id(batch_id, run_id)
            except Exception:
                logging.exception(
                    'cron-batch %s: append run_id %d failed (non-fatal)',
                    batch_id, run_id,
                )
            return scraper, _wait_for_scraper_run(
                run_id, max_wait_seconds=_CRON_BATCH_PER_RUN_MAX_SECONDS,
            )

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(_run_one, s) for s in enabled]
            for fut in as_completed(futures):
                try:
                    _scraper, finished = fut.result()
                except Exception:
                    logging.exception(
                        'cron-batch %s: worker future raised', batch_id,
                    )
                    fail += 1
                    continue
                if not finished or not finished.get('finished_at'):
                    fail += 1
                    continue
                status = (finished.get('status') or '').strip().lower()
                if status == 'success':
                    ok += 1
                else:
                    fail += 1

        final_status = (
            'success' if fail == 0 and ok > 0 else
            'failed'  if ok == 0 else
            'partial'
        )
        update_cron_batch(
            batch_id,
            status=final_status,
            finished_at=_dt.utcnow(),
            note=f'done — {ok} succeeded, {fail} failed',
        )
    except Exception:
        logging.exception('cron-batch %s: coordinator crashed', batch_id)
        try:
            update_cron_batch(
                batch_id,
                status='failed',
                finished_at=_dt.utcnow(),
                note='coordinator crashed — see server logs',
            )
        except Exception:
            pass


@admin_required
@require_http_methods(['POST'])
def admin_run_cron_now(request):
    """Kick a manual cron pass — runs every enabled scraper in series
    with kind='cron'/mode='cron' so it's indistinguishable from the
    scheduled job. Returns the batch_id immediately so the UI can
    start polling /admin-panel/scrapers/cron/batch/<id>/."""
    from .db import create_cron_batch
    try:
        kicked_by = request.session.get('user_id')
        batch_id = create_cron_batch(kicked_by=kicked_by)
    except Exception:
        logging.exception('admin_run_cron_now: failed to create batch row')
        return JsonResponse(
            {'ok': False, 'error': 'Could not start cron batch — check server logs.'},
            status=500,
        )
    try:
        _spawn_batch_subprocess(batch_id, kind='cron')
    except Exception:
        logging.exception('admin_run_cron_now: failed to spawn subprocess')
        try:
            from .db import update_cron_batch
            from datetime import datetime as _dt
            update_cron_batch(batch_id, status='failed', finished_at=_dt.utcnow(),
                              note='failed to spawn coordinator subprocess')
        except Exception:
            pass
        return JsonResponse({'ok': False,
                             'error': 'Could not spawn coordinator subprocess.'},
                            status=500)
    return JsonResponse({'ok': True, 'batch_id': batch_id})


# ── "Run All Scrapers" — concurrent batch from the scrapers list ────
#
# Unlike the serial cron batch (which marches through scrapers one at a
# time), this worker runs up to N scrapers concurrently so the admin
# doesn't have to wait 3+ hours for 60 scrapers to finish serially.

_RUN_ALL_CONCURRENCY = 5

def _run_all_batch_worker(batch_id: int, concurrency: int = _RUN_ALL_CONCURRENCY,
                          scraper_ids: list | None = None,
                          max_pages: int | None = None) -> None:
    """Daemon thread body. Runs scrapers with up to ``concurrency`` in
    flight at once. By default runs every enabled scraper; if
    ``scraper_ids`` is supplied, runs exactly that subset (admin's
    explicit "Run selected" choice — honoured even for disabled rows).
    Progress is persisted to ``cron_batches`` + individual
    ``scraper_runs`` rows so the polling endpoint works cross-worker."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .db import (
        list_enabled_scrapers_all, list_scrapers_by_ids, get_scraper_run,
        update_cron_batch, append_cron_batch_run_id,
        reap_stale_scraper_runs,
    )
    from .scraper_accela import run_scraper_async
    from datetime import datetime as _dt, timedelta as _td
    try:
        try:
            reap_stale_scraper_runs(30)
        except Exception:
            logging.exception('run-all %s: reaper failed (non-fatal)', batch_id)

        if scraper_ids:
            # "Run selected" — honour the admin's explicit pick, but a
            # disabled scraper is OFF: skip any selected rows that aren't
            # enabled so "disabled never runs" holds for this path too.
            enabled = [s for s in list_scrapers_by_ids(scraper_ids)
                       if s.get('enabled')]
            empty_note = 'no enabled scrapers in selection — nothing to run'
        else:
            enabled = list_enabled_scrapers_all()
            empty_note = 'no enabled scrapers — nothing to run'
        if not enabled:
            update_cron_batch(
                batch_id,
                status='success',
                finished_at=_dt.utcnow(),
                note=empty_note,
            )
            return

        import json as _json
        all_sids = [int(s['id']) for s in enabled]  # captured early for crash handler
        update_cron_batch(
            batch_id,
            note=_json.dumps({'done': 0, 'total': len(enabled), 'scraper_ids': all_sids}),
        )

        _today = _dt.utcnow().date()
        _default_date_to   = _today.isoformat()
        _default_date_from = (_today - _td(days=7)).isoformat()

        ok = 0
        fail = 0

        def _run_one(scraper):
            # Cooperative cancel — checked BEFORE kicking the child so
            # a stopped batch doesn't keep spawning new scraper runs.
            # In-proc set kept as fast-path; cross-proc stop comes via
            # the DB-poll flag flipped by admin_stop_all_scrapers.
            if batch_id in _BATCH_CANCEL or _cron_batch_is_stopping(batch_id):
                return scraper, None
            sid = int(scraper['id'])
            run_id = run_scraper_async(
                sid, mode='single', kind='cron',
                date_from=_default_date_from,
                date_to=_default_date_to,
                max_pages=max_pages,
            )
            try:
                append_cron_batch_run_id(batch_id, run_id)
            except Exception:
                logging.exception('run-all %s: append run_id %d failed', batch_id, run_id)
            finished = _wait_for_scraper_run(
                run_id, max_wait_seconds=_CRON_BATCH_PER_RUN_MAX_SECONDS,
            )
            return scraper, finished

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_run_one, s): s for s in enabled}
            done_count = 0
            for fut in as_completed(futures):
                done_count += 1
                try:
                    scraper, finished = fut.result()
                except Exception:
                    fail += 1
                    logging.exception('run-all %s: scraper future crashed', batch_id)
                    try:
                        update_cron_batch(
                            batch_id,
                            note=_json.dumps({'done': done_count, 'total': len(enabled), 'scraper_ids': all_sids}),
                        )
                    except Exception:
                        pass
                    continue
                if not finished or not finished.get('finished_at'):
                    fail += 1
                else:
                    status = (finished.get('status') or '').strip().lower()
                    if status == 'success':
                        ok += 1
                    else:
                        fail += 1
                try:
                    update_cron_batch(
                        batch_id,
                        note=_json.dumps({'done': done_count, 'total': len(enabled), 'scraper_ids': all_sids}),
                    )
                except Exception:
                    pass

        was_cancelled = (batch_id in _BATCH_CANCEL
                         or _cron_batch_is_stopping(batch_id))
        _BATCH_CANCEL.discard(batch_id)

        if was_cancelled:
            final_status = 'cancelled'
            summary_text = f'cancelled by admin — {ok} succeeded, {fail} failed before stop'
        else:
            final_status = (
                'success' if fail == 0 and ok > 0 else
                'failed'  if ok == 0 else
                'partial'
            )
            summary_text = f'done — {ok} succeeded, {fail} failed'
        update_cron_batch(
            batch_id,
            status=final_status,
            finished_at=_dt.utcnow(),
            note=_json.dumps({
                'done': done_count, 'total': len(enabled),
                'scraper_ids': all_sids,
                'summary': summary_text,
            }),
        )
    except Exception:
        _BATCH_CANCEL.discard(batch_id)
        logging.exception('run-all %s: coordinator crashed', batch_id)
        try:
            update_cron_batch(
                batch_id,
                status='failed',
                finished_at=_dt.utcnow(),
                note=_json.dumps({
                    'done': 0, 'total': 0,
                    'scraper_ids': all_sids if 'all_sids' in locals() else [],
                    'summary': 'coordinator crashed — see server logs',
                }),
            )
        except Exception:
            pass


@admin_required
@require_http_methods(['POST'])
def admin_run_all_scrapers(request):
    """Run All Scrapers — concurrent batch from the scrapers list page.
    Accepts an optional ``concurrency`` param (1–10, default 5).
    Returns the batch_id for polling via ``admin_cron_batch_status``."""
    from .db import create_cron_batch
    try:
        concurrency = int(request.POST.get('concurrency') or _RUN_ALL_CONCURRENCY)
        concurrency = max(1, min(concurrency, 10))
    except (TypeError, ValueError):
        concurrency = _RUN_ALL_CONCURRENCY
    # Global "# of pages" from the toolbar. None => worker leaves it
    # unset, run_scraper_async falls back to ACCELA_MAX_PAGES_DEFAULT.
    max_pages = _parse_global_max_pages(request)
    try:
        kicked_by = request.session.get('user_id')
        batch_id = create_cron_batch(kicked_by=kicked_by)
    except Exception:
        logging.exception('admin_run_all_scrapers: failed to create batch row')
        return JsonResponse(
            {'ok': False, 'error': 'Could not start batch — check server logs.'},
            status=500,
        )
    try:
        _spawn_batch_subprocess(batch_id, kind='run-all',
                                concurrency=concurrency,
                                max_pages=max_pages)
    except Exception:
        logging.exception('admin_run_all_scrapers: failed to spawn subprocess')
        try:
            from .db import update_cron_batch
            from datetime import datetime as _dt
            update_cron_batch(batch_id, status='failed', finished_at=_dt.utcnow(),
                              note='failed to spawn coordinator subprocess')
        except Exception:
            pass
        return JsonResponse({'ok': False,
                             'error': 'Could not spawn coordinator subprocess.'},
                            status=500)
    return JsonResponse({'ok': True, 'batch_id': batch_id,
                         'concurrency': concurrency, 'max_pages': max_pages})


@admin_required
@require_http_methods(['POST'])
def admin_run_selected_scrapers(request):
    """Run a caller-supplied subset of scrapers concurrently. Same
    contract as ``admin_run_all_scrapers`` but the worker is given an
    explicit ``scraper_ids`` list instead of pulling enabled rows from
    the DB. Accepts ``ids=`` repeated (form-encoded) OR comma-separated
    plus optional ``concurrency`` (1-10, default 5). Returns the
    ``batch_id`` so the same polling UI can show progress."""
    from .db import create_cron_batch
    raw = request.POST.getlist('ids')
    if not raw:
        raw = (request.POST.get('ids') or '').split(',')
    ids: list[int] = []
    for tok in raw:
        tok = (tok or '').strip()
        if tok.isdigit():
            ids.append(int(tok))
    if not ids:
        return JsonResponse({'ok': False, 'error': 'No scraper ids provided.'},
                            status=400)
    try:
        concurrency = int(request.POST.get('concurrency') or _RUN_ALL_CONCURRENCY)
        concurrency = max(1, min(concurrency, 10))
    except (TypeError, ValueError):
        concurrency = _RUN_ALL_CONCURRENCY
    max_pages = _parse_global_max_pages(request)
    try:
        kicked_by = request.session.get('user_id')
        batch_id = create_cron_batch(kicked_by=kicked_by)
    except Exception:
        logging.exception('admin_run_selected_scrapers: failed to create batch row')
        return JsonResponse(
            {'ok': False, 'error': 'Could not start batch — check server logs.'},
            status=500,
        )
    try:
        _spawn_batch_subprocess(batch_id, kind='run-all',
                                concurrency=concurrency,
                                scraper_ids=ids,
                                max_pages=max_pages)
    except Exception:
        logging.exception('admin_run_selected_scrapers: failed to spawn subprocess')
        try:
            from .db import update_cron_batch
            from datetime import datetime as _dt
            update_cron_batch(batch_id, status='failed', finished_at=_dt.utcnow(),
                              note='failed to spawn coordinator subprocess')
        except Exception:
            pass
        return JsonResponse({'ok': False,
                             'error': 'Could not spawn coordinator subprocess.'},
                            status=500)
    return JsonResponse({'ok': True, 'batch_id': batch_id,
                         'concurrency': concurrency, 'count': len(ids),
                         'max_pages': max_pages})


def _parse_global_max_pages(request):
    """Parse the toolbar's "# of pages" POST param into a clamped int
    (1-1000). Returns None when missing/invalid so the worker can fall
    back to ACCELA_MAX_PAGES_DEFAULT instead of guessing."""
    raw = (request.POST.get('max_pages') or '').strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return max(1, min(v, 1000))


@admin_required
@require_http_methods(['POST'])
def admin_scrapers_save_default_pages(request):
    """Persist the toolbar's "# of pages" knob to
    ``system_settings.scrapers_default_pages`` so the value survives
    reloads and applies across admin sessions. Auto-called by the
    toolbar input's change/blur handler — no Save button."""
    from .db import set_system_setting
    raw = (request.POST.get('pages') or '').strip()
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'pages must be an integer'},
                            status=400)
    v = max(1, min(v, 1000))
    try:
        set_system_setting('scrapers_default_pages', str(v))
    except Exception:
        logging.exception('admin_scrapers_save_default_pages: write failed')
        return JsonResponse(
            {'ok': False, 'error': 'Could not save — check server logs.'},
            status=500,
        )
    return JsonResponse({'ok': True, 'pages': v})


@admin_required
@require_http_methods(['POST'])
def admin_scrapers_save_default_threads(request):
    """Persist the toolbar's "Threads" knob to
    ``system_settings.scrapers_default_threads`` so the cron worker
    (and any future entry point) can read the same value the admin
    set in the UI. Clamped 1-10 to match the input. Auto-called by
    the toolbar input's change/blur handler — no Save button."""
    from .db import set_system_setting
    raw = (request.POST.get('threads') or '').strip()
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'threads must be an integer'},
                            status=400)
    v = max(1, min(v, 10))
    try:
        set_system_setting('scrapers_default_threads', str(v))
    except Exception:
        logging.exception('admin_scrapers_save_default_threads: write failed')
        return JsonResponse(
            {'ok': False, 'error': 'Could not save — check server logs.'},
            status=500,
        )
    return JsonResponse({'ok': True, 'threads': v})


@admin_required
def admin_active_batch(request):
    """Return the most recent unfinished Run-All batch (if any).
    Called on page load so the UI can resume polling.  If the
    coordinator thread is dead (server restart), finalize the
    orphaned batch and sweep stuck runs so the button un-locks."""
    from .db import pg as _pg, update_cron_batch
    from datetime import datetime as _dt
    # 'stopping' is included so a batch that's actively being torn
    # down by admin_stop_all_scrapers still shows up in the UI until
    # the subprocess writes its final 'cancelled' row.
    row = _pg.query_one(
        "SELECT id, coordinator_pid FROM cron_batches "
        "WHERE finished_at IS NULL AND status IN ('running', 'stopping') "
        "ORDER BY id DESC LIMIT 1"
    )
    if row:
        bid = int(row['id'])
        pid = row.get('coordinator_pid')
        # Coordinator now lives in a detached subprocess (spawned via
        # _spawn_batch_subprocess). Liveness = os.kill(pid, 0). A NULL
        # pid means the subprocess hasn't stamped yet — grace-period
        # check below by ``started_at`` so a racing first poll doesn't
        # nuke a healthy batch that's still booting Django.
        alive = _cron_batch_pid_alive(pid)
        if not alive and pid is None:
            # Boot grace period — Django startup in a fresh process
            # can take 5-10s before run_scrapers_batch.handle() stamps
            # the PID. Treat any batch < 30s old with no PID as alive.
            try:
                started = _pg.query_one(
                    "SELECT started_at FROM cron_batches WHERE id = %s",
                    (bid,),
                )
                if started and started.get('started_at'):
                    age = (_dt.utcnow().replace(tzinfo=started['started_at'].tzinfo)
                           - started['started_at']).total_seconds()
                    if age < 30:
                        alive = True
            except Exception:
                logging.exception('admin_active_batch: grace-period check failed')
        if not alive:
            try:
                update_cron_batch(bid, status='cancelled',
                                  finished_at=_dt.utcnow(),
                                  note='batch coordinator gone (server restart)')
            except Exception:
                logging.exception('admin_active_batch: finalize failed for %s', bid)
            from .scraper_accela import sweep_orphan_runs
            try:
                sweep_orphan_runs()
            except Exception:
                logging.exception('admin_active_batch: sweep failed')
            return JsonResponse({'ok': True, 'batch_id': None})
        return JsonResponse({'ok': True, 'batch_id': bid})
    return JsonResponse({'ok': True, 'batch_id': None})


@admin_required
@require_http_methods(['POST'])
def admin_stop_all_scrapers(request):
    """Stop the currently-running Run-All batch.  Sets the in-process
    cancel flag (so queued futures exit early) AND cooperatively cancels
    every child run that's still in-flight."""
    from .db import pg as _pg, get_cron_batch, get_scraper_run, \
        request_cancel_scraper_run, update_cron_batch
    from .scraper_accela import finalize_orphan_run
    row = _pg.query_one(
        "SELECT id FROM cron_batches "
        "WHERE finished_at IS NULL AND status IN ('running', 'stopping') "
        "ORDER BY id DESC LIMIT 1"
    )
    if not row:
        return JsonResponse({'ok': False, 'error': 'No active batch to stop.'},
                            status=404)
    bid = int(row['id'])
    # In-proc fast-path (for any legacy thread coordinator that's
    # still in this process) AND cross-process flag (the subprocess
    # coordinator polls cron_batches.status between scrapers).
    _BATCH_CANCEL.add(bid)
    try:
        update_cron_batch(bid, status='stopping')
    except Exception:
        logging.exception('admin_stop_all_scrapers: status=stopping write '
                          'failed for batch=%s', bid)
    batch = get_cron_batch(bid)
    cancelled = 0
    for rid in (batch.get('run_ids') or []):
        rid = int(rid)
        try:
            run = get_scraper_run(rid)
        except Exception:
            continue
        if not run or run.get('finished_at'):
            continue
        if finalize_orphan_run(rid, reason='batch stopped by admin'):
            cancelled += 1
            continue
        result = request_cancel_scraper_run(rid)
        if result.get('ok'):
            cancelled += 1
    return JsonResponse({
        'ok': True,
        'msg': f'Batch #{bid} stopping — {cancelled} run(s) cancelled.',
    })


@admin_required
@require_http_methods(['POST'])
def admin_stop_scraper_latest_run(request, sid):
    """Stop the most recent running run for a single scraper.  If the
    run is orphaned (PID gone), finalise it immediately.  If no running
    run exists but the scraper status is stuck on 'running', reset it."""
    from .db import pg as _pg, request_cancel_scraper_run, \
        get_scraper as _get_scraper, update_scraper as _update_scraper
    from .scraper_accela import finalize_orphan_run
    row = _pg.query_one(
        "SELECT id FROM scraper_runs "
        "WHERE scraper_id = %s AND status = 'running' "
        "AND finished_at IS NULL ORDER BY id DESC LIMIT 1",
        (int(sid),),
    )
    if not row:
        scraper = _get_scraper(int(sid))
        if scraper and (scraper.get('last_run_status') or '').lower() == 'running':
            _update_scraper(int(sid), last_run_status='cancelled')
            return JsonResponse({'ok': True,
                                 'msg': 'Stale running status cleared.'})
        return JsonResponse({'ok': False,
                             'error': 'No running task to stop.'},
                            status=404)
    rid = int(row['id'])
    if finalize_orphan_run(rid, reason='stopped by admin from list'):
        return JsonResponse({'ok': True,
                             'msg': 'Orphaned run cancelled.'})
    result = request_cancel_scraper_run(rid)
    if result.get('ok'):
        return JsonResponse({'ok': True,
                             'msg': 'Stop requested — will halt at the '
                                    'next safe point.'})
    return JsonResponse({'ok': False,
                         'error': 'Could not stop the run.'},
                        status=409)


@admin_required
def admin_cron_batch_status(request, batch_id):
    """Polling endpoint for the multi-scraper cron terminal panel.
    Returns the batch row plus a live snapshot of every child run."""
    from .db import get_cron_batch, get_scraper_run, get_scraper
    batch = get_cron_batch(batch_id)
    if not batch:
        return JsonResponse({'ok': False, 'error': 'batch not found'}, status=404)
    children = []
    for rid in (batch.get('run_ids') or []):
        try:
            run = get_scraper_run(int(rid))
        except Exception:
            run = None
        if not run:
            continue
        sid = int(run.get('scraper_id') or 0)
        scraper_name = ''
        if sid:
            try:
                sc = get_scraper(sid)
                scraper_name = (sc or {}).get('name') or f'scraper #{sid}'
            except Exception:
                scraper_name = f'scraper #{sid}'
        total = max(1, int(run.get('total_targets') or 1))
        processed = int(run.get('processed') or 0)
        pct = round(100 * processed / total, 1) if total else 0.0
        # Cap step_log to last 60 lines so a long backfill transcript
        # doesn't bloat each poll. The per-scraper detail page is the
        # place to read full history.
        step_log = run.get('step_log') or []
        if len(step_log) > 60:
            step_log = step_log[-60:]
        children.append({
            'run_id':       run['id'],
            'scraper_id':   sid,
            'scraper_name': scraper_name,
            'status':       run.get('status'),
            'total':        total,
            'processed':    processed,
            'succeeded':    int(run.get('succeeded') or 0),
            'failed':       int(run.get('failed') or 0),
            'pct':          pct,
            'current_step': run.get('current_step') or '',
            'step_log':     step_log,
            'finished':     bool(run.get('finished_at')),
        })
    note_raw = batch.get('note') or ''
    all_scraper_ids = []
    try:
        note_obj = json.loads(note_raw)
        if isinstance(note_obj, dict):
            all_scraper_ids = note_obj.get('scraper_ids') or []
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return JsonResponse({
        'ok':              True,
        'batch_id':        batch['id'],
        'status':          batch.get('status'),
        'started_at':      batch['started_at'].isoformat() if batch.get('started_at') else None,
        'finished_at':     batch['finished_at'].isoformat() if batch.get('finished_at') else None,
        'note':            note_raw,
        'children':        children,
        'finished':        bool(batch.get('finished_at')),
        'all_scraper_ids': all_scraper_ids,
    })


# ── Scraper Cron sub-page ─────────────────────────────────────────────

_DAYS_OF_WEEK = [
    {'value': 'mon', 'label': 'Mon'},
    {'value': 'tue', 'label': 'Tue'},
    {'value': 'wed', 'label': 'Wed'},
    {'value': 'thu', 'label': 'Thu'},
    {'value': 'fri', 'label': 'Fri'},
    {'value': 'sat', 'label': 'Sat'},
    {'value': 'sun', 'label': 'Sun'},
]
_VALID_DAY_VALUES = {d['value'] for d in _DAYS_OF_WEEK}


def _human_dt(dt):
    """Format a datetime for an admin table — returns '' on falsy."""
    if not dt:
        return ''
    try:
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(dt)


def _human_duration(start, end):
    if not start or not end:
        return ''
    try:
        delta = end - start
        secs = int(delta.total_seconds())
    except Exception:
        return ''
    if secs < 0:
        return ''
    if secs < 60:
        return f'{secs}s'
    if secs < 3600:
        return f'{secs // 60}m {secs % 60}s'
    h = secs // 3600
    m = (secs % 3600) // 60
    return f'{h}h {m}m'


def _load_cron_settings():
    """Read all cron settings into one dict for the template."""
    from .db import get_system_setting
    raw_days = (get_system_setting('scrapers_cron_days') or '').strip()
    days = [d for d in raw_days.split(',') if d in _VALID_DAY_VALUES]
    try:
        count = int(get_system_setting('scrapers_cron_count') or 50)
    except (TypeError, ValueError):
        count = 50
    try:
        window = int(get_system_setting('scrapers_cron_window_minutes') or 30)
    except (TypeError, ValueError):
        window = 30
    return {
        'enabled':        bool(get_system_setting('scrapers_cron_enabled')),
        'at_utc':         (get_system_setting('scrapers_cron_at_utc') or '08:00').strip(),
        'days':           days,
        'count':          max(1, min(count, 500)),
        'window_minutes': max(1, min(window, 720)),
        'saved_at':       (get_system_setting('scrapers_cron_saved_at') or '').strip(),
    }


def _parse_utc_stamp(s: str):
    """Parse the 'YYYY-MM-DD HH:MM:SS UTC' format we write everywhere
    in system_settings. Returns a naive UTC datetime or None."""
    if not s:
        return None
    from datetime import datetime as _dt
    try:
        return _dt.strptime(s.strip(), '%Y-%m-%d %H:%M:%S UTC')
    except (TypeError, ValueError):
        return None


def _human_age(dt) -> str:
    """Render 'just now / 12 minutes ago / 3 hours ago / 2 days ago'."""
    if not dt:
        return ''
    from datetime import datetime as _dt
    try:
        secs = int((_dt.utcnow() - dt).total_seconds())
    except Exception:
        return ''
    if secs < 0:    secs = 0
    if secs < 45:   return 'just now'
    if secs < 90:   return 'a minute ago'
    if secs < 3600: return f'{secs // 60} minutes ago'
    if secs < 5400: return 'an hour ago'
    if secs < 86400:return f'{secs // 3600} hours ago'
    if secs < 172800:return 'a day ago'
    return f'{secs // 86400} days ago'


def _load_cron_health(cron_settings: dict) -> dict:
    """Compute the heartbeat / last-fired view-model for the Cron page.

    Status is one of:
      * ``never``   — script has never been observed (no heartbeat key).
                      External trigger almost certainly not wired up.
      * ``healthy`` — last heartbeat is younger than the expected gap
                      (2× the configured ± window, clamped to 30–180m).
      * ``stale``   — heartbeat exists but is older than that gap. The
                      external trigger has stopped firing, or is firing
                      far less often than the schedule needs.
    """
    from .db import get_system_setting
    hb_at_raw   = (get_system_setting('scrapers_cron_last_heartbeat_at') or '').strip()
    hb_outcome  = (get_system_setting('scrapers_cron_last_heartbeat_outcome') or '').strip()
    fired_at_raw = (get_system_setting('scrapers_cron_last_fired_at') or '').strip()

    hb_dt    = _parse_utc_stamp(hb_at_raw)
    fired_dt = _parse_utc_stamp(fired_at_raw)

    # Expected max gap between heartbeats: 2× the schedule window so a
    # cron that wakes every 5min around an 8:00 ± 30min slot still
    # reads "healthy" outside the window. Clamp to a sane 30m..3h.
    window = int(cron_settings.get('window_minutes') or 30)
    expected_gap_min = max(30, min(window * 2, 180))

    if hb_dt is None:
        status, label = 'never', 'No heartbeat yet'
    else:
        from datetime import datetime as _dt
        age_secs = int((_dt.utcnow() - hb_dt).total_seconds())
        if age_secs <= expected_gap_min * 60:
            status, label = 'healthy', 'Server scheduler is running'
        else:
            status, label = 'stale', 'Server scheduler looks stalled'

    return {
        'status':            status,
        'label':             label,
        'expected_gap_min':  expected_gap_min,
        'heartbeat_at':      hb_at_raw,
        'heartbeat_age':     _human_age(hb_dt),
        'heartbeat_outcome': hb_outcome,
        'last_fired_at':     fired_at_raw,
        'last_fired_age':    _human_age(fired_dt),
    }


@admin_required
def admin_scraper_cron_view(request):
    """Render the Scraper → Cron sub-page: schedule editor, recent
    batches, and the live terminal panel."""
    from .db import recent_cron_batches
    cron = _load_cron_settings()
    raw_batches = recent_cron_batches(limit=20)
    batches = []
    for b in raw_batches:
        batches.append({
            'id':              b['id'],
            'started_human':   _human_dt(b.get('started_at')),
            'finished_human':  _human_dt(b.get('finished_at')),
            'duration_human':  _human_duration(b.get('started_at'), b.get('finished_at')),
            'status':          (b.get('status') or 'queued'),
            'note':            b.get('note') or '',
            'child_count':     int(b.get('child_count') or 0),
        })
    return render(request, 'core/admin_scraper_cron.html', {
        'cron':           cron,
        'health':         _load_cron_health(cron),
        'days_of_week':   _DAYS_OF_WEEK,
        'batches':        batches,
        'cron_trigger_url':     request.build_absolute_uri('/api/v1/scrapers/run-cron/'),
        'cron_trigger_key':     _effective_scraper_key(),
        'cron_trigger_key_set': bool(_effective_scraper_key()),
        # If the env var is set, the in-app input is read-only because
        # writing to system_settings would have no effect (env wins).
        'cron_trigger_key_locked_by_env': bool((os.environ.get('SCRAPER_INGEST_KEY') or '').strip()),
        **_admin_base_ctx(request, 'scraper_cron'),
    })


@admin_required
def admin_cron_heartbeat_status(request):
    """JSON endpoint for the live heartbeat pill on the Cron page.

    The page polls this every 30s so the admin can watch for the next
    external invocation without reloading. Returns the same shape as
    ``_load_cron_health`` plus a small ``ok=True`` envelope.
    """
    cron = _load_cron_settings()
    health = _load_cron_health(cron)
    return JsonResponse({'ok': True, 'health': health})


@admin_required
@require_http_methods(['POST'])
def admin_scraper_cron_save(request):
    """Persist cron schedule settings to system_settings, then redirect
    back to the cron page with a success message."""
    from .db import set_system_setting
    from datetime import datetime as _dt
    enabled = request.POST.get('enabled') == '1'
    at_utc  = (request.POST.get('at_utc') or '08:00').strip()
    # Validate HH:MM, fall back to 08:00 if it's nonsense — never crash.
    if not re.fullmatch(r'\d{1,2}:\d{2}', at_utc):
        at_utc = '08:00'
    else:
        try:
            hh, mm = at_utc.split(':')
            at_utc = f'{int(hh):02d}:{int(mm):02d}'
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                at_utc = '08:00'
        except Exception:
            at_utc = '08:00'

    try:
        count = int(request.POST.get('count') or 50)
    except (TypeError, ValueError):
        count = 50
    count = max(1, min(count, 500))
    try:
        window = int(request.POST.get('window_minutes') or 30)
    except (TypeError, ValueError):
        window = 30
    window = max(1, min(window, 720))

    raw_days = request.POST.getlist('days')
    days = sorted({d for d in raw_days if d in _VALID_DAY_VALUES})

    set_system_setting('scrapers_cron_enabled', enabled)
    set_system_setting('scrapers_cron_at_utc', at_utc)
    set_system_setting('scrapers_cron_count', str(count))
    set_system_setting('scrapers_cron_window_minutes', str(window))
    set_system_setting('scrapers_cron_always_fire', False)
    set_system_setting('scrapers_cron_days', ','.join(days))
    set_system_setting(
        'scrapers_cron_saved_at',
        _dt.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
    )
    return redirect('/admin-panel/scrapers/cron/')


@admin_required
@require_http_methods(['POST'])
def admin_scraper_cron_save_key(request):
    """AJAX endpoint that persists / rotates / clears the
    ``scraper_ingest_key`` in ``system_settings`` so the admin can
    manage the cron-trigger key entirely from the Cron tab (no shell
    access / no platform-secrets dashboard required).

    Body (form-urlencoded JSON or plain form):
      action = 'save' | 'generate' | 'clear'
      key    = (only used with 'save'; ignored otherwise)

    Returns ``{ok, key, key_masked, source}`` on success — the
    plaintext key is returned so the UI can preview the new value
    once after generation; the page is admin-only and served over
    HTTPS, so no extra leakage path is opened.

    Hard-blocked when the env var is set — env always wins, and
    silently persisting a DB value that the auth path would ignore
    would be worse than just rejecting the call.
    """
    from .db import set_system_setting
    if (os.environ.get('SCRAPER_INGEST_KEY') or '').strip():
        return JsonResponse({
            'ok': False,
            'error': 'SCRAPER_INGEST_KEY is set as a server env var — '
                     'clear the env var first or edit it on your host '
                     '(env wins over the in-app value).',
        }, status=409)

    action = (request.POST.get('action') or 'save').strip().lower()
    if action == 'generate':
        new_key = secrets.token_urlsafe(48)  # ~64 chars, URL-safe
        set_system_setting('scraper_ingest_key', new_key)
    elif action == 'clear':
        set_system_setting('scraper_ingest_key', '')
        new_key = ''
    else:  # 'save'
        candidate = (request.POST.get('key') or '').strip()
        if len(candidate) < 24:
            return JsonResponse({
                'ok': False,
                'error': 'Key must be at least 24 characters. '
                         'Click "Generate" for a strong random key.',
            }, status=400)
        if len(candidate) > 256:
            return JsonResponse({'ok': False, 'error': 'Key too long (max 256 chars).'}, status=400)
        set_system_setting('scraper_ingest_key', candidate)
        new_key = candidate

    masked = (new_key[:4] + '…' + new_key[-4:]) if len(new_key) >= 12 else ('•' * len(new_key))
    return JsonResponse({
        'ok': True,
        'key': new_key,
        'key_masked': masked,
        'source': 'db' if new_key else 'unset',
    })


# ── Accela permit-search finder ───────────────────────────────────────
#
# Two endpoints:
#   GET  /admin-panel/scrapers/accela-search/        → the page.
#   POST /admin-panel/scrapers/accela-search/run/    → JSON, one city per
#                                                       call so the
#                                                       browser can
#                                                       update the table
#                                                       progressively.
#
# Per-city flow: local HTTP search plus the configured OSS model picks
# the best Accela URL, then a local browser fetch verifies the page.
# We enforce the accela.com host rule both in the prompt and
# defensively in the Python response handler.

@admin_required
def admin_scraper_accela_search_view(request):
    """Render the Accela permit-search finder page.

    Embeds the curated per-state city list AND the DO Inference finder
    defaults (prompt template, model, max output tokens) so the controls
    at the top of the page render with sane initial values.

    Also embeds the set of (state, city) pairs that ALREADY exist in
    the ``scrapers`` table so the JS can subtract them from the
    pre-populated city list.
    """
    from .us_cities_top import US_STATES, CITIES_BY_STATE
    from .scraper_accela import (
        ACCELA_FINDER_DEFAULT_PROMPT,
        ACCELA_FINDER_DEFAULT_MODEL,
        ACCELA_FINDER_DEFAULT_MAX_TOKENS,
        ACCELA_FINDER_MAX_TOKENS_CAP,
    )
    from .scrapers.base import OSS_MODELS
    from .db import list_scraper_state_city_options

    saved_prompt = (get_system_setting('accela_finder_prompt_template') or '').strip()
    saved_model  = (get_system_setting('accela_finder_model') or '').strip()
    saved_tokens_raw = (get_system_setting('accela_finder_max_tokens') or '').strip()
    saved_at     = (get_system_setting('accela_finder_settings_saved_at') or '').strip()

    initial_prompt = saved_prompt or ACCELA_FINDER_DEFAULT_PROMPT
    initial_model  = saved_model or ACCELA_FINDER_DEFAULT_MODEL
    try:
        initial_tokens = int(saved_tokens_raw) if saved_tokens_raw else ACCELA_FINDER_DEFAULT_MAX_TOKENS
    except (TypeError, ValueError):
        initial_tokens = ACCELA_FINDER_DEFAULT_MAX_TOKENS
    initial_tokens = max(1, min(initial_tokens, ACCELA_FINDER_MAX_TOKENS_CAP))

    existing = list_scraper_state_city_options().get('cities_by_state') or {}

    return render(request, 'core/admin_scraper_accela_search.html', {
        'us_states':                   US_STATES,
        'cities_by_state':             CITIES_BY_STATE,
        'existing_cities_by_state':    existing,
        'finder_default_prompt':       initial_prompt,
        'finder_default_model':        initial_model,
        'finder_models':               list(OSS_MODELS.keys()),
        'finder_default_tokens':       initial_tokens,
        'finder_tokens_cap':           ACCELA_FINDER_MAX_TOKENS_CAP,
        'finder_settings_saved_at':    saved_at,
        **_admin_base_ctx(request, 'scraper_accela_search'),
    })


@admin_required
@require_http_methods(['POST'])
def admin_scraper_accela_search_save_settings(request):
    """Persist the DO Inference finder settings shown at the top of the
    Accela permit-search finder page.

    Body (JSON): ``prompt_template`` (string ≤4000 chars),
    ``model`` (free-text DO Inference model id),
    ``max_tokens`` (positive int clamped to
    ``ACCELA_FINDER_MAX_TOKENS_CAP``). Returns
    ``{ok, saved_at}`` on success or ``{ok:false, error}`` with a
    400/413 on bad input.
    """
    from .scraper_accela import ACCELA_FINDER_MAX_TOKENS_CAP
    import re as _re

    raw_body = request.body or b''
    if len(raw_body) > 16384:
        return JsonResponse({'ok': False, 'error': 'Request body too large.'},
                            status=413)

    if request.content_type and request.content_type.startswith('application/json'):
        try:
            payload = json.loads(raw_body.decode('utf-8') or '{}')
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({'ok': False, 'error': 'Invalid JSON body.'},
                                status=400)
        if not isinstance(payload, dict):
            return JsonResponse(
                {'ok': False, 'error': 'JSON body must be an object.'},
                status=400,
            )
    else:
        payload = request.POST

    raw_prompt = payload.get('prompt_template', '')
    if not isinstance(raw_prompt, str):
        return JsonResponse(
            {'ok': False, 'error': 'prompt_template must be a string.'},
            status=400,
        )
    prompt_template = raw_prompt.strip()
    if not prompt_template:
        return JsonResponse(
            {'ok': False, 'error': 'prompt_template cannot be empty.'},
            status=400,
        )
    if len(prompt_template) > 4000:
        return JsonResponse(
            {'ok': False, 'error': 'prompt_template too long (max 4000 chars).'},
            status=400,
        )

    raw_model = payload.get('model', '')
    if not isinstance(raw_model, str):
        return JsonResponse({'ok': False, 'error': 'model must be a string.'},
                            status=400)
    model = raw_model.strip()
    if not model or len(model) > 200:
        return JsonResponse({
            'ok': False,
            'error': 'model is required (max 200 chars).',
        }, status=400)
    if not _re.match(r'^[A-Za-z0-9._\-/]+$', model):
        return JsonResponse({
            'ok': False,
            'error': 'model contains invalid characters.',
        }, status=400)

    raw_tokens = payload.get('max_tokens', None)
    if raw_tokens is None or raw_tokens == '':
        return JsonResponse(
            {'ok': False, 'error': 'max_tokens is required.'},
            status=400,
        )
    try:
        max_tokens = int(raw_tokens)
    except (TypeError, ValueError):
        return JsonResponse(
            {'ok': False, 'error': 'max_tokens must be an integer.'},
            status=400,
        )
    if max_tokens < 1:
        return JsonResponse({
            'ok': False,
            'error': 'max_tokens must be a positive integer.',
        }, status=400)
    if max_tokens > ACCELA_FINDER_MAX_TOKENS_CAP:
        return JsonResponse({
            'ok': False,
            'error': f'max_tokens cannot exceed {ACCELA_FINDER_MAX_TOKENS_CAP}.',
        }, status=400)

    from .db import set_system_setting
    set_system_setting('accela_finder_prompt_template', prompt_template)
    set_system_setting('accela_finder_model',           model)
    set_system_setting('accela_finder_max_tokens',      str(max_tokens))
    saved_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    set_system_setting('accela_finder_settings_saved_at', saved_at)

    return JsonResponse({'ok': True, 'saved_at': saved_at})


@admin_required
@require_http_methods(['POST'])
def admin_scraper_accela_search_run(request):
    """Run one city through DO Inference → JSON envelope, then auto-push
    it to the ``scrapers`` table if a usable URL was returned.

    Body (JSON): ``state`` (2-letter code), ``city`` (string), optional
    ``prompt_template``, ``model``, ``max_tokens``. Returns
    ``{ok, city, state, url, confidence, reason, error, log, auto_push}``
    where ``auto_push`` is one of::

        {'status': 'pushed',  'scraper_id', 'name', 'edit_url'}
        {'status': 'exists',  'existing': {...}}
        {'status': 'skipped', 'reason': str}
        {'status': 'invalid', 'error': str}
        {'status': 'error',   'error': str}
        None

    ``ok=false`` is returned with HTTP 200 so the table can render
    failures inline alongside successful rows.
    """
    from .scraper_accela import (
        oss_finder_pick, ScraperError,
    )
    from .us_cities_top import get_state_name

    raw_body = request.body or b''
    if len(raw_body) > 16384:
        return JsonResponse({'ok': False, 'error': 'Request body too large.'},
                            status=413)

    if request.content_type and request.content_type.startswith('application/json'):
        try:
            payload = json.loads(raw_body.decode('utf-8') or '{}')
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({'ok': False, 'error': 'Invalid JSON body.'},
                                status=400)
        if not isinstance(payload, dict):
            return JsonResponse(
                {'ok': False, 'error': 'JSON body must be an object.'},
                status=400,
            )
    else:
        payload = request.POST

    raw_state = payload.get('state', '')
    raw_city  = payload.get('city',  '')
    if not isinstance(raw_state, str) or not isinstance(raw_city, str):
        return JsonResponse(
            {'ok': False, 'error': 'state and city must be strings.'},
            status=400,
        )
    state = raw_state.strip().upper()
    city  = raw_city.strip()
    if len(state) != 2 or not state.isalpha() or not city:
        return JsonResponse(
            {'ok': False, 'error': 'state (2-letter) and city are required.'},
            status=400,
        )
    if len(city) > 80:
        return JsonResponse({'ok': False, 'error': 'city name too long.'},
                            status=400)

    raw_prompt = payload.get('prompt_template', None)
    if raw_prompt is not None and not isinstance(raw_prompt, str):
        return JsonResponse(
            {'ok': False, 'error': 'prompt_template must be a string.'},
            status=400,
        )
    if isinstance(raw_prompt, str) and len(raw_prompt) > 4000:
        return JsonResponse(
            {'ok': False, 'error': 'prompt_template too long (max 4000 chars).'},
            status=400,
        )
    prompt_template = raw_prompt.strip() if isinstance(raw_prompt, str) else None

    raw_model = payload.get('model', None)
    if raw_model is not None and not isinstance(raw_model, str):
        return JsonResponse(
            {'ok': False, 'error': 'model must be a string.'},
            status=400,
        )
    model = raw_model.strip() if isinstance(raw_model, str) else None

    raw_credits = payload.get('max_tokens', None)
    max_tokens = None
    if raw_credits is not None and raw_credits != '':
        try:
            max_tokens = int(raw_credits)
        except (TypeError, ValueError):
            return JsonResponse(
                {'ok': False, 'error': 'max_tokens must be an integer.'},
                status=400,
            )
        if max_tokens < 1:
            return JsonResponse({
                'ok': False,
                'error': 'max_tokens must be a positive integer.',
            }, status=400)

    state_name = get_state_name(state) or state

    try:
        result = oss_finder_pick(
            city, state_name,
            prompt_template=prompt_template,
            model=model,
            max_tokens=max_tokens,
        )
    except ScraperError as e:
        return JsonResponse({
            'ok':        False,
            'city':      city,
            'state':     state,
            'error':     str(e),
            'log':       None,
            'auto_push': None,
        })

    auto_push = None
    chosen_url = result.get('url') if result.get('ok') else None
    if chosen_url:
        try:
            push_res = _create_or_dedup_accela_scraper(
                chosen_url, city, state, name='',
            )
            auto_push = push_res
        except Exception as e:
            log.exception('accela finder auto-push failed for %s/%s', state, city)
            auto_push = {'status': 'error', 'error': f'DB error: {e}'}
    elif result.get('ok'):
        auto_push = {'status': 'skipped',
                     'reason': result.get('reason') or 'No URL returned.'}

    return JsonResponse({
        'ok':         bool(result.get('ok')),
        'city':       city,
        'state':      state,
        'url':        result.get('url'),
        'confidence': result.get('confidence') or 'low',
        'reason':     result.get('reason') or '',
        'error':      result.get('error'),
        'log':        result.get('log'),
        'auto_push':  auto_push,
    })


@admin_required
@require_http_methods(['GET'])
def admin_finder_request_log(request):
    """Return the most recent finder request log entries as JSON."""
    from .db import list_finder_requests, count_finder_requests
    limit  = min(200, max(1, int(request.GET.get('limit', 100))))
    offset = max(0, int(request.GET.get('offset', 0)))
    rows   = list_finder_requests(limit=limit, offset=offset)
    total  = count_finder_requests()
    safe = []
    for r in rows:
        entry = {}
        for k, v in r.items():
            if hasattr(v, 'isoformat'):
                entry[k] = v.isoformat()
            else:
                entry[k] = v
        safe.append(entry)
    return JsonResponse({'ok': True, 'rows': safe, 'total': total})


@admin_required
@require_http_methods(['POST'])
def admin_scraper_accela_search_push(request):
    """Create a new ``scrapers`` row directly from a finder result.

    Removes the old "open the new-scraper popup pre-filled" dance —
    the admin clicks once and the scraper is live. Body (JSON):
    ``url`` (https accela.com), ``city`` (string), ``state``
    (2-letter), optional ``name`` (defaults to "{City} {ST} Accela
    Permits"). Returns ``{ok:true, scraper_id, name, edit_url}`` on
    success.

    Dedup behavior: if a scrapers row already exists for the same URL
    *or* the same (city, state) pair, returns HTTP 409 with
    ``{ok:false, error, existing:{id, name, url, city, state}}`` so
    the UI can render an "⚠ Already exists (View)" pill linking to
    the original. (city, state) match is the common case — same city
    pushed twice — and url match catches duplicates that landed under
    a different label.

    NOTE: the body of this endpoint is delegated to
    :func:`_create_or_dedup_accela_scraper` so the auto-push step in
    the finder ``…/run/`` view shares one source of truth for the
    advisory-locked dedup-then-insert.
    """
    raw_body = request.body or b''
    if len(raw_body) > 8192:
        return JsonResponse({'ok': False, 'error': 'Request body too large.'},
                            status=413)
    try:
        payload = json.loads(raw_body.decode('utf-8') or '{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON body.'},
                            status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'ok': False, 'error': 'JSON body must be an object.'},
                            status=400)

    raw_url   = payload.get('url',   '')
    raw_city  = payload.get('city',  '')
    raw_state = payload.get('state', '')
    raw_name  = payload.get('name',  '')
    if not all(isinstance(v, str) for v in (raw_url, raw_city, raw_state, raw_name)):
        return JsonResponse(
            {'ok': False, 'error': 'url/city/state/name must be strings.'},
            status=400,
        )
    url   = raw_url.strip()
    city  = raw_city.strip()
    state = raw_state.strip().upper()
    name  = raw_name.strip()

    if not url or not url.lower().startswith('https://'):
        return JsonResponse({'ok': False, 'error': 'A full https:// URL is required.'},
                            status=400)
    if len(state) != 2 or not state.isalpha():
        return JsonResponse({'ok': False, 'error': 'state must be a 2-letter code.'},
                            status=400)
    if not city or len(city) > 80:
        return JsonResponse({'ok': False, 'error': 'city is required (≤80 chars).'},
                            status=400)
    if len(url) > 1000:
        return JsonResponse({'ok': False, 'error': 'url too long (max 1000 chars).'},
                            status=400)
    if len(name) > 200:
        return JsonResponse({'ok': False, 'error': 'name too long (max 200 chars).'},
                            status=400)

    result = _create_or_dedup_accela_scraper(url, city, state, name)
    if result['status'] == 'pushed':
        return JsonResponse({
            'ok':         True,
            'scraper_id': result['scraper_id'],
            'name':       result['name'],
            'edit_url':   result['edit_url'],
        })
    if result['status'] == 'exists':
        return JsonResponse({
            'ok':       False,
            'error':    'A scraper for this URL or city already exists.',
            'existing': result['existing'],
        }, status=409)
    # 'invalid' — bad accela URL after parsing
    return JsonResponse({
        'ok':    False,
        'error': result.get('error') or 'URL must be on accela.com (or a subdomain).',
    }, status=400)


def _create_or_dedup_accela_scraper(url: str, city: str, state: str,
                                    name: str = '') -> dict:
    """Shared helper used by both the manual ``…/push/`` endpoint and
    the auto-push step inside ``…/run/``.

    Returns one of three envelopes (no JsonResponse — callers wrap)::

        {'status': 'pushed',  'scraper_id': int, 'name': str, 'edit_url': str}
        {'status': 'exists',  'existing':   {'id': int, 'name': str, 'url': str,
                                             'city': str, 'state': str,
                                             'edit_url': str}}
        {'status': 'invalid', 'error':      str}

    Preconditions: caller has already validated url/city/state shape
    (length, scheme, etc.) — this helper still validates the accela
    host via ``parse_accela_url`` because it's the dedup-safety
    gate that protects the DB even if a future caller skimps on
    upstream checks.

    Race-safety: dedup-then-insert is wrapped in one Postgres txn with
    a per-(state, city) advisory lock so concurrent pushes for the
    same city serialise — the second caller sees the first one's row
    and returns ``status='exists'`` cleanly. Different cities don't
    block each other (different lock key).
    """
    from .db import pg
    from .scraper_accela import parse_accela_url

    # Defence-in-depth: the manual push endpoint already enforces
    # https://; the auto-push path goes through Claude which is told
    # to return https only — but Claude could still return an http://
    # link or a malformed URL. Require https here so this single
    # helper is the canonical gate for both paths.
    if not isinstance(url, str) or not url.lower().startswith('https://'):
        return {'status': 'invalid',
                'error':  'URL must start with https://.'}

    parsed = parse_accela_url(url)
    if not parsed:
        return {'status': 'invalid',
                'error':  'URL must be on accela.com (or a subdomain).'}

    # Mirror `create_scraper`'s column transforms so the dedup query
    # compares apples-to-apples with what would actually be stored.
    city_norm  = city.title()
    state_norm = state.upper()
    # Race-safety: dedup matches on URL OR (city, state), so we must
    # lock on BOTH keys — locking only the (city, state) pair allowed
    # two concurrent pushes of the same URL with DIFFERENT cities to
    # both insert, defeating URL-level dedup. Take the locks in
    # deterministic sort order to avoid AB/BA deadlock between
    # concurrent transactions racing on the same pair of keys.
    city_lock_key = f'scraper:push:city:{state_norm}:{city_norm.lower()}'
    url_lock_key  = f'scraper:push:url:{url.lower()}'
    lock_keys     = sorted({city_lock_key, url_lock_key})

    with pg.conn() as c, c.cursor() as cur:
        for lk in lock_keys:
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (lk,))
        cur.execute(
            """SELECT id, name, url, city, state
                 FROM scrapers
                WHERE LOWER(url) = LOWER(%s)
                   OR (city = %s AND state = %s)
                ORDER BY id ASC
                LIMIT 1""",
            (url, city_norm, state_norm),
        )
        existing_row = cur.fetchone()
        if existing_row:
            existing_dict = dict(existing_row)
            return {
                'status': 'exists',
                'existing': {
                    'id':       int(existing_dict['id']),
                    'name':     existing_dict.get('name') or '',
                    'url':      existing_dict.get('url') or '',
                    'city':     existing_dict.get('city') or '',
                    'state':    existing_dict.get('state') or '',
                    'edit_url': f'/admin-panel/scrapers/{int(existing_dict["id"])}/',
                },
            }

        final_name = (name or '').strip() or f'{city_norm} {state_norm} Accela Permits'
        # Wrap the INSERT in a SAVEPOINT so that if the partial unique
        # index `scrapers_accela_one_per_city_uidx` fires (race that
        # squeaked past the advisory lock, OR a future code path that
        # bypasses this helper entirely), we can ROLLBACK TO that
        # savepoint and continue running queries inside the same
        # outer transaction. Without this, Postgres marks the whole
        # transaction as aborted and the recovery SELECT below would
        # fail with InFailedSqlTransaction.
        cur.execute('SAVEPOINT accela_dedup_insert')
        try:
            cur.execute(
                """INSERT INTO scrapers (name, source, url, agency_code, module,
                                          cap_id_template, city, state, enabled, config)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
                   RETURNING id""",
                (
                    final_name[:200],
                    'accela',
                    url,
                    (parsed.get('agency_code') or '').strip().upper() or None,
                    (parsed.get('module') or '').strip() or None,
                    json.dumps(parsed),
                    city_norm or None,
                    state_norm or None,
                    True,
                    json.dumps({}),
                ),
            )
            sid = int(cur.fetchone()['id'])
            cur.execute('RELEASE SAVEPOINT accela_dedup_insert')
        except psycopg.errors.UniqueViolation:
            cur.execute('ROLLBACK TO SAVEPOINT accela_dedup_insert')
            # Now safe to query within the same connection — the
            # partial-rollback brought the transaction back to the
            # state right before the failed INSERT.
            cur.execute(
                """SELECT id, name, url, city, state
                     FROM scrapers
                    WHERE source = 'accela'
                      AND state = %s AND LOWER(city) = LOWER(%s)
                    LIMIT 1""",
                (state_norm, city_norm),
            )
            row = cur.fetchone()
            if row:
                d = dict(row)
                return {
                    'status': 'exists',
                    'existing': {
                        'id':       int(d['id']),
                        'name':     d.get('name') or '',
                        'url':      d.get('url') or '',
                        'city':     d.get('city') or '',
                        'state':    d.get('state') or '',
                        'edit_url': f'/admin-panel/scrapers/{int(d["id"])}/',
                    },
                }
            # UniqueViolation but no matching row? Shouldn't happen —
            # surface explicitly instead of silently 500-ing.
            return {'status': 'invalid',
                    'error':  'Race during dedup — please retry.'}

    return {
        'status':     'pushed',
        'scraper_id': sid,
        'name':       final_name,
        'edit_url':   f'/admin-panel/scrapers/{sid}/',
    }


# ── Finder batch (Run All States — subprocess) ──────────────────────
#
# Runs the USA rotation in a standalone subprocess (management command)
# so it survives Django dev-server restarts / auto-reloads.  Progress
# is persisted to ``finder_batches`` after every city.

_FINDER_INTER_CITY_SECONDS = 8


def _finder_batch_is_cancelled(batch_id: int) -> bool:
    from .db import pg as _pg
    row = _pg.query_one(
        "SELECT status FROM finder_batches WHERE id = %s", (batch_id,)
    )
    return row and row['status'] == 'stopping'


def _finder_worker_pid_alive(pid_str: str | None) -> bool:
    if not pid_str:
        return False
    try:
        pid = int(pid_str.replace('pid:', ''))
    except (ValueError, TypeError):
        return False
    import os as _os
    try:
        _os.kill(pid, 0)
        return True
    except OSError:
        return False


def _finder_batch_worker(batch_id: int, settings: dict) -> None:
    import time as _time
    from datetime import datetime as _dt
    from .us_cities_top import US_STATES, CITIES_BY_STATE, get_state_name
    from .scraper_accela import oss_finder_pick, ScraperError
    from .db import (
        get_finder_batch, update_finder_batch,
        append_finder_batch_log, list_scraper_state_city_options,
    )

    prompt_template = settings.get('prompt_template') or None
    model = settings.get('model') or None
    max_tokens = settings.get('max_tokens')
    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = None

    try:
        existing_raw = list_scraper_state_city_options().get('cities_by_state') or {}
        existing_sets = {}
        for st, cities in existing_raw.items():
            existing_sets[st.upper()] = {c.strip().lower() for c in cities}

        plan = []
        global_total = 0
        for code, name in US_STATES:
            all_cities = CITIES_BY_STATE.get(code, [])
            have = existing_sets.get(code.upper(), set())
            fresh = [c for c in all_cities if c.strip().lower() not in have]
            if fresh:
                plan.append((code, name, fresh))
                global_total += len(fresh)

        update_finder_batch(
            batch_id,
            total_cities=global_total,
            states_total=len(plan),
            note=f'plan: {len(plan)} states, {global_total} cities',
        )

        if global_total == 0:
            update_finder_batch(
                batch_id,
                status='success',
                finished_at=_dt.utcnow(),
                note='all cities already in scrapers — nothing to do',
            )
            return

        ok = 0
        fail = 0
        processed = 0
        is_first = True

        for si, (code, state_name_full, cities) in enumerate(plan):
            if _finder_batch_is_cancelled(batch_id):
                break

            update_finder_batch(
                batch_id,
                current_state=code,
                states_done=si,
            )

            append_finder_batch_log(batch_id, {
                't': _dt.utcnow().strftime('%H:%M:%S'),
                'type': 'state_start',
                'state': code,
                'state_name': state_name_full,
                'count': len(cities),
            })

            for ci, city in enumerate(cities):
                if _finder_batch_is_cancelled(batch_id):
                    break

                update_finder_batch(
                    batch_id,
                    current_city=city,
                )

                if not is_first:
                    _time.sleep(_FINDER_INTER_CITY_SECONDS)
                is_first = False

                if _finder_batch_is_cancelled(batch_id):
                    break

                entry = {
                    't': _dt.utcnow().strftime('%H:%M:%S'),
                    'type': 'city',
                    'state': code,
                    'city': city,
                }

                try:
                    result = oss_finder_pick(
                        city, state_name_full,
                        prompt_template=prompt_template,
                        model=model,
                        max_tokens=max_tokens,
                    )
                except ScraperError as e:
                    result = {'ok': False, 'error': str(e)}
                except Exception as e:
                    logging.exception('finder-batch %s: inference call failed '
                                      'for %s/%s', batch_id, code, city)
                    result = {'ok': False, 'error': str(e)}

                chosen_url = result.get('url') if result.get('ok') else None
                push_status = None
                push_scraper_id = None
                push_name = None
                if chosen_url:
                    try:
                        push_res = _create_or_dedup_accela_scraper(
                            chosen_url, city, code, name='',
                        )
                        push_status = push_res.get('status')
                        push_scraper_id = push_res.get('scraper_id')
                        push_name = push_res.get('name')
                    except Exception as e:
                        logging.exception('finder-batch %s: push failed '
                                          'for %s/%s', batch_id, code, city)
                        push_status = 'error'

                if result.get('ok') and chosen_url:
                    ok += 1
                    entry['status'] = 'ok'
                    entry['url'] = chosen_url
                    entry['confidence'] = result.get('confidence', 'low')
                    entry['push'] = push_status
                    entry['scraper_id'] = push_scraper_id
                    entry['scraper_name'] = push_name
                elif result.get('ok') and not chosen_url:
                    fail += 1
                    entry['status'] = 'error'
                    entry['error'] = result.get('reason') or 'No URL found/verified'
                else:
                    fail += 1
                    entry['status'] = 'error'
                    entry['error'] = result.get('error', 'unknown')

                processed += 1
                append_finder_batch_log(batch_id, entry)
                update_finder_batch(
                    batch_id,
                    processed=processed,
                    succeeded=ok,
                    failed=fail,
                )

            if not _finder_batch_is_cancelled(batch_id):
                update_finder_batch(batch_id, states_done=si + 1)

        was_cancelled = _finder_batch_is_cancelled(batch_id)

        if was_cancelled:
            final_status = 'cancelled'
            summary = f'cancelled — {ok} found, {fail} failed, {processed}/{global_total} processed'
        else:
            final_status = 'success' if fail == 0 and ok > 0 else 'failed' if ok == 0 else 'partial'
            summary = f'done — {ok} found, {fail} failed'

        update_finder_batch(
            batch_id,
            status=final_status,
            finished_at=_dt.utcnow(),
            note=summary,
        )
    except Exception:
        logging.exception('finder-batch %s: worker crashed', batch_id)
        try:
            update_finder_batch(
                batch_id,
                status='failed',
                finished_at=_dt.utcnow(),
                note='worker thread crashed — see server logs',
            )
        except Exception:
            pass


@admin_required
@require_http_methods(['POST'])
def admin_finder_batch_start(request):
    import subprocess, sys
    from .db import create_finder_batch, get_finder_batch
    from .db import pg as _pg

    active = _pg.query_one(
        "SELECT id, thread_name FROM finder_batches "
        "WHERE finished_at IS NULL AND status IN ('running', 'stopping') "
        "ORDER BY id DESC LIMIT 1"
    )
    if active:
        if _finder_worker_pid_alive(active.get('thread_name')):
            return JsonResponse({'ok': False,
                                 'error': 'A finder batch is already running.',
                                 'batch_id': int(active['id'])},
                                status=409)

    raw_body = request.body or b'{}'
    try:
        payload = json.loads(raw_body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}

    user_id = getattr(request, '_cached_user', {}).get('id')
    batch_id = create_finder_batch(kicked_by=user_id)

    cmd = [sys.executable, 'manage.py', 'run_finder_batch', str(batch_id)]
    model = payload.get('model')
    if model:
        cmd += ['--model', str(model)]
    max_tokens = payload.get('max_tokens')
    if max_tokens:
        cmd += ['--max-tokens', str(max_tokens)]
    prompt_template = payload.get('prompt_template')
    if prompt_template:
        cmd += ['--prompt-template', str(prompt_template)]

    env = os.environ.copy()
    env.setdefault('DJANGO_DEBUG', '1')
    env['DJANGO_SETTINGS_MODULE'] = 'permitdaily.settings'

    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=open('/tmp/finder_batch_%d.log' % batch_id, 'w'),
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
    )

    from .db import update_finder_batch
    update_finder_batch(batch_id, thread_name=f'pid:{proc.pid}')

    return JsonResponse({'ok': True, 'batch_id': batch_id})


@admin_required
def admin_finder_batch_status(request, batch_id):
    from .db import get_finder_batch
    batch = get_finder_batch(int(batch_id))
    if not batch:
        return JsonResponse({'ok': False, 'error': 'batch not found'},
                            status=404)

    log = batch.get('log') or []
    if len(log) > 200:
        log = log[-200:]

    finished = bool(batch.get('finished_at'))

    if not finished:
        pid_str = batch.get('thread_name')
        alive = _finder_worker_pid_alive(pid_str)
        if not alive:
            from .db import update_finder_batch
            from datetime import datetime as _dt
            try:
                update_finder_batch(
                    int(batch_id),
                    status='aborted',
                    finished_at=_dt.utcnow(),
                    note='worker process gone (crash)',
                )
            except Exception:
                logging.exception('finder_batch_status: finalize failed '
                                  'for %s', batch_id)
            batch['status'] = 'aborted'
            batch['finished_at'] = _dt.utcnow()
            finished = True

    return JsonResponse({
        'ok':            True,
        'batch_id':      int(batch['id']),
        'status':        batch.get('status'),
        'started_at':    batch['started_at'].isoformat() if batch.get('started_at') else None,
        'finished_at':   batch['finished_at'].isoformat() if batch.get('finished_at') else None,
        'total_cities':  int(batch.get('total_cities') or 0),
        'processed':     int(batch.get('processed') or 0),
        'succeeded':     int(batch.get('succeeded') or 0),
        'failed':        int(batch.get('failed') or 0),
        'current_state': batch.get('current_state') or '',
        'current_city':  batch.get('current_city') or '',
        'states_done':   int(batch.get('states_done') or 0),
        'states_total':  int(batch.get('states_total') or 0),
        'log':           log,
        'note':          batch.get('note') or '',
        'finished':      finished,
    })


@admin_required
@require_http_methods(['POST'])
def admin_finder_batch_stop(request):
    from .db import pg as _pg, update_finder_batch
    row = _pg.query_one(
        "SELECT id FROM finder_batches "
        "WHERE finished_at IS NULL AND status = 'running' "
        "ORDER BY id DESC LIMIT 1"
    )
    if not row:
        return JsonResponse({'ok': False,
                             'error': 'No active finder batch to stop.'},
                            status=404)
    bid = int(row['id'])
    update_finder_batch(bid, status='stopping')
    return JsonResponse({'ok': True,
                         'msg': f'Finder batch #{bid} stopping…'})


@admin_required
def admin_finder_batch_active(request):
    from .db import pg as _pg, update_finder_batch
    from datetime import datetime as _dt

    row = _pg.query_one(
        "SELECT id, thread_name FROM finder_batches "
        "WHERE finished_at IS NULL AND status IN ('running', 'stopping') "
        "ORDER BY id DESC LIMIT 1"
    )
    if row:
        bid = int(row['id'])
        pid_str = row.get('thread_name')
        alive = _finder_worker_pid_alive(pid_str)
        if not alive:
            try:
                update_finder_batch(bid, status='aborted',
                                    finished_at=_dt.utcnow(),
                                    note='worker process gone (crash)')
            except Exception:
                logging.exception('finder_batch_active: finalize failed '
                                  'for %s', bid)
            return JsonResponse({'ok': True, 'batch_id': None,
                                 'last_batch_id': bid})
        return JsonResponse({'ok': True, 'batch_id': bid})

    last = _pg.query_one(
        "SELECT id FROM finder_batches ORDER BY id DESC LIMIT 1"
    )
    last_id = int(last['id']) if last else None
    return JsonResponse({'ok': True, 'batch_id': None,
                         'last_batch_id': last_id})


@admin_required
def admin_scraper_run_log_view(request, sid, rid):
    """Return one run's full step_log + permit-lineage summary as JSON.

    Powers the inline "📜 View log" panel on the scraper-detail page —
    the modal shows the CLI-style transcript the worker streamed PLUS
    a count and small preview of every permit this run created (so
    the admin can decide whether to delete-run or delete-run+permits).

    Scoped under the scraper id so the URL is self-explanatory and we
    can 404 cleanly when someone hand-edits the path with a run id
    that belongs to a different scraper.
    """
    from .db import (get_scraper_run, count_permits_for_run,
                     list_permits_for_run, get_scraper)
    if not get_scraper(sid):
        return JsonResponse({'ok': False, 'error': 'scraper not found'},
                            status=404)
    run = get_scraper_run(rid)
    if not run or int(run.get('scraper_id') or 0) != int(sid):
        return JsonResponse({'ok': False, 'error': 'run not found'},
                            status=404)
    permits = list_permits_for_run(rid, limit=200)
    return JsonResponse({
        'ok':              True,
        'run_id':          run['id'],
        'scraper_id':      run['scraper_id'],
        'status':          run.get('status'),
        'mode':            run.get('mode'),
        'kind':            run.get('kind'),
        'started_at':      str(run.get('started_at') or ''),
        'finished_at':     str(run.get('finished_at') or ''),
        'created_at':      str(run.get('created_at') or ''),
        'date_from':       str(run.get('date_from') or ''),
        'date_to':         str(run.get('date_to') or ''),
        'total_targets':   int(run.get('total_targets') or 0),
        'processed':       int(run.get('processed') or 0),
        'succeeded':       int(run.get('succeeded') or 0),
        'failed':          int(run.get('failed') or 0),
        'current_step':    run.get('current_step') or '',
        'step_log':        run.get('step_log') or [],
        'errors':          run.get('error') or [],
        # `permits_count` is the live link-back via permits.scraper_run_id —
        # always trust this over `succeeded` because a permit can be
        # deleted/relabelled later without touching the run row.
        'permits_count':   count_permits_for_run(rid),
        'permits_preview': [
            {
                'id':              p['id'],
                'permit_number':   p.get('permit_number') or '',
                'address':         p.get('address') or '',
                'city':            p.get('city') or '',
                'state':           p.get('state') or '',
                'contractor_name': p.get('contractor_name') or '',
                'applied_date':    str(p.get('applied_date') or ''),
                'issued_date':     str(p.get('issued_date') or ''),
                'valuation_cents': p.get('valuation_cents'),
                'ai_score':        p.get('ai_score'),
                'ai_grade':        p.get('ai_grade') or '',
            }
            for p in permits
        ],
    })


@admin_required
@require_http_methods(['POST'])
def admin_scraper_run_delete_view(request, sid, rid):
    """Delete one scraper run. Form param `delete_permits=on` opts into
    cascading the delete to every permit row that this run produced.

    Returns JSON when called with X-Requested-With (the inline modal),
    otherwise redirects back to the scraper detail page so plain
    `<form>` POSTs still work without JS.
    """
    from .db import delete_scraper_run, get_scraper, get_scraper_run, ScraperRunBusy
    if not get_scraper(sid):
        raise Http404
    run = get_scraper_run(rid)
    if not run or int(run.get('scraper_id') or 0) != int(sid):
        raise Http404
    delete_permits = (request.POST.get('delete_permits') or '').lower() in (
        '1', 'on', 'true', 'yes',
    )
    is_xhr = (request.headers.get('X-Requested-With') == 'XMLHttpRequest'
              or 'application/json' in (request.headers.get('Accept') or ''))
    try:
        result = delete_scraper_run(rid, delete_permits=delete_permits)
    except ScraperRunBusy as e:
        # Worker is still owning this run; refuse to race the cascade
        # against it. 409 Conflict so the JS surfaces a clean toast and
        # plain form-submit fallbacks land on a readable error page.
        if is_xhr:
            return JsonResponse(
                {'ok': False, 'error': str(e), 'code': 'run_busy'},
                status=409,
            )
        return HttpResponse(str(e), status=409, content_type='text/plain')
    if is_xhr:
        return JsonResponse({'ok': True, **result})
    return redirect(f'/admin-panel/scrapers/{sid}/')


# ── Scraper Logs (separate menu tab) ─────────────────────────────────
# Two-level nav: index lists every scraper with its run-history counts,
# detail shows ALL runs for one scraper plus a bulk-delete checkbox UI.
# We keep the "Recent runs" sidebar on the existing scraper detail page
# untouched — this is an additional surface, not a replacement.

@admin_required
def admin_scraper_logs_index_view(request):
    """List every scraper with run counts so admins can pick which one's
    history to manage. Uses the same search/pagination shape as the
    main /admin-panel/scrapers/ view so the muscle memory matches."""
    from .db import list_scrapers_with_run_counts
    q = (request.GET.get('q') or '').strip()
    try:
        page = int(request.GET.get('page') or 1)
    except (TypeError, ValueError):
        page = 1
    per_page = 50
    rows, total, total_pages, page = list_scrapers_with_run_counts(q, page, per_page)
    enriched = []
    for s in rows:
        url = s.get('url') or ''
        try:
            host = urllib.parse.urlparse(url).netloc or url
        except Exception:
            host = url
        last = s.get('last_run_at_actual') or s.get('last_run_at')
        enriched.append({
            **s,
            'host':           host,
            'last_run_human': last.strftime('%b %d, %Y %I:%M %p') if last else '—',
        })
    ctx = {
        **_admin_base_ctx(request, 'scraper_logs'),
        'scrapers':    enriched,
        'q':           q,
        'page':        page,
        'per_page':    per_page,
        'total':       total,
        'total_pages': total_pages,
        'has_prev':    page > 1,
        'has_next':    page < total_pages,
        'prev_page':   max(1, page - 1),
        'next_page':   min(total_pages, page + 1),
    }
    return render(request, 'core/admin_scraper_logs_index.html', ctx)


@admin_required
def admin_scraper_logs_detail_view(request, sid):
    """Show every run for one scraper with bulk-delete checkboxes.
    Limit defaults to 500 — more than enough for the UI; if anyone
    crosses that we'll add server-side pagination then."""
    from .db import get_scraper, list_scraper_runs, count_permits_for_run
    scraper = get_scraper(sid)
    if not scraper:
        raise Http404
    # Hard cap so the page never blows up on a runaway scraper. The
    # template surfaces this honestly via `truncated` so the admin
    # knows there's older history that the UI isn't showing.
    RUN_LIMIT = 500
    runs = list_scraper_runs(sid, limit=RUN_LIMIT + 1)
    truncated = len(runs) > RUN_LIMIT
    if truncated:
        runs = runs[:RUN_LIMIT]
    # Pre-compute the cascade preview so the admin sees "X permits
    # will be deleted" inline before ticking "with permits".
    enriched = []
    for r in runs:
        enriched.append({
            **r,
            'permits_count':  count_permits_for_run(r['id']),
            'created_human':  r['created_at'].strftime('%b %d, %Y %I:%M %p')
                              if r.get('created_at') else '—',
            'is_busy':        (r.get('status') or '').lower() in ('queued', 'running'),
        })
    ctx = {
        **_admin_base_ctx(request, 'scraper_logs'),
        'scraper':       scraper,
        'runs':          enriched,
        'total_runs':    len(enriched),
        'truncated':     truncated,
        'run_limit':     RUN_LIMIT,
    }
    return render(request, 'core/admin_scraper_logs_detail.html', ctx)


@admin_required
@require_http_methods(['POST'])
def admin_scraper_logs_bulk_delete_view(request, sid):
    """Bulk-delete runs from the scraper-logs detail page. Accepts:
      • ``run_ids`` — repeated form param (one per checked checkbox)
      • ``delete_permits=on`` — opt-in cascade

    Returns JSON for XHR, redirects for plain form POSTs (with a
    ``?msg=`` so the receiving page can flash a confirmation toast)."""
    from .db import get_scraper, bulk_delete_scraper_runs
    scraper = get_scraper(sid)
    if not scraper:
        raise Http404
    raw_ids = request.POST.getlist('run_ids')
    run_ids: list[int] = []
    for v in raw_ids:
        try:
            run_ids.append(int(v))
        except (TypeError, ValueError):
            continue
    delete_permits = (request.POST.get('delete_permits') or '').lower() in (
        '1', 'on', 'true', 'yes',
    )
    is_xhr = (request.headers.get('X-Requested-With') == 'XMLHttpRequest'
              or 'application/json' in (request.headers.get('Accept') or ''))
    result = bulk_delete_scraper_runs(sid, run_ids, delete_permits=delete_permits)
    if is_xhr:
        return JsonResponse({'ok': True, **result})
    # Plain-form fallback: redirect with a small summary in the query
    # string so the detail page can flash a banner without us needing
    # session-based messages framework wiring.
    msg = f"Deleted {result['runs_deleted']} run(s)"
    if delete_permits and result['permits_deleted']:
        msg += f" + {result['permits_deleted']} permit(s)"
    if result['busy']:
        msg += f" · skipped {len(result['busy'])} in-flight"
    return redirect(f"/admin-panel/scraper-logs/{sid}/?msg={urllib.parse.quote(msg)}")


@admin_required
def admin_permit_raw_view(request, pid):
    """Return the `raw` JSONB blob for one permit so the admin can
    audit exactly what the scraper saw — markdown, JSON extract, source
    URL, scrape mode, fetched-at. Used by the "View source" modal on
    the scraper detail page.

    Returns 404 if the permit doesn't exist OR isn't an Accela-scraped
    permit (we only want admins peeking at scrape provenance, not the
    seeded demo rows)."""
    from .db import pg
    row = pg.query_one(
        'SELECT id, source, source_permit_id, permit_number, address, '
        'city, state, raw, scraped_at, created_at '
        'FROM permits WHERE id = %s',
        (pid,),
    )
    if not row:
        return JsonResponse({'ok': False, 'error': 'Permit not found.'}, status=404)
    src = (row.get('source') or '')
    if not src.startswith('accela:'):
        return JsonResponse({'ok': False,
                             'error': 'Source page is only available for permits scraped by this system.'},
                            status=400)
    raw = row.get('raw') or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {'markdown': raw}
    return JsonResponse({
        'ok':              True,
        'permit_id':       row['id'],
        'permit_number':   row.get('permit_number') or '',
        'address':         row.get('address') or '',
        'city':            row.get('city') or '',
        'state':           row.get('state') or '',
        'scraped_url':     (raw.get('scraped_url')
                            or raw.get('detail_url')
                            or raw.get('list_url') or ''),
        'mode':            raw.get('mode') or 'detail',
        'fetched_at':      raw.get('fetched_at') or '',
        'markdown':        raw.get('markdown') or '',
        # Prefer current keys and fall back to historical raw shapes so
        # old rows still render in the modal after scraper changes.
        'extracted_json':  (raw.get('agent_extracted')
                            or raw.get('extracted_json')
                            or raw.get(legacy_extract_key)
                            or raw.get('list_row') or {}),
        'metadata':        raw.get('metadata') or raw.get(legacy_metadata_key) or {},
        # Per-permit LLM debug payload — input we sent the model,
        # raw text response, parsed JSON, token counts. Empty {}
        # for legacy rows that pre-date the agent pivot.
        'llm_debug':       raw.get('llm_debug') or {},
    })


@admin_required
@require_http_methods(['GET', 'POST'])
def admin_permit_edit_view(request, pid):
    """Admin manual editor for a single permit.

    GET  → returns the current value of every editable field plus the
           list of fields already locked by a prior manual edit (so the
           modal can flag them).
    POST → writes the submitted fields via ``db.update_permit`` with
           ``mark_manual=True`` — locking them so future scraper upserts
           can never clobber the human-corrected value.

    Only contractor contact is ever exposed/edited here — owner contact
    is intentionally NOT editable to preserve the contractor-only rule."""
    from .db import get_permit_by_id, update_permit

    # Allow-list of columns the admin may edit from the modal. Owner
    # *name* is editable (it's public record), but owner phone/email are
    # deliberately absent — we never surface homeowner contact.
    EDITABLE = [
        ('permit_number',    'Permit #'),
        ('permit_type',      'Permit type'),
        ('address',          'Address'),
        ('city',             'City'),
        ('state',            'State'),
        ('zip',              'ZIP'),
        ('status',           'Status'),
        ('contractor_name',  'Contractor name'),
        ('contractor_email', 'Contractor email'),
        ('contractor_phone', 'Contractor phone'),
        ('owner_name',       'Owner name'),
        ('description',      'Description'),
        ('trade',            'Trade'),
        ('valuation_cents',  'Project value (USD $)'),
        ('applied_date',     'Applied date'),
        ('issued_date',      'Issued date'),
        ('expires_date',     'Expires date'),
        ('ai_score',         'AI score (0-100)'),
        ('ai_grade',         'AI grade'),
        ('ai_tier',          'AI tier (hot/warm/cool)'),
    ]
    editable_keys = [k for k, _ in EDITABLE]
    _DATE_KEYS  = ('applied_date', 'issued_date', 'expires_date')
    _INT_KEYS   = ('ai_score',)
    _MONEY_KEYS = ('valuation_cents',)

    def _widget(k):
        """Per-field input metadata so the modal can render the right
        control (date picker, number input, dollar input, tier dropdown)."""
        if k in _DATE_KEYS:
            return {'type': 'date'}
        if k in _MONEY_KEYS:
            return {'type': 'money'}
        if k == 'ai_score':
            return {'type': 'number', 'min': 0, 'max': 100, 'step': 1}
        if k == 'ai_tier':
            return {'type': 'select', 'options': ['', 'hot', 'warm', 'cool']}
        return {'type': 'text'}

    row = get_permit_by_id(pid)
    if not row:
        return JsonResponse({'ok': False, 'error': 'Permit not found.'}, status=404)

    if request.method == 'GET':
        manual = row.get('manual_fields') or []
        if isinstance(manual, str):
            try:
                manual = json.loads(manual)
            except Exception:
                manual = []
        fields = {}
        for k in editable_keys:
            v = row.get(k)
            if k in _DATE_KEYS and v is not None:
                v = str(v)
            elif k in _MONEY_KEYS and v not in (None, ''):
                # Store cents, but display/edit in dollars.
                try:
                    cents = int(v)
                    v = str(cents // 100) if cents % 100 == 0 else f'{cents / 100:.2f}'
                except (TypeError, ValueError):
                    v = ''
            fields[k] = '' if v is None else v
        return JsonResponse({
            'ok':            True,
            'permit_id':     row['id'],
            'fields':        fields,
            'labels':        {k: lbl for k, lbl in EDITABLE},
            'order':         editable_keys,
            'widgets':       {k: _widget(k) for k in editable_keys},
            'locked_fields': [m for m in manual if m in editable_keys],
        })

    # POST — collect only the editable columns that were actually sent.
    updates: dict = {}
    for k in editable_keys:
        if k not in request.POST:
            continue
        val = (request.POST.get(k) or '').strip()
        if k in _MONEY_KEYS:
            if not val:
                updates[k] = None
            else:
                cleaned = re.sub(r'[^0-9.]', '', val)
                try:
                    updates[k] = int(round(float(cleaned) * 100)) if cleaned else None
                except ValueError:
                    updates[k] = None
        elif k in _INT_KEYS:
            if not val:
                updates[k] = None
            else:
                digits = re.sub(r'[^0-9]', '', val)
                updates[k] = int(digits) if digits else None
        else:
            updates[k] = val or None
    if not updates:
        return JsonResponse({'ok': False, 'error': 'No editable fields supplied.'},
                            status=400)
    ok = update_permit(pid, updates, mark_manual=True)
    if not ok:
        return JsonResponse({'ok': False, 'error': 'Update failed — nothing changed.'},
                            status=400)
    return JsonResponse({'ok': True, 'locked_fields': list(updates.keys())})


@admin_required
@require_http_methods(['POST'])
def admin_scraper_delete(request, sid):
    from .db import delete_scraper, get_scraper
    if not get_scraper(sid):
        raise Http404
    delete_scraper(sid)
    return redirect('/admin-panel/scrapers/')


@admin_required
@require_http_methods(['POST'])
def admin_scrapers_bulk_delete(request):
    """Delete many scrapers at once from the admin scrapers list page.

    Accepts ``ids=`` repeated (form-encoded) or ``ids`` as a comma-
    separated string. Returns JSON ``{ok, deleted, requested}``.
    Permits are not touched — same contract as the per-row delete.
    """
    from .db import bulk_delete_scrapers
    raw = request.POST.getlist('ids')
    if not raw:
        raw = (request.POST.get('ids') or '').split(',')
    ids: list[int] = []
    for tok in raw:
        tok = (tok or '').strip()
        if tok.isdigit():
            ids.append(int(tok))
    if not ids:
        return JsonResponse({'ok': False, 'error': 'No scraper ids provided.'},
                            status=400)
    deleted = bulk_delete_scrapers(ids)
    return JsonResponse({'ok': True, 'deleted': deleted, 'requested': len(ids)})


@admin_required
@require_http_methods(['POST'])
def admin_scraper_update_meta(request, sid):
    """Patch scraper name / url / city / state from the inline Edit
    modal on /admin-panel/scrapers/. Returns JSON {ok: true} on
    success or {ok: false, error: '…'} on validation failure.

    The URL is required and validated as http(s)://… so a typo
    cannot brick the scraper by pointing it at a relative path.
    City / state may be cleared by posting an empty string."""
    from .db import get_scraper, update_scraper
    existing = get_scraper(sid)
    if not existing:
        raise Http404
    name  = (request.POST.get('name')  or '').strip()
    url   = (request.POST.get('url')   or '').strip()
    city  = (request.POST.get('city')  or '').strip()
    state = (request.POST.get('state') or '').strip().upper()
    # Optional Accela-specific filter — the literal ``value=""`` of
    # the desired Permit Type ``<option>``. Posting an empty string
    # CLEARS the filter; not posting the key at all leaves the
    # existing value alone (so other update paths don't accidentally
    # wipe it). Only ``permit_type`` survives the JSONB merge — we
    # don't want this endpoint becoming a generic config setter.
    permit_type_raw = request.POST.get('permit_type')
    # Optional dropdown-loop field name (Accela tenants where a
    # <select> must be picked to fetch all permits — e.g. FTL's
    # Street Suffix). Same posting contract as ``permit_type``:
    # empty string CLEARS, missing key leaves it alone.
    loop_field_raw  = request.POST.get('loop_field')
    # Optional General-Search text-box value (typed into Accela's
    # "Permit Number" input — e.g. "bd" on Roseville CA returns every
    # BD-prefixed record). Same posting contract as ``permit_type``:
    # empty string CLEARS, missing key leaves it alone.
    search_input_raw = request.POST.get('search_input')
    if not name:
        return JsonResponse({'ok': False, 'error': 'Name is required.'}, status=400)
    if len(name) > 200:
        return JsonResponse({'ok': False, 'error': 'Name must be 200 characters or fewer.'}, status=400)
    if not url:
        return JsonResponse({'ok': False, 'error': 'URL is required.'}, status=400)
    if not (url.startswith('http://') or url.startswith('https://')):
        return JsonResponse({'ok': False, 'error': 'URL must start with http:// or https://'}, status=400)
    if len(url) > 2000:
        return JsonResponse({'ok': False, 'error': 'URL is too long.'}, status=400)
    if state and (len(state) != 2 or not state.isalpha()):
        return JsonResponse({'ok': False, 'error': 'State must be a 2-letter postal code (e.g. CA).'}, status=400)
    if len(city) > 120:
        return JsonResponse({'ok': False, 'error': 'City name is too long.'}, status=400)
    # ── Auto-infer state from the trailing 2-letter token of `name`
    # when the admin left the State field blank. Every scraper in the
    # app is named "{City} {ST}" by convention, so a row called
    # "Fremont CA" with an empty state field is almost always a slip
    # of the keyboard — not an intentional NULL. We only accept a
    # real US state code (rejects "Lehigh Acres FL" → "FL" ✓ but also
    # blocks "Permit-Search GO" → "GO" ✗ because GO isn't a state).
    if not state:
        from .us_cities_top import STATE_NAME_BY_CODE
        tail = (name.rsplit(' ', 1)[-1] if ' ' in name else '').upper()
        if len(tail) == 2 and tail.isalpha() and tail in STATE_NAME_BY_CODE:
            state = tail
    update_kwargs = dict(
        name  = name,
        url   = url,
        city  = city  or None,
        state = state or None,
    )
    if (permit_type_raw is not None or loop_field_raw is not None
            or search_input_raw is not None):
        cfg = dict(existing.get('config') or {})
        if permit_type_raw is not None:
            pt = permit_type_raw.strip()
            if pt:
                if len(pt) > 500:
                    return JsonResponse({'ok': False,
                                         'error': 'Permit Type value is too long.'},
                                        status=400)
                cfg['permit_type'] = pt
            else:
                cfg.pop('permit_type', None)
        if loop_field_raw is not None:
            lf = loop_field_raw.strip()
            if lf:
                if len(lf) > 200:
                    return JsonResponse({'ok': False,
                                         'error': 'Loop dropdown field name is too long.'},
                                        status=400)
                # Soft sanity check: Accela form names always start
                # with ``ctl00$``. Reject obvious typos so a saved
                # value can never silently disable the loop.
                if not lf.startswith('ctl00$'):
                    return JsonResponse({'ok': False,
                                         'error': 'Loop dropdown must be the FULL Accela form-field name (starts with "ctl00$").'},
                                        status=400)
                cfg['loop_field'] = lf
            else:
                cfg.pop('loop_field', None)
        if search_input_raw is not None:
            si = search_input_raw.strip()
            if si:
                if len(si) > 200:
                    return JsonResponse({'ok': False,
                                         'error': 'Search input is too long.'},
                                        status=400)
                cfg['search_input'] = si
            else:
                cfg.pop('search_input', None)
        update_kwargs['config'] = cfg
    update_scraper(sid, **update_kwargs)
    # Echo the resolved state back so the client can show the inferred
    # value as a friendly toast / hint without re-querying.
    return JsonResponse({'ok': True, 'state': state or ''})


# ── States Manager ────────────────────────────────────────────────
#
# Single admin page that combines three previously scattered jobs:
#   1. Per-state permit stats
#      — rendered as a server-side DataTable so a long state list
#      doesn't blow up the page DOM. 5 rows per page.
#   2. Banned-states list: scraper ingest drops any permit whose
#      state matches one of these codes (see ``bulk_upsert_permits``
#      in core/db.py — the check lives there so the API path AND any
#      future direct-DB caller honour it automatically).
#   3. Per-state "delete all permits" button for clearing out
#      states we don't sell (or want to re-ingest fresh).

@admin_required
def admin_states_view(request):
    from .db import get_banned_states
    return render(request, 'core/admin_states.html', {
        **_admin_base_ctx(request, 'states'),
        'banned_states': get_banned_states(),
    })


@admin_required
def admin_states_data(request):
    """JSON source for the per-state DataTable on /admin-panel/states/.

    Returned in the ``{"data":[...]}`` shape DataTables expects when
    ``serverSide:false, ajax`` is used — full dataset (states are a
    bounded ~55-row list, so client-side paging is fine and lets the
    built-in search/sort work without round-trips).
    """
    from .db import get_permits_by_state, get_banned_states
    by_state = get_permits_by_state()
    banned = set(get_banned_states())
    rows = []
    for s in by_state:
        n = s['d30']
        if   n >= 500: tier, tier_kind = 'rich',  'rich'
        elif n >= 100: tier, tier_kind = 'solid', 'solid'
        elif n >= 20:  tier, tier_kind = 'thin',  'thin'
        else:          tier, tier_kind = 'tiny',  'tiny'
        rows.append({
            'state':     s['state'],
            'today':     s['today'],
            'd7':        s['d7'],
            'd30':       s['d30'],
            'total':     s['total'],
            'tier':      tier,
            'tier_kind': tier_kind,
            'banned':    s['state'] in banned,
        })
    return JsonResponse({'data': rows})


@admin_required
@require_http_methods(['POST'])
def admin_states_ban(request):
    from .db import add_banned_state, remove_banned_state
    action = (request.POST.get('action') or '').strip()
    code   = (request.POST.get('state')  or '').strip().upper()
    if not code or len(code) != 2 or not code.isalpha():
        return JsonResponse({'ok': False, 'error': 'state must be a 2-letter code'}, status=400)
    if action == 'add':
        codes = add_banned_state(code)
    elif action == 'remove':
        codes = remove_banned_state(code)
    else:
        return JsonResponse({'ok': False, 'error': 'unknown action'}, status=400)
    return JsonResponse({'ok': True, 'banned_states': codes})


@admin_required
@require_http_methods(['POST'])
def admin_states_delete_permits(request, code: str):
    """Hard-delete every permit row for the given state code. Body
    must include ``confirm=<CODE>`` so a misclick can't drop a state.
    """
    from .db import delete_permits_by_state
    code = (code or '').strip().upper()
    confirm = (request.POST.get('confirm') or '').strip().upper()
    if confirm != code:
        return JsonResponse({'ok': False,
                             'error': f'Confirmation must equal "{code}".'},
                            status=400)
    n = delete_permits_by_state(code)
    return JsonResponse({'ok': True, 'deleted': n, 'state': code})


def _enrich_users_for_admin_table(all_users):
    """Add display-only fields the admin Users table renders.

    Extracted so both the page-shell view (``admin_users_view``) and
    the htmx rows partial (``admin_users_rows_view``) can share the
    exact same enrichment — guarantees the partial-loaded table is
    byte-identical to the legacy inline-rendered version.
    """
    sorted_users = sorted(all_users, key=lambda u: u.get('joined', '2000-01-01'), reverse=True)
    for u in sorted_users:
        plan = u.get('plan', 'starter').lower()
        u['is_admin']     = (u.get('email') or '').lower().strip() in ADMIN_EMAILS
        # "No Plan" — user signed up but never completed a Whop checkout
        # (or their subscription expired / was cancelled fully). The
        # plan field defaults to 'starter' on signup, so without this
        # flag the admin table would dishonestly show every brand-new
        # signup as a paying $29/mo Starter customer. Admins are
        # exempted because their access doesn't go through Whop —
        # they're allow-listed by email.
        u['is_no_plan']   = not (u['is_admin'] or u.get('subscription_active'))
        # Revenue column shows $0 for no-plan users so MRR totals on
        # the admin Users tab match what's actually being collected.
        # Per-user revenue column: pull from system_settings via
        # _user_monthly_price so the admin Users table shows the price
        # the admin actually configured on the pricing page (per-user
        # whop_mode respected — dev-flagged users show $1, prod show
        # the live $79 / $349 / $749 etc.). Hidden for no-plan users.
        u['price']        = 0 if u['is_no_plan'] else _user_monthly_price(u)
        raw_cities        = u.get('cities', [])
        u['cities_count'] = len(raw_cities)
        u['cities']       = [
            c if ', ' in c else (c + ', ' + _city_state(c) if _city_state(c) else c)
            for c in raw_cities
        ]
        parts = u.get('name', '').split()
        u['avatar_initials'] = (parts[0][0] + parts[-1][0]).upper() if len(parts) >= 2 else u.get('name', 'U')[:2].upper()
        # Per-user Whop billing mode for the admin toggle column.
        # Treat any value other than 'dev' as 'prod' so legacy users
        # without the field rendered before the migration default to live.
        u['whop_mode'] = 'dev' if u.get('whop_mode') == 'dev' else 'prod'
        # Per-user email-code login verification state for the admin
        # toggle column. The stored field is `email_code_disabled`
        # (default missing/False = code REQUIRED) but the template
        # renders an `email_code` string of 'on' | 'off' so the
        # toggle pill HTML mirrors the Whop PROD/DEV pattern.
        u['email_code'] = 'off' if u.get('email_code_disabled') else 'on'
    return sorted_users


@admin_required
@cached_admin_html(15)
def admin_users_view(request):
    """Admin Users page — renders the page chrome + stats only.

    The actual table rows are loaded async via htmx from
    ``/admin-panel/users/_rows/`` (see ``admin_users_rows_view``).
    Splitting the heavy per-user enrichment + 25 KB of <tr> markup
    into a separate request lets the shell ship in <50 ms even on a
    cold worker, instead of staring at a blank page for 200-300 ms
    while the entire users list is enriched and rendered.
    """
    all_users   = get_all_users()
    today       = date.today()
    plan_counts = _admin_plan_counts(all_users)
    this_month  = today.strftime('%Y-%m')
    new_this_month = sum(1 for u in all_users if u.get('joined', '').startswith(this_month))
    ctx = {
        **_admin_base_ctx(request, 'users'),
        # The table body is now loaded by htmx, so the shell doesn't
        # need the full users list — just the count for the heading.
        'users':          [],
        'total_users':    len(all_users),
        'plan_counts':    plan_counts,
        'new_this_month': new_this_month,
        'deleted':        request.GET.get('deleted', ''),
        'banned':         request.GET.get('banned', ''),
        'bulk_deleted':   request.GET.get('bulk_deleted', ''),
        'bulk_self':      request.GET.get('bulk_self', ''),
        'bulk_admin':     request.GET.get('bulk_admin', ''),
        'bulk_synced':    request.GET.get('bulk_synced', ''),
        'bulk_no_member': request.GET.get('bulk_no_member', ''),
        'bulk_failed':    request.GET.get('bulk_failed', ''),
        'mode_set':       request.GET.get('mode_set', ''),
        'bulk_mode':      request.GET.get('bulk_mode', ''),
        'mode':           request.GET.get('mode', ''),
        # Per-user email-code login verification toggle outcomes.
        # Mirrors the mode_set / mode pair so the same toast pattern
        # at the top of admin_users.html can render the result.
        'email_code_set': request.GET.get('email_code_set', ''),
        'email_code':     request.GET.get('email_code', ''),
        'pricing_prod':   wp.get_pricing_dict('prod'),
        'pricing_dev':    wp.get_pricing_dict('dev'),
    }
    return render(request, 'core/admin_users.html', ctx)


@admin_required
@cached_admin_html(15)
def admin_users_rows_view(request):
    """htmx partial: just the <tr> rows for the admin Users table.

    Returned to the ``hx-get="/admin-panel/users/_rows/"`` request the
    main ``admin_users.html`` page fires the moment its empty <tbody>
    enters the DOM. Cached for 15 s like every other admin GET, and
    invalidated automatically by ``AdminHTMLCacheInvalidationMiddleware``
    when any admin write happens (delete user, flip whop mode, etc.).
    """
    sorted_users = _enrich_users_for_admin_table(list(get_all_users()))
    return render(request, 'core/_admin_users_rows.html', {'users': sorted_users})


@admin_required
@cached_admin_html(15)
def admin_revenue_view(request):
    # Mirrors the Whop merchant dashboard: a "Today" panel (gross today vs
    # yesterday with an hourly sparkline) and a "Stats" panel with a
    # date-range picker (1d/7d/30d/90d/365d) that drives 5 KPI line charts
    # (Gross / Net / New users / MRR / ARR) plus a payments-status
    # breakdown bar. All numbers come from Whop's memberships+payments
    # APIs (5-min in-process cache) — we fall back to a minimal local-DB
    # snapshot only when Whop is unreachable, so the page never blanks on
    # a transient API blip.
    range_key = request.GET.get('range', '7d')
    try:
        from .whop import get_revenue_stats, _RANGE_OPTIONS
        if range_key not in _RANGE_OPTIONS:
            range_key = '7d'
        whop_stats = get_revenue_stats(range_key=range_key)
    except Exception:
        whop_stats = None

    if whop_stats:
        ctx = {
            **_admin_base_ctx(request, 'revenue'),
            'whop':         whop_stats,
            'range_key':    range_key,
            'data_source':  'whop',
        }
        resp = render(request, 'core/admin_revenue.html', ctx)
        # Opt out of the @cached_admin_html(15) HTML cache when we're
        # serving a degraded payload (payments endpoint failed) so the
        # next request retries instead of pinning the missing chart for
        # 15 seconds. core.cache.cached_admin_html honours no-store.
        if not whop_stats.get('payments_ok'):
            resp['Cache-Control'] = 'no-store'
        return resp

    # ── Fallback: minimal local-DB snapshot (Whop unreachable) ───────
    all_users   = get_all_users()
    plan_counts = _admin_plan_counts(all_users)
    # Sum per-user via _user_monthly_price for DB-driven, mode-aware MRR.
    mrr         = sum(_user_monthly_price(u) for u in all_users)
    active_subs = plan_counts['pro'] + plan_counts['agency']
    ctx = {
        **_admin_base_ctx(request, 'revenue'),
        'whop':         None,
        'range_key':    range_key,
        'data_source':  'local',
        'fallback': {
            'mrr':         mrr,
            'active_subs': active_subs,
            'plan_counts': plan_counts,
            'total_users': len(all_users),
        },
    }
    return render(request, 'core/admin_revenue.html', ctx)


@admin_required
def admin_cities_manager_view(request):
    msg   = ''
    error = ''
    if request.method == 'POST':
        action = request.POST.get('action', '')
        city   = request.POST.get('city', '').strip().title()
        state  = request.POST.get('state', '').strip().upper()
        if action == 'add':
            if not city or not state:
                error = 'City and state are required.'
            elif add_supported_city(city, state):
                msg = f'{city}, {state} added successfully.'
            else:
                error = f'{city} is already in the supported cities list.'
        elif action == 'remove':
            if remove_supported_city(city):
                msg = f'{city} removed from supported cities.'
            else:
                error = f'City "{city}" not found.'
        elif action == 'bulk_remove':
            names = [n.strip() for n in request.POST.getlist('city_names') if n.strip()]
            removed = bulk_remove_supported_cities(names)
            if removed:
                msg = f'Removed {removed} cit{"y" if removed == 1 else "ies"} from supported list.'
            else:
                error = 'No matching cities were removed.'
    # Admin asked: this page should only list cities we actually have
    # permit data for. Intersect the curated supported_cities list with
    # the distinct (city, state) pairs currently in the permits table,
    # keyed case-insensitively so a 'BHM, AL' entry matches a permit
    # row with 'Bhm, AL'. Dropped entries are still in supported_cities
    # under the hood — they just don't render until a scrape produces
    # at least one matching permit.
    from .db import pg as _pg
    supported = get_supported_cities()
    rows = _pg.query(
        "SELECT DISTINCT lower(city) AS c, upper(state) AS s "
        "FROM permits WHERE city IS NOT NULL AND state IS NOT NULL"
    ) or []
    have = {(r['c'] or '', r['s'] or '') for r in rows}
    visible = [
        c for c in supported
        if (str(c.get('city','')).strip().lower(),
            str(c.get('state','')).strip().upper()) in have
    ]
    states = sorted({c['state'] for c in visible})
    ctx = {
        **_admin_base_ctx(request, 'cities_manager'),
        'supported':   visible,
        'states':      states,
        'city_count':  len(visible),
        'state_count': len(states),
        'hidden_count': len(supported) - len(visible),
        'msg':         msg,
        'error':       error,
    }
    return render(request, 'core/admin_cities_manager.html', ctx)


# ════════════════════════════════════════════════════════════════
# Database Utilities (admin-only)
# ════════════════════════════════════════════════════════════════
# A single small page exposing two dangerous-but-occasionally-needed
# maintenance buttons:
#
#   1. "Wipe permits" — TRUNCATE the permits table (+ runs/ledger
#      sibling tables) and reset every per-scraper counter to 0 so
#      the admin grid + dashboard match the now-empty table. This
#      is the exact same SQL we previously had to ssh in and run by
#      hand whenever the dataset got polluted with bad extractions.
#
#   2. "Backup DB" — shell out to `pg_dump` against
#      $SUPABASE_DATABASE_URL, gzip the result, and write it into
#      ./backups/ inside the project. Retention is capped at the 3
#      MOST RECENT backups so this can never quietly fill the disk.
#
# Both actions are admin-only (decorator) and POST-only (CSRF). The
# wipe button additionally requires the literal confirmation word
# "WIPE" in the request body so a misclick can't drop the table.

import subprocess as _subprocess
import gzip as _gzip
import shutil as _shutil
import http.client as _http_client
import threading as _threading
import urllib.request as _urlreq
import urllib.error as _urlerr
from pathlib import Path as _Path

_BACKUP_DIR = _Path(__file__).resolve().parent.parent / 'backups'
_BACKUP_RETAIN = 3   # keep at most this many *.sql.gz backups on local disk
# NOTE: remote (Supabase Storage) copies are NOT pruned — they're the
# durable copy that must survive container restarts. Manage retention
# in the Supabase dashboard if you want to cap them.

# ── Dropbox helpers (durable backup target) ──────────────────────
# Credentials live in `system_settings` (managed from
# /admin-panel/db-utils/) so the admin can rotate them without
# touching env vars. We use the OAuth 2.0 refresh-token flow:
# the long-lived refresh_token is exchanged for a short-lived
# access_token (~4h) on demand and cached in-process.
#
# Required scopes on the Dropbox app: files.content.write,
# files.content.read, files.metadata.read, files.metadata.write.
# Files are uploaded as /<folder>/<filename> (folder defaults to
# `/Permitlify-Backups`). For "App folder" apps Dropbox maps that
# under /Apps/<AppName>/ automatically.


def _pg_dump_executable() -> str:
    """Find pg_dump on PATH or in common Windows PostgreSQL installs."""
    candidates = []
    configured = (os.environ.get('PG_DUMP_PATH') or '').strip()
    if configured:
        p = _Path(configured)
        candidates.append(p / 'pg_dump.exe' if p.is_dir() else p)
    found = _shutil.which('pg_dump') or _shutil.which('pg_dump.exe')
    if found:
        candidates.append(_Path(found))
    if os.name == 'nt':
        roots = [
            _Path(os.environ.get('ProgramFiles') or r'C:\Program Files') / 'PostgreSQL',
            _Path(os.environ.get('ProgramFiles(x86)') or r'C:\Program Files (x86)') / 'PostgreSQL',
        ]
        for root in roots:
            if not root.exists():
                continue
            for install in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
                candidates.append(install / 'bin' / 'pg_dump.exe')
                candidates.append(install / 'pgAdmin 4' / 'runtime' / 'pg_dump.exe')
    for p in candidates:
        try:
            if p.exists() and p.is_file():
                return str(p)
        except OSError:
            continue
    return ''


def _dbx_normalize_folder(folder: str, app_name: str = '') -> tuple[str, str]:
    """Return (api_folder, app_name) for Dropbox App-folder apps.

    Dropbox's web UI shows App-folder files under /Apps/<app-name>/..., but
    API calls are already scoped to that app root. If an admin pastes the full
    web path, strip the /Apps/<app-name> prefix so uploads land in the visible
    /Apps/<app-name>/<folder> folder instead of a nested /Apps/... folder.
    """
    folder = (folder or _DBX_DEFAULT_FOLDER).strip() or _DBX_DEFAULT_FOLDER
    app_name = (app_name or '').strip().strip('/')
    if folder.lower().startswith(('http://', 'https://')):
        parsed = urllib.parse.urlparse(folder)
        folder = urllib.parse.unquote(parsed.path or '')
    if not folder.startswith('/'):
        folder = '/' + folder
    folder = folder.rstrip('/') or _DBX_DEFAULT_FOLDER
    parts = [p for p in folder.split('/') if p]
    if parts and parts[0].lower() == 'home':
        parts = parts[1:]
    if len(parts) >= 3 and parts[0].lower() == 'apps':
        app_name = app_name or parts[1]
        folder = '/' + '/'.join(parts[2:])
    elif parts:
        folder = '/' + '/'.join(parts)
    return folder, app_name

_DBX_DEFAULT_FOLDER = '/Permitlify-Backups'
_DBX_TOKEN_CACHE = {'access_token': None, 'expires_at': 0.0}
_BACKUP_JOB_LOCK = _threading.Lock()
_BACKUP_JOBS: dict[str, dict] = {}
_BACKUP_ACTIVE_JOB_ID: str | None = None


def _dbx_cfg():
    """Return dict with all Dropbox settings + a `configured` flag.
    Configured means we have enough to obtain an access token —
    either (app_key + app_secret + refresh_token) or a manually
    pasted access_token."""
    app_key       = (get_system_setting('dropbox_app_key') or '').strip()
    app_secret    = (get_system_setting('dropbox_app_secret') or '').strip()
    refresh_token = (get_system_setting('dropbox_refresh_token') or '').strip()
    access_token  = (get_system_setting('dropbox_access_token') or '').strip()
    folder        = (get_system_setting('dropbox_folder') or '').strip() or _DBX_DEFAULT_FOLDER
    app_name      = (get_system_setting('dropbox_app_name') or '').strip()
    folder, app_name = _dbx_normalize_folder(folder, app_name)
    has_refresh = bool(app_key and app_secret and refresh_token)
    return {
        'app_key':       app_key,
        'app_secret':    app_secret,
        'refresh_token': refresh_token,
        'access_token':  access_token,
        'folder':        folder,
        'app_name':      app_name,
        'configured':    has_refresh or bool(access_token),
        'has_refresh':   has_refresh,
    }


def _dbx_web_url(cfg):
    """Build the dropbox.com web URL pointing at the configured
    backup folder so the admin can open it in one click. For
    "App folder" apps Dropbox sandboxes the real path under
    /Apps/<app-name>/, so we prepend that when app_name is set."""
    folder = (cfg.get('folder') or _DBX_DEFAULT_FOLDER).lstrip('/')
    app    = (cfg.get('app_name') or '').strip().strip('/')
    if app:
        return f'https://www.dropbox.com/home/Apps/{app}/{folder}'
    return f'https://www.dropbox.com/home/{folder}'


def _dbx_get_access_token():
    """Return a usable access token or None.
    Prefers the refresh-token flow (long-lived setup). Caches the
    fresh access token in-process for ~3.5h to avoid hammering
    Dropbox's /oauth2/token endpoint on every backup."""
    cfg = _dbx_cfg()
    if not cfg['configured']:
        return None, 'Dropbox not configured'
    now = time.time()
    if cfg['has_refresh']:
        if (_DBX_TOKEN_CACHE['access_token'] and
                _DBX_TOKEN_CACHE['expires_at'] > now + 60):
            return _DBX_TOKEN_CACHE['access_token'], None
        body = urllib.parse.urlencode({
            'grant_type':    'refresh_token',
            'refresh_token': cfg['refresh_token'],
            'client_id':     cfg['app_key'],
            'client_secret': cfg['app_secret'],
        }).encode()
        req = _urlreq.Request('https://api.dropbox.com/oauth2/token',
                              data=body, method='POST',
                              headers={'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with _urlreq.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read())
        except _urlerr.HTTPError as e:
            err = (e.read() or b'').decode('utf-8', 'replace')[:200]
            return None, f'Dropbox token refresh failed (HTTP {e.code}): {err}'
        except Exception as e:
            return None, f'Dropbox token refresh error: {e}'
        tok = payload.get('access_token')
        if not tok:
            return None, 'Dropbox token refresh: no access_token in response'
        _DBX_TOKEN_CACHE['access_token'] = tok
        _DBX_TOKEN_CACHE['expires_at'] = now + int(payload.get('expires_in', 14400)) - 60
        return tok, None
    # Fallback: manually-pasted access token. Short-lived (~4h)
    # unless the app is configured for legacy long-lived tokens.
    return cfg['access_token'], None


def _dbx_rpc(endpoint, payload, *, timeout=120):
    """Call a Dropbox RPC endpoint (POST JSON, get JSON back).
    Returns (status, parsed_json_or_None, raw_text)."""
    tok, err = _dbx_get_access_token()
    if not tok:
        return 0, None, err or 'no token'
    req = _urlreq.Request(f'https://api.dropboxapi.com/2/{endpoint}',
                          data=json.dumps(payload).encode(), method='POST',
                          headers={'Authorization': f'Bearer {tok}',
                                   'Content-Type': 'application/json'})
    try:
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:    return resp.status, json.loads(raw), raw.decode('utf-8', 'replace')
            except: return resp.status, None, raw.decode('utf-8', 'replace')
    except _urlerr.HTTPError as e:
        raw = (e.read() or b'').decode('utf-8', 'replace')
        return e.code, None, raw
    except Exception as e:
        return 0, None, str(e)


def _dbx_upload_backup(filename, local_path, progress_cb=None):
    """Upload a local backup file to the configured Dropbox folder.
    Returns (ok, message)."""
    cfg = _dbx_cfg()
    if not cfg['configured']:
        return False, 'Dropbox not configured'
    tok, err = _dbx_get_access_token()
    if not tok:
        return False, err or 'no token'
    api_arg = json.dumps({
        'path':       f'{cfg["folder"]}/{filename}',
        'mode':       'overwrite',
        'autorename': False,
        'mute':       True,
        'strict_conflict': False,
    })
    total = int(_Path(local_path).stat().st_size)
    conn = None
    try:
        conn = _http_client.HTTPSConnection('content.dropboxapi.com', timeout=600)
        conn.putrequest('POST', '/2/files/upload')
        conn.putheader('Authorization', f'Bearer {tok}')
        conn.putheader('Content-Type', 'application/octet-stream')
        conn.putheader('Dropbox-API-Arg', api_arg)
        conn.putheader('Content-Length', str(total))
        conn.endheaders()
        sent = 0
        with open(local_path, 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                if progress_cb:
                    progress_cb(sent, total)
        resp = conn.getresponse()
        raw = resp.read()
        if 200 <= resp.status < 300:
            return True, f'uploaded to Dropbox ({cfg["folder"]}/{filename})'
        msg = raw.decode('utf-8', 'replace')[:200]
        return False, f'upload HTTP {resp.status}: {msg}'
    except Exception as e:
        return False, f'upload error: {e}'
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


def _dbx_list_backups():
    """List every file in the configured Dropbox folder.

    Returns ``(items, error)`` — ``items`` is a list of dicts shaped
    like ``_list_db_backups``; ``error`` is ``None`` on success or a
    short human-readable string when the Dropbox API call failed
    (missing scope, wrong folder, token expired, etc.). The error is
    surfaced to the admin UI so a silent empty list never masks a
    real config problem.

    Lists ALL files (not just the ``permitlify-*.sql.gz`` pattern)
    so anything the admin uploads to the backup folder shows up.
    Folder entries + dotfiles (e.g. ``.permitlify-write-test``)
    are skipped."""
    cfg = _dbx_cfg()
    if not cfg['configured']:
        return [], None
    # Dropbox quirk: ``files/list_folder`` requires path='' (empty
    # string) to list the *app root* of an App-folder app. Passing
    # '/Permitlify-Backups' works only when the folder actually
    # exists; otherwise we get a 409 path/not_found and silently
    # return []. We try the configured folder first, then fall back
    # to listing the app root and filtering — that way we surface
    # files even when the saved folder name drifted (case / typo /
    # legacy) from what's actually in Dropbox.
    # Strategy: always do a recursive list from the app root
    # (``path=''``). For App-folder apps the root IS the app sandbox
    # (i.e. /Apps/<app-name>/) so this walks the configured backup
    # subfolder AND any sibling folders the admin might have created
    # by hand. For Full-Dropbox apps `path=''` lists the whole
    # account — which would be too much — so we fall back to the
    # configured folder in that case.
    paths_to_try = ['(app root, recursive)']
    status, body, raw = _dbx_rpc('files/list_folder', {
        'path': '', 'recursive': True, 'include_deleted': False,
        'include_media_info': False, 'include_has_explicit_shared_members': False,
    })
    if status != 200 or not body:
        # Probably a Full-Dropbox app — try the configured folder.
        paths_to_try.append(cfg['folder'])
        status, body, raw = _dbx_rpc('files/list_folder', {
            'path': cfg['folder'], 'recursive': True,
            'include_deleted': False, 'include_media_info': False,
            'include_has_explicit_shared_members': False,
        })
    if status == 409:
        # Folder genuinely doesn't exist yet — treat as empty, no
        # error (Dropbox auto-creates on first upload).
        return [], None
    if status != 200 or not body:
        snippet = (raw or '')[:200] if isinstance(raw, str) else (raw or b'')[:200]
        try: print(f'[dbx-list] HTTP {status} on {paths_to_try!r}: {snippet}', flush=True)
        except Exception: pass
        # Friendly error message so the admin can act on it.
        err = f'Dropbox list_folder HTTP {status}'
        try:
            txt = snippet.decode() if isinstance(snippet, (bytes, bytearray)) else snippet
            if 'missing_scope' in txt or 'files.metadata' in txt:
                err = ('Dropbox token is missing the files.metadata.read '
                       'scope. Add it on the Dropbox app page and '
                       'click "Generate refresh token" again.')
            elif 'expired_access_token' in txt or 'invalid_access_token' in txt:
                err = 'Dropbox access token expired — refresh your token.'
            elif txt:
                err = f'Dropbox list_folder HTTP {status}: {txt[:140]}'
        except Exception:
            pass
        return [], err
    out = []
    for it in body.get('entries', []):
        if it.get('.tag') != 'file':
            continue
        name = it.get('name') or ''
        if name.startswith('.'):
            continue
        size = int(it.get('size') or 0)
        ts = (it.get('server_modified') or it.get('client_modified') or '')[:19]
        try:
            mt = datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S')
        except Exception:
            mt = datetime.utcnow()
        out.append({'name': name, 'size': size,
                    'size_mb': round(size / (1024 * 1024), 2), 'mtime': mt,
                    'remote_path': it.get('path_display') or it.get('path_lower') or f'{cfg["folder"]}/{name}'})
    return out, None


def _dbx_delete_backup(filename):
    """Delete a single backup file from Dropbox. Returns True on
    success / not-configured / not-found so the delete flow never
    blocks the admin's UI."""
    cfg = _dbx_cfg()
    if not cfg['configured']:
        return True
    paths = {f'{cfg["folder"]}/{filename}'}
    try:
        remote_items, _ = _dbx_list_backups()
        for item in remote_items:
            if item.get('name') == filename and item.get('remote_path'):
                paths.add(item['remote_path'])
    except Exception:
        pass
    ok = True
    for path in paths:
        status, _, _ = _dbx_rpc('files/delete_v2', {'path': path})
        if status not in (200, 409):
            ok = False
    return ok


def _dbx_download_backup(filename):
    """Stream a backup file back from Dropbox. Returns raw bytes
    or None if not found / unconfigured."""
    cfg = _dbx_cfg()
    if not cfg['configured']:
        return None
    tok, _ = _dbx_get_access_token()
    if not tok:
        return None
    paths = [f'{cfg["folder"]}/{filename}']
    try:
        remote_items, _ = _dbx_list_backups()
        for item in remote_items:
            p = item.get('remote_path')
            if item.get('name') == filename and p and p not in paths:
                paths.append(p)
    except Exception:
        pass
    for path in paths:
        api_arg = json.dumps({'path': path})
        req = _urlreq.Request(
            'https://content.dropboxapi.com/2/files/download',
            method='POST',
            headers={'Authorization':   f'Bearer {tok}',
                     'Dropbox-API-Arg': api_arg},
        )
        try:
            with _urlreq.urlopen(req, timeout=300) as resp:
                if resp.status == 200:
                    return resp.read()
        except Exception:
            continue
    return None


def _list_db_backups():
    """Local-only backups (kept for prune logic).
    Returns dicts newest-first with size + mtime."""
    if not _BACKUP_DIR.exists():
        return []
    out = []
    for p in _BACKUP_DIR.glob('permitlify-*.sql.gz'):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            'name':     p.name,
            'size':     st.st_size,
            'size_mb':  round(st.st_size / (1024 * 1024), 2),
            'mtime':    datetime.utcfromtimestamp(st.st_mtime),
        })
    out.sort(key=lambda b: b['mtime'], reverse=True)
    return out


def _list_all_backups():
    """Merged view: every local file + every remote object, deduped
    by filename. Each row gets `local` / `remote` flags so the UI
    can show where each backup actually lives. This is what the
    admin page renders — so remote-only backups still appear after
    a redeploy wipes local disk.

    Returns ``(merged_list, dbx_error_or_None)`` so the caller can
    surface a real Dropbox failure (e.g. missing scope) instead of
    silently dropping remote backups."""
    local = {b['name']: b for b in _list_db_backups()}
    remote_items, dbx_err = _dbx_list_backups()
    remote = {b['name']: b for b in remote_items}
    names = set(local) | set(remote)
    merged = []
    for name in names:
        loc = local.get(name)
        rem = remote.get(name)
        src = loc or rem
        merged.append({
            'name':    name,
            'size':    src['size'],
            'size_mb': src['size_mb'],
            'mtime':   src['mtime'],
            'local':   bool(loc),
            'remote':  bool(rem),
            'remote_path': (rem or {}).get('remote_path') or '',
        })
    merged.sort(key=lambda b: b['mtime'], reverse=True)
    return merged, dbx_err


def _backup_to_json(b):
    """Shape a backup dict for the AJAX client. ISO timestamps so
    the browser can format them in the user's local timezone."""
    return {
        'name':         b['name'],
        'size':         b['size'],
        'size_mb':      b['size_mb'],
        'mtime_iso':    b['mtime'].isoformat() + 'Z',
        'mtime_human':  b['mtime'].strftime('%Y-%m-%d %H:%M:%S UTC'),
        'local':        bool(b.get('local', True)),
        'remote':       bool(b.get('remote', False)),
        'remote_path':  b.get('remote_path') or '',
        'download_url': f'/admin-panel/db-utils/backup/{b["name"]}/download/',
        'delete_url':   f'/admin-panel/db-utils/backup/{b["name"]}/delete/',
    }


def _prune_db_backups(keep: int = _BACKUP_RETAIN):
    """Delete oldest backups so at most `keep` remain on disk."""
    backups = _list_db_backups()
    removed = []
    for old in backups[keep:]:
        try:
            (_BACKUP_DIR / old['name']).unlink()
            removed.append(old['name'])
        except OSError:
            pass
    return removed


def _utc_now_label() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


def _backup_job_update(job_id: str, **updates) -> dict:
    updates.setdefault('updated_at', _utc_now_label())
    with _BACKUP_JOB_LOCK:
        job = _BACKUP_JOBS.setdefault(job_id, {})
        job.update(updates)
        return dict(job)


def _backup_job_snapshot(job_id: str) -> dict | None:
    with _BACKUP_JOB_LOCK:
        job = _BACKUP_JOBS.get(job_id)
        return dict(job) if job else None


def _backup_active_job() -> dict | None:
    with _BACKUP_JOB_LOCK:
        if not _BACKUP_ACTIVE_JOB_ID:
            return None
        job = _BACKUP_JOBS.get(_BACKUP_ACTIVE_JOB_ID)
        if job and not job.get('done'):
            return dict(job)
        return None


def _create_db_backup(*, source: str = 'manual', progress_cb=None) -> dict:
    """Create a full database dump and upload it to Dropbox when configured."""
    def progress(percent: int, stage: str, message: str = '', **extra) -> None:
        if progress_cb:
            progress_cb(max(0, min(int(percent), 100)), stage, message, **extra)

    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    fp = _BACKUP_DIR / f'permitlify-{ts}.sql.gz'
    dsn = os.environ['SUPABASE_DATABASE_URL']
    pg_dump = _pg_dump_executable()
    if not pg_dump:
        return {'ok': False, 'error': 'pg_dump is not installed or not configured. Install PostgreSQL client tools or set PG_DUMP_PATH.'}

    progress(8, 'dump', 'Running pg_dump for the full database...')
    try:
        with _gzip.open(fp, 'wb') as gz:
            proc = _subprocess.run(
                [pg_dump, '--no-owner', '--no-acl', '--format=plain', dsn],
                stdout=_subprocess.PIPE, stderr=_subprocess.PIPE,
                check=False, timeout=600,
            )
            if proc.returncode != 0:
                try: fp.unlink()
                except OSError: pass
                err = proc.stderr.decode('utf-8', 'replace')[:400] or 'pg_dump failed'
                return {'ok': False, 'error': 'Backup failed: ' + err}
            progress(42, 'compress', 'Compressing SQL dump...')
            gz.write(proc.stdout)
    except _subprocess.TimeoutExpired:
        try: fp.unlink()
        except OSError: pass
        return {'ok': False, 'error': 'Backup timed out after 10 minutes.', 'status': 504}
    except FileNotFoundError:
        try: fp.unlink()
        except OSError: pass
        return {'ok': False, 'error': 'pg_dump is not installed on this server.'}
    except Exception as e:
        try: fp.unlink()
        except OSError: pass
        return {'ok': False, 'error': f'Backup failed: {e}'}

    size = fp.stat().st_size if fp.exists() else 0
    progress(52, 'upload', f'Uploading {round(size / (1024 * 1024), 2)} MB backup to Dropbox...',
             filename=fp.name, size=size, upload_sent=0, upload_total=size)

    def upload_progress(sent: int, total: int) -> None:
        pct = 52 + round((sent / max(total, 1)) * 40)
        progress(pct, 'upload',
                 f'Uploading to Dropbox: {round(sent / (1024 * 1024), 2)} / {round(total / (1024 * 1024), 2)} MB',
                 filename=fp.name, size=size, upload_sent=sent, upload_total=total)

    dbx_ok, dbx_msg = _dbx_upload_backup(fp.name, fp, upload_progress)
    progress(94, 'finalize', 'Refreshing backup list...', filename=fp.name,
             size=size, dbx_ok=dbx_ok, dbx_msg=dbx_msg)

    pruned = _prune_db_backups(_BACKUP_RETAIN)
    msg = f'Created backup {fp.name}'
    if dbx_ok:
        msg += ' - ' + dbx_msg
    else:
        msg += ' - WARNING: ' + dbx_msg + ' (file is on local disk only until Dropbox upload succeeds)'
    if pruned:
        msg += f' (pruned {len(pruned)} older local backup(s) - keeping {_BACKUP_RETAIN} most recent on disk; Dropbox copies are never pruned)'
    merged, dbx_list_error = _list_all_backups()
    backups = [_backup_to_json(b) for b in merged]
    return {'ok': True, 'msg': msg, 'filename': fp.name, 'size': size,
            'pruned': pruned, 'dbx_ok': dbx_ok, 'dbx_msg': dbx_msg,
            'backups': backups, 'retain': _BACKUP_RETAIN,
            'dbx_list_error': dbx_list_error, 'source': source}


def _run_backup_job(job_id: str, *, source: str = 'manual') -> None:
    global _BACKUP_ACTIVE_JOB_ID
    def progress(percent: int, stage: str, message: str = '', **extra) -> None:
        _backup_job_update(job_id, percent=percent, stage=stage,
                           message=message, **extra)

    _backup_job_update(job_id, percent=2, stage='start', message='Starting backup...',
                       started_at=_utc_now_label())
    result = _create_db_backup(source=source, progress_cb=progress)
    if result.get('ok'):
        updates = dict(result)
        updates.update(done=True, percent=100, stage='done',
                       message=result.get('msg') or 'Backup complete.',
                       finished_at=_utc_now_label())
        _backup_job_update(job_id, **updates)
    else:
        _backup_job_update(job_id, done=True, ok=False, percent=100,
                           stage='error', message=result.get('error') or 'Backup failed.',
                           error=result.get('error') or 'Backup failed.',
                           finished_at=_utc_now_label())
    if source == 'cron':
        final = _backup_job_snapshot(job_id) or {}
        set_system_setting('db_backup_cron_last_finished_at', final.get('finished_at') or _utc_now_label())
        set_system_setting('db_backup_cron_last_status', 'success' if final.get('ok') else 'failed')
        set_system_setting('db_backup_cron_last_message', final.get('message') or final.get('error') or '')
        set_system_setting('db_backup_cron_last_filename', final.get('filename') or '')
        set_system_setting('db_backup_cron_last_job_id', job_id)
    with _BACKUP_JOB_LOCK:
        if _BACKUP_ACTIVE_JOB_ID == job_id:
            _BACKUP_ACTIVE_JOB_ID = None
        # Keep the latest few jobs for polling/history; older in-memory rows
        # are not useful after the browser has consumed them.
        if len(_BACKUP_JOBS) > 8:
            for old_id in sorted(_BACKUP_JOBS, key=lambda k: _BACKUP_JOBS[k].get('created_at', ''))[:-8]:
                if old_id != _BACKUP_ACTIVE_JOB_ID:
                    _BACKUP_JOBS.pop(old_id, None)


def _start_backup_job(*, source: str = 'manual') -> dict:
    global _BACKUP_ACTIVE_JOB_ID
    with _BACKUP_JOB_LOCK:
        if _BACKUP_ACTIVE_JOB_ID:
            active = _BACKUP_JOBS.get(_BACKUP_ACTIVE_JOB_ID)
            if active and not active.get('done'):
                return dict(active)
        job_id = secrets.token_urlsafe(12)
        job = {
            'ok': True, 'job_id': job_id, 'source': source, 'done': False,
            'percent': 0, 'stage': 'queued', 'message': 'Queued...',
            'created_at': _utc_now_label(), 'updated_at': _utc_now_label(),
        }
        _BACKUP_JOBS[job_id] = job
        _BACKUP_ACTIVE_JOB_ID = job_id
    thread = _threading.Thread(target=_run_backup_job, args=(job_id,),
                               kwargs={'source': source},
                               name=f'permitlify-db-backup-{source}', daemon=True)
    thread.start()
    return dict(job)


def _load_db_backup_cron_settings() -> dict:
    enabled_raw = get_system_setting('db_backup_cron_enabled', None)
    enabled = False if enabled_raw is None else bool(enabled_raw)
    at_utc = (get_system_setting('db_backup_cron_at_utc') or '03:00').strip()
    if not re.fullmatch(r'\d{2}:\d{2}', at_utc or ''):
        at_utc = '03:00'
    try:
        window = int(get_system_setting('db_backup_cron_window_minutes') or 30)
    except (TypeError, ValueError):
        window = 30
    return {
        'enabled': enabled,
        'at_utc': at_utc,
        'window_minutes': max(1, min(window, 720)),
        'last_check_at': get_system_setting('db_backup_cron_last_check_at') or '',
        'last_outcome': get_system_setting('db_backup_cron_last_outcome') or '',
        'last_started_at': get_system_setting('db_backup_cron_last_started_at') or '',
        'last_finished_at': get_system_setting('db_backup_cron_last_finished_at') or '',
        'last_status': get_system_setting('db_backup_cron_last_status') or '',
        'last_message': get_system_setting('db_backup_cron_last_message') or '',
        'last_filename': get_system_setting('db_backup_cron_last_filename') or '',
        'last_job_id': get_system_setting('db_backup_cron_last_job_id') or '',
    }


def _db_backup_cron_stamp(outcome: str) -> None:
    set_system_setting('db_backup_cron_last_check_at', _utc_now_label())
    set_system_setting('db_backup_cron_last_outcome', outcome)


def _db_backup_cron_tick() -> dict:
    settings = _load_db_backup_cron_settings()
    if not settings['enabled']:
        outcome = 'skipped: db backup cron disabled'
        _db_backup_cron_stamp(outcome)
        return {'ok': True, 'fired': False, 'outcome': outcome}
    now = datetime.utcnow()
    try:
        hh, mm = settings['at_utc'].split(':')
        target = int(hh) * 60 + int(mm)
        cur = now.hour * 60 + now.minute
    except Exception:
        outcome = f'skipped: invalid db_backup_cron_at_utc={settings["at_utc"]!r}'
        _db_backup_cron_stamp(outcome)
        return {'ok': True, 'fired': False, 'outcome': outcome}
    raw_delta = abs(cur - target)
    delta = min(raw_delta, 1440 - raw_delta)
    if delta > settings['window_minutes']:
        outcome = (f'skipped: now {now.strftime("%H:%M")} UTC is outside '
                   f'{settings["at_utc"]} +/- {settings["window_minutes"]}m window')
        _db_backup_cron_stamp(outcome)
        return {'ok': True, 'fired': False, 'outcome': outcome}
    slot = f'{now.strftime("%Y-%m-%d")} {settings["at_utc"]} UTC'
    if (get_system_setting('db_backup_cron_last_slot') or '').strip() == slot:
        outcome = f'skipped: already fired slot {slot}'
        _db_backup_cron_stamp(outcome)
        return {'ok': True, 'fired': False, 'outcome': outcome, 'slot': slot}
    active = _backup_active_job()
    if active:
        outcome = f'skipped: backup job already running ({active.get("job_id")})'
        _db_backup_cron_stamp(outcome)
        return {'ok': True, 'fired': False, 'outcome': outcome, 'slot': slot}
    set_system_setting('db_backup_cron_last_slot', slot)
    set_system_setting('db_backup_cron_last_started_at', _utc_now_label())
    job = _start_backup_job(source='cron')
    set_system_setting('db_backup_cron_last_job_id', job.get('job_id') or '')
    outcome = f'fired: started backup job {job.get("job_id")} for slot {slot}'
    _db_backup_cron_stamp(outcome)
    return {'ok': True, 'fired': True, 'outcome': outcome,
            'slot': slot, 'job_id': job.get('job_id')}


@admin_required
def admin_db_utils_view(request):
    """Render the DB Utilities admin page."""
    # Live counts so the user can see exactly what a wipe would
    # destroy before clicking the button.
    with psycopg.connect(os.environ['SUPABASE_DATABASE_URL']) as c, c.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM permits'); permits_count = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM scrapers'); scrapers_count = cur.fetchone()[0]
        try:
            cur.execute('SELECT COUNT(*) FROM scraper_runs'); runs_count = cur.fetchone()[0]
        except Exception:
            runs_count = 0
        try:
            cur.execute('SELECT COUNT(*) FROM claude_calls'); cl_count = cur.fetchone()[0]
        except Exception:
            cl_count = 0
        try:
            cur.execute('SELECT COUNT(*) FROM junk_permits'); junk_count = cur.fetchone()[0]
        except Exception:
            junk_count = 0

    backups, dbx_list_error = _list_all_backups()
    dbx = _dbx_cfg()
    pg_dump_path = _pg_dump_executable()
    ctx = {
        **_admin_base_ctx(request, 'db_utils'),
        'permits_count':  permits_count,
        'scrapers_count': scrapers_count,
        'runs_count':     runs_count,
        'claude_count':    cl_count,
        'junk_count':      junk_count,
        'backups':        backups,
        # Raw list (not JSON string) so the template can use the
        # `json_script` filter, which escapes any embedded `</script>`
        # / HTML-significant chars safely. Seeds the AJAX table on
        # first paint before the first XHR round-trip completes.
        'backups_initial': [_backup_to_json(b) for b in backups],
        'dbx_list_error': dbx_list_error or '',
        'backup_retain':  _BACKUP_RETAIN,
        'pg_dump_available': bool(pg_dump_path),
        'pg_dump_path':   pg_dump_path,
        'backup_active_job': _backup_active_job(),
        'db_backup_cron': _load_db_backup_cron_settings(),
        # Dropbox settings. Secrets are never echoed back to the
        # template — we just emit a `has_*` boolean so the input
        # can render a masked placeholder.
        'dbx_app_key':     dbx['app_key'],
        'dbx_folder':      dbx['folder'],
        'dbx_app_name':    dbx['app_name'],
        'dbx_web_url':     _dbx_web_url(dbx),
        'dbx_web_path':    _dbx_web_url(dbx),
        'dbx_configured':  dbx['configured'],
        'dbx_has_secret':  bool(dbx['app_secret']),
        'dbx_has_refresh': bool(dbx['refresh_token']),
        'dbx_has_access':  bool(dbx['access_token']),
        'msg':             request.GET.get('msg', ''),
        'error':           request.GET.get('error', ''),
    }
    return render(request, 'core/admin_db_utils.html', ctx)


@admin_required
@require_http_methods(['POST'])
def admin_db_utils_storage_save(request):
    """Save Dropbox credentials to system_settings, then probe the
    connection so the admin gets immediate feedback. Returns JSON
    for AJAX, redirects otherwise. Empty fields clear the value;
    the masked placeholder (`••••`) means "leave as-is"."""
    app_key       = (request.POST.get('dropbox_app_key') or '').strip()
    app_secret    = (request.POST.get('dropbox_app_secret') or '').strip()
    refresh_token = (request.POST.get('dropbox_refresh_token') or '').strip()
    access_token  = (request.POST.get('dropbox_access_token') or '').strip()
    folder        = (request.POST.get('dropbox_folder') or '').strip() or _DBX_DEFAULT_FOLDER
    app_name      = (request.POST.get('dropbox_app_name') or '').strip().strip('/')
    web_path      = (request.POST.get('dropbox_web_path') or '').strip()
    folder, app_name = _dbx_normalize_folder(web_path or folder, app_name)

    set_system_setting('dropbox_app_key',  app_key)
    set_system_setting('dropbox_folder',   folder)
    set_system_setting('dropbox_app_name', app_name)
    # Secrets: only overwrite when the field changed (not the masked placeholder)
    if app_secret and not app_secret.startswith('•'):
        set_system_setting('dropbox_app_secret', app_secret)
    elif not app_secret:
        set_system_setting('dropbox_app_secret', '')
    if refresh_token and not refresh_token.startswith('•'):
        set_system_setting('dropbox_refresh_token', refresh_token)
    elif not refresh_token:
        set_system_setting('dropbox_refresh_token', '')
    if access_token and not access_token.startswith('•'):
        set_system_setting('dropbox_access_token', access_token)
    elif not access_token:
        set_system_setting('dropbox_access_token', '')

    # Reset the in-process token cache so the next call uses the
    # freshly saved credentials.
    _DBX_TOKEN_CACHE['access_token'] = None
    _DBX_TOKEN_CACHE['expires_at']   = 0.0

    # Connectivity probe: do a real end-to-end write test by
    # uploading a tiny sentinel file then deleting it. This catches
    # all the real-world failure modes the cheap /check/user probe
    # misses — missing/unsubmitted scopes, wrong refresh token,
    # locked App folder, bad folder path, etc.
    probe = {'ok': True, 'msg': 'Saved.'}
    cfg = _dbx_cfg()
    if cfg['configured']:
        tok, err = _dbx_get_access_token()
        if not tok:
            probe = {'ok': False,
                     'msg': 'Saved, but Dropbox token refresh failed: ' + (err or '') +
                            '. Double-check App key, App secret, and Refresh token.'}
        else:
            test_name = '.permitlify-write-test'
            ok, msg = _dbx_write_test(tok, cfg['folder'], test_name)
            if ok:
                probe['msg'] = (
                    f'Saved + verified — Dropbox write test succeeded. Backups will '
                    f'upload to {cfg["folder"]}/. Tip: for "App folder" apps Dropbox '
                    f'places this under /Apps/<your-app-name>{cfg["folder"]}/ in your '
                    f'actual Dropbox.')
            else:
                # Surface the "missing_scope" case as a specific, loud
                # call to action — this is almost always the issue when
                # scopes are visibly ticked on the Permissions tab. The
                # token was minted BEFORE the scopes were submitted, so
                # it still has the old (empty) scope set. The fix is
                # NOT to re-check the permissions tab — it's to
                # regenerate the refresh token via the helper.
                if 'missing_scope' in msg:
                    probe = {'ok': False,
                             'msg': ('Saved, but Dropbox rejected the upload with '
                                     'missing_scope. Your refresh token was minted '
                                     'BEFORE you submitted the scopes, so it still '
                                     'has the old (empty) scope set. ✅ Fix: scroll '
                                     'up to the yellow "🛠 Refresh-token helper" box, '
                                     'click "🔗 Open Dropbox authorize page" again to '
                                     'generate a NEW refresh token (the consent screen '
                                     'will now list the scopes), paste the new auth '
                                     'code, click Exchange, then click Save & verify '
                                     'connection. Raw error: ' + msg)}
                else:
                    probe = {'ok': False,
                             'msg': 'Saved, but Dropbox write test FAILED: ' + msg +
                                    '. Most common cause: app permissions weren\'t '
                                    'submitted — go to your Dropbox app → Permissions tab, '
                                    'ensure files.content.write + files.content.read + '
                                    'files.metadata.read/write are checked, then click '
                                    '"Submit". You may need to re-generate the refresh '
                                    'token after changing scopes.'}
    if _is_ajax(request):
        return JsonResponse(probe)
    return redirect('/admin-panel/db-utils/?msg=' + urllib.parse.quote(probe['msg']))


@admin_required
@require_http_methods(['POST'])
def admin_db_utils_dropbox_exchange(request):
    """Exchange a one-time Dropbox authorization code for a long-lived
    refresh token. Saves the admin from having to run curl manually
    after the OAuth /authorize redirect — they just paste the code
    here and we hit /oauth2/token with grant_type=authorization_code.

    The most common mistake users make on the Dropbox card is pasting
    the auth code into the "Refresh token" field (which then fails at
    refresh time with "invalid_grant: refresh token is malformed").
    This helper makes the right path one click instead of a 3-step
    terminal dance.

    Returns JSON {ok, refresh_token, error?}. The caller is expected
    to populate the form field and submit — we deliberately do NOT
    persist the refresh token here so the admin still has to click
    Save (and run the full write-test probe)."""
    app_key    = (request.POST.get('app_key') or '').strip()
    app_secret = (request.POST.get('app_secret') or '').strip()
    code       = (request.POST.get('code') or '').strip()
    if not (app_key and app_secret and code):
        return JsonResponse({'ok': False,
            'error': 'App key, App secret, and Authorization code are all required.'},
            status=400)
    # If the secret field came through as the masked placeholder
    # (•••), fall back to the saved value so the admin doesn't have
    # to re-type it just to run the exchange.
    if app_secret.startswith('•'):
        app_secret = (get_system_setting('dropbox_app_secret') or '').strip()
        if not app_secret:
            return JsonResponse({'ok': False,
                'error': 'App secret not saved yet — type it in before exchanging.'},
                status=400)
    body = urllib.parse.urlencode({
        'code': code, 'grant_type': 'authorization_code',
        'client_id': app_key, 'client_secret': app_secret,
    }).encode()
    req = _urlreq.Request('https://api.dropbox.com/oauth2/token',
                          data=body, method='POST',
                          headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with _urlreq.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except _urlerr.HTTPError as e:
        raw = (e.read() or b'').decode('utf-8', 'replace')[:300]
        hint = ''
        if 'invalid_grant' in raw:
            hint = (' Common causes: the code was already used (each code is '
                    'single-use — generate a fresh one from /oauth2/authorize), '
                    'or the code was copied with extra whitespace, or App key / '
                    'secret don\'t match the app that issued the code.')
        return JsonResponse({'ok': False,
            'error': f'Dropbox returned HTTP {e.code}: {raw}.{hint}'},
            status=400)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Network error: {e}'}, status=500)
    refresh = payload.get('refresh_token')
    if not refresh:
        return JsonResponse({'ok': False,
            'error': ('Dropbox did not return a refresh token. Make sure your '
                      '/oauth2/authorize URL included token_access_type=offline.')},
            status=400)
    return JsonResponse({'ok': True, 'refresh_token': refresh,
                         'msg': 'Got it — refresh token filled in. Click "Save & verify connection" to persist.'})


def _dbx_write_test(tok, folder, name):
    """End-to-end write probe: upload a 1-byte file, then delete it.
    Returns (ok, message). Exposes the real Dropbox error body on
    failure so the admin can fix it without guessing."""
    api_arg = json.dumps({
        'path': f'{folder}/{name}',
        'mode': 'overwrite', 'autorename': False, 'mute': True,
    })
    req = _urlreq.Request(
        'https://content.dropboxapi.com/2/files/upload',
        data=b'.', method='POST',
        headers={'Authorization': f'Bearer {tok}',
                 'Content-Type': 'application/octet-stream',
                 'Dropbox-API-Arg': api_arg})
    try:
        with _urlreq.urlopen(req, timeout=30) as resp:
            if not (200 <= resp.status < 300):
                return False, f'upload HTTP {resp.status}'
    except _urlerr.HTTPError as e:
        raw = (e.read() or b'').decode('utf-8', 'replace')[:300]
        return False, f'HTTP {e.code} from /files/upload — {raw}'
    except Exception as e:
        return False, f'network error contacting Dropbox: {e}'
    # Best-effort cleanup. Don't fail the probe if delete fails —
    # the upload (the part that actually matters) already succeeded.
    try:
        del_req = _urlreq.Request('https://api.dropboxapi.com/2/files/delete_v2',
            data=json.dumps({'path': f'{folder}/{name}'}).encode(), method='POST',
            headers={'Authorization': f'Bearer {tok}',
                     'Content-Type': 'application/json'})
        _urlreq.urlopen(del_req, timeout=15).read()
    except Exception:
        pass
    return True, 'ok'


@admin_required
@require_http_methods(['POST'])
def admin_db_utils_wipe_permits(request):
    """TRUNCATE permits + reset every per-scraper counter to 0.

    Safety: admin-only (decorator), POST-only (CSRF), client-side confirm,
    and server-side literal ``WIPE`` confirmation.
    """
    if (request.POST.get('confirm') or '').strip().upper() != 'WIPE':
        return redirect('/admin-panel/db-utils/?error=' +
                        urllib.parse.quote('Type WIPE to confirm the destructive permit wipe.'))
    deleted = 0
    with psycopg.connect(os.environ['SUPABASE_DATABASE_URL']) as c, c.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM permits'); deleted = cur.fetchone()[0]
        # Only TRUNCATE tables that actually exist in this environment
        # (early dev DBs predate the ledger tables; missing-table errors
        # would otherwise abort the wipe and leave the admin stuck).
        candidate_tables = ['permits', 'scraper_runs', 'claude_calls']
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (candidate_tables,),
        )
        existing = {row[0] for row in cur.fetchall()}
        present = [t for t in candidate_tables if t in existing]
        if present:
            # CASCADE so anything referencing permits.id (e.g. scoring
            # history, future child rows) goes with it. RESTART
            # IDENTITY so the next permit starts at id=1 again.
            cur.execute('TRUNCATE ' + ', '.join(present) + ' RESTART IDENTITY CASCADE')
        # Mirror the empty table in the scrapers grid: no permits,
        # no last-run state, nothing showing as "running".
        cur.execute("UPDATE scrapers SET total_permits = 0, "
                    "last_run_at = NULL, last_run_status = NULL")
        c.commit()

    return redirect('/admin-panel/db-utils/?msg=' +
                    urllib.parse.quote(f'Wiped {deleted:,} permits and reset all scraper counters.'))


@admin_required
@require_http_methods(['POST'])
def admin_db_utils_wipe_junk(request):
    """Empty the junk_permits blacklist.

    junk_permits is the "known-junk" cache scrapers consult to skip rows
    already judged non-actionable. Wiping it forces the next run to
    re-evaluate everything (and re-pay for those detail fetches), which is
    occasionally what an admin wants after tuning the junk heuristics.
    Admin-only + POST-only; the template wraps the button in a confirm().
    """
    from .db import wipe_junk_permits
    try:
        n = wipe_junk_permits()
    except Exception:
        log.exception('wipe_junk_permits failed')
        return redirect('/admin-panel/db-utils/?msg=' +
                        urllib.parse.quote('Could not wipe junk data — see logs.'))
    return redirect('/admin-panel/db-utils/?msg=' +
                    urllib.parse.quote(f'Wiped {n:,} junk records.'))


@admin_required
@require_http_methods(['POST'])
def admin_db_utils_backup_create(request):
    """Shell out to pg_dump, gzip the dump, save to ./backups/,
    then prune oldest backups so at most `_BACKUP_RETAIN` remain.

    Runs synchronously — the admin page shows the resulting file in
    the list after the redirect. Large dumps will block the request
    for a few seconds; that's fine for a manual admin action.
    """
    if _is_ajax(request):
        job = _start_backup_job(source='manual')
        return JsonResponse({'ok': True, 'started': True, 'job': job,
                             'job_id': job.get('job_id')})
    result = _create_db_backup(source='manual')
    if not result.get('ok'):
        return _backup_error(request, result.get('error') or 'Backup failed.',
                             status=int(result.get('status') or 500))
    return redirect('/admin-panel/db-utils/?msg=' + urllib.parse.quote(result.get('msg') or 'Backup created.'))


@admin_required
def admin_db_utils_backup_status(request, job_id: str):
    job = _backup_job_snapshot(job_id)
    if not job:
        return JsonResponse({'ok': False, 'error': 'Unknown backup job.'}, status=404)
    return JsonResponse({'ok': True, 'job': job})


@admin_required
@require_http_methods(['POST'])
def admin_db_utils_backup_cron_save(request):
    enabled = request.POST.get('enabled') == '1'
    at_utc = (request.POST.get('at_utc') or '03:00').strip()
    if not re.fullmatch(r'\d{2}:\d{2}', at_utc):
        at_utc = '03:00'
    try:
        hh, mm = [int(x) for x in at_utc.split(':')]
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            at_utc = '03:00'
    except Exception:
        at_utc = '03:00'
    try:
        window = int(request.POST.get('window_minutes') or 30)
    except (TypeError, ValueError):
        window = 30
    window = max(1, min(window, 720))
    set_system_setting('db_backup_cron_enabled', enabled)
    set_system_setting('db_backup_cron_at_utc', at_utc)
    set_system_setting('db_backup_cron_window_minutes', window)
    set_system_setting('db_backup_cron_saved_at', _utc_now_label())
    msg = f'Daily DB backup cron saved: {"enabled" if enabled else "disabled"} at {at_utc} UTC.'
    if _is_ajax(request):
        return JsonResponse({'ok': True, 'msg': msg,
                             'settings': _load_db_backup_cron_settings()})
    return redirect('/admin-panel/db-utils/?msg=' + urllib.parse.quote(msg))


def _is_ajax(request):
    """True for fetch()/XHR calls from our admin page."""
    return (request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in (request.headers.get('Accept') or ''))


def _backup_error(request, message, *, status=500):
    """Uniform error shape: JSON for AJAX, redirect for legacy."""
    if _is_ajax(request):
        return JsonResponse({'ok': False, 'error': message}, status=status)
    return redirect('/admin-panel/db-utils/?error=' + urllib.parse.quote(message))


@admin_required
def admin_db_utils_dropbox_diag(request):
    """Raw diagnostic dump of the Dropbox account/folder state so we
    can debug a "file exists in Dropbox but doesn't show up here"
    report. Calls a few read-only Dropbox endpoints and returns the
    JSON unmodified. Admin-only — no secrets leak (we don't echo
    tokens, only the account email + folder contents)."""
    cfg = _dbx_cfg()
    out = {'configured': cfg['configured'], 'folder': cfg['folder'],
           'app_name': cfg['app_name'], 'has_refresh': cfg['has_refresh'],
           'has_access': bool(cfg['access_token'])}
    if not cfg['configured']:
        return JsonResponse(out)
    tok, err = _dbx_get_access_token()
    out['token_ok'] = bool(tok); out['token_err'] = err
    if not tok:
        return JsonResponse(out)
    # Who am I? confirms the right Dropbox account is linked.
    s, b, r = _dbx_rpc('users/get_current_account', None)
    out['account_status'] = s
    if isinstance(b, dict):
        out['account_email'] = b.get('email')
        out['account_name']  = (b.get('name') or {}).get('display_name')
        out['account_type']  = (b.get('account_type') or {}).get('.tag')
    else:
        out['account_raw'] = (r or '')[:300]
    # List configured folder (non-recursive).
    s, b, r = _dbx_rpc('files/list_folder', {
        'path': cfg['folder'], 'recursive': False,
        'include_deleted': False, 'include_media_info': False,
        'include_has_explicit_shared_members': False,
    })
    out['list_configured_status'] = s
    if isinstance(b, dict):
        out['list_configured_entries'] = [
            {'name': e.get('name'), 'tag': e.get('.tag'),
             'path_display': e.get('path_display'),
             'size': e.get('size'),
             'server_modified': e.get('server_modified')}
            for e in b.get('entries', [])
        ]
    else:
        out['list_configured_raw'] = (r or '')[:400]
    # List from app root recursively (this is what our normal flow
    # now uses). Shows every file Dropbox returns for this token.
    s, b, r = _dbx_rpc('files/list_folder', {
        'path': '', 'recursive': True,
        'include_deleted': False, 'include_media_info': False,
        'include_has_explicit_shared_members': False,
    })
    out['list_root_status'] = s
    if isinstance(b, dict):
        out['list_root_entries'] = [
            {'name': e.get('name'), 'tag': e.get('.tag'),
             'path_display': e.get('path_display'),
             'size': e.get('size'),
             'server_modified': e.get('server_modified')}
            for e in b.get('entries', [])
        ]
    else:
        out['list_root_raw'] = (r or '')[:400]
    return JsonResponse(out, json_dumps_params={'indent': 2})


@admin_required
def admin_db_utils_backup_list(request):
    """JSON endpoint — returns the current backup list for the
    AJAX-driven backup table on /admin-panel/db-utils/. Lets the
    page refresh after a new backup or delete without a full
    page reload."""
    merged, dbx_list_error = _list_all_backups()
    backups = [_backup_to_json(b) for b in merged]
    return JsonResponse({'ok': True, 'backups': backups,
                         'retain': _BACKUP_RETAIN,
                         'dbx_list_error': dbx_list_error})


@admin_required
def admin_db_utils_backup_download(request, filename: str):
    """Serve a single backup file for download.

    Filename is whitelist-validated against the exact format we
    write, so a user can't craft `../../etc/passwd` style paths.
    Falls back to streaming from Dropbox when the file is no longer
    on local disk (typical after a redeploy).
    """
    import re as _re
    if not _re.fullmatch(r'permitlify-\d{8}-\d{6}\.sql\.gz', filename):
        raise Http404
    fp = _BACKUP_DIR / filename
    if fp.exists():
        with open(fp, 'rb') as f:
            data = f.read()
    else:
        data = _dbx_download_backup(filename)
        if data is None:
            raise Http404
    resp = HttpResponse(data, content_type='application/gzip')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    resp['Content-Length'] = str(len(data))
    return resp


@admin_required
@require_http_methods(['POST'])
def admin_db_utils_backup_delete(request, filename: str):
    """Delete a single backup file (admin-initiated cleanup). Removes
    both the local copy AND the remote Dropbox file so the admin's
    intent is honoured fully.

    Returns JSON for AJAX callers (the in-page backup table) so the
    list can refresh without a full page reload; falls back to the
    original redirect for non-AJAX clients.
    """
    import re as _re
    if not _re.fullmatch(r'permitlify-\d{8}-\d{6}\.sql\.gz', filename):
        return _backup_error(request, 'Invalid backup filename.', status=400)
    fp = _BACKUP_DIR / filename
    if fp.exists():
        try:
            fp.unlink()
        except OSError as e:
            return _backup_error(request, f'Delete failed: {e}', status=500)
    dbx_deleted = _dbx_delete_backup(filename)
    if not dbx_deleted:
        return _backup_error(request, f'Deleted local copy of {filename}, but Dropbox deletion failed. Try again or check Dropbox credentials.', status=502)
    if _is_ajax(request):
        merged, dbx_list_error = _list_all_backups()
        backups = [_backup_to_json(b) for b in merged]
        return JsonResponse({'ok': True, 'msg': f'Deleted {filename}.',
                             'backups': backups, 'retain': _BACKUP_RETAIN,
                             'dbx_list_error': dbx_list_error})
    return redirect('/admin-panel/db-utils/?msg=' +
                    urllib.parse.quote(f'Deleted {filename}.'))


@admin_required
@cached_admin_html(15)
def admin_banned_view(request):
    banned_list = get_all_banned()
    ctx = {
        **_admin_base_ctx(request, 'banned'),
        'banned_list': banned_list,
        'unbanned':    request.GET.get('unbanned', ''),
        'banned_count': len(banned_list),
    }
    return render(request, 'core/admin_banned.html', ctx)


_EMAIL_SERVICES = {
    'billing': {
        'label':       'Billing',
        'icon_color':  '#1d4ed8',
        'bg_color':    'rgba(29,78,216,.1)',
        'description': 'Invoices, plan changes, payment confirmations, and upgrade/downgrade notices.',
        'defaults': {
            'from_email':  'billing@permitlify.com',
            'from_name':   'Permitlify Billing',
            'reply_to':    'billing@permitlify.com',
        },
        'events': [
            {'label': 'Plan upgraded',         'note': 'Sent on upgrade'},
            {'label': 'Plan downgraded',       'note': 'Sent on downgrade'},
            {'label': 'Trial started',         'note': 'Welcome + trial details'},
            {'label': 'Payment receipt',       'note': 'Via Whop'},
            {'label': 'Subscription cancelled','note': 'Via Whop'},
        ],
    },
    'support': {
        'label':       'Support',
        'icon_color':  '#059669',
        'bg_color':    'rgba(5,150,105,.1)',
        'description': 'Ticket confirmations, agent replies, and resolution notices.',
        'defaults': {
            'from_email':  'support@permitlify.com',
            'from_name':   'Permitlify Support',
            'reply_to':    'support@permitlify.com',
        },
        'events': [
            {'label': 'Ticket opened',         'note': 'Confirmation to user'},
            {'label': 'Agent reply',           'note': 'Notifies user of response'},
            {'label': 'Ticket resolved',       'note': 'Closure notice + CSAT'},
        ],
    },
    'alerts': {
        'label':       'Alerts & Notifications',
        'icon_color':  '#dc2626',
        'bg_color':    'rgba(220,38,38,.08)',
        'description': 'Hot lead alerts, daily digests, weekly performance reports, and city coverage updates.',
        'defaults': {
            'from_email':  'alerts@permitlify.com',
            'from_name':   'Permitlify Alerts',
            'reply_to':    'noreply@permitlify.com',
        },
        'events': [
            {'label': 'Hot lead alert (80+)',  'note': 'Within 5 min of filing'},
            {'label': 'Daily digest',          'note': 'Every morning at 6 AM'},
            {'label': 'Weekly report',         'note': 'Monday 6 AM'},
            {'label': 'New city coverage',     'note': 'On coverage expansion'},
        ],
    },
    'marketing': {
        'label':       'Marketing & Onboarding',
        'icon_color':  '#d97706',
        'bg_color':    'rgba(217,119,6,.08)',
        'description': 'Welcome sequences, product updates, feature announcements, and newsletters.',
        'defaults': {
            'from_email':  'hello@permitlify.com',
            'from_name':   'Permitlify Team',
            'reply_to':    'hello@permitlify.com',
        },
        'events': [
            {'label': 'Welcome email',         'note': 'On signup'},
            {'label': 'Onboarding tips',       'note': 'Day 1, 3, 7 sequence'},
            {'label': 'Product update',        'note': 'On new feature launch'},
            {'label': 'Newsletter',            'note': 'Monthly digest'},
        ],
    },
    'system': {
        'label':       'System & Security',
        'icon_color':  '#6b7280',
        'bg_color':    'rgba(107,114,128,.1)',
        'description': 'Password resets, 2FA codes, login alerts, and account security notices.',
        'defaults': {
            'from_email':  'noreply@permitlify.com',
            'from_name':   'Permitlify',
            'reply_to':    '',
        },
        'events': [
            {'label': 'Password reset',        'note': 'Link valid for 1 hour'},
            {'label': '2FA code',              'note': 'Valid for 10 minutes'},
            {'label': 'New login alert',       'note': 'On unrecognised device'},
            {'label': 'Account locked',        'note': 'On repeated failed logins'},
        ],
    },
}


def _email_settings_current() -> dict:
    """Return all service email settings, falling back to defaults."""
    result = {}
    for svc, cfg in _EMAIL_SERVICES.items():
        result[svc] = {
            'from_email': get_system_setting(f'{svc}_from_email', cfg['defaults']['from_email']),
            'from_name':  get_system_setting(f'{svc}_from_name',  cfg['defaults']['from_name']),
            'reply_to':   get_system_setting(f'{svc}_reply_to',   cfg['defaults']['reply_to']),
        }
    return result


@admin_required
def admin_email_settings(request):
    """Admin page for per-service sender configuration + delivery testing.

    Three POST actions, all dispatched via the ``action`` field and all
    returning JSON when called with ``X-Requested-With: XMLHttpRequest``:

    * ``save``        — persist From / From Name / Reply-To for one service
    * ``test_send``   — send a real test email via the configured transport
                        (Resend if RESEND_API_KEY is set, otherwise SMTP)
    * ``verify``      — admin checked their inbox; mark the service as
                        verified (or clear verification)
    """
    from datetime import datetime, timezone
    from .email_service import send_email, get_service_health, get_transport_status, get_smtp_config, get_resend_api_key

    result = {'ok': False, 'action': '', 'service': '', 'error': '', 'detail': ''}

    if request.method == 'POST':
        action = (request.POST.get('action') or 'save').strip()
        svc    = (request.POST.get('service') or '').strip()

        # Transport credentials are global (not per-service), so we handle
        # them before the per-service dispatch.
        if action == 'save_transport':
            try:
                provider = (request.POST.get('provider') or 'resend').strip().lower()
                if provider == 'resend':
                    api_key = (request.POST.get('resend_api_key') or '').strip()
                    # Empty string is treated as "clear it"; non-empty replaces.
                    set_system_setting('email_resend_api_key', api_key)
                    detail = 'Resend API key cleared.' if not api_key else 'Resend API key saved.'
                elif provider == 'smtp':
                    fields = {
                        'email_smtp_host':     (request.POST.get('smtp_host') or '').strip(),
                        'email_smtp_port':     (request.POST.get('smtp_port') or '').strip() or '587',
                        'email_smtp_user':     (request.POST.get('smtp_user') or '').strip(),
                        'email_smtp_use_tls':  '1' if (request.POST.get('smtp_use_tls') or '').strip().lower() in ('1', 'true', 'yes', 'on') else '0',
                    }
                    pwd_raw = request.POST.get('smtp_password')
                    # Only overwrite the password if a new value was actually
                    # submitted — empty string from a "leave blank to keep"
                    # field shouldn't wipe the saved one.
                    if pwd_raw is not None and pwd_raw.strip():
                        fields['email_smtp_password'] = pwd_raw.strip()
                    for k, v in fields.items():
                        set_system_setting(k, v)
                    detail = 'SMTP settings saved.'
                elif provider == 'clear':
                    for k in ('email_resend_api_key', 'email_smtp_host', 'email_smtp_port',
                              'email_smtp_user', 'email_smtp_password', 'email_smtp_use_tls'):
                        set_system_setting(k, '')
                    detail = 'All transport credentials cleared.'
                else:
                    raise ValueError(f'Unknown provider "{provider}"')
                result = {'ok': True, 'action': 'save_transport', 'service': '',
                          'error': '', 'detail': detail}
            except Exception as e:
                result = {'ok': False, 'action': 'save_transport', 'service': '',
                          'error': str(e), 'detail': ''}

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                payload = dict(result)
                payload['transport'] = get_transport_status()
                return JsonResponse(payload)

        elif action == 'save_signup_notify':
            # Top-level (no service) — must be handled BEFORE the
            # `svc not in _EMAIL_SERVICES` guard, because this action
            # intentionally posts an empty service field.
            try:
                enabled_raw = (request.POST.get('enabled') or '').strip().lower()
                enabled     = '1' if enabled_raw in ('1', 'true', 'yes', 'on') else '0'
                to_addr     = (request.POST.get('to') or '').strip()
                if enabled == '1' and ('@' not in to_addr or not to_addr):
                    raise ValueError('A valid recipient email is required when notifications are enabled.')
                set_system_setting('notify_signup_enabled', enabled)
                set_system_setting('notify_signup_to',      to_addr)
                detail = ('Notifications enabled — alerts will be sent to '
                          f'{to_addr} from the Alerts sender.') if enabled == '1' \
                         else 'New-user signup notifications disabled.'
                result = {'ok': True, 'action': 'save_signup_notify', 'service': '',
                          'error': '', 'detail': detail}
            except Exception as e:
                result = {'ok': False, 'action': 'save_signup_notify', 'service': '',
                          'error': str(e), 'detail': ''}
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse(result)

        elif svc not in _EMAIL_SERVICES:
            result = {'ok': False, 'action': action, 'service': svc,
                      'error': 'Unknown service.', 'detail': ''}
        elif action == 'save':
            try:
                for field in ('from_email', 'from_name', 'reply_to'):
                    val = request.POST.get(field, '').strip()
                    set_system_setting(f'{svc}_{field}', val)
                result = {'ok': True, 'action': 'save', 'service': svc,
                          'error': '', 'detail': 'Saved.'}
            except Exception as e:
                result = {'ok': False, 'action': 'save', 'service': svc,
                          'error': str(e), 'detail': ''}

        elif action == 'test_send':
            to_addr = (request.POST.get('to') or '').strip()
            subject = (request.POST.get('subject') or '').strip()
            body    = (request.POST.get('body') or '').strip()
            now_iso = datetime.now(timezone.utc).isoformat(timespec='seconds')
            ok, info = send_email(svc, to_addr, subject, body)
            try:
                set_system_setting(f'{svc}_last_test_at',     now_iso)
                set_system_setting(f'{svc}_last_test_to',     to_addr)
                set_system_setting(f'{svc}_last_test_status', 'sent' if ok else 'failed')
                set_system_setting(f'{svc}_last_test_error',  '' if ok else info)
            except Exception:
                pass
            result = {
                'ok': ok, 'action': 'test_send', 'service': svc,
                'error':  '' if ok else info,
                'detail': (f'Sent to {to_addr}. Check the inbox, then mark below.'
                           if ok else info),
            }

        elif action == 'verify':
            working = (request.POST.get('working') or '').strip().lower() in ('1', 'true', 'yes', 'on')
            try:
                if working:
                    set_system_setting(f'{svc}_verified_at',
                                       datetime.now(timezone.utc).isoformat(timespec='seconds'))
                    set_system_setting(f'{svc}_verified_by',
                                       request.session.get('user_email') or '')
                else:
                    set_system_setting(f'{svc}_verified_at', '')
                    set_system_setting(f'{svc}_verified_by', '')
                result = {'ok': True, 'action': 'verify', 'service': svc,
                          'error': '', 'detail': 'Marked working.' if working else 'Verification cleared.'}
            except Exception as e:
                result = {'ok': False, 'action': 'verify', 'service': svc,
                          'error': str(e), 'detail': ''}
        else:
            result = {'ok': False, 'action': action, 'service': svc,
                      'error': f'Unknown action "{action}".', 'detail': ''}

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            payload = dict(result)
            if action in ('test_send', 'verify'):
                payload['health'] = get_service_health(svc)
            return JsonResponse(payload)

    services_data = []
    current = _email_settings_current()
    admin_email = request.session.get('user_email') or ''
    transport_status = get_transport_status()
    smtp_cfg         = get_smtp_config()
    has_resend_key   = bool(get_resend_api_key())
    for svc_key, cfg in _EMAIL_SERVICES.items():
        vals = current[svc_key]
        defs = cfg['defaults']
        is_customised = (
            vals['from_email'] != defs['from_email'] or
            vals['from_name']  != defs['from_name']  or
            vals['reply_to']   != defs['reply_to']
        )
        services_data.append({
            'key':           svc_key,
            'label':         cfg['label'],
            'icon_color':    cfg['icon_color'],
            'bg_color':      cfg['bg_color'],
            'description':   cfg['description'],
            'events':        cfg['events'],
            'vals':          vals,
            'is_customised': is_customised,
            'health':        get_service_health(svc_key),
        })

    notify_signup_enabled = (get_system_setting('notify_signup_enabled', '') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    notify_signup_to      = get_system_setting('notify_signup_to', '') or ''

    ctx = {
        **_admin_base_ctx(request, 'email_settings'),
        'services':              services_data,
        'save_result':           result,
        'admin_email':           admin_email,
        'transport_configured':  transport_status['configured'],
        'transport_status':      transport_status,
        'has_resend_key':        has_resend_key,
        'smtp_cfg':              smtp_cfg,
        'notify_signup_enabled': notify_signup_enabled,
        'notify_signup_to':      notify_signup_to,
    }
    return render(request, 'core/admin_email_settings.html', ctx)


@admin_required
def admin_whop_settings(request):
    """Admin page to manage Whop API credentials and checkout URLs."""

    # Annual billing has been removed site-wide; only monthly plan IDs
    # and checkout URLs are configurable from this admin page. Any
    # legacy annual ``whop_*_annual`` settings in the database are
    # left in place untouched for renewing existing annual subscribers
    # — they're simply no longer surfaced in the UI.
    _CHECKOUT_FIELDS = [
        ('starter_monthly', 'Starter'),
        ('pro_monthly',     'Pro'),
        ('agency_monthly',  'Agency'),
    ]
    _PLAN_ID_FIELDS = _CHECKOUT_FIELDS  # same 3 monthly plans
    _CRED_FIELDS = [
        ('whop_api_key',        'API Key',        'password'),
        ('whop_webhook_secret', 'Webhook Secret', 'password'),
        ('whop_company_id',     'Company ID',     'text'),
    ]

    save_result = {'ok': False, 'section': '', 'error': ''}

    if request.method == 'POST':
        section = request.POST.get('section', '')
        try:
            if section == 'credentials':
                for key, _, _ in _CRED_FIELDS:
                    val = request.POST.get(key, '').strip()
                    if val:
                        set_system_setting(key, val)
                save_result = {'ok': True, 'section': 'credentials', 'error': ''}
            elif section == 'checkout_urls':
                for key, _ in _CHECKOUT_FIELDS:
                    val = request.POST.get(f'whop_checkout_{key}', '').strip()
                    if val:
                        set_system_setting(f'whop_checkout_{key}', val)
                save_result = {'ok': True, 'section': 'checkout_urls', 'error': ''}
            elif section == 'plan_ids_prod':
                for key, _ in _PLAN_ID_FIELDS:
                    val = request.POST.get(f'whop_plan_id_{key}', '').strip()
                    set_system_setting(f'whop_plan_id_{key}', val)
                save_result = {'ok': True, 'section': 'plan_ids_prod', 'error': ''}
            elif section == 'plan_ids_dev':
                for key, _ in _PLAN_ID_FIELDS:
                    val = request.POST.get(f'whop_plan_id_dev_{key}', '').strip()
                    set_system_setting(f'whop_plan_id_dev_{key}', val)
                save_result = {'ok': True, 'section': 'plan_ids_dev', 'error': ''}
            elif section == 'whop_mode':
                mode = request.POST.get('whop_mode', 'prod')
                if mode in ('dev', 'prod'):
                    set_system_setting('whop_mode', mode)
                save_result = {'ok': True, 'section': 'whop_mode', 'mode': mode, 'error': ''}
            elif section == 'plan_ids':
                for key, _ in _PLAN_ID_FIELDS:
                    val = request.POST.get(f'whop_plan_id_{key}', '').strip()
                    set_system_setting(f'whop_plan_id_{key}', val)
                save_result = {'ok': True, 'section': 'plan_ids', 'error': ''}
        except Exception as e:
            save_result = {'ok': False, 'section': section, 'error': str(e)}
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse(save_result)

    # Build current credential values (masked for display)
    from .db import get_all_system_settings
    all_settings = get_all_system_settings()

    creds = []
    for key, label, input_type in _CRED_FIELDS:
        raw = all_settings.get(key, '') or os.environ.get(key.upper(), '')
        masked = (raw[:6] + '••••••••••••') if raw else ''
        creds.append({'key': key, 'label': label, 'type': input_type,
                      'masked': masked, 'value': raw, 'set': bool(raw)})

    checkout_urls = []
    for key, label in _CHECKOUT_FIELDS:
        db_key = f'whop_checkout_{key}'
        val    = all_settings.get(db_key) or wp.DEFAULT_CHECKOUT_URLS.get(key, '')
        checkout_urls.append({'key': key, 'label': label, 'url': val})

    whop_mode = all_settings.get('whop_mode', 'prod') or 'prod'
    plan_ids_prod = []
    plan_ids_dev  = []
    for key, label in _PLAN_ID_FIELDS:
        prod_val = all_settings.get(f'whop_plan_id_{key}', '') or ''
        dev_val  = all_settings.get(f'whop_plan_id_dev_{key}', '') or ''
        plan_ids_prod.append({'key': key, 'label': label, 'value': prod_val, 'set': bool(prod_val)})
        plan_ids_dev.append( {'key': key, 'label': label, 'value': dev_val,  'set': bool(dev_val)})

    ctx = {
        **_admin_base_ctx(request, 'whop_settings'),
        'creds':          creds,
        'checkout_urls':  checkout_urls,
        'plan_ids_prod':  plan_ids_prod,
        'plan_ids_dev':   plan_ids_dev,
        'whop_mode':      whop_mode,
        'save_result':    save_result,
        'webhook_events': [
            'membership.went_valid',
            'membership.went_invalid',
            'membership.cancelled',
            'payment.succeeded',
        ],
    }
    return render(request, 'core/admin_whop_settings.html', ctx)


@admin_required
@require_http_methods(['POST'])
def admin_whop_resync_user(request):
    """Admin-only: look up a user's Whop memberships by email, optionally
    apply the highest-tier valid one (or a specific one) to their account.

    This is the manual repair tool for the case where a user's plan in
    our database does not match their actual Whop subscription. The
    same code path that fires on every login (`_whop_login_sync`) is
    used here, so the result is identical — we just expose it as an
    admin action so admins can fix users who haven't logged in since
    the last bug-fix deploy, without making them log in.

    POST params:
      email   — required, the user's email
      action  — 'preview' (default): just look up, no writes
                'apply':              apply highest-tier valid membership
                'apply_id:<mem_id>':  apply a specific membership by ID

    Returns JSON with:
      memberships:  every membership Whop returned, with detected plan
      best_plan:    highest-tier valid plan
      applied:      the patch that was applied (or null on preview)
      user_*:       what the user record looked like before/after
    """
    email  = (request.POST.get('email') or '').strip().lower()
    action = (request.POST.get('action') or 'preview').strip()

    if not email:
        return JsonResponse({'ok': False, 'error': 'Email is required.'}, status=400)

    # Fetch from Whop. Generous 10s timeout — admin is waiting.
    try:
        mems = wp.get_memberships_by_email(email, timeout=10)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Whop lookup failed: {e}'}, status=502)

    enriched     = []
    best_mem     = None
    best_plan    = ''
    best_rank    = -1
    for m in mems:
        detected = wp.plan_from_membership(m, default='') or ''
        is_valid = bool(m.get('valid'))
        prod_obj = m.get('product')
        prod_id  = ''
        if isinstance(prod_obj, str):
            prod_id = prod_obj
        elif isinstance(prod_obj, dict):
            prod_id = prod_obj.get('id', '')
        enriched.append({
            'id':                   m.get('id', ''),
            'plan_id':              m.get('plan_id', '') or m.get('plan', ''),
            'product_id':           prod_id,
            'valid':                is_valid,
            'detected_plan':        detected,
            'cancel_at_period_end': bool(m.get('cancel_at_period_end')),
            'created_at':           m.get('created_at', 0),
            'expires_at':           m.get('expires_at') or m.get('renewal_period_end', 0),
            'status':               m.get('status', ''),
        })
        if is_valid:
            r = _PLAN_RANK.get(detected, -1)
            if r > best_rank:
                best_rank = r
                best_mem  = m
                best_plan = detected

    user        = get_user_by_email(email)
    user_found  = bool(user)
    response    = {
        'ok':                   True,
        'error':                '',
        'user_found':           user_found,
        'user_id':              (user.get('id') if user else None),
        'current_plan':         ((user.get('plan') or '') if user else ''),
        'current_membership_id': ((user.get('whop_membership_id') or '') if user else ''),
        'memberships':          enriched,
        'best_plan':            best_plan,
        'best_membership_id':   (best_mem.get('id', '') if best_mem else ''),
        'applied':              None,
    }

    if action == 'preview':
        return JsonResponse(response)

    if not user_found:
        return JsonResponse({**response, 'error': 'No user with that email in our DB.'}, status=404)

    # Pick the membership to apply.
    target      = None
    target_plan = ''
    if action == 'apply':
        if not best_mem:
            # No active membership at Whop -> deactivate the user, mirror
            # of the bulk no-member branch in admin_bulk_whop_sync. The
            # admin clicked "apply" on a user with zero valid Whop
            # memberships, so the right outcome is to demote them to
            # inactive (not return 400 and leave them stuck on a stale
            # paid plan). `plan` and `whop_membership_id` are kept as
            # historical breadcrumbs; entitlement checks (_api_auth,
            # has_ls_sub, admin table) are gated on subscription_active.
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            patch = {
                'subscription_active': False,
                'whop_resync_pending': False,
                'whop_status':         'expired',
                'whop_paused':         False,
                'whop_cancelled':      False,
                'whop_renews_at':      '',
                'last_whop_sync_at':   now_iso,
            }
            try:
                update_user(user['id'], **patch)
            except Exception as e:
                return JsonResponse({**response,
                    'error': f'DB update failed: {e}'}, status=500)
            return JsonResponse({
                **response,
                'applied':   patch,
                'deactivated': True,
                'message':  'No active Whop membership for this email; '
                            'user marked inactive.',
            })
        target, target_plan = best_mem, best_plan
    elif action.startswith('apply_id:'):
        wanted = action.split(':', 1)[1].strip()
        for m in mems:
            if m.get('id') == wanted:
                target      = m
                target_plan = wp.plan_from_membership(m, default='') or ''
                break
        if not target:
            return JsonResponse({**response, 'error':
                f'Membership ID {wanted!r} not found for this email.'}, status=404)
    else:
        return JsonResponse({**response, 'error':
            f'Unknown action {action!r}.'}, status=400)

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    patch = {
        'whop_membership_id':  target.get('id', ''),
        'whop_cancelled':      bool(target.get('cancel_at_period_end')),
        'subscription_active': bool(target.get('valid', True)),
        'last_whop_sync_at':   now_iso,
        'whop_resync_pending': False,
    }
    if target_plan in ('starter', 'pro', 'agency'):
        patch['plan'] = target_plan

    try:
        update_user(user['id'], **patch)
    except Exception as e:
        return JsonResponse({**response, 'error': f'DB update failed: {e}'}, status=500)

    # Best-effort billing/invoice sync — don't fail the response on this.
    try:
        _sync_billing_and_invoice(user['id'], target)
    except Exception:
        pass

    response['applied'] = patch
    return JsonResponse(response)


@admin_required
@require_http_methods(['GET', 'POST'])
def admin_google_settings(request):
    """Admin page to manage the Google Sign-In OAuth client at runtime.

    Three save sections (POSTed individually, with section= ... in the body):
      * ``credentials`` — write google_client_id / google_client_secret.
        Empty values are skipped (so you can rotate one without re-typing the
        other). Use the ``clear_secret`` section to actively wipe the secret.
      * ``enabled`` — flip the master toggle on/off. The "Continue with
        Google" buttons on /login/ and /signup/ only show when this is on
        AND the credentials are filled in.
      * ``clear_secret`` — wipe the saved client_secret without touching the
        client_id. Useful when rotating or revoking.
    """
    save_result = {'ok': False, 'section': '', 'error': ''}

    if request.method == 'POST':
        section = request.POST.get('section', '')
        try:
            if section == 'credentials':
                cid = request.POST.get('google_client_id', '').strip()
                sec = request.POST.get('google_client_secret', '').strip()
                if cid:
                    set_system_setting('google_client_id', cid)
                if sec:
                    set_system_setting('google_client_secret', sec)
                save_result = {'ok': True, 'section': 'credentials', 'error': ''}
            elif section == 'enabled':
                enabled = request.POST.get('google_oauth_enabled') == 'on'
                set_system_setting('google_oauth_enabled', enabled)
                save_result = {'ok': True, 'section': 'enabled',
                               'enabled': enabled, 'error': ''}
            elif section == 'clear_secret':
                set_system_setting('google_client_secret', '')
                save_result = {'ok': True, 'section': 'clear_secret', 'error': ''}
            elif section == 'clear_client_id':
                set_system_setting('google_client_id', '')
                save_result = {'ok': True, 'section': 'clear_client_id', 'error': ''}
            elif section == 'urls':
                # Allow the admin to pin the exact origin + redirect URI we
                # send to Google. These MUST match what's registered on the
                # Google Cloud Console OAuth client. Empty values clear the
                # override and we fall back to auto-detect from the request.
                redir = request.POST.get('google_redirect_uri', '').strip()
                orig  = request.POST.get('google_authorized_origin', '').strip()
                # Light validation: must be absolute http(s) URLs when set.
                for label, val in (('redirect URI', redir), ('origin', orig)):
                    if val and not (val.startswith('http://') or val.startswith('https://')):
                        raise ValueError(f'{label.title()} must start with http:// or https://')
                if redir and not redir.endswith('/auth/google/callback/'):
                    raise ValueError('Redirect URI must end with /auth/google/callback/')
                set_system_setting('google_redirect_uri', redir)
                set_system_setting('google_authorized_origin', orig)
                save_result = {'ok': True, 'section': 'urls', 'error': ''}
            elif section == 'reset_urls':
                set_system_setting('google_redirect_uri', '')
                set_system_setting('google_authorized_origin', '')
                save_result = {'ok': True, 'section': 'reset_urls', 'error': ''}
        except Exception as e:
            save_result = {'ok': False, 'section': section, 'error': str(e)}
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse(save_result)

    from .google_auth import (
        get_google_settings,
        build_redirect_uri,
        auto_redirect_uri,
        auto_authorized_origin,
        get_redirect_uri_override,
        get_authorized_origin_override,
        is_google_oauth_ready,
    )
    s = get_google_settings()
    cid = s['client_id']
    sec = s['client_secret']
    masked_id = (cid[:14] + '…' + cid[-12:]) if len(cid) > 30 else (cid or '')
    masked_secret = (sec[:6] + '••••••••••••') if sec else ''

    redirect_override = get_redirect_uri_override()
    origin_override   = get_authorized_origin_override()
    auto_redir        = auto_redirect_uri(request)
    auto_origin       = auto_authorized_origin(request)
    effective_redir   = redirect_override or auto_redir
    effective_origin  = origin_override   or auto_origin

    ctx = {
        **_admin_base_ctx(request, 'google_settings'),
        'g_client_id_set':        bool(cid),
        'g_client_secret_set':    bool(sec),
        'g_client_id_masked':     masked_id,
        'g_client_secret_masked': masked_secret,
        'g_enabled':              s['enabled'],
        'g_ready':                is_google_oauth_ready(),
        'redirect_uri':           effective_redir,
        'authorized_origin':      effective_origin,
        'redirect_uri_value':     redirect_override,
        'authorized_origin_value': origin_override,
        'redirect_uri_auto':      auto_redir,
        'authorized_origin_auto': auto_origin,
        'urls_overridden':        bool(redirect_override or origin_override),
        'save_result':            save_result,
    }
    return render(request, 'core/admin_google_settings.html', ctx)


@admin_required
@require_http_methods(['POST'])
def admin_delete_user(request, user_id):
    current_user_id = request.session.get('user_id')
    if int(user_id) == int(current_user_id):
        return redirect('/admin-panel/users/?deleted=self')
    ok = delete_user(user_id)
    if ok:
        return redirect('/admin-panel/users/?deleted=ok')
    return redirect('/admin-panel/users/?deleted=fail')


@admin_required
@require_http_methods(['POST'])
def admin_bulk_delete_users(request):
    """Delete multiple users at once. Skips the current admin and any
    accounts protected by ADMIN_EMAILS."""
    raw_ids = request.POST.getlist('user_ids')
    current_user_id = int(request.session.get('user_id') or 0)

    candidate_ids: list[int] = []
    skipped_self  = False
    for r in raw_ids:
        try:
            uid = int(r)
        except (TypeError, ValueError):
            continue
        if uid == current_user_id:
            skipped_self = True
            continue
        candidate_ids.append(uid)

    # Fetch only the requested rows instead of the full users table.
    by_id = {u['id']: u for u in get_users_by_ids(candidate_ids)}

    target_ids: list[int] = []
    skipped_admin = 0
    for uid in candidate_ids:
        u = by_id.get(uid)
        if u is None:
            continue
        if (u.get('email') or '').lower().strip() in ADMIN_EMAILS:
            skipped_admin += 1
            continue
        target_ids.append(uid)

    deleted = bulk_delete_users(target_ids) if target_ids else 0
    qs = f'bulk_deleted={deleted}'
    if skipped_self:
        qs += '&bulk_self=1'
    if skipped_admin:
        qs += f'&bulk_admin={skipped_admin}'
    return redirect(f'/admin-panel/users/?{qs}')


# ── Per-user Whop billing mode ───────────────────────────────────────────
# Each user document carries its own ``whop_mode`` ('prod' | 'dev'). New
# signups default to 'prod' (live billing) — see core/db.create_user. Admins
# can flip individual users from /admin-panel/users/ (the toggle below the
# user's plan badge) or many at once via the bulk action bar. The flag is
# read at checkout time by ls_embed_checkout → wp.mode_for_user(user) so
# only that user's checkout is routed to the $1 Whop dev plan IDs.

def _normalize_whop_mode(raw: str) -> str:
    """Coerce form input to a known mode; default to 'prod' for safety."""
    m = (raw or '').strip().lower()
    return m if m in ('dev', 'prod') else 'prod'


@admin_required
@require_http_methods(['POST'])
def admin_set_user_whop_mode(request, user_id):
    """Flip a single user between 'dev' and 'prod' Whop billing mode.

    Self-protection: refuses to mutate the *calling* admin's own
    account so a stray click can't accidentally route the admin's
    own real billing to the $1 dev plans (they can ask another
    admin to flip it). Other admins CAN be toggled by a fellow
    admin — testing the dev plans on a super-admin account is a
    legitimate workflow and forcing a JSONB hand-edit isn't a real
    safeguard.
    """
    target = get_user_by_id(user_id)
    if not target:
        return redirect('/admin-panel/users/?mode_set=missing')
    current_user_id = int(request.session.get('user_id') or 0)
    if int(user_id) == current_user_id:
        return redirect('/admin-panel/users/?mode_set=self')
    new_mode = _normalize_whop_mode(request.POST.get('whop_mode'))
    update_user(int(user_id), whop_mode=new_mode)
    return redirect(f'/admin-panel/users/?mode_set=ok&mode={new_mode}')


# ── Per-user email-code login verification toggle ────────────────────────
# Each user document carries an optional ``email_code_disabled`` boolean
# (omitted / falsy = email-code verification REQUIRED on every email-
# password login; True = bypass the code and log the user in directly).
# Defaults to required for everyone — the admin only flips the bit OFF
# for accounts whose ESP can't reliably deliver our 6-digit codes
# (corporate spam filters, etc.). The login view at lines ~1405-1406
# reads this flag together with `is_email_transport_configured()` to
# decide whether to redirect to /login/verify-code/ or mint the
# session immediately. TOTP-protected accounts are unaffected — the
# TOTP check fires earlier and short-circuits before we reach this
# code path.

@admin_required
@require_http_methods(['POST'])
def admin_set_user_email_code(request, user_id):
    """Flip a single user's email-code verification ON/OFF.

    Mirrors the Whop-mode endpoint's self-protection: refuses to
    mutate the *calling* admin's own account so an admin can't lock
    themselves out of email-code MFA in one stray click (they can
    still ask another admin to flip it for them). Other admin
    accounts CAN be toggled by a fellow admin — corp spam filters
    and ESP deliverability issues hit super-admins too, and forcing
    them to edit JSONB by hand isn't a real safeguard.

    The form sends ``email_code='on'|'off'``; anything else is
    coerced to ``'on'`` (the safe default) so a hand-crafted POST
    can never silently disable a security feature.
    """
    target = get_user_by_id(user_id)
    if not target:
        return redirect('/admin-panel/users/?email_code_set=missing')
    current_user_id = int(request.session.get('user_id') or 0)
    if int(user_id) == current_user_id:
        return redirect('/admin-panel/users/?email_code_set=self')
    raw = (request.POST.get('email_code') or '').strip().lower()
    new_state = 'off' if raw == 'off' else 'on'
    update_user(int(user_id), email_code_disabled=(new_state == 'off'))
    return redirect(f'/admin-panel/users/?email_code_set=ok&email_code={new_state}')


@admin_required
@require_http_methods(['POST'])
def admin_bulk_set_whop_mode(request):
    """Flip many users between 'dev' and 'prod' in one shot.

    Mirrors the bulk-delete safety contract: skips the current admin and
    any account whose email is in ADMIN_EMAILS so an admin can't
    accidentally route their own real billing to the $1 dev plans.
    """
    raw_ids = request.POST.getlist('user_ids')
    new_mode = _normalize_whop_mode(request.POST.get('whop_mode'))
    current_user_id = int(request.session.get('user_id') or 0)

    candidate_ids: list[int] = []
    skipped_self = False
    for r in raw_ids:
        try:
            uid = int(r)
        except (TypeError, ValueError):
            continue
        if uid == current_user_id:
            skipped_self = True
            continue
        candidate_ids.append(uid)

    by_id = {u['id']: u for u in get_users_by_ids(candidate_ids)}
    target_ids: list[int] = []
    skipped_admin = 0
    for uid in candidate_ids:
        u = by_id.get(uid)
        if u is None:
            continue
        if (u.get('email') or '').lower().strip() in ADMIN_EMAILS:
            skipped_admin += 1
            continue
        target_ids.append(uid)

    updated = 0
    for uid in target_ids:
        if update_user(uid, whop_mode=new_mode):
            updated += 1

    qs = f'bulk_mode={updated}&mode={new_mode}'
    if skipped_self:
        qs += '&bulk_self=1'
    if skipped_admin:
        qs += f'&bulk_admin={skipped_admin}'
    return redirect(f'/admin-panel/users/?{qs}')


@admin_required
@require_http_methods(['POST'])
def admin_bulk_whop_sync(request):
    """Force a fresh Whop API sync for many users at once.

    Used by the admin to fix users whose locally-cached plan / membership
    info has drifted (e.g. a webhook was missed, or the user paid via a
    plan_id that wasn't yet registered in /admin-panel/whop-settings/).

    For each user we:
    1. Look up their active memberships by email at Whop (live API).
    2. Pick the highest-tier active membership.
    3. Re-bind whop_membership_id / plan / subscription_active.
    4. Snapshot the formatted membership (status / renews_at / paused /
       cancelled) into the JSONB doc so the settings page reflects it.

    Bypasses the 1-hour _whop_login_sync throttle by calling the Whop API
    directly. Skips the current admin and ADMIN_EMAILS as a safety mirror
    of the bulk-delete / bulk-mode contracts. Counts and reports both
    successes and failures so the admin can see at a glance how many
    accounts genuinely have no Whop membership.
    """
    raw_ids = request.POST.getlist('user_ids')
    current_user_id = int(request.session.get('user_id') or 0)
    from datetime import datetime, timezone

    candidate_ids: list[int] = []
    skipped_self = False
    for r in raw_ids:
        try:
            uid = int(r)
        except (TypeError, ValueError):
            continue
        if uid == current_user_id:
            skipped_self = True
            continue
        candidate_ids.append(uid)

    by_id = {u['id']: u for u in get_users_by_ids(candidate_ids)}
    target_users: list[dict] = []
    skipped_admin = 0
    for uid in candidate_ids:
        u = by_id.get(uid)
        if u is None:
            continue
        if (u.get('email') or '').lower().strip() in ADMIN_EMAILS:
            skipped_admin += 1
            continue
        target_users.append(u)

    synced = 0       # Whop returned an active membership and we re-bound it
    no_member = 0    # email lookup returned nothing — user has no Whop record
    failed = 0       # exception talking to Whop or writing to DB

    for u in target_users:
        uid   = u['id']
        email = (u.get('email') or '').strip()
        if not email:
            no_member += 1
            continue
        try:
            mems = wp.get_memberships_by_email(email, timeout=5)
        except Exception:
            failed += 1
            log.exception("admin_bulk_whop_sync: get_memberships_by_email failed for user %s", uid)
            continue

        active = [m for m in mems if m.get('valid')]
        if not active:
            # No active membership at Whop. Flip the user to inactive so
            # the admin list / MRR / billing logic stop treating them as
            # a paying customer. Without this the user would keep their
            # stale `subscription_active=True` + plan badge forever (the
            # whole point of the admin clicking Sync Whop is to *fix*
            # that drift).
            #
            # We deliberately leave `plan` and `whop_membership_id` as
            # historical breadcrumbs — the admin table is driven by
            # `subscription_active`, and clearing the membership id
            # would lose the audit trail of what the user used to have.
            no_member += 1
            try:
                update_user(uid,
                    subscription_active = False,
                    whop_resync_pending = False,
                    whop_status         = 'expired',
                    whop_paused         = False,
                    whop_cancelled      = False,
                    whop_renews_at      = '',
                    last_whop_sync_at   = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                )
            except Exception:
                log.exception("admin_bulk_whop_sync: clear-inactive update failed for user %s", uid)
            continue

        # Pick the highest-tier active membership (mirrors _whop_login_sync).
        best_m, best_plan, best_rank = None, '', -1
        for m in active:
            d = wp.plan_from_membership(m, default='')
            r = _PLAN_RANK.get(d, -1)
            if r > best_rank:
                best_m, best_plan, best_rank = m, d, r
        if best_m is None:
            best_m = active[0]
            best_plan = ''

        try:
            patch = {
                'whop_membership_id':  best_m.get('id', ''),
                'whop_cancelled':      bool(best_m.get('cancel_at_period_end')),
                'subscription_active': True,
                'last_whop_sync_at':   datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'whop_resync_pending': False,
            }
            if best_plan in ('starter', 'pro', 'agency'):
                patch['plan'] = best_plan
            update_user(uid, **patch)
            _snapshot_whop_to_user(uid, best_m)
            synced += 1
        except Exception:
            failed += 1
            log.exception("admin_bulk_whop_sync: update/snapshot failed for user %s", uid)

    qs = f'bulk_synced={synced}&bulk_no_member={no_member}&bulk_failed={failed}'
    if skipped_self:
        qs += '&bulk_self=1'
    if skipped_admin:
        qs += f'&bulk_admin={skipped_admin}'
    return redirect(f'/admin-panel/users/?{qs}')


@admin_required
@require_http_methods(['POST'])
def admin_ban_user(request, user_id):
    current_user_id = request.session.get('user_id')
    if int(user_id) == int(current_user_id):
        return redirect('/admin-panel/users/?banned=self')
    target = get_user_by_id(user_id)
    if not target:
        return redirect('/admin-panel/users/?banned=fail')
    ban_email(
        email=target['email'],
        name=target.get('name', ''),
        banned_by=request.session.get('user_email', ''),
    )
    delete_user(user_id)
    return redirect('/admin-panel/users/?banned=ok')


@admin_required
@require_http_methods(['POST'])
def admin_unban_email(request):
    email = request.POST.get('email', '').strip()
    if email:
        unban_email(email)
    return redirect('/admin-panel/banned/?unbanned=ok')

# ── API helpers ───────────────────────────────────────────────

_DEMO_USER_EMAIL = 'mk@permitdaily.com'


def _generate_api_key():
    return 'pl_live_' + secrets.token_urlsafe(24)


def _api_auth(request):
    """Authenticate API request. Returns user dict (with 'id') on success, None on failure.
    Sets request._api_user for downstream tracking."""
    key = (request.GET.get('api_key') or
           request.headers.get('Authorization', '').replace('Bearer ', '').strip())
    if not key:
        return None
    if key == DEMO_API_KEY:
        user = get_user_by_email(_DEMO_USER_EMAIL)
        if user:
            user['_demo_key'] = True
            request._api_user = user
            return user
        return None
    # Single-row JSONB containment lookup (backed by users_api_keys_gin_idx)
    # instead of scanning every user on every API request.
    user = get_user_by_api_key(key)
    if not user:
        return None
    if user.get('plan', 'starter').lower() != 'agency':
        return None
    # Plan field on the user record is treated as a historical breadcrumb
    # by `admin_bulk_whop_sync` / `admin_whop_resync_user` (we don't clear
    # it on cancel so admins can see what the user used to have). That's
    # only safe if entitlement decisions go through `subscription_active`,
    # which is the canonical "do they currently have access" flag (set by
    # the Whop webhook on cancel/expire/payment events). Without this
    # check, a cancelled agency user would still pass API auth.
    if not user.get('subscription_active'):
        return None
    for k in user.get('api_keys', []):
        if k.get('key') == key and k.get('active', True):
            user['_matched_key'] = key
            request._api_user = user
            return user
    return None


def _api_unauth():
    return JsonResponse({'error': 'Invalid or missing API key.', 'code': 'unauthorized',
                         'docs': 'https://permitlify.com/developers/'}, status=401)


def _api_track(request, endpoint='unknown'):
    """Track an API call for the authenticated user (per-key, per-endpoint, per-day)."""
    user = getattr(request, '_api_user', None)
    if not user:
        return
    user_id = user.get('id')
    if not user_id:
        return
    try:
        if user.get('_demo_key'):
            increment_user_field(user_id, 'api_calls', 1)
            return
        matched_key = user.get('_matched_key')
        full_user = get_user_by_id(user_id)
        if not full_user:
            return
        api_keys = full_user.get('api_keys', [])
        today = date.today().isoformat()
        for k in api_keys:
            if k.get('key') == matched_key:
                k['calls'] = k.get('calls', 0) + 1
                k['last_used'] = datetime.utcnow().isoformat()
                ep = k.get('endpoint_stats', {})
                ep[endpoint] = ep.get(endpoint, 0) + 1
                k['endpoint_stats'] = ep
                daily = k.get('daily_calls', {})
                daily[today] = daily.get(today, 0) + 1
                # Keep only last 30 days
                if len(daily) > 30:
                    oldest = sorted(daily.keys())[0]
                    del daily[oldest]
                k['daily_calls'] = daily
                break
        update_user(user_id, api_keys=api_keys)
        increment_user_field(user_id, 'api_calls', 1)
    except Exception:
        pass

# ── API endpoints ─────────────────────────────────────────────

def api_permits(request):
    if not _api_auth(request):
        return _api_unauth()
    _api_track(request, 'permits')

    # Build the user's city restriction set. The demo key (and only the
    # demo key) sees the full corpus; real keys are restricted to the
    # cities the customer has paid for. `None` means "no restriction"
    # for the SQL helper; an empty set means "user has no cities → 0
    # results" (the helper short-circuits and never hits the DB).
    api_user = getattr(request, '_api_user', {})
    if api_user.get('_demo_key'):
        user_city_set: set | None = None
    else:
        # State codes since the May-2026 pricing migration.
        user_city_set = set()
        for c in api_user.get('cities', []) or []:
            code = (c or '').strip().upper()
            if len(code) == 2:
                user_city_set.add(code)

    state          = request.GET.get('state', '').strip()
    city           = request.GET.get('city', '').strip()
    trade          = request.GET.get('trade', '').strip()
    status         = request.GET.get('status', '').strip()
    min_score_s    = request.GET.get('min_score', '').strip()
    max_score_s    = request.GET.get('max_score', '').strip()
    tier           = request.GET.get('tier', '').strip().lower()
    owner          = request.GET.get('owner', '').strip()
    keyword        = request.GET.get('keyword', '').strip()
    issued_after   = request.GET.get('issued_after', '').strip()
    expires_before = request.GET.get('expires_before', '').strip()
    limit_s        = request.GET.get('limit', '50').strip()
    offset_s       = request.GET.get('offset', '0').strip()

    # Validate score filters BEFORE we hit the DB so a bad query
    # parameter returns 400 instantly (and to keep the previous public
    # API contract — the v1 docs promise a 400 on non-integer scores).
    try:
        min_score = int(min_score_s) if min_score_s else None
    except ValueError:
        return JsonResponse({'error': 'min_score must be an integer.', 'code': 'bad_request'}, status=400)
    try:
        max_score = int(max_score_s) if max_score_s else None
    except ValueError:
        return JsonResponse({'error': 'max_score must be an integer.', 'code': 'bad_request'}, status=400)

    # Clamp limit/offset BEFORE building the SQL — a negative `limit`
    # would emit `LIMIT -1` and crash with a 500 (Postgres rejects it),
    # which the previous in-memory implementation never tripped because
    # Python list slicing handles negatives silently. Floor at 1, cap
    # at 200 to bound payload size for the v1 API contract.
    try:
        limit  = max(1, min(int(limit_s), 200))
        offset = max(0, int(offset_s))
    except ValueError:
        limit, offset = 50, 0

    # All filtering, paging, and the COUNT(*) for pagination metadata
    # happen in a single round-trip to Postgres.
    results, total = query_permits_view(
        city_set       = user_city_set,
        state          = state,
        city           = city,
        trade          = trade,
        status         = status,
        tier           = tier,
        min_score      = min_score,
        max_score      = max_score,
        owner          = owner,
        keyword        = keyword,
        issued_after   = issued_after,
        expires_before = expires_before,
        limit          = limit,
        offset         = offset,
    )

    return JsonResponse({
        'count':    total,
        'returned': len(results),
        'offset':   offset,
        'limit':    limit,
        'results':  results,
        'meta':     {'api_version': 'v1'},
    })

def api_permit_detail(request, number):
    if not _api_auth(request):
        return _api_unauth()
    _api_track(request, 'permit_detail')

    # Honor the same per-key city restriction the list endpoint uses
    # so /v1/permits/<id>/ cannot leak permits in cities the caller
    # didn't pay for. The demo key sees everything.
    api_user = getattr(request, '_api_user', {})
    if api_user.get('_demo_key'):
        city_set: set | None = None
    else:
        # State codes since the May-2026 pricing migration.
        city_set = set()
        for c in api_user.get('cities', []) or []:
            code = (c or '').strip().upper()
            if len(code) == 2:
                city_set.add(code)

    permit = get_permit_by_number(number, city_set)
    if not permit:
        return JsonResponse({'error': 'Permit not found.', 'code': 'not_found'}, status=404)
    return JsonResponse({'result': permit, 'meta': {'api_version': 'v1'}})

def api_cities(request):
    if not _api_auth(request):
        return _api_unauth()
    _api_track(request, 'cities')
    cities = [{'name': c['name'], 'state': c['state'], 'permit_count': c['count']} for c in SAMPLE_CITIES]
    return JsonResponse({'count': len(cities), 'results': cities, 'meta': {'api_version': 'v1'}})

def _score_description(desc: str) -> tuple[int, str]:
    """Analyse permit description text. Returns (points 0-25, note)."""
    if not desc:
        return 0, 'No description provided'
    d = desc.lower()

    strong_pos = [
        ('full replacement', 8, 'full replacement'),
        ('complete replacement', 8, 'complete replacement'),
        ('total replacement', 8, 'total replacement'),
        ('new construction', 8, 'new construction'),
        ('new build', 8, 'new build'),
        ('hail damage', 7, 'storm/hail damage urgency'),
        ('storm damage', 7, 'storm damage urgency'),
        ('fire damage', 7, 'fire damage urgency'),
        ('water damage', 6, 'water damage urgency'),
        ('commercial', 5, 'commercial project'),
        ('emergency', 6, 'emergency work'),
    ]
    medium_pos = [
        ('replacement', 4, 'replacement work'),
        ('upgrade', 3, 'system upgrade'),
        ('installation', 3, 'new installation'),
        ('new system', 4, 'new system'),
        ('addition', 3, 'addition/expansion'),
        ('remodel', 3, 'remodel work'),
        ('expansion', 3, 'expansion'),
        ('solar', 3, 'solar install'),
        ('panel upgrade', 4, 'panel upgrade'),
        ('full', 2, 'full scope'),
    ]
    weak_neg = [
        ('minor repair', -6, 'minor repair'),
        ('patch', -4, 'patch work only'),
        ('inspection only', -8, 'inspection only'),
        ('temporary', -5, 'temporary permit'),
        ('survey', -4, 'survey/assessment only'),
        ('demo only', -5, 'demolition only'),
        ('demolition only', -5, 'demolition only'),
    ]

    pts = 0
    signals = []
    for phrase, weight, label in strong_pos:
        if phrase in d:
            pts += weight
            signals.append(label)
            break
    for phrase, weight, label in medium_pos:
        if phrase in d and label not in signals:
            pts += weight
            signals.append(label)
    for phrase, weight, label in weak_neg:
        if phrase in d:
            pts += weight
            signals.append(label)

    pts = max(0, min(25, pts))
    note = ', '.join(signals[:3]) if signals else 'Standard permit description'
    if pts >= 18:
        note = 'Strong value signals: ' + note
    elif pts >= 10:
        note = 'Moderate signals: ' + note
    elif pts == 0:
        note = 'No strong signals detected'
    else:
        note = note.capitalize()
    return pts, note


def _score_context(owner: str, permit_type: str, project: str) -> tuple[int, str]:
    """Score based on owner type and permit type context. Returns (points 0-15, note)."""
    o = (owner or '').lower()
    pt = (permit_type or '').lower()
    pr = (project or '').lower()

    commercial_keywords = ['llc', 'corp', 'inc', 'ltd', 'group', 'properties', 'holdings',
                           'partners', 'associates', 'enterprises', 'services', 'company',
                           'management', 'development', 'real estate', 'retail', 'commercial']
    residential_keywords = ['residential', 'homeowner', 'single-family', 'single family',
                            'duplex', 'townhome', 'condo']
    high_type_keywords = ['commercial', 'industrial', 'multifamily', 'multi-family',
                          'new build', 'new construction']
    low_type_keywords = ['garage', 'fence', 'shed', 'minor', 'temporary', 'demo']

    pts = 8
    note_parts = []

    is_commercial = any(k in o for k in commercial_keywords)
    if is_commercial:
        pts += 7
        note_parts.append('commercial entity owner')
    elif any(k in o for k in residential_keywords):
        pts += 2
        note_parts.append('residential homeowner')
    else:
        pts += 4
        note_parts.append('individual owner')

    for k in high_type_keywords:
        if k in pt or k in pr:
            pts += 2
            note_parts.append('high-value permit type')
            break
    for k in low_type_keywords:
        if k in pt or k in pr:
            pts -= 3
            note_parts.append('lower-value scope')
            break

    pts = max(0, min(15, pts))
    return pts, (', '.join(note_parts) if note_parts else 'Standard context').capitalize()


def _score_grade(score: int) -> str:
    if score >= 93: return 'A+'
    if score >= 88: return 'A'
    if score >= 83: return 'A-'
    if score >= 78: return 'B+'
    if score >= 73: return 'B'
    if score >= 68: return 'B-'
    if score >= 63: return 'C+'
    if score >= 58: return 'C'
    if score >= 53: return 'C-'
    if score >= 45: return 'D'
    return 'F'


def _score_summary(trade: str, value: float, desc_note: str, tier: str, score: int) -> str:
    tier_word = {'hot': 'High-priority', 'warm': 'Moderate-priority', 'cool': 'Lower-priority'}.get(tier, 'Scored')
    trade_label = {'roofing': 'roofing', 'hvac': 'HVAC', 'electrical': 'electrical', 'plumbing': 'plumbing',
                   'solar': 'solar', 'civil': 'civil/construction'}.get(trade, trade)
    val_label = f'${value:,.0f} value' if value > 0 else 'no value provided'
    signals = desc_note.split(': ')[-1] if ': ' in desc_note else desc_note
    return f'{tier_word} {trade_label} lead — {val_label}. {signals}. Score: {score}/99.'


@csrf_exempt
@require_http_methods(['POST'])
def api_score(request):
    if not _api_auth(request):
        return _api_unauth()
    _api_track(request, 'score')
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Request body must be valid JSON.', 'code': 'bad_request'}, status=400)

    trade       = str(data.get('trade', 'roofing')).lower().strip()
    value_str   = str(data.get('value', '0')).replace('$', '').replace(',', '').strip()
    city        = str(data.get('city', '')).strip()
    state       = str(data.get('state', '')).strip()
    description = str(data.get('description', '')).strip()
    permit_type = str(data.get('permit_type', '')).strip()
    owner       = str(data.get('owner', '')).strip()
    status      = str(data.get('status', '')).lower().strip()
    project     = str(data.get('project', '')).strip()
    issued_date = str(data.get('issued_date', '')).strip()
    expires_date = str(data.get('expires_date', '')).strip()

    try:
        value = float(value_str)
    except ValueError:
        value = 0

    # ── Factor 1: Trade (0–25) ────────────────────────────────
    trade_scores = {
        'roofing': 25, 'hvac': 25, 'solar': 23,
        'electrical': 18, 'plumbing': 18, 'civil': 15,
    }
    f_trade = trade_scores.get(trade, 10)
    trade_notes = {
        'roofing': 'Roofing — highest conversion rate trade',
        'hvac': 'HVAC — high-urgency, high-value trade',
        'solar': 'Solar — growing high-value market',
        'electrical': 'Electrical — solid conversion trade',
        'plumbing': 'Plumbing — reliable demand',
        'civil': 'Civil/construction — project-based lead',
    }
    f_trade_note = trade_notes.get(trade, f'Trade: {trade}')

    # ── Factor 2: Value (0–25) ────────────────────────────────
    if value > 20000:
        f_value, f_value_note = 25, f'High-value permit (${value:,.0f})'
    elif value > 12000:
        f_value, f_value_note = 18, f'Good permit value (${value:,.0f})'
    elif value > 6000:
        f_value, f_value_note = 12, f'Moderate value (${value:,.0f})'
    elif value > 2000:
        f_value, f_value_note = 6, f'Lower value permit (${value:,.0f})'
    elif value > 0:
        f_value, f_value_note = 3, f'Minimal declared value (${value:,.0f})'
    else:
        f_value, f_value_note = 0, 'No permit value provided'

    # ── Factor 3: Description analysis (0–25) ─────────────────
    f_desc, f_desc_note = _score_description(description)

    # ── Factor 4: Context — owner + permit type (0–15) ────────
    f_ctx, f_ctx_note = _score_context(owner, permit_type, project)

    # ── Factor 5: Status (0–10) ───────────────────────────────
    status_scores = {'approved': 10, 'review': 7, 'pending': 5, 'expired': 0}
    f_status = status_scores.get(status, 8)
    status_notes = {
        'approved': 'Permit approved — immediately actionable',
        'review': 'In review — likely to be approved soon',
        'pending': 'Pending approval — act early',
        'expired': 'Permit expired — lower urgency',
    }
    f_status_note = status_notes.get(status, 'Status not provided — estimated active')

    raw_score = f_trade + f_value + f_desc + f_ctx + f_status
    score = max(10, min(99, raw_score))
    tier  = 'hot' if score >= 80 else 'warm' if score >= 60 else 'cool'
    grade = _score_grade(score)

    return JsonResponse({
        'score':   score,
        'tier':    tier,
        'grade':   grade,
        'summary': _score_summary(trade, value, f_desc_note, tier, score),
        'factors': {
            'trade': {
                'points': f_trade, 'max': 25,
                'note': f_trade_note,
            },
            'value': {
                'points': f_value, 'max': 25,
                'note': f_value_note,
            },
            'description': {
                'points': f_desc, 'max': 25,
                'note': f_desc_note,
            },
            'context': {
                'points': f_ctx, 'max': 15,
                'note': f_ctx_note,
            },
            'status': {
                'points': f_status, 'max': 10,
                'note': f_status_note,
            },
        },
        'inputs': {
            'trade': trade, 'value': value, 'city': city, 'state': state,
            'description': description, 'permit_type': permit_type,
            'owner': owner, 'status': status, 'project': project,
            'issued_date': issued_date, 'expires_date': expires_date,
        },
        'meta': {'api_version': 'v1', 'model': 'permitlify-score-v3'},
    })


# ── API Portal (authenticated, Agency-only) ───────────────────

@login_required
@subscription_required
def api_portal(request):
    ctx = _user_ctx(request)
    user_id = request.session.get('user_id')
    user = get_user_by_id(user_id) if user_id else None
    plan = (user.get('plan', 'starter') if user else 'starter').lower()
    if plan != 'agency':
        ctx['plan'] = plan
        return render(request, 'core/api_portal.html', ctx)
    api_keys = user.get('api_keys', [])
    total_calls = sum(k.get('calls', 0) for k in api_keys)
    ep_totals = {}
    daily_totals = {}
    for k in api_keys:
        for ep, cnt in k.get('endpoint_stats', {}).items():
            ep_totals[ep] = ep_totals.get(ep, 0) + cnt
        for day, cnt in k.get('daily_calls', {}).items():
            daily_totals[day] = daily_totals.get(day, 0) + cnt
    today = date.today()
    last_7 = [(today.replace(day=1) if False else
               (today.__class__.fromordinal(today.toordinal() - i))).isoformat()
              for i in range(6, -1, -1)]
    chart_data = [{'date': d, 'calls': daily_totals.get(d, 0)} for d in last_7]
    raw = user.get('cities', [])
    plan_cities, plan_states = [], set()
    for c in raw:
        c = c.strip()
        name, st = (c.rsplit(', ', 1) + [''])[:2] if ', ' in c else (c, _city_state(c))
        plan_cities.append({'name': name.strip(), 'state': st.strip()})
        if st: plan_states.add(st.strip())
    plan_states = sorted(plan_states)
    new_key_val = request.session.pop('api_new_key', None)
    new_key_name = request.session.pop('api_new_key_name', None)
    ctx.update({
        'plan': 'agency',
        'api_keys': api_keys,
        'api_keys_json': json.dumps(api_keys),
        'total_calls': total_calls,
        'ep_totals': ep_totals,
        'chart_data': json.dumps(chart_data),
        'today_calls': daily_totals.get(date.today().isoformat(), 0),
        'new_key_val': new_key_val,
        'new_key_name': new_key_name,
        'plan_cities_json': json.dumps(plan_cities),
        'plan_states': plan_states,
    })
    return render(request, 'core/api_portal.html', ctx)


@login_required
@require_http_methods(['POST'])
def api_key_create(request):
    user_id = request.session.get('user_id')
    user = get_user_by_id(user_id) if user_id else None
    if not user or user.get('plan', 'starter').lower() != 'agency':
        return redirect('api_portal')
    name = request.POST.get('name', '').strip() or 'My Key'
    api_keys = user.get('api_keys', [])
    if len(api_keys) >= 5:
        return redirect('api_portal')
    new_key = {
        'key': _generate_api_key(),
        'name': name[:60],
        'created': datetime.utcnow().isoformat(),
        'last_used': None,
        'calls': 0,
        'active': True,
        'endpoint_stats': {},
        'daily_calls': {},
    }
    api_keys.append(new_key)
    update_user(user_id, api_keys=api_keys)
    request.session['api_new_key'] = new_key['key']
    request.session['api_new_key_name'] = new_key['name']
    return redirect('api_portal')


@login_required
@require_http_methods(['POST'])
def api_key_revoke(request):
    user_id = request.session.get('user_id')
    user = get_user_by_id(user_id) if user_id else None
    if not user or user.get('plan', 'starter').lower() != 'agency':
        return redirect('api_portal')
    key_val = request.POST.get('key', '').strip()
    api_keys = user.get('api_keys', [])
    for k in api_keys:
        if k.get('key') == key_val:
            k['active'] = False
            break
    update_user(user_id, api_keys=api_keys)
    return redirect('api_portal')


def _fmt_notif_time(iso_str: str) -> str:
    """Format ISO datetime to human-readable display string."""
    try:
        dt   = datetime.fromisoformat(iso_str)
        now  = datetime.now()
        diff = (now.date() - dt.date()).days
        t    = dt.strftime('%I:%M %p').lstrip('0')
        if diff == 0:   return f'Today {t}'
        if diff == 1:   return f'Yesterday {t}'
        if dt.year == now.year:
            return f'{dt.strftime("%b")} {dt.day} {t}'
        return f'{dt.strftime("%b")} {dt.day}, {dt.year} {t}'
    except Exception:
        return iso_str


@login_required
@subscription_required
def notifications(request):
    ctx     = _user_ctx(request)
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) or {}

    page_size   = 10
    page        = max(1, int(request.GET.get('page', 1)))
    type_filter = request.GET.get('type', 'all')
    offset      = (page - 1) * page_size

    raw_notifs  = get_notifications_for_user(user_id, limit=page_size,
                                              offset=offset, type_filter=type_filter)
    total_count = count_notifications_for_user(user_id, type_filter=type_filter)
    page_count  = max(1, (total_count + page_size - 1) // page_size)

    for n in raw_notifs:
        n['sent_at_display'] = _fmt_notif_time(n.get('sent_at', ''))

    stats     = get_notification_stats(user_id)
    prefs     = get_notif_prefs(user_id)
    channels  = get_notif_channels(user_id)
    # Slack / generic webhook / SMS channels are no longer surfaced in
    # the customer UI — daily Email Digest is the only delivery channel
    # we support today. Email is always-on, so the active-channel count
    # collapses to a simple "is email on?" check.
    ch_active = 1 if channels.get('email') else 0
    from .db import get_digest_schedule as _gds

    page_range = list(range(max(1, page - 2), min(page_count, page + 2) + 1))

    ctx.update({
        'alerts_sent':       stats['total'],
        'alerts_week':       stats['this_week'],
        'alerts_week_delta': stats['week_delta'],
        'open_rate':         stats['open_rate'],
        'channels_active':   ch_active,
        'notifications':     raw_notifs,
        'total_count':       total_count,
        'page':              page,
        'page_count':        page_count,
        'page_range':        page_range,
        'type_filter':       type_filter,
        'notif_prefs':       prefs,
        'notif_channels':    channels,
        'webhook_configured': bool(channels.get('webhook_url')),
        'digest_schedule':   _gds(user_id),
        'digest_timezones':  _DIGEST_TZ_CHOICES,
    })
    return render(request, 'core/notifications.html', ctx)


@login_required
def save_notif_prefs_view(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    user_id = request.session.get('user_id')
    valid_keys = {'daily_digest', 'billing_reminders', 'product_updates'}
    existing = get_notif_prefs(user_id)
    updated  = False
    for k in valid_keys:
        if k in request.POST:
            existing[k] = request.POST.get(k) == '1'
            updated = True
    if updated:
        save_notif_prefs(user_id, existing)
    # Daily digest delivery schedule (time + timezone) is editable from
    # the Notifications page too — every plan picks its own clock time.
    from .db import save_digest_schedule as _sds
    t  = request.POST.get('digest_time', '').strip()
    tz = request.POST.get('digest_tz',   '').strip()
    if t or tz:
        _sds(user_id, t, tz)
    return JsonResponse({'ok': True})


@login_required
def save_notif_channel_view(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    user_id = request.session.get('user_id')
    key     = request.POST.get('key', '')
    value   = request.POST.get('value', '')
    # Slack, custom webhook, and SMS channels are Agency-only. Email is always allowed.
    if key in {'slack_webhook', 'webhook_url', 'sms_phone'}:
        user = get_user_by_id(user_id) if user_id else None
        if not user or user.get('plan', 'starter').lower() != 'agency':
            return JsonResponse({'ok': False, 'error': 'Agency plan required'}, status=403)
    ok = save_notif_channel(user_id, key, value)
    if not ok:
        return JsonResponse({'ok': False, 'error': 'Invalid channel key'}, status=400)
    channels  = get_notif_channels(user_id)
    ch_active = sum(1 for v in [channels.get('email'), channels.get('slack_webhook'),
                                 channels.get('webhook_url'), channels.get('sms_phone')] if v)
    return JsonResponse({'ok': True, 'channels_active': ch_active})


# ── Notification dispatch (per-channel test) ──────────────────────

def _build_sample_permit():
    return {
        'ai_score': 92, 'ai_grade': 'A',
        'address': '4821 Ridgemont Dr', 'owner_name': 'Sarah T. Monroe',
        'trade': 'roofing', 'permit_type': 'Residential Roofing',
        'valuation_cents': 1850000,
        'city': 'Fort Worth', 'state': 'TX',
        'permit_number': 'TEST-' + datetime.utcnow().strftime('%Y%m%d-%H%M%S'),
        'issued_date': date.today().isoformat(),
    }


def _audit_dispatch(user_id, *, subject, channel, recipient, ok, detail):
    """Best-effort write to the notifications audit table; never raises."""
    try:
        create_notification(
            user_id=int(user_id),
            type_key='system', type_label='Test',
            subject=subject,
            preview=('Test sent successfully' if ok else f'Failed: {detail}')[:200],
            recipient=(recipient or '')[:120],
            channel=channel,
            status_key=('sent' if ok else 'failed'),
            status_label=('Sent' if ok else 'Failed'),
            sent_at=datetime.utcnow().isoformat(),
        )
    except Exception:
        pass


@login_required
def notif_test_view(request):
    """Send a real test message through the user-saved channel.

    Accepts ``key`` of ``slack_webhook`` or ``webhook_url``. ``sms_phone`` is
    rejected with a ``coming_soon`` flag so the UI can show the right message.
    Returns ``{ok: bool, detail?: str, error?: str, coming_soon?: bool}``.
    Logs an audit row in the notifications table either way (Slack/Webhook URLs
    are redacted before storage — they are bearer credentials).
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)

    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) if user_id else None
    if not user:
        return JsonResponse({'ok': False, 'error': 'Not signed in'}, status=401)
    if user.get('plan', 'starter').lower() != 'agency':
        return JsonResponse({'ok': False, 'error': 'Agency plan required'}, status=403)

    key = (request.POST.get('key') or request.GET.get('key') or '').strip()

    # SMS is intentionally not yet implemented — surface a friendly state.
    if key == 'sms_phone':
        return JsonResponse({'ok': False,
                             'coming_soon': True,
                             'error': 'SMS alerts are coming soon.'}, status=400)

    if key not in {'slack_webhook', 'webhook_url'}:
        return JsonResponse({'ok': False, 'error': 'Unsupported channel.'}, status=400)

    channels = get_notif_channels(user_id)
    target   = (channels.get(key) or '').strip()
    if not target:
        return JsonResponse({'ok': False,
                             'error': 'Save a URL for this channel first.'}, status=400)

    sample = _build_sample_permit()

    if key == 'slack_webhook':
        text, blocks = _integrations.build_permit_alert_blocks(sample)
        text = '✅ Permitlify test alert — your Slack channel is connected.\n' + text
        ok, detail = _integrations.post_slack_webhook(target, text=text, blocks=blocks)
        _audit_dispatch(user_id, subject='Slack test message', channel='Slack',
                        recipient=_integrations.redact_webhook_url(target),
                        ok=ok, detail=detail)
        if ok:
            return JsonResponse({'ok': True, 'detail': 'Sent. Check your Slack channel.'})
        return JsonResponse({'ok': False, 'error': detail}, status=502)

    # key == 'webhook_url' — generic JSON push to the user's endpoint.
    payload = {
        'event':       'test',
        'sent_at':     datetime.utcnow().isoformat() + 'Z',
        'message':     'Permitlify test webhook — your endpoint is connected.',
        'permit':      sample,
    }
    ok, detail = _integrations.post_generic_webhook(target, payload)
    _audit_dispatch(user_id, subject='Webhook test message', channel='Webhook',
                    recipient=_integrations.redact_webhook_url(target),
                    ok=ok, detail=detail)
    if ok:
        return JsonResponse({'ok': True, 'detail': f'Sent ({detail}).'})
    return JsonResponse({'ok': False, 'error': detail}, status=502)


# ── CRM integrations (Zapier webhook + HubSpot/GHL OAuth) ──────────

def _crm_redirect_uri(request, provider: str) -> str:
    """Absolute URL the OAuth provider should redirect back to."""
    return request.build_absolute_uri(f'/integrations/{provider}/callback/')


@login_required
def crm_save_zapier_view(request):
    """POST {url} — save a Zapier 'Catch Hook' webhook URL for this user."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) if user_id else None
    if not user:
        return JsonResponse({'ok': False, 'error': 'Not signed in'}, status=401)
    if user.get('plan', 'starter').lower() != 'agency':
        return JsonResponse({'ok': False, 'error': 'Agency plan required'}, status=403)

    url = (request.POST.get('url') or '').strip()
    if url and not url.startswith('https://'):
        return JsonResponse({'ok': False, 'error': 'Webhook URL must be https://'}, status=400)
    save_zapier_webhook(user_id, url)
    return JsonResponse({'ok': True, 'connected': bool(url)})


@login_required
def crm_test_view(request):
    """POST {provider} — send a sample payload through a CRM integration."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) if user_id else None
    if not user:
        return JsonResponse({'ok': False, 'error': 'Not signed in'}, status=401)
    if user.get('plan', 'starter').lower() != 'agency':
        return JsonResponse({'ok': False, 'error': 'Agency plan required'}, status=403)

    provider = (request.POST.get('provider') or '').strip()
    integrations = get_crm_integrations(user_id)
    sample       = _build_sample_permit()

    if provider == 'zapier':
        url = (integrations.get('zapier', {}).get('webhook_url') or '').strip()
        if not url:
            return JsonResponse({'ok': False, 'error': 'Save a Zapier webhook URL first.'}, status=400)
        ok, detail = _integrations.post_generic_webhook(url, {
            'event':   'test',
            'sent_at': datetime.utcnow().isoformat() + 'Z',
            'permit':  sample,
        })
        _audit_dispatch(user_id, subject='Zapier test', channel='Zapier',
                        recipient=_integrations.redact_webhook_url(url),
                        ok=ok, detail=detail)
        if ok:
            return JsonResponse({'ok': True, 'detail': f'Sent ({detail}).'})
        return JsonResponse({'ok': False, 'error': detail}, status=502)

    if provider == 'hubspot':
        token = (integrations.get('hubspot', {}).get('access_token') or '').strip()
        if not token:
            return JsonResponse({'ok': False, 'error': 'Connect HubSpot first.'}, status=400)
        ok, detail = _integrations.hubspot_test_connection(token)
        _audit_dispatch(user_id, subject='HubSpot ping', channel='HubSpot',
                        recipient=_integrations.redact_token(token),
                        ok=ok, detail=detail)
        if ok:
            return JsonResponse({'ok': True, 'detail': f'HubSpot reachable ({detail}).'})
        return JsonResponse({'ok': False, 'error': detail}, status=502)

    if provider == 'ghl':
        rec   = integrations.get('ghl', {}) or {}
        token = (rec.get('access_token') or '').strip()
        loc   = (rec.get('locationId') or '').strip()
        if not token:
            return JsonResponse({'ok': False, 'error': 'Connect GoHighLevel first.'}, status=400)
        ok, detail = _integrations.ghl_test_connection(token, location_id=loc)
        _audit_dispatch(user_id, subject='GoHighLevel ping', channel='GoHighLevel',
                        recipient=_integrations.redact_token(token),
                        ok=ok, detail=detail)
        if ok:
            return JsonResponse({'ok': True, 'detail': f'GoHighLevel reachable ({detail}).'})
        return JsonResponse({'ok': False, 'error': detail}, status=502)

    return JsonResponse({'ok': False, 'error': 'Unsupported provider.'}, status=400)


@login_required
def crm_disconnect_view(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) if user_id else None
    if not user:
        return JsonResponse({'ok': False, 'error': 'Not signed in'}, status=401)
    if user.get('plan', 'starter').lower() != 'agency':
        return JsonResponse({'ok': False, 'error': 'Agency plan required'}, status=403)
    provider = (request.POST.get('provider') or '').strip()
    if provider not in CRM_PROVIDERS:
        return JsonResponse({'ok': False, 'error': 'Unsupported provider.'}, status=400)
    disconnect_crm_provider(user_id, provider)
    return JsonResponse({'ok': True})


@login_required
def crm_oauth_start(request, provider):
    """Begin an OAuth flow — redirect the user to the vendor's authorize URL."""
    user = get_user_by_id(request.session.get('user_id')) if request.session.get('user_id') else None
    if not user or user.get('plan', 'starter').lower() != 'agency':
        return HttpResponseForbidden('Agency plan required')
    if provider not in {'hubspot', 'ghl'}:
        return HttpResponseBadRequest('Unsupported provider')
    if not _integrations.oauth_provider_configured(provider):
        # Friendly fallback — bounce to settings with an error flag.
        return redirect(f'/settings/?tab=integrations&oauth_error={provider}_not_configured')
    state = secrets.token_urlsafe(24)
    request.session['crm_oauth'] = {
        'provider': provider,
        'state':    state,
        'user_id':  request.session.get('user_id'),
    }
    request.session.modified = True
    url = _integrations.oauth_authorize_url(provider, _crm_redirect_uri(request, provider), state)
    if not url:
        return redirect(f'/settings/?tab=integrations&oauth_error={provider}_not_configured')
    return redirect(url)


@login_required
def crm_oauth_callback(request, provider):
    """Receive ``code`` + ``state`` from the vendor and exchange for tokens."""
    if provider not in {'hubspot', 'ghl'}:
        return HttpResponseBadRequest('Unsupported provider')

    user_id = request.session.get('user_id')
    user    = get_user_by_id(user_id) if user_id else None
    # Re-check the plan — a user could have downgraded between authorize and
    # callback. Storing the resulting tokens for a non-Agency account would
    # silently grant them gated functionality.
    if not user or user.get('plan', 'starter').lower() != 'agency':
        request.session.pop('crm_oauth', None)
        return HttpResponseForbidden('Agency plan required')

    flow_state = (request.session.get('crm_oauth') or {})
    expected_state    = flow_state.get('state')
    expected_provider = flow_state.get('provider')
    expected_user     = flow_state.get('user_id')

    incoming_state = request.GET.get('state', '')
    code           = request.GET.get('code', '')

    if request.GET.get('error'):
        request.session.pop('crm_oauth', None)
        return redirect(f'/settings/?tab=integrations&oauth_error={provider}_denied')

    if (not expected_state or not incoming_state or
            not secrets.compare_digest(expected_state, incoming_state) or
            expected_provider != provider or expected_user != user_id):
        request.session.pop('crm_oauth', None)
        return redirect(f'/settings/?tab=integrations&oauth_error=state_mismatch')

    request.session.pop('crm_oauth', None)

    if not code:
        return redirect(f'/settings/?tab=integrations&oauth_error=no_code')

    ok, payload = _integrations.oauth_exchange_code(provider, code, _crm_redirect_uri(request, provider))
    if not ok or not isinstance(payload, dict) or not payload.get('access_token'):
        # payload is a string error message in the failure case.
        detail = payload if isinstance(payload, str) else (payload.get('message') or 'token exchange failed')
        _audit_dispatch(user_id, subject=f'{provider} connect failed', channel=provider.upper(),
                        recipient='', ok=False, detail=detail)
        return redirect(f'/settings/?tab=integrations&oauth_error=exchange_failed')

    tokens = _integrations.normalize_token_response(payload)
    label  = ''
    if provider == 'hubspot' and payload.get('hub_id'):
        label = f"hub {payload['hub_id']}"
    elif provider == 'ghl' and payload.get('locationId'):
        label = f"location {payload['locationId']}"
    set_crm_oauth_tokens(user_id, provider, tokens, account_label=label)
    _audit_dispatch(user_id, subject=f'{provider} connected', channel=provider.upper(),
                    recipient=label or _integrations.redact_token(tokens.get('access_token', '')),
                    ok=True, detail='oauth ok')
    return redirect('/settings/?tab=integrations&oauth_ok=1')


# ── Scraper ingest API ────────────────────────────────────────────

INGEST_MAX_BATCH = 1000


def _effective_scraper_key() -> str:
    """Return the active SCRAPER_INGEST_KEY, env var winning over the
    DB fallback. The DB fallback (``system_settings.scraper_ingest_key``)
    lets the admin manage the key entirely from the Cron tab on hosts
    where setting platform env vars requires a restart or isn't possible
    at all (e.g. cron-job.org's free tier strips custom headers, so we
    need an in-app rotate flow). Env still takes precedence so a
    deliberate platform-level override always wins."""
    env_val = (os.environ.get('SCRAPER_INGEST_KEY') or '').strip()
    if env_val:
        return env_val
    try:
        from .db import get_system_setting
        return (get_system_setting('scraper_ingest_key', '') or '').strip()
    except Exception:
        return ''


def _scraper_authed(request) -> bool:
    expected = _effective_scraper_key()
    if not expected:
        return False
    presented = (request.headers.get('X-Scraper-Key') or '').strip()
    if not presented:
        return False
    # constant-time compare
    return secrets.compare_digest(expected, presented)


@csrf_exempt
@require_http_methods(['POST'])
def api_permits_ingest(request):
    """Bulk ingest permits from the external scraper platform.

    Auth: ``X-Scraper-Key`` header must match the ``SCRAPER_INGEST_KEY``
    server secret. Body: ``{"permits": [ {...}, {...} ]}`` (max 1000).
    Each permit must include at least ``source``, ``source_permit_id``,
    ``state`` and ``city``; everything else is optional.
    """
    if not _scraper_authed(request):
        return JsonResponse({'ok': False, 'error': 'Invalid or missing X-Scraper-Key header.'}, status=401)

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'Body must be valid JSON.'}, status=400)

    permits = body.get('permits')
    if not isinstance(permits, list):
        return JsonResponse({'ok': False, 'error': 'Body must contain a "permits" array.'}, status=400)
    if not permits:
        return JsonResponse({'ok': True, 'inserted': 0, 'updated': 0, 'skipped': 0, 'errors': 0})
    if len(permits) > INGEST_MAX_BATCH:
        return JsonResponse({'ok': False,
                             'error': f'Batch too large (max {INGEST_MAX_BATCH} permits per request).'},
                            status=413)

    result = bulk_upsert_permits(permits)
    return JsonResponse({'ok': True, **result})


# ── HTTP cron trigger ─────────────────────────────────────────────
#
# DigitalOcean App Platform has no native scheduled jobs and you
# can't ssh in to add a crontab line — so this endpoint lets ANY
# external scheduler (cron-job.org, GitHub Actions, UptimeRobot,
# etc.) hit a URL on a fixed cadence and trigger the same daily
# scraper pass that ``scripts/run_scrapers.py`` runs.
#
# It mirrors the script's main() exactly: applies the schedule gate
# from system_settings, stamps the heartbeat keys (so the Cron
# health card on /admin-panel/scrapers/cron/ flips green), and then
# kicks the existing _run_cron_batch_worker in a daemon thread so
# the HTTP request returns immediately (most schedulers time out
# at 30-60s; a real cron pass can take hours).
#
# Auth: ``X-Scraper-Key`` header OR ``?key=`` query param (some free
# schedulers like cron-job.org's free tier strip custom headers) —
# both checked with constant-time compare against
# ``SCRAPER_INGEST_KEY``.

_CRON_DAY_INDEX = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def _cron_trigger_authed(request) -> bool:
    """Header-or-querystring auth for the HTTP cron trigger."""
    expected = _effective_scraper_key()
    if not expected:
        return False
    presented = ((request.headers.get('X-Scraper-Key')
                  or request.GET.get('key')
                  or '').strip())
    if not presented:
        return False
    return secrets.compare_digest(expected, presented)


def _cron_gate_check() -> str | None:
    """Re-implementation of scripts/run_scrapers.py::_gate_by_schedule
    so the HTTP endpoint enforces the exact same admin-configurable
    schedule gate. Returns None to proceed, or a reason string to skip.
    """
    from .db import get_system_setting
    if not get_system_setting('scrapers_cron_enabled'):
        return 'scrapers_cron_enabled is false'
    now = datetime.utcnow()
    raw_days = (get_system_setting('scrapers_cron_days') or '').strip()
    days = {d for d in raw_days.split(',') if d in _CRON_DAY_INDEX}
    if days:
        today = _CRON_DAY_INDEX[now.weekday()]
        if today not in days:
            return f'today ({today}) is not in scrapers_cron_days={sorted(days)}'
    at_utc = (get_system_setting('scrapers_cron_at_utc') or '').strip()
    if at_utc:
        try:
            hh, mm = at_utc.split(':')
            target = int(hh) * 60 + int(mm)
            cur    = now.hour * 60 + now.minute
            try:
                window = int(get_system_setting('scrapers_cron_window_minutes') or 30)
            except (TypeError, ValueError):
                window = 30
            window = max(1, min(window, 720))
            raw_delta = abs(cur - target)
            delta = min(raw_delta, 1440 - raw_delta)
            if delta > window:
                return (f'now {now.strftime("%H:%M")} UTC is outside '
                        f'{at_utc} ± {window}m window')
        except Exception:
            logging.warning('invalid scrapers_cron_at_utc=%r — ignoring gate', at_utc)
    return None


def _cron_stamp_heartbeat(outcome: str, *, fired: bool = False) -> None:
    """Mirror of scripts/run_scrapers.py::_stamp_heartbeat — writes
    the keys the Cron-health card polls. Best-effort; never raises."""
    from .db import set_system_setting
    now_iso = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    try:
        set_system_setting('scrapers_cron_last_heartbeat_at', now_iso)
        set_system_setting('scrapers_cron_last_heartbeat_outcome', outcome)
        if fired:
            set_system_setting('scrapers_cron_last_fired_at', now_iso)
    except Exception:
        logging.exception('cron-heartbeat write failed (non-fatal)')


def _server_cron_slot_key() -> str:
    """Return the nearest scheduled UTC slot key for de-duping.

    The server-local scheduler wakes repeatedly. When using the normal
    Run-at/window schedule, one eligible window must create at most one
    batch, so we remember this key in system_settings. The HTTP trigger
    path intentionally does not use this key because external schedulers
    already define their own cadence.
    """
    from .db import get_system_setting
    raw = (get_system_setting('scrapers_cron_at_utc') or '08:00').strip()
    try:
        hh_s, mm_s = raw.split(':')
        hh, mm = int(hh_s), int(mm_s)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        hh, mm = 8, 0
    now = datetime.utcnow()
    today = datetime(now.year, now.month, now.day, hh, mm)
    from datetime import timedelta as _td
    candidates = [today - _td(days=1), today, today + _td(days=1)]
    slot = min(candidates, key=lambda d: abs((now - d).total_seconds()))
    return slot.strftime('%Y-%m-%d %H:%M UTC')


def _cron_signal_fire(*, source: str = 'http') -> dict:
    """Apply cron gates and spawn a cron batch if this signal should fire."""
    skip_reason = _cron_gate_check()
    if skip_reason:
        outcome = f'skipped: {skip_reason}'
        _cron_stamp_heartbeat(outcome, fired=False)
        return {'ok': True, 'fired': False, 'batch_id': None,
                'outcome': outcome}

    from .db import (list_enabled_scrapers_all, create_cron_batch,
                     reap_stale_cron_batches, pg as _pg_cron)

    server_slot = ''
    if source == 'server':
        try:
            server_slot = _server_cron_slot_key()
            last_slot = (get_system_setting('scrapers_cron_server_last_slot') or '').strip()
            if server_slot and last_slot == server_slot:
                outcome = f'skipped: server already fired slot {server_slot}'
                _cron_stamp_heartbeat(outcome, fired=False)
                return {'ok': True, 'fired': False, 'batch_id': None,
                        'outcome': outcome, 'slot': server_slot}
        except Exception:
            logging.exception('server cron: slot de-dupe check failed (non-fatal)')

    try:
        reap_stale_cron_batches(60)
    except Exception:
        logging.exception('cron-trigger: reap_stale_cron_batches failed (non-fatal)')
    try:
        active = _pg_cron.query_one(
            "SELECT id, started_at FROM cron_batches "
            "WHERE finished_at IS NULL AND status = 'running' "
            "ORDER BY id DESC LIMIT 1"
        )
    except Exception:
        logging.exception('cron-trigger: active-batch lookup failed (non-fatal)')
        active = None
    if active:
        outcome = (f'skipped: cron batch #{active["id"]} still running '
                   f'(started {active.get("started_at")})')
        _cron_stamp_heartbeat(outcome, fired=False)
        return {'ok': True, 'fired': False, 'batch_id': None,
                'outcome': outcome, 'active_batch_id': int(active['id'])}

    try:
        enabled = list_enabled_scrapers_all()
    except Exception:
        logging.exception('cron-trigger: list_enabled_scrapers_all failed')
        _cron_stamp_heartbeat('failed: list_enabled_scrapers_all crashed', fired=False)
        return {'ok': False, 'error': 'Could not enumerate enabled scrapers — see server logs.'}
    if not enabled:
        outcome = 'skipped: no enabled scrapers'
        _cron_stamp_heartbeat(outcome, fired=False)
        return {'ok': True, 'fired': False, 'batch_id': None,
                'outcome': outcome}

    try:
        batch_id = create_cron_batch(kicked_by=None)
    except Exception:
        logging.exception('cron-trigger: create_cron_batch failed')
        return {'ok': False, 'error': 'Could not create cron_batch row — see server logs.'}

    try:
        _spawn_batch_subprocess(batch_id, kind='cron')
    except Exception:
        logging.exception('cron-trigger: failed to spawn coordinator subprocess')
        try:
            from .db import update_cron_batch
            from datetime import datetime as _dt
            update_cron_batch(batch_id, status='failed', finished_at=_dt.utcnow(),
                              note='failed to spawn coordinator subprocess')
        except Exception:
            pass
        return {'ok': False, 'error': 'Could not spawn coordinator subprocess.'}

    if server_slot:
        try:
            set_system_setting('scrapers_cron_server_last_slot', server_slot)
        except Exception:
            logging.exception('server cron: failed to store last slot')
    _cron_stamp_heartbeat(f'fired ({source})', fired=True)
    return {'ok': True, 'fired': True, 'batch_id': batch_id,
            'outcome': f'fired ({source})', 'enabled_scrapers': len(enabled),
            'slot': server_slot}


@csrf_exempt
@require_http_methods(['POST', 'GET'])
def api_run_scrapers_cron(request):
    """HTTP cron trigger — kicks scripts/run_scrapers.py-equivalent
    work in a background thread and returns immediately.

    Designed for DO App Platform / any host without first-class cron.
    Point a free external scheduler at this URL on a frequent cadence
    (every 5–30 min); the admin schedule on /admin-panel/scrapers/cron/
    decides whether each invocation actually fires or just records a
    heartbeat and skips.

    Auth: ``X-Scraper-Key`` header OR ``?key=`` query param matching
    the ``SCRAPER_INGEST_KEY`` env var.

    Returns 202 Accepted with one of:
      * ``{"ok": true,  "fired": true,  "batch_id": N, "outcome": "fired"}``
      * ``{"ok": true,  "fired": false, "batch_id": null, "outcome": "skipped: <reason>"}``
    Or 401 if auth fails.
    """
    if not _cron_trigger_authed(request):
        return JsonResponse(
            {'ok': False, 'error': 'Invalid or missing scraper key '
                                   '(use X-Scraper-Key header or ?key=…).'},
            status=401,
        )

    result = _cron_signal_fire(source='http')
    if not result.get('ok'):
        return JsonResponse(result, status=500)
    return JsonResponse(
        result,
        status=202,
    )


@login_required
def mark_notif_opened_view(request, notif_id):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    mark_notification_opened(notif_id)
    return JsonResponse({'ok': True})


@login_required
def notifications_export_csv(request):
    user_id     = request.session.get('user_id')
    type_filter = request.GET.get('type', 'all')
    all_notifs  = get_all_notifications_for_user(user_id, type_filter=type_filter)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="notifications.csv"'
    writer   = csv.writer(response)
    writer.writerow(['Type', 'Subject', 'Preview', 'Recipient', 'Channel', 'Status', 'Sent At'])
    for n in all_notifs:
        writer.writerow([
            n.get('type_label', ''),
            n.get('subject', ''),
            n.get('preview', ''),
            n.get('recipient', ''),
            n.get('channel', ''),
            n.get('status_label', ''),
            _fmt_notif_time(n.get('sent_at', '')),
        ])
    return response



# ─── SEO: robots.txt + sitemap.xml ───────────────────────────────────────
# Hand-rolled (no Django sitemap framework dependency, no DB hits) so they
# stay fast under load. Both responses are cached at the HTTP layer for an
# hour via Cache-Control to take the few requests/hour off our origin.

_PUBLIC_PATHS = [
    # (path,                changefreq, priority)
    ('/',                   'weekly',   '1.0'),
    ('/pricing/',           'weekly',   '0.9'),
    ('/blog/',              'weekly',   '0.8'),
    ('/developers/',        'monthly',  '0.7'),
    ('/contact/',           'monthly',  '0.6'),
    ('/careers/',           'monthly',  '0.5'),
    ('/press/',             'monthly',  '0.5'),
    ('/privacy/',           'yearly',   '0.3'),
    ('/terms/',             'yearly',   '0.3'),
    ('/login/',             'yearly',   '0.4'),
    ('/signup/',            'monthly',  '0.6'),
]

# Authenticated / app surfaces that must NEVER appear in search results, even
# if a third party links to them. Crawlers respect this list before fetching.
_DISALLOW_PATHS = [
    '/admin-panel/', '/dashboard/', '/permits/', '/notifications/',
    '/settings/', '/profile/', '/onboarding/', '/paywall/', '/support/',
    '/billing/', '/api/', '/auth/', '/integrations/',
    '/login/2fa/', '/forgot-password/', '/reset-password/',
    '/r/',  # referral redirector — not an index target
    '/logout/',
]


def _seo_origin(request):
    """
    Return the canonical site origin for SEO output.

    Prefers ``settings.SITE_ORIGIN`` (env-driven, locked to the production
    domain) so a forged Host header can't poison robots.txt or sitemap.xml.
    Falls back to the live request scheme + host when the env var isn't set
    (local dev / preview deploys), which keeps URLs working end-to-end.
    """
    from django.conf import settings as _s
    origin = (getattr(_s, 'SITE_ORIGIN', '') or '').rstrip('/')
    if origin:
        return origin
    scheme = 'https' if request.is_secure() else request.scheme
    return f'{scheme}://{request.get_host()}'


def robots_txt(request):
    """`/robots.txt` — tells crawlers what's off-limits and where the sitemap is."""
    origin = _seo_origin(request)
    lines = ['User-agent: *']
    for p in _DISALLOW_PATHS:
        lines.append(f'Disallow: {p}')
    lines += ['', f'Sitemap: {origin}/sitemap.xml', '']
    resp = HttpResponse('\n'.join(lines), content_type='text/plain; charset=utf-8')
    resp['Cache-Control'] = 'public, max-age=3600'
    return resp


def sitemap_xml(request):
    """`/sitemap.xml` — every public URL we want Google to crawl."""
    origin = _seo_origin(request)
    today = datetime.utcnow().strftime('%Y-%m-%d')
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, freq, prio in _PUBLIC_PATHS:
        parts.append(
            '<url>'
            f'<loc>{origin}{path}</loc>'
            f'<lastmod>{today}</lastmod>'
            f'<changefreq>{freq}</changefreq>'
            f'<priority>{prio}</priority>'
            '</url>'
        )
    # Per-article blog URLs — pulled from the same dict the views render.
    try:
        for slug in BLOG_ARTICLES.keys():
            parts.append(
                '<url>'
                f'<loc>{origin}/blog/{slug}/</loc>'
                f'<lastmod>{today}</lastmod>'
                '<changefreq>monthly</changefreq>'
                '<priority>0.7</priority>'
                '</url>'
            )
    except Exception:
        # Sitemap must never 500 — if blog imports fail, skip articles.
        pass
    parts.append('</urlset>')
    resp = HttpResponse(''.join(parts), content_type='application/xml; charset=utf-8')
    resp['Cache-Control'] = 'public, max-age=3600'
    return resp


# ── Dev-only email preview ────────────────────────────────────
# Renders any transactional email template against a baked sample
# context so we can iterate on the design without triggering a real
# send. Gated on settings.DEBUG so the route 404s in production —
# never expose this on a deployed env (it would let anyone read what
# our security-sensitive emails look like, and probe for template
# injection by inspecting the page source).

_EMAIL_PREVIEW_FIXTURES = {
    'login_code': {
        'name':           'Mohamed',
        'code':           '494670',
        'code_digits':    list('494670'),
        'expires_in_min': 10,
        'request_ip':     '198.51.100.42',
        'request_ua':     'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
    },
    'login_alert': {
        'first_name':   'Mohamed',
        'device':       'Edge on Windows',
        'ip':           '100.127.8.131',
        'ua':           'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                        '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0',
        'method_label': 'Email + password (with email code)',
        'when_pretty':  'Apr 29, 2026 · 05:24 UTC',
        'security_url': 'https://permitlify.com/settings/security/',
    },
    'welcome': {
        'first_name':    'Mohamed',
        'joined_pretty': 'Apr 29, 2026 · 05:24 UTC',
        'dashboard_url': 'https://permitlify.com/dashboard/',
        'pricing_url':   'https://permitlify.com/pricing/',
        'support_url':   'https://permitlify.com/support/',
    },
    'payment_success': {
        'first_name':         'Mohamed',
        'plan_label':         'Pro',
        'amount_display':     '$49.00 USD',
        'billing_email':      'mohamed@example.com',
        'paid_at_pretty':     'Apr 29, 2026 · 05:24 UTC',
        'next_charge_pretty': 'May 29, 2026',
        'membership_id':      'mem_01HZX9K2P7QVR5MNAB3TYWFJ4S',
        'billing_url':        'https://permitlify.com/settings/billing/',
    },
    'support_reply': {
        'recipient':     'Mohamed',
        'agent':         'Sarah from Permitlify',
        'ref':           'PL-7421',
        'subject_topic': 'City filter not saving',
        'snippet':       "Hi Mohamed — thanks for the report! I just pushed a fix for the saved-city filter "
                         "issue. Could you reload the dashboard and confirm your Austin + Dallas filters now "
                         "stick? Happy to jump on a quick call if anything is still off.",
        'link':          'https://permitlify.com/support/tickets/PL-7421/',
    },
    'support_status': {
        'recipient':     'Mohamed',
        'ref':           'PL-7421',
        'subject_topic': 'City filter not saving',
        'status_key':    'in_progress',
        'status_lower':  'in progress',
        'status_pretty': 'In progress',
        'link':          'https://permitlify.com/support/tickets/PL-7421/',
    },
    'reset_password': {
        'name':          'Mohamed',
        'expires_in_hr': 1,
        'reset_link':    'https://permitlify.com/reset/?t=ZmFrZS10b2tlbi1mb3ItcHJldmlldy1vbmx5LWRvLW5vdC11c2U',
    },
}

def email_preview(request, template_name):
    from django.conf import settings as _s
    if not _s.DEBUG:
        raise Http404
    from django.template.loader import render_to_string
    fixture = dict(_EMAIL_PREVIEW_FIXTURES.get(template_name, {}))
    fixture.setdefault('year', date.today().year)
    try:
        html = render_to_string(f'core/emails/{template_name}.html', fixture)
    except Exception as e:
        return HttpResponse(f'<pre>Template render failed: {e}</pre>', status=500)
    return HttpResponse(html)


# ─────────────────────────────────────────────────────────────────────
# Admin: Blog editor
#
# Three-step authoring flow under /admin-panel/blog/:
#   1. List       — every published post + "+ New Post" button.
#   2. Editor     — paste URL → AJAX scrape (Playwright) → AJAX rewrite
#                   (local GPT-OSS) → form fields → publish.
#   3. Settings   — system_settings keys: datacenter_proxy and
#                   blog_rewrite_model.
# ─────────────────────────────────────────────────────────────────────

from .blog_ai import (
    playwright_scrape, inference_rewrite, slugify as _ai_slugify,
    BlogAIError, DEFAULT_MODEL as _DEFAULT_REWRITE_MODEL,
)
from .browser_fetch import datacenter_proxy_info, datacenter_proxy_raw


def _blog_date_label(d: date, *, with_weekday: bool = False) -> str:
    prefix = f"{d.strftime('%A')}, " if with_weekday else ''
    return f"{prefix}{d.strftime('%B')} {d.day}, {d.year}"


def _admin_ctx(request, **extra):
    """Tiny helper that builds the kwargs the admin sidebar expects
    (admin_initials / admin_name / today). Mirrors the pattern other
    admin views use without adding a new import surface."""
    user_id = request.session.get('user_id')
    user = get_user_by_id(user_id) or {} if user_id else {}
    name = (user.get('display_name') or user.get('email') or 'Admin').strip()
    initials = ''.join(part[:1].upper() for part in name.split()[:2]) or 'AD'
    ctx = {
        'admin_name':     name,
        'admin_initials': initials,
        'today':          _blog_date_label(date.today(), with_weekday=True),
    }
    ctx.update(extra)
    return ctx


@admin_required
def admin_blog_view(request):
    """Blog post list. Search + pagination piggy-back on list_blog_posts."""
    q = (request.GET.get('q') or '').strip()
    try:
        page = int(request.GET.get('page') or 1)
    except (TypeError, ValueError):
        page = 1
    rows, total, total_pages, page = list_blog_posts(query=q, page=page, per_page=20)
    return render(request, 'core/admin_blog_list.html', _admin_ctx(
        request,
        active_section='blog',
        posts=rows,
        total=total,
        total_pages=total_pages,
        page=page,
        q=q,
    ))


@admin_required
def admin_blog_editor_view(request, slug=None):
    """Render the editor.

    * ``slug=None``  → blank "new post" form.
    * ``slug=<x>``   → load existing post for editing (the same form, just
                       pre-filled; Publish becomes "Save changes").
    """
    post = None
    if slug:
        post = get_blog_post(slug)
        if not post:
            raise Http404
    has_inference = True  # local GPT-OSS parser/rewrite endpoint needs no paid key
    return render(request, 'core/admin_blog_editor.html', _admin_ctx(
        request,
        active_section='blog',
        post=post,
        editing=bool(post),
        has_inference=has_inference,
    ))


def _json_err(msg, status=400):
    return JsonResponse({'ok': False, 'error': str(msg)}, status=status)


@admin_required
@require_http_methods(['POST'])
def admin_blog_scrape(request):
    """AJAX: scrape a URL via Playwright and return readable text + metadata."""
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return _json_err('Invalid JSON body')
    try:
        result = playwright_scrape(body.get('url') or '')
    except BlogAIError as e:
        return _json_err(e)
    return JsonResponse({
        'ok':       True,
        'markdown': result['markdown'],
        'metadata': result['metadata'],
    })


@admin_required
@require_http_methods(['POST'])
def admin_blog_rewrite(request):
    """AJAX: hand scraped markdown to DO Serverless Inference and return a
    normalised post dict ready to drop into the editor form."""
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return _json_err('Invalid JSON body')
    try:
        post = inference_rewrite(
            scraped_markdown=body.get('markdown') or '',
            source_url=body.get('source_url') or '',
            extra_hint=body.get('hint') or '',
        )
    except BlogAIError as e:
        return _json_err(e)
    # Suggest a non-colliding slug if the AI's pick is taken.
    base_slug = post['slug']
    candidate = base_slug
    n = 2
    while slug_exists(candidate):
        candidate = f'{base_slug}-{n}'
        n += 1
        if n > 50:
            break
    post['slug'] = candidate
    return JsonResponse({'ok': True, 'post': post})


@admin_required
@require_http_methods(['POST'])
def admin_blog_publish(request):
    """Save (insert or update) a blog post.

    Accepts a normal form POST so the Publish button can be a plain
    ``<form>`` submit and we get free CSRF + browser nav semantics.
    """
    f = request.POST
    title = (f.get('title') or '').strip()
    if not title:
        return _json_err('Title is required')

    raw_slug   = (f.get('slug') or '').strip()
    slug       = _ai_slugify(raw_slug or title)
    original   = (f.get('original_slug') or '').strip()  # set when editing
    is_editing = bool(original)

    # If creating, auto-suffix to dodge collisions; if editing and the slug
    # changed to one that exists, refuse rather than silently overwrite a
    # different post.
    if not is_editing:
        candidate = slug
        n = 2
        while slug_exists(candidate):
            candidate = f'{slug}-{n}'
            n += 1
            if n > 50:
                break
        slug = candidate
    elif slug != original and slug_exists(slug):
        return _json_err(f"Slug '{slug}' already exists. Pick another.")

    # Preserve the original published_at on edit so the post keeps its
    # spot in the date-sorted list; only mint a new timestamp on create.
    # ``upsert_blog_post`` writes this into a NOT NULL TIMESTAMPTZ column,
    # so we must always supply a value.
    existing_published_at = None
    if is_editing:
        prior = get_blog_post(original)
        if prior:
            existing_published_at = prior.get('published_at')
    published_at = existing_published_at or datetime.utcnow()

    record = {
        'slug':            slug,
        'title':           title,
        'author':          (f.get('author') or 'Permitlify Team').strip(),
        'author_initials': (f.get('author_initials') or 'PL').strip()[:8],
        # ``upsert_blog_post`` reads this under the ``date`` key (writes to
        # the ``date_label`` column).
        'date':            (f.get('date_label') or _blog_date_label(date.today())).strip(),
        'published_at':    published_at,
        'read_time':       (f.get('read_time') or '5 min read').strip()[:40],
        'tag':             (f.get('tag') or 'Insights').strip()[:60],
        'tag_color':       (f.get('tag_color') or 'blue').strip()[:20],
        'thumb':           (f.get('thumb') or '📝').strip()[:8],
        'thumb_bg':        (f.get('thumb_bg') or 'linear-gradient(135deg,#1d4ed8,#059669)').strip(),
        'excerpt':         (f.get('excerpt') or '').strip(),
        'content':         (f.get('content') or '').strip(),
        'related':         [],
        'is_featured':     (f.get('is_featured') == 'on'),
    }
    if not record['excerpt']:
        return _json_err('Excerpt is required')
    if not record['content']:
        return _json_err('Content is required')

    # If editing AND the slug changed, drop the old row before upserting.
    if is_editing and slug != original:
        delete_blog_post(original)

    try:
        upsert_blog_post(record)
    except Exception as e:
        return _json_err(f'Database error: {e}', status=500)

    return JsonResponse({
        'ok':       True,
        'slug':     slug,
        'view_url': f'/blog/{slug}/',
        'edit_url': f'/admin-panel/blog/edit/{slug}/',
    })


@admin_required
@require_http_methods(['POST'])
def admin_blog_delete(request, slug):
    delete_blog_post(slug)
    return redirect('admin_blog')


@admin_required
def admin_blog_settings_view(request):
    """AI config page: datacenter proxy + local GPT-OSS rewrite model."""
    if request.method == 'POST':
        section = request.POST.get('section', '')
        try:
            if section == 'proxy':
                proxy = (request.POST.get('datacenter_proxy') or '').strip()
                if proxy:
                    from .scrapers.base import parse_proxy_string
                    if not parse_proxy_string(proxy):
                        raise ValueError('Proxy format is invalid. Use user:pass@host:port or host:port.')
                set_system_setting('datacenter_proxy', proxy)
                result = {
                    'ok': True,
                    'section': 'proxy',
                    'datacenter_proxy': datacenter_proxy_raw(),
                }
            elif section == 'rewrite':
                model = (request.POST.get('blog_rewrite_model') or '').strip()
                if model:
                    set_system_setting('blog_rewrite_model', model)
                result = {'ok': True, 'section': 'rewrite'}
            elif section == 'clear_proxy':
                set_system_setting('datacenter_proxy', '')
                result = {'ok': True, 'section': 'clear_proxy'}
            else:
                result = {'ok': False, 'error': 'Unknown section'}
        except Exception as e:
            result = {'ok': False, 'error': str(e)}
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse(result)
        return redirect('admin_blog_settings')

    has_inference = True  # local GPT-OSS endpoint needs no paid key
    return render(request, 'core/admin_blog_settings.html', _admin_ctx(
        request,
        active_section='blog_settings',
        has_inference=has_inference,
        proxy_info=datacenter_proxy_info(),
        datacenter_proxy_value=datacenter_proxy_raw(),
        rewrite_model=(get_system_setting('blog_rewrite_model') or '').strip() or _DEFAULT_REWRITE_MODEL,
        default_rewrite_model=_DEFAULT_REWRITE_MODEL,
    ))


# ══════════════════════════════════════════════════════════════════════
# Marketing — Recovery Emails, Trade-specific landing pages.
# ══════════════════════════════════════════════════════════════════════

# Default recovery-email template set. Used when no admin override
# exists yet so the page shows ready-to-use copy from day one.
_RECOVERY_DEFAULTS = [
    {
        'step': 1, 'enabled': True, 'delay_hours': 1,
        'label': 'Same-day nudge',
        'delay_label': '1 hour',
        'subject': "{{name}}, your Permitlify trial is one click away",
        'body': (
            "<p>Hi {{name}},</p>"
            "<p>You started signing up at Permitlify earlier — did something get in the way?</p>"
            "<p>Your account is one click away from getting fresh, AI-scored permit leads delivered to your inbox tomorrow morning.</p>"
            "<p><a href=\"{{trial_link}}\" style=\"background:#1d4ed8;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:700;\">Finish signup → start free trial</a></p>"
            "<p>Cancel in one click before day 7 — never charged.</p>"
            "<p>— The Permitlify team</p>"
        ),
    },
    {
        'step': 2, 'enabled': True, 'delay_hours': 24,
        'label': 'Day-1 value reminder',
        'delay_label': '24 hours',
        'subject': "We pulled 14,200+ permits in your area yesterday",
        'body': (
            "<p>Hi {{name}},</p>"
            "<p>Yesterday alone, Permitlify scored over 14,200 new building permits across 248 US cities.</p>"
            "<p>The top 10% are the ones contractors like you actually want — fresh, high-budget, ready to call.</p>"
            "<p><a href=\"{{trial_link}}\" style=\"background:#1d4ed8;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:700;\">See today's top-scored permits →</a></p>"
            "<p>7-day free trial · Cancel anytime · No charge before day 7.</p>"
        ),
    },
    {
        'step': 3, 'enabled': True, 'delay_hours': 72,
        'label': 'Last-call (3-day) offer',
        'delay_label': '72 hours',
        'subject': "Last call — your Permitlify trial slot is closing",
        'body': (
            "<p>Hi {{name}},</p>"
            "<p>This is the last email in this sequence — we won't keep nagging you. 🙏</p>"
            "<p>If permit leads aren't a fit right now, no worries. If they are, your free trial is still waiting:</p>"
            "<p><a href=\"{{trial_link}}\" style=\"background:#059669;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:700;\">Activate my 7-day free trial →</a></p>"
            "<p>Cancel any time before day 7 and you won't be charged a cent.</p>"
        ),
    },
]


def _recovery_templates_get() -> list:
    """Merge admin overrides with hard-coded defaults."""
    from .db import get_system_setting
    saved = get_system_setting('mk_recovery_templates') or []
    by_step = {int(t.get('step', 0)): t for t in (saved or []) if isinstance(t, dict)}
    out = []
    for d in _RECOVERY_DEFAULTS:
        merged = dict(d)
        if d['step'] in by_step:
            merged.update({k: v for k, v in by_step[d['step']].items()
                           if k in ('subject', 'body', 'delay_hours', 'enabled')})
        out.append(merged)
    return out


@admin_required
def admin_recovery_emails_view(request):
    """Admin page: edit the 3 recovery templates + view the queue."""
    from .db import (set_system_setting, recovery_stats, recovery_recent)
    msg = err = ''
    if request.method == 'POST':
        if request.POST.get('action') == 'save_templates':
            try:
                payload = []
                for d in _RECOVERY_DEFAULTS:
                    s = d['step']
                    payload.append({
                        'step':        s,
                        'enabled':     bool(request.POST.get(f'step{s}_enabled')),
                        'delay_hours': max(0, int(request.POST.get(f'step{s}_delay_hours') or d['delay_hours'])),
                        'subject':     (request.POST.get(f'step{s}_subject') or '').strip(),
                        'body':        (request.POST.get(f'step{s}_body') or '').strip(),
                    })
                set_system_setting('mk_recovery_templates', payload)
                msg = 'Templates saved. Newly-queued emails will use these.'
            except Exception as exc:
                log.exception("admin_recovery save failed")
                err = f'Save failed: {exc}'
    templates = _recovery_templates_get()
    # Re-decorate with display labels (label/delay_label live only in defaults)
    label_map = {d['step']: (d['label'], d['delay_label']) for d in _RECOVERY_DEFAULTS}
    for t in templates:
        lbl, dlbl = label_map.get(t['step'], ('', ''))
        t['label'] = lbl
        t['delay_label'] = f"{t['delay_hours']} hour{'s' if t['delay_hours'] != 1 else ''}"
    ctx = _admin_base_ctx(request, 'mk_recovery')
    ctx.update({
        'templates': templates,
        'stats':     recovery_stats(),
        'recent':    recovery_recent(50),
        'msg':       msg, 'err': err,
    })
    return render(request, 'core/admin_recovery_emails.html', ctx)


@admin_required
def admin_pricing_view(request):
    """Admin page: edit the displayed plan prices (monthly + annual,
    prod + dev) site-wide. Writes to the ``plan_price_<mode>_<plan>_<period>``
    system_settings keys that :func:`core.whop.get_plan_price` reads
    from; an empty value clears the override and falls back to the
    hard-coded default in :data:`core.whop._DEFAULT_DISPLAY_PRICES`.

    ``set_system_setting`` triggers ``clear_settings_cache`` which in
    turn calls ``_clear_pricing_dict_cache`` — so a save is reflected
    everywhere (pricing page, paywall, onboarding, settings billing
    tab, signup flow) on the next request, no restart required.
    """
    from .db import get_system_setting, set_system_setting
    # Annual billing has been fully removed from the user-facing site
    # (only the back-end retains annual price reads for legacy
    # subscribers). The admin editor therefore only manages monthly
    # prices; the annual keys are left untouched, falling back to
    # their hard-coded defaults.
    TIERS   = ('starter', 'pro', 'agency')
    PERIODS = ('monthly',)
    MODES   = ('prod', 'dev')
    msg = err = ''
    if request.method == 'POST':
        try:
            for mode in MODES:
                for t in TIERS:
                    for p in PERIODS:
                        raw = (request.POST.get(f'{mode}_{t}_{p}') or '').strip()
                        key = f'plan_price_{mode}_{t}_{p}'
                        if raw == '':
                            set_system_setting(key, '')
                        else:
                            try:
                                n = max(0, int(float(raw)))
                                set_system_setting(key, str(n))
                            except (TypeError, ValueError):
                                # Silently skip invalid number rather than nuking the whole save.
                                pass
                    # Per-plan trial length (days). Stored separately from
                    # the price keys so trial changes don't perturb price
                    # rendering and vice versa.
                    raw_t = (request.POST.get(f'{mode}_{t}_trial') or '').strip()
                    tkey  = f'plan_trial_{mode}_{t}'
                    if raw_t == '':
                        set_system_setting(tkey, '')
                    else:
                        try:
                            d = max(0, int(float(raw_t)))
                            set_system_setting(tkey, str(d))
                        except (TypeError, ValueError):
                            pass
            msg = 'Prices and trial lengths saved. Pricing page, paywall, onboarding, and settings now reflect the new values.'
        except Exception as exc:
            log.exception("admin_pricing save failed")
            err = f'Save failed: {exc}'

    # Build the form values: override (raw setting) + effective (override OR default)
    # so the admin can see what's actually being rendered today.
    prices = {}
    effective = {}
    trials = {}
    effective_trials = {}
    for mode in MODES:
        defaults = wp._DEFAULT_DISPLAY_PRICES.get(mode, {})
        trial_defaults = wp._DEFAULT_TRIAL_DAYS.get(mode, {})
        for t in TIERS:
            for p in PERIODS:
                raw = get_system_setting(f'plan_price_{mode}_{t}_{p}') or ''
                prices[f'{mode}_{t}_{p}']    = raw
                effective[f'{mode}_{t}_{p}'] = raw if raw else defaults.get((t, p), 0)
            raw_t = get_system_setting(f'plan_trial_{mode}_{t}') or ''
            trials[f'{mode}_{t}']           = raw_t
            effective_trials[f'{mode}_{t}'] = raw_t if raw_t else trial_defaults.get(t, 7)

    annual_enabled = wp.annual_billing_enabled()
    ctx = _admin_base_ctx(request, 'mk_prices')
    ctx.update({
        'prices':           prices,
        'effective':        effective,
        'trials':           trials,
        'effective_trials': effective_trials,
        'annual_enabled':   annual_enabled,
        'msg': msg, 'err': err,
    })
    return render(request, 'core/admin_pricing.html', ctx)


@csrf_exempt
@require_http_methods(['POST'])
def api_recovery_emails_tick(request):
    """HTTP cron trigger for the recovery-email dispatcher. Invokes the
    same ``recovery_emails_tick`` management command the admin can run
    manually, so a GitHub Actions schedule (or any external scheduler)
    can fire pending recovery emails every 15 minutes.

    Auth: ``X-Scraper-Key`` header OR ``?key=`` query param matched
    against ``SCRAPER_INGEST_KEY`` (same key the existing cron trigger
    uses — no new secret to manage).

    Returns JSON {ok, sent, skipped, failed, elapsed_ms}.
    """
    if not _cron_trigger_authed(request):
        return JsonResponse({'ok': False, 'error': 'unauthorized'},
                            status=401)
    try:
        limit = int(request.GET.get('limit') or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 2000))

    from .db import recovery_due_rows, recovery_mark, get_user_by_id
    from .email_service import send_email
    import time as _time
    sent = skipped = failed = 0
    t0 = _time.time()
    try:
        due = recovery_due_rows(limit=limit)
    except Exception:
        log.exception('api_recovery_emails_tick: recovery_due_rows failed')
        return JsonResponse({'ok': False, 'error': 'queue read failed'},
                            status=500)

    for row in due:
        rid  = row['id']
        uid  = row['user_id']
        try:
            user = get_user_by_id(uid) or {}
            if not user:
                recovery_mark(rid, 'skipped', note='user missing')
                skipped += 1
                continue
            if user.get('subscription_active') or user.get('marketing_unsub'):
                recovery_mark(rid, 'skipped',
                              note='paid or unsubscribed')
                skipped += 1
                continue
            tpl  = row.get('template') or {}
            subj = tpl.get('subject', '')
            body = tpl.get('body', '')
            if not subj or not body:
                recovery_mark(rid, 'skipped', note='empty template')
                skipped += 1
                continue
            first_name = (user.get('name') or '').split(' ', 1)[0] or 'there'
            ctx = {
                'name':       first_name,
                'email':      user.get('email', ''),
                'trial_link': row.get('trial_link')
                              or 'https://permitlify.com/signup/',
            }
            for k, v in ctx.items():
                subj = subj.replace('{{' + k + '}}', str(v))
                body = body.replace('{{' + k + '}}', str(v))
            ok, err = send_email(
                svc_key='recovery',
                to=user.get('email', ''),
                subject=subj,
                body_text='',
                body_html=_render_recovery_html(subj, body),
            )
            if ok:
                recovery_mark(rid, 'sent')
                sent += 1
            else:
                recovery_mark(rid, 'failed', note=str(err)[:240])
                failed += 1
        except Exception as exc:
            log.exception('api_recovery_emails_tick: row %s failed', rid)
            try:
                recovery_mark(rid, 'failed', note=str(exc)[:240])
            except Exception:
                pass
            failed += 1

    return JsonResponse({
        'ok':         True,
        'sent':       sent,
        'skipped':    skipped,
        'failed':     failed,
        'processed':  len(due),
        'elapsed_ms': int((_time.time() - t0) * 1000),
    })


# ─────────────────────────────────────────────────────────────────────
# Email campaigns (bulk Resend, CSV-driven, daily-capped) ────────────
# Admin: /admin-panel/marketing/campaigns/
# Cron:  /api/cron/campaigns/tick/   (also: python manage.py campaigns_tick)
# Webhook: /webhooks/resend/         (bounces / complaints / opens)
# Unsub:   /u/<token>/               (one-click, Gmail Feb-2024 requirement)
# ─────────────────────────────────────────────────────────────────────

def _campaign_unsub_token(recipient_id: int, email: str) -> str:
    """HMAC-signed token so one-click unsub URLs can't be forged."""
    import hmac as _hmac, hashlib as _h
    from django.conf import settings as _s
    secret = (getattr(_s, 'SECRET_KEY', '') or '').encode()
    payload = f"{recipient_id}:{(email or '').lower()}".encode()
    sig = _hmac.new(secret, payload, _h.sha256).hexdigest()[:24]
    return f"{recipient_id}.{sig}"


def _campaign_unsub_verify(token: str) -> tuple[int, str]:
    """Returns (recipient_id, email) on valid token, (0,'') otherwise."""
    from .db import pg as _pg
    try:
        rid_s, sig = (token or '').split('.', 1)
        rid = int(rid_s)
    except Exception:
        return 0, ''
    row = _pg.query_one(
        "SELECT email FROM email_campaign_recipients WHERE id = %s",
        (rid,),
    )
    if not row:
        return 0, ''
    expected = _campaign_unsub_token(rid, row['email']).split('.', 1)[1]
    import hmac as _hmac
    if not _hmac.compare_digest(expected, sig):
        return 0, ''
    return rid, row['email']


def _render_campaign_html(subject: str, body_html: str,
                          unsubscribe_url: str = '') -> str:
    """Wrap a campaign body in the branded shell. Fallback to raw body
    on render failure so a template typo can never block a send.

    If the body is already a full, standalone HTML document (e.g. one of the
    ready-made campaign templates), it is returned as-is so the branded shell
    hero/footer is NOT stacked on top of it.
    """
    _head = (body_html or '').lstrip()[:200].lower()
    if _head.startswith('<!doctype') or _head.startswith('<html'):
        return body_html
    try:
        from django.template.loader import render_to_string
        import datetime as _dt, re as _re
        preheader = _re.sub(r'<[^>]+>', ' ', body_html or '')
        preheader = _re.sub(r'\s+', ' ', preheader).strip()[:110]
        return render_to_string('core/emails/campaign.html', {
            'subject':         subject,
            'body_html':       body_html,
            'preheader':       preheader,
            'unsubscribe_url': unsubscribe_url or 'https://permitlify.com/unsubscribe/',
            'year':            _dt.datetime.utcnow().year,
        })
    except Exception:
        log.exception("campaign shell render failed; sending raw body")
        return body_html


def _campaign_substitute(text: str, recipient: dict, unsubscribe_url: str = '') -> str:
    """Substitute {{name}} / {{email}} / {{unsubscribe_url}} in subject/body."""
    name = (recipient.get('name') or '').strip()
    first = name.split(' ', 1)[0] if name else 'there'
    repl = {
        'name':            first,
        'full_name':       name or first,
        'email':           recipient.get('email', ''),
        'unsubscribe_url': unsubscribe_url,
    }
    out = text or ''
    for k, v in repl.items():
        out = out.replace('{{' + k + '}}', str(v))
    return out


def _campaigns_auto_pull_once(min_hours: int = 20, dry: bool = False) -> dict:
    """Daily top-up: for every campaign flagged ``auto_pull_count > 0`` that
    is sending and hasn't been pulled in the last ``min_hours`` hours, fetch
    its top-N newest highest-scored contractor emails and add them. New
    leads only — suppressed and already-enrolled addresses are excluded by
    ``contractor_emails_top_for_campaign``. The send cap still applies
    later in ``_campaign_tick_once`` (this only enqueues recipients).
    """
    from . import db as _db
    out = {'campaigns': 0, 'inserted': 0, 'duplicates': 0, 'detail': []}
    try:
        due = _db.campaigns_due_for_auto_pull(min_hours=min_hours)
    except Exception:
        log.exception('campaigns_due_for_auto_pull failed')
        return out
    for c in due:
        cid = c['id']
        n = max(1, min(int(c.get('auto_pull_count') or 0), 5000))
        try:
            rows = _db.contractor_emails_top_for_campaign(cid, n)
            if dry:
                log.info('DRY auto-pull campaign #%s: would add up to %s '
                         '(found %s candidates)', cid, n, len(rows))
                out['detail'].append({'id': cid, 'name': c.get('name'),
                                      'candidates': len(rows), 'dry': True})
                continue
            res = (_db.campaign_recipients_bulk_insert(cid, rows) if rows
                   else {'inserted': 0, 'duplicates': 0})
            _db.campaign_mark_auto_pulled(cid)
            out['campaigns'] += 1
            out['inserted'] += res.get('inserted', 0)
            out['duplicates'] += res.get('duplicates', 0)
            out['detail'].append({'id': cid, 'name': c.get('name'),
                                  'inserted': res.get('inserted', 0),
                                  'duplicates': res.get('duplicates', 0)})
            log.info('auto-pull campaign #%s: +%s new contractor emails',
                     cid, res.get('inserted', 0))
        except Exception:
            log.exception('auto-pull campaign #%s failed', cid)
    return out


def _campaign_tick_once(cid: int, per_tick: int = 20, dry: bool = False) -> dict:
    """Send up to ``per_tick`` recipients for a single campaign, while
    respecting the campaign's 24h daily_cap. Marks rows sent/failed/skipped
    and recalcs aggregate stats at the end.
    """
    from . import db as _db
    from .email_service import send_email
    sent = skipped = failed = 0
    c = _db.campaign_get(cid)
    if not c or c.get('status') != 'sending':
        return {'sent': 0, 'skipped': 0, 'failed': 0, 'quota_left': 0,
                'note': 'campaign missing or not in sending state'}

    # Self-heal: any row stuck in 'sending' for >10 min is the carcass of
    # a crashed prior tick — release it back to 'pending' so the next
    # tick can retry. 10 min >> any send_email timeout (12s) so this is
    # never a live in-flight row.
    from .db import pg as _pg_recover
    _pg_recover.execute(
        """UPDATE email_campaign_recipients
              SET status = 'pending'
            WHERE campaign_id = %s
              AND status = 'sending'
              AND COALESCE(sent_at, created_at) < NOW() - INTERVAL '10 minutes'""",
        (cid,),
    )
    today_sent = _db.campaign_today_sent_count(cid)
    quota_left = max(0, int(c['daily_cap']) - today_sent)
    if quota_left <= 0:
        return {'sent': 0, 'skipped': 0, 'failed': 0, 'quota_left': 0,
                'note': 'daily cap reached'}
    take = min(per_tick, quota_left)
    # Atomically claim rows (pending -> sending). Concurrent ticks see
    # different rows, so the daily cap is enforced even with parallel
    # cron runs or manual + cron overlap.
    rows = _db.campaign_recipients_claim(cid, limit=take)
    if not rows:
        # Nothing pending — flip to done so stats stop ticking.
        if c.get('total') and c.get('sent_count', 0) + c.get('failed_count', 0) \
           + c.get('skipped_count', 0) >= c.get('total', 0):
            _db.campaign_update(cid, status='done')
        return {'sent': 0, 'skipped': 0, 'failed': 0,
                'quota_left': quota_left, 'note': 'no pending recipients'}

    subj_tpl = c.get('subject') or ''
    body_tpl = c.get('body_html') or ''
    skip_users = bool(c.get('skip_existing_users'))

    for r in rows:
        rid = r['id']
        em  = r['email']
        try:
            sup = _db.suppression_check(em)
            if sup:
                _db.campaign_recipient_mark(rid, 'skipped', error=f'suppressed:{sup}')
                skipped += 1
                continue
            if skip_users and _db.email_exists_as_user(em):
                _db.campaign_recipient_mark(rid, 'skipped', error='existing-user')
                skipped += 1
                continue
            unsub_url = f"https://permitlify.com/u/{_campaign_unsub_token(rid, em)}/"
            subj = _campaign_substitute(subj_tpl, r, unsub_url)
            body = _campaign_substitute(body_tpl, r, unsub_url)
            html = _render_campaign_html(subj, body, unsub_url)
            if dry:
                log.info("DRY campaign #%s -> %s (subj=%r)", cid, em, subj)
                continue
            ok, info = send_email(
                svc_key='recovery',  # reuse the "recovery" sender identity
                to=em,
                subject=subj,
                body_text='',
                body_html=html,
                # RFC 8058 one-click unsubscribe. The mailbox provider's native
                # "unsubscribe" button issues a POST with the One-Click body —
                # our /u/<token>/ view only acts on POST, so security scanners
                # that merely GET the link can't trigger a false unsubscribe.
                headers={
                    'List-Unsubscribe': f'<{unsub_url}>',
                    'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
                },
            )
            if ok:
                _db.campaign_recipient_mark(rid, 'sent', message_id=info or '')
                sent += 1
            else:
                _db.campaign_recipient_mark(rid, 'failed', error=str(info)[:480])
                failed += 1
        except Exception as exc:
            log.exception("campaign #%s recipient %s failed", cid, rid)
            try:
                _db.campaign_recipient_mark(rid, 'failed', error=str(exc)[:480])
            except Exception:
                pass
            failed += 1

    _db.campaign_recalc_stats(cid)
    # Bump last_send_at if we actually sent anything.
    if sent and not dry:
        from .db import pg as _pg
        _pg.execute("UPDATE email_campaigns SET last_send_at = NOW() WHERE id = %s", (cid,))
    return {'sent': sent, 'skipped': skipped, 'failed': failed,
            'quota_left': quota_left - sent - failed - skipped}


# ── Admin pages ──────────────────────────────────────────────────────

def _parse_recipients_csv(raw: str) -> list:
    """Parse uploaded CSV into [{email,name}, ...]. Accepts:
      * 'email' or 'email,name' or 'name,email' headers (case-insensitive)
      * No header (assumes first col = email, second col = name)
    """
    import csv as _csv, io as _io
    out = []
    if not raw:
        return out
    text = raw.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return out
    rdr = _csv.reader(_io.StringIO(text))
    rows = list(rdr)
    if not rows:
        return out
    head = [(c or '').strip().lower() for c in rows[0]]
    has_header = ('email' in head) or ('e-mail' in head)
    email_idx = name_idx = -1
    if has_header:
        for i, h in enumerate(head):
            if h in ('email', 'e-mail'):
                email_idx = i
            elif h in ('name', 'full name', 'first name', 'contact'):
                name_idx = i
        body_rows = rows[1:]
    else:
        email_idx, name_idx = 0, (1 if rows[0] and len(rows[0]) > 1 else -1)
        body_rows = rows
    for row in body_rows:
        if not row:
            continue
        em = (row[email_idx] if email_idx >= 0 and email_idx < len(row) else '').strip()
        if not em or '@' not in em:
            continue
        nm = (row[name_idx] if name_idx >= 0 and name_idx < len(row) else '').strip()
        out.append({'email': em, 'name': nm})
    return out


@admin_required
def admin_campaigns_list_view(request):
    from .db import (campaigns_list, campaign_create,
                     campaign_recipients_bulk_insert)
    msg = err = ''
    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'create':
            try:
                name = (request.POST.get('name') or '').strip()[:200] or 'Untitled campaign'
                subject = (request.POST.get('subject') or '').strip()[:300]
                body = (request.POST.get('body_html') or '').strip()
                cap = max(1, min(int(request.POST.get('daily_cap') or 200), 10000))
                skip_users = bool(request.POST.get('skip_existing_users'))
                if not subject or not body:
                    raise ValueError('Subject and body are required.')
                uploaded = request.FILES.get('csv_file')
                rows = []
                if uploaded:
                    # Cap at 10 MB so a malicious/accidental huge upload
                    # can't OOM the app server. 10 MB ≈ ~300k recipients,
                    # well above any reasonable single-campaign batch.
                    if getattr(uploaded, 'size', 0) > 10 * 1024 * 1024:
                        raise ValueError('CSV too large (10 MB max). Split into smaller files.')
                    raw = uploaded.read().decode('utf-8', errors='replace')
                    rows = _parse_recipients_csv(raw)
                cid = campaign_create(
                    name=name, subject=subject, body_html=body,
                    daily_cap=cap, skip_existing_users=skip_users,
                    created_by=request.session.get('user_id') or 0,
                )
                if rows:
                    res = campaign_recipients_bulk_insert(cid, rows)
                    msg = (f'Campaign created (#{cid}). {res["inserted"]} recipients added, '
                           f'{res["duplicates"]} duplicates, {res["invalid"]} invalid.')
                else:
                    msg = f'Campaign created (#{cid}). Upload a CSV on the detail page to add recipients.'
                return redirect('admin_campaign_detail', cid=cid)
            except Exception as exc:
                log.exception('admin_campaigns_list create failed')
                err = f'Create failed: {exc}'
    camps = campaigns_list(limit=200)
    ctx = _admin_base_ctx(request, 'mk_campaigns')
    ctx.update({'campaigns': camps, 'msg': msg, 'err': err})
    return render(request, 'core/admin_campaigns_list.html', ctx)


@admin_required
def admin_campaign_detail_view(request, cid: int):
    from .db import (campaign_get, campaign_update, campaign_delete,
                     campaign_recipients_bulk_insert, campaign_today_sent_count,
                     campaign_recalc_stats)
    msg = err = ''
    if request.method == 'POST':
        action = request.POST.get('action', '')
        try:
            if action == 'save':
                # Auto-pull: 0 = off. Only honoured when the enable checkbox
                # is ticked, so unchecking it cleanly disables the daily pull.
                if request.POST.get('auto_pull_enabled'):
                    auto_pull = max(0, min(int(request.POST.get('auto_pull_count') or 0), 5000))
                else:
                    auto_pull = 0
                campaign_update(
                    cid,
                    name=(request.POST.get('name') or '').strip()[:200] or 'Untitled',
                    subject=(request.POST.get('subject') or '').strip()[:300],
                    body_html=(request.POST.get('body_html') or '').strip(),
                    daily_cap=max(1, min(int(request.POST.get('daily_cap') or 200), 10000)),
                    skip_existing_users=bool(request.POST.get('skip_existing_users')),
                    auto_pull_count=auto_pull,
                )
                msg = 'Saved.'
            elif action == 'upload_csv':
                uploaded = request.FILES.get('csv_file')
                if not uploaded:
                    raise ValueError('No file uploaded.')
                if getattr(uploaded, 'size', 0) > 10 * 1024 * 1024:
                    raise ValueError('CSV too large (10 MB max). Split into smaller files.')
                raw = uploaded.read().decode('utf-8', errors='replace')
                rows = _parse_recipients_csv(raw)
                res = campaign_recipients_bulk_insert(cid, rows)
                msg = (f'{res["inserted"]} new recipients added, '
                       f'{res["duplicates"]} duplicates skipped, '
                       f'{res["invalid"]} invalid rows.')
            elif action == 'start':
                campaign_update(cid, status='sending')
                from .db import pg as _pg
                _pg.execute("UPDATE email_campaigns SET started_at = COALESCE(started_at, NOW()) WHERE id = %s", (cid,))
                msg = 'Campaign started. Cron will dispatch within 15 minutes.'
            elif action == 'pause':
                campaign_update(cid, status='paused')
                msg = 'Campaign paused.'
            elif action == 'resume':
                campaign_update(cid, status='sending')
                msg = 'Campaign resumed.'
            elif action == 'delete':
                campaign_delete(cid)
                return redirect('admin_campaigns_list')
            elif action == 'send_test':
                to = (request.POST.get('test_email') or '').strip()
                if '@' not in to:
                    raise ValueError('Invalid test email.')
                from .email_service import send_email
                c = campaign_get(cid) or {}
                fake = {'email': to, 'name': 'Test User'}
                unsub_url = f"https://permitlify.com/u/test/"
                subj = _campaign_substitute(c.get('subject', ''), fake, unsub_url)
                body = _campaign_substitute(c.get('body_html', ''), fake, unsub_url)
                html = _render_campaign_html(subj, body, unsub_url)
                ok, info = send_email(svc_key='recovery', to=to,
                                      subject=f'[TEST] {subj}',
                                      body_text='', body_html=html)
                if ok:
                    msg = f'Test sent to {to} ({info or "no id"}).'
                else:
                    err = f'Test send failed: {info}'
        except Exception as exc:
            log.exception('admin_campaign_detail action %s failed', action)
            err = f'{action} failed: {exc}'

    c = campaign_get(cid)
    if not c:
        return redirect('admin_campaigns_list')
    campaign_recalc_stats(cid)
    c = campaign_get(cid)
    today_sent = campaign_today_sent_count(cid)
    from .email_templates import get_campaign_templates
    ctx = _admin_base_ctx(request, 'mk_campaigns')
    ctx.update({
        'c':          c,
        'today_sent': today_sent,
        'remaining_today': max(0, int(c.get('daily_cap', 0)) - today_sent),
        'msg': msg, 'err': err,
        'email_templates': get_campaign_templates(),
    })
    return render(request, 'core/admin_campaign_detail.html', ctx)


@admin_required
def admin_campaign_recipients_data(request, cid: int):
    """Server-side DataTables endpoint for the recipients table."""
    from .db import campaign_recipients_page
    try:
        offset = max(0, int(request.GET.get('start') or 0))
        length = max(1, min(int(request.GET.get('length') or 50), 500))
    except (TypeError, ValueError):
        offset, length = 0, 50
    search = (request.GET.get('search[value]') or request.GET.get('search') or '').strip()
    status = (request.GET.get('status') or '').strip()
    draw   = int(request.GET.get('draw') or 1)
    rows, filtered, total = campaign_recipients_page(
        cid, offset=offset, limit=length, search=search, status=status,
    )
    def _fmt(dt):
        return dt.strftime('%b %d, %H:%M') if dt else ''
    data = [{
        'id':           r['id'],
        'email':        r['email'],
        'name':         r['name'] or '',
        'status':       r['status'],
        'sent_at':      _fmt(r.get('sent_at')),
        'delivered_at': _fmt(r.get('delivered_at')),
        'opened_at':    _fmt(r.get('opened_at')),
        'error':        (r.get('error') or '')[:160],
    } for r in rows]
    return JsonResponse({
        'draw': draw, 'recordsTotal': total, 'recordsFiltered': filtered,
        'data': data,
    })


# ── Contractor email pool ────────────────────────────────────────────

@admin_required
def admin_contractor_emails_view(request):
    """The pool of every contractor email we've scraped. Grows daily as
    scrapers run. From here the admin pulls the top-N most-targeted
    (highest AI score) emails into a campaign to send 50/100 a day."""
    from .db import campaigns_list, contractor_emails_pool_count
    ctx = _admin_base_ctx(request, 'mk_campaigns')
    ctx.update({
        'pool_count': contractor_emails_pool_count(),
        'campaigns':  campaigns_list(limit=200),
    })
    return render(request, 'core/admin_contractor_emails.html', ctx)


@admin_required
def admin_contractor_emails_data(request):
    """Server-side DataTables endpoint for the contractor email pool."""
    from .db import contractor_emails_dt
    try:
        offset = max(0, int(request.GET.get('start') or 0))
        length = max(1, min(int(request.GET.get('length') or 50), 500))
    except (TypeError, ValueError):
        offset, length = 0, 50
    search = (request.GET.get('search[value]')
              or request.GET.get('search') or '').strip()
    try:
        order_col = int(request.GET.get('order[0][column]') or 5)
    except (TypeError, ValueError):
        order_col = 5
    order_dir = (request.GET.get('order[0][dir]') or 'desc').strip()
    draw = int(request.GET.get('draw') or 1)
    rows, filtered, total = contractor_emails_dt(
        offset=offset, limit=length, search=search,
        order_col=order_col, order_dir=order_dir,
    )

    def _fmt(dt):
        return dt.strftime('%b %d, %Y') if dt else ''
    data = [{
        'email':         r['email'],
        'name':          r.get('name') or '',
        'phone':         r.get('phone') or '',
        'location':      ', '.join([p for p in (r.get('city'), r.get('state')) if p]),
        'permit_count':  int(r.get('permit_count') or 0),
        'best_score':    r.get('best_score'),
        'last_seen':     _fmt(r.get('last_seen')),
        'suppressed':    bool(r.get('suppressed')),
        'times_emailed': int(r.get('times_emailed') or 0),
    } for r in rows]
    return JsonResponse({
        'draw': draw, 'recordsTotal': total, 'recordsFiltered': filtered,
        'data': data,
    })


@admin_required
@require_http_methods(['POST'])
def admin_contractor_emails_pull(request):
    """Pull the top-N most-targeted contractor emails into a campaign.

    ``cid`` may reference an existing campaign, or be blank to create a
    fresh draft on the fly. The selection is highest-AI-score first,
    excluding suppressed addresses and anyone already in that campaign,
    so running it daily simply tops the campaign up with newly-scraped,
    not-yet-contacted leads.
    """
    from .db import (campaign_get, campaign_create,
                     contractor_emails_top_for_campaign,
                     campaign_recipients_bulk_insert)
    try:
        n = max(1, min(int(request.POST.get('count') or 50), 5000))
    except (TypeError, ValueError):
        n = 50
    cid_raw = (request.POST.get('cid') or '').strip()
    try:
        if cid_raw:
            cid = int(cid_raw)
            c = campaign_get(cid)
            if not c:
                raise ValueError('Campaign not found.')
        else:
            # Create a new draft seeded from the pool. Cap defaults to the
            # batch size so the cron sends roughly this many per day.
            name = (request.POST.get('new_name') or '').strip()[:200] \
                or f'Contractor outreach — {_dt.datetime.utcnow():%b %d}'
            cid = campaign_create(
                name=name,
                subject='Quick question about your recent permit',
                body_html='<p>Hi {{name}},</p><p>...</p>',
                daily_cap=n,
                skip_existing_users=True,
                created_by=request.session.get('user_id') or 0,
            )
        rows = contractor_emails_top_for_campaign(cid, n)
        if not rows:
            return JsonResponse({
                'ok': True, 'cid': cid, 'inserted': 0,
                'message': 'No new contractor emails available to add — '
                           'this campaign already has all current leads.',
            })
        res = campaign_recipients_bulk_insert(cid, rows)
        return JsonResponse({
            'ok': True, 'cid': cid,
            'inserted': res['inserted'], 'duplicates': res['duplicates'],
            'message': (f'Added {res["inserted"]} top-targeted contractor '
                        f'emails to the campaign.'),
        })
    except Exception as exc:
        log.exception('admin_contractor_emails_pull failed')
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)


@admin_required
@require_http_methods(['POST'])
def admin_campaign_preview(request, cid: int):
    """Render the campaign through the branded shell using current
    (possibly unsaved) subject + body from the editor. Same code path
    as a real send so preview is byte-identical to inbox.
    """
    subj = (request.POST.get('subject') or '').strip()
    body = (request.POST.get('body_html') or '').strip()
    fake = {'email': 'preview@permitlify.com', 'name': 'Khemiri'}
    unsub_url = request.build_absolute_uri('/u/preview/')
    subj = _campaign_substitute(subj, fake, unsub_url)
    body = _campaign_substitute(body, fake, unsub_url)
    html = _render_campaign_html(subj or '(no subject)',
                                 body or '<p>(empty body)</p>', unsub_url)
    return HttpResponse(html, content_type='text/html; charset=utf-8')


@csrf_exempt
@require_http_methods(['POST'])
def api_campaigns_tick_view(request):
    """HTTP cron endpoint. Auth: same cron token as recovery emails
    (``X-Scraper-Key`` header or ``?key=`` matched to SCRAPER_INGEST_KEY).
    Must NOT be @admin_required — external schedulers (GitHub Actions cron)
    have no admin session; the cron key IS the auth. CSRF-exempt for the
    same reason, mirroring api_recovery_emails_tick.
    """
    if not _cron_trigger_authed(request):
        return JsonResponse({'ok': False, 'error': 'unauthorized'}, status=401)
    try:
        per_tick = max(1, min(int(request.GET.get('per_tick') or 20), 500))
    except (TypeError, ValueError):
        per_tick = 20
    from . import db as _db
    # Daily top-up first so freshly-pulled leads can ship in this same pass.
    auto_pull = _campaigns_auto_pull_once()
    camps = _db.campaigns_list(limit=200)
    active = [c for c in camps if c.get('status') == 'sending']
    totals = {'sent': 0, 'skipped': 0, 'failed': 0, 'campaigns': 0}
    per_camp = []
    for c in active:
        res = _campaign_tick_once(c['id'], per_tick=per_tick)
        per_camp.append({'id': c['id'], 'name': c['name'], **res})
        for k in ('sent', 'skipped', 'failed'):
            totals[k] += res.get(k, 0)
        totals['campaigns'] += 1
    return JsonResponse({'ok': True, **totals, 'detail': per_camp,
                         'auto_pull': auto_pull})


# ── Resend webhook (bounces / complaints / delivered / opened) ──────

def _resend_webhook_verify(request) -> bool:
    """Verify Svix signature on a Resend webhook. Returns True if the
    request is authentic OR if no webhook secret is configured (best-
    effort dev mode). The secret lives in system_settings under
    ``resend_webhook_secret`` (paste from Resend dashboard).
    """
    from .db import get_system_setting
    from django.conf import settings as _dj
    secret = (get_system_setting('resend_webhook_secret') or '').strip()
    if not secret:
        # Fail OPEN only in DEBUG (local dev / first-time setup). In
        # production an unset secret means anyone on the internet could
        # forge bounce/complaint events and poison the suppression list,
        # so we MUST reject.
        return bool(getattr(_dj, 'DEBUG', False))
    svix_id   = request.META.get('HTTP_SVIX_ID', '')
    svix_ts   = request.META.get('HTTP_SVIX_TIMESTAMP', '')
    svix_sig  = request.META.get('HTTP_SVIX_SIGNATURE', '')
    if not (svix_id and svix_ts and svix_sig):
        return False
    try:
        import base64 as _b64, hmac as _hmac, hashlib as _h
        key_b = _b64.b64decode(secret.split('_', 1)[1]) if secret.startswith('whsec_') \
                else secret.encode()
        signed = f"{svix_id}.{svix_ts}.{request.body.decode('utf-8','replace')}".encode()
        expected = _b64.b64encode(_hmac.new(key_b, signed, _h.sha256).digest()).decode()
        for token in svix_sig.split(' '):
            if not token.startswith('v1,'):
                continue
            if _hmac.compare_digest(expected, token[3:]):
                return True
    except Exception:
        log.exception('resend webhook signature verify failed')
    return False


@csrf_exempt
@require_http_methods(['POST'])
def resend_webhook(request):
    """Receive Resend (Svix) webhook events and update recipient rows
    + global suppression list. Configure URL in Resend dashboard:
    https://YOUR_DOMAIN/webhooks/resend/
    """
    if not _resend_webhook_verify(request):
        return HttpResponse('Invalid signature', status=403)
    try:
        evt = json.loads(request.body or b'{}')
    except Exception:
        return HttpResponse('Bad JSON', status=400)
    etype = (evt.get('type') or '').lower()
    data  = evt.get('data') or {}
    mid   = data.get('email_id') or data.get('id') or ''
    to    = (data.get('to') or [''])
    email = (to[0] if isinstance(to, list) and to else (data.get('to') or '')) or ''
    from . import db as _db
    handled = False
    try:
        if etype == 'email.delivered':
            row = _db.campaign_recipient_mark_by_message(mid, 'delivered', 'delivered_at')
            handled = bool(row)
        elif etype == 'email.opened':
            row = _db.campaign_recipient_mark_by_message(mid, 'opened', 'opened_at')
            handled = bool(row)
        elif etype in ('email.bounced', 'email.hard_bounced'):
            row = _db.campaign_recipient_mark_by_message(mid, 'bounced')
            if email:
                _db.suppression_add(email, 'bounced',
                                    note=str(data.get('reason') or '')[:200])
            if row:
                _db.campaign_recalc_stats(row['campaign_id'])
            handled = True
        elif etype == 'email.complained':
            row = _db.campaign_recipient_mark_by_message(mid, 'complained')
            if email:
                _db.suppression_add(email, 'complained')
            if row:
                _db.campaign_recalc_stats(row['campaign_id'])
            handled = True
    except Exception:
        log.exception('resend_webhook: dispatch failed for %s', etype)
    return JsonResponse({'ok': True, 'type': etype, 'handled': handled})


# ── One-click unsubscribe ────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def campaign_unsubscribe_view(request, token: str):
    """One-click unsubscribe (Gmail/Yahoo Feb-2024 bulk-sender rules).

    Bot-safe two-step flow:

    * **GET** (a human clicking the link in the email, OR a mailbox security
      scanner / link-prefetcher silently visiting every URL) only *shows* a
      confirmation page with a button. It does NOT unsubscribe. This stops the
      flood of false unsubscribes from Outlook Safe Links, corporate spam
      filters and antivirus that GET every link the instant a mail arrives.
    * **POST** actually unsubscribes. Two things POST here: (a) the mailbox
      provider's native one-click button, which sends ``List-Unsubscribe=
      One-Click`` per RFC 8058, and (b) our own confirmation-page button.

    Marks the recipient row + adds the email to the global suppression list so
    no future campaign can reach them.
    """
    from . import db as _db
    rid, email = _campaign_unsub_verify(token)
    if not email:
        return HttpResponse(
            '<h2 style="font-family:sans-serif">Invalid unsubscribe link</h2>'
            '<p>This unsubscribe link is invalid or expired.</p>',
            status=400,
        )

    import html as _html
    e = _html.escape(email)
    if request.method == 'GET':
        # Confirmation page only — no side effects. Bots that merely fetch the
        # link land here and leave the recipient untouched.
        return HttpResponse(
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="robots" content="noindex"><title>Unsubscribe</title></head>'
            '<body style="font-family:-apple-system,Segoe UI,sans-serif;'
            'max-width:520px;margin:64px auto;padding:24px;text-align:center;color:#0f172a">'
            '<h2 style="margin-bottom:8px">Unsubscribe from Permitlify emails?</h2>'
            f'<p style="color:#475569">Click below to stop emails to <strong>{e}</strong>.</p>'
            f'<form method="post" action="/u/{token}/" style="margin-top:24px">'
            '<button type="submit" style="background:#dc2626;color:#fff;border:0;'
            'border-radius:8px;padding:12px 28px;font-size:15px;font-weight:600;'
            'cursor:pointer">Unsubscribe</button></form>'
            '<p style="color:#94a3b8;font-size:13px;margin-top:32px">'
            '— The Permitlify team</p></body></html>',
            content_type='text/html; charset=utf-8',
        )

    # POST → actually unsubscribe.
    _db.campaign_recipient_mark(rid, 'unsubscribed', error='one-click unsub')
    _db.suppression_add(email, 'unsubscribed')
    # Recalc parent campaign stats so the unsub count updates immediately.
    try:
        from .db import pg as _pg
        row = _pg.query_one(
            "SELECT campaign_id FROM email_campaign_recipients WHERE id = %s", (rid,))
        if row:
            _db.campaign_recalc_stats(row['campaign_id'])
    except Exception:
        pass
    return HttpResponse(
        '<!doctype html><html><body style="font-family:-apple-system,Segoe UI,sans-serif;'
        'max-width:520px;margin:64px auto;padding:24px;text-align:center;color:#0f172a">'
        '<h2 style="color:#16a34a;margin-bottom:8px">You\'re unsubscribed.</h2>'
        f'<p style="color:#475569">We won\'t email <strong>{e}</strong> again.</p>'
        '<p style="color:#94a3b8;font-size:13px;margin-top:32px">— The Permitlify team</p>'
        '</body></html>',
        content_type='text/html; charset=utf-8',
    )


@admin_required
@require_http_methods(['POST'])
def admin_recovery_email_preview(request):
    """Render a recovery email through the shared branded shell using
    whatever the admin currently has in the Subject + Body editor on
    ``/admin-panel/marketing/recovery-emails/``. Used by the Preview
    button so the admin sees exactly what recipients will get — same
    helper the real dispatchers use.

    Sample data is substituted for ``{{name}}`` / ``{{email}}`` /
    ``{{trial_link}}`` so the preview shows a realistic email instead
    of the raw template markers.
    """
    subj = (request.POST.get('subject') or '').strip()
    body = (request.POST.get('body') or '').strip()
    sample = {
        'name':       'Khemiri',
        'email':      'preview@permitlify.com',
        'trial_link': request.build_absolute_uri('/signup/'),
    }
    for k, v in sample.items():
        subj = subj.replace('{{' + k + '}}', v)
        body = body.replace('{{' + k + '}}', v)
    html = _render_recovery_html(subj or '(no subject)', body or '<p>(empty body)</p>')
    return HttpResponse(html, content_type='text/html; charset=utf-8')


def _render_recovery_html(subject: str, body_html: str) -> str:
    """Wrap an admin-configured recovery email body in the recovery
    shell (``core/emails/recovery.html`` — the clean single-card,
    login-alert-style layout). Falls back to the raw body on render
    failure so a template typo can never block a send.
    """
    try:
        from django.template.loader import render_to_string
        import datetime as _dt, re as _re
        # Strip tags for the inbox preheader preview (first ~110 chars).
        preheader = _re.sub(r'<[^>]+>', ' ', body_html or '')
        preheader = _re.sub(r'\s+', ' ', preheader).strip()[:110]
        return render_to_string('core/emails/recovery.html', {
            'subject':   subject,
            'body_html': body_html,
            'preheader': preheader,
            'year':      _dt.datetime.utcnow().year,
        })
    except Exception:
        log.exception("recovery shell render failed; sending raw body")
        return body_html


def enqueue_recovery_for_user(user_id: int, trigger: str,
                              trial_link: str = '') -> int:
    """Public helper: queue all 3 recovery emails for ``user_id``.

    Called from ``signup_view`` (trigger='signup_no_trial') after a user
    creates an account, and from ``ls_webhook`` membership.cancelled
    (trigger='trial_cancelled'). Failures are swallowed so a queue
    hiccup never blocks signup / breaks the webhook.
    """
    try:
        from .db import recovery_enqueue
        steps = _recovery_templates_get()
        return recovery_enqueue(user_id, trigger, steps,
                                trial_link=trial_link)
    except Exception:
        log.exception("enqueue_recovery_for_user failed (user=%s, trigger=%s)",
                      user_id, trigger)
        return 0


# ── Trade-specific landing pages ──────────────────────────────────

_TRADE_LANDING = {
    'roofing': {
        'slug': 'roofing', 'short': 'Roofing',
        'tint': 'rgba(220,38,38,.06)', 'accent': '#dc2626',
        'eyebrow': 'BUILT FOR ROOFING CONTRACTORS',
        'h1_pre': 'Fresh roofing permits,', 'h1_accent': 'every morning',
        'h1_post': 'before your competition wakes up.',
        'title': 'Roofing Lead Generation · Daily Permit Alerts',
        'meta_description': 'Daily AI-scored building permits for roofing contractors. Get re-roof, repair, and new-construction leads in 248+ US cities — delivered before 7 AM.',
        'keywords': 'roofing leads, roof repair leads, re-roof permits, roofing contractor leads, AI lead scoring',
        'sub': 'Permitlify monitors every roofing permit pulled in your service area, scores it 0–100 by AI, and delivers the hottest leads to your inbox or CRM every morning.',
        'intro': 'Every plan covers re-roofs, repairs, full tear-offs, and new construction. Filter by job value, age of property, or owner type so you only call the leads that match your crew size.',
        'benefits': [
            {'icon': '🏠', 'h': 'Hail / storm follow-up',
             'p': 'Storm-damage permits surface within hours of issuance — beat the door-knockers and out-of-town contractors to the punch.'},
            {'icon': '💰', 'h': 'High-value re-roof alerts',
             'p': 'Filter by declared job value so you spend your callbacks on the $15K+ re-roofs, not the $400 tile replacements.'},
            {'icon': '🗺️', 'h': '248+ US cities',
             'p': 'Cover one zip code or a whole metro. Add and drop service areas any time without re-onboarding.'},
        ],
        'example': {'score': 91, 'project': 'Re-roof, asphalt shingles',
                    'value': '18,400', 'city': 'Plano, TX', 'issued': 'This morning',
                    'reason': 'High-value re-roof in a $450K+ owner-occupied home, hail-corridor zip code, permit issued under 4 hours ago — top decile of roofing leads in your area.'},
    },
    'hvac': {
        'slug': 'hvac', 'short': 'HVAC',
        'tint': 'rgba(2,132,199,.06)', 'accent': '#0284c7',
        'eyebrow': 'BUILT FOR HVAC CONTRACTORS',
        'h1_pre': 'Find HVAC replacement jobs', 'h1_accent': 'the day they are permitted.',
        'h1_post': '',
        'title': 'HVAC Lead Generation · Daily Permit Alerts',
        'meta_description': 'Daily AI-scored mechanical & HVAC permits for installers. Replacement systems, new construction, light commercial — delivered before 7 AM in 248+ cities.',
        'keywords': 'HVAC leads, HVAC permits, mechanical permits, AC replacement leads, furnace replacement leads',
        'sub': 'Permitlify watches every mechanical / HVAC permit in your service area and ranks them by replacement-system likelihood, so your inside sales team starts the day with a hot call list.',
        'intro': 'Coverage spans residential furnace & AC replacements, new-construction equipment, and light commercial RTU installs. Filter by tonnage, property type, and budget.',
        'benefits': [
            {'icon': '❄️', 'h': 'Pre-summer replacement queue',
             'p': 'Replacement permits spike March–May. We surface them weeks before your competitors find them on county portals.'},
            {'icon': '🏭', 'h': 'Light commercial filter',
             'p': 'Toggle commercial-only to focus on rooftop unit replacements, multi-tenant retrofits, and tenant improvements.'},
            {'icon': '🔌', 'h': 'CRM-ready',
             'p': 'Push leads straight into GoHighLevel, HubSpot, or any Zapier-connected CRM the moment they score above your threshold.'},
        ],
        'example': {'score': 88, 'project': 'Residential AC replacement, 4-ton',
                    'value': '9,200', 'city': 'Phoenix, AZ', 'issued': 'Today, 6:02 AM',
                    'reason': '4-ton condenser swap on a single-family home, 14+ year-old system based on prior permit history, peak-cooling-month timing — high close probability for residential HVAC.'},
    },
    'plumbing': {
        'slug': 'plumbing', 'short': 'Plumbing',
        'tint': 'rgba(8,145,178,.06)', 'accent': '#0891b2',
        'eyebrow': 'BUILT FOR PLUMBING CONTRACTORS',
        'h1_pre': 'Plumbing jobs,', 'h1_accent': 'scored & delivered',
        'h1_post': 'before the homeowner calls a competitor.',
        'title': 'Plumbing Lead Generation · Daily Permit Alerts',
        'meta_description': 'Daily AI-scored plumbing permits for licensed plumbers. Repipes, water heaters, sewer, new construction — delivered before 7 AM in 248+ US cities.',
        'keywords': 'plumbing leads, plumber leads, plumbing permits, water heater leads, sewer leads, repipe leads',
        'sub': 'Every plumbing permit in your service area — repipes, water heaters, sewer mains, gas lines — scored 0–100 and routed to your CRM the same morning.',
        'intro': 'Filter by scope (water heater, sewer, repipe, gas, new construction) so each crew gets exactly the lead types they close best.',
        'benefits': [
            {'icon': '🚰', 'h': 'Repipes & water heaters',
             'p': 'High-margin replacements surfaced first. Skip the $80 service calls and chase the $4K+ repipes.'},
            {'icon': '🏗️', 'h': 'New-construction queue',
             'p': 'Spot rough-in permits for spec homes & multi-family early — bid before the GC finalizes their sub list.'},
            {'icon': '📲', 'h': 'Mobile-first',
             'p': 'Drive routes with leads grouped by zip. Top-scoring permits land first in the daily email & in-app feed.'},
        ],
        'example': {'score': 84, 'project': 'Whole-home repipe (PEX)',
                    'value': '7,800', 'city': 'San Antonio, TX', 'issued': 'Today, 6:15 AM',
                    'reason': 'Repipe permit on a 1970s home — owner-occupied, fits historical plumbing-failure pattern in this neighborhood. Strong same-day callback target.'},
    },
    'electrical': {
        'slug': 'electrical', 'short': 'Electrical',
        'tint': 'rgba(245,158,11,.06)', 'accent': '#d97706',
        'eyebrow': 'BUILT FOR ELECTRICAL CONTRACTORS',
        'h1_pre': 'Service upgrades, EV chargers, solar tie-ins —', 'h1_accent': 'scored daily.',
        'h1_post': '',
        'title': 'Electrical Lead Generation · Daily Permit Alerts',
        'meta_description': 'Daily AI-scored electrical permits for licensed electricians. Panel upgrades, EV chargers, generator hookups — delivered before 7 AM in 248+ US cities.',
        'keywords': 'electrical leads, electrician leads, electrical permits, panel upgrade leads, EV charger installation leads',
        'sub': 'Permitlify scores every electrical permit pulled — service upgrades, EV charger installs, generator hookups, solar interconnects — and delivers the best ones every morning.',
        'intro': 'Filter by scope: 200A service upgrades, EV charger installs, generator hookups, sub-panels, solar net-metering. Get only the jobs your license tier and crew size actually want.',
        'benefits': [
            {'icon': '🔋', 'h': 'EV charger goldmine',
             'p': 'Level-2 EV charger permits are exploding. We tag them automatically so you can be first to bid the install.'},
            {'icon': '⚡', 'h': 'Service-upgrade alerts',
             'p': '200A → 400A service upgrades are some of the highest-margin residential electrical jobs. Surface them before the homeowner calls 3 competitors.'},
            {'icon': '🤝', 'h': 'Solar / generator tie-ins',
             'p': 'Solar interconnect permits & whole-home generator hookups are flagged separately so the right crew gets them.'},
        ],
        'example': {'score': 86, 'project': '200A panel upgrade + EV charger',
                    'value': '6,400', 'city': 'Austin, TX', 'issued': 'Today, 6:08 AM',
                    'reason': 'Panel upgrade + Tesla wall connector — high-intent residential job, owner-occupied, average ticket $5K–$7K in this market.'},
    },
    'solar': {
        'slug': 'solar', 'short': 'Solar',
        'tint': 'rgba(234,88,12,.06)', 'accent': '#ea580c',
        'eyebrow': 'BUILT FOR SOLAR INSTALLERS',
        'h1_pre': 'Solar permits, scored', 'h1_accent': 'before the panels',
        'h1_post': 'are even on the truck.',
        'title': 'Solar Lead Generation · Daily Permit Alerts',
        'meta_description': 'Daily AI-scored solar permits for installers. Rooftop, ground-mount, battery storage, and commercial — delivered before 7 AM in 248+ US cities.',
        'keywords': 'solar leads, solar installer leads, solar permits, residential solar leads, battery storage leads',
        'sub': 'Spot every residential & commercial solar permit pulled in your service area, scored by AI, ready to drop into your sales pipeline by morning.',
        'intro': 'Includes rooftop, ground-mount, battery-only, and solar + storage permits. Filter by system size, battery presence, and zip code so you target the deals you actually want to install.',
        'benefits': [
            {'icon': '🔆', 'h': 'Solar + battery first',
             'p': 'Battery-storage permits are the highest-margin solar work right now. They get flagged & scored separately so they always rise to the top.'},
            {'icon': '🏬', 'h': 'Commercial filter',
             'p': 'Toggle a commercial-only view to focus on warehouse rooftops, ag, and small-utility-scale projects.'},
            {'icon': '🗓️', 'h': 'Before utility activation',
             'p': "We catch permits days before the utility's interconnect queue updates — earliest possible lead window."},
        ],
        'example': {'score': 92, 'project': 'Residential solar + 13.5 kWh battery',
                    'value': '38,000', 'city': 'San Diego, CA', 'issued': 'Today, 6:11 AM',
                    'reason': 'Battery-equipped 8.4 kW residential solar — highest-margin permit type in your area, owner-occupied, NEM 3.0 compliant. Top-decile solar lead.'},
    },
}


def trade_landing_view(request, trade: str):
    """Public landing page tailored to one trade.

    URL: ``/leads/<roofing|hvac|plumbing|electrical|solar>/``. Renders
    ``trade_landing.html`` with the trade's config dict + the live
    24-hour permit counter + any published testimonials.
    """
    from .db import permits_count_last_24h, list_testimonials
    cfg = _TRADE_LANDING.get((trade or '').lower())
    if not cfg:
        from django.http import Http404
        raise Http404("Unknown trade")
    # Stamp the page-view conversion event so analytics fires once.
    request.session['fire_conversion'] = 'view_pricing'  # treated as engaged-traffic
    return render(request, 'core/trade_landing.html', {
        'trade':        cfg,
        'permits_24h':  f"{permits_count_last_24h():,}",
        'testimonials': list_testimonials(published_only=True, limit=3),
    })
