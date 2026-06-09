from . import whop as wp


# Default delivery promise rendered on every public surface that asks
# about cancellation policy. Single source of truth — change it here
# and pricing cards, trade landing pages, onboarding step 3, and the
# trade-landing footer all update together.
TRIAL_CANCEL_PROMISE = "Cancel anytime in 1 click — no charge if you cancel before day 7."


def marketing(request):
    """Expose marketing-side state to every template.

    * Pixel / analytics IDs (so ``_analytics_head.html`` can render
      GA4, Google Ads, Meta Pixel, and Microsoft UET snippets).
    * Server-stamped conversion events (set by views with
      ``request.session['fire_conversion'] = 'start_trial'``). The
      session key is popped after exactly one render so events fire
      once and only once.
    * The single-source-of-truth cancel-anytime promise.

    Wrapped in try/except so a DB hiccup never breaks public pages.
    """
    payload = {
        'ga4_id':                '',
        'google_ads_id':         '',
        'google_ads_conv_label': '',
        'meta_pixel_id':         '',
        'meta_capi_token':       '',  # only used server-side, never rendered
        'uet_tag_id':            '',
        'custom_head_html':      '',
        'fire_conversion':       '',
        'fire_conversion_value': 0,
        'cancel_promise':        TRIAL_CANCEL_PROMISE,
    }
    try:
        from .db import get_system_setting
        for k in ('ga4_id', 'google_ads_id', 'google_ads_conv_label',
                  'meta_pixel_id', 'uet_tag_id', 'custom_head_html'):
            v = get_system_setting('mk_' + k)
            if v:
                payload[k] = v
    except Exception:
        # DB unreachable — public page must still render. The pixel
        # snippets are guarded with `{% if marketing.<id> %}` so an
        # empty payload simply emits nothing.
        pass
    s = getattr(request, 'session', None)
    if s is not None:
        ev = s.pop('fire_conversion', '')
        if ev:
            payload['fire_conversion'] = ev
            try:
                payload['fire_conversion_value'] = float(
                    s.pop('fire_conversion_value', 0) or 0)
            except (TypeError, ValueError):
                payload['fire_conversion_value'] = 0
    return {'marketing': payload}


_FALLBACK_PRICING = {
    'mode': 'prod', 'is_dev': False,
    # State-based pricing (May 2026): starter = 1 state @ $79/mo,
    # pro = 2 states @ $149/mo, agency = 5 states @ $349/mo.
    # Annual rates are ~20% off the monthly equivalent.
    'starter_monthly':  79, 'starter_annual':   63,
    'pro_monthly':     149, 'pro_annual':      119,
    'agency_monthly':  349, 'agency_annual':   279,
    'starter_monthly_total':  948, 'starter_annual_total':  756, 'starter_annual_save':  192,
    'pro_monthly_total':     1788, 'pro_annual_total':     1428, 'pro_annual_save':     360,
    'agency_monthly_total':  4188, 'agency_annual_total':  3348, 'agency_annual_save':    840,
}


def pricing(request):
    """Expose mode-aware plan pricing dict to every template.

    The pricing A/B test page has been removed; everyone now sees the
    same control pricing. ``pricing.variant`` is kept (always 'a') and
    ``pricing.ab_enabled`` is always False so any straggler templates
    referencing them keep rendering.
    """
    try:
        base = dict(wp.get_pricing_dict())
    except Exception:
        base = dict(_FALLBACK_PRICING)

    base['variant'] = 'a'
    base['ab_enabled'] = False
    # Annual billing is hidden across every public surface (pricing
    # page, onboarding, paywall, settings billing tab) until Whop is
    # configured with a real registered business entity that supports
    # annual plans. See core.whop.annual_billing_enabled() for the
    # underlying flag — flip the `billing_annual_enabled` system_setting
    # to '1' to re-enable. Existing annual subscribers continue to
    # render their current annual billing-cycle label in Settings.
    try:
        base['annual_billing_enabled'] = wp.annual_billing_enabled()
    except Exception:
        base['annual_billing_enabled'] = False

    return {'pricing': base}


def site_origin(request):
    """
    Expose the canonical site origin (e.g. ``https://permitlify.com``) to
    every template so SEO tags (canonical, OG, Twitter, JSON-LD) can build
    absolute URLs that always point at the production domain — never at
    whatever Host header reached the server.

    Falls back to the live request scheme + host when ``SITE_ORIGIN`` is
    not set (local dev, staging, etc.) so canonicals still work end-to-end.
    """
    from django.conf import settings as _s
    origin = getattr(_s, 'SITE_ORIGIN', '') or ''
    if not origin:
        try:
            origin = f"{request.scheme}://{request.get_host()}"
        except Exception:
            origin = ''
    return {'site_origin': origin}


def user_session(request):
    """
    Expose session-derived user info to every template.

    Public pages (homepage, pricing, blog, etc.) use this to swap the
    "Log In / Get Started Free" buttons for a single "Dashboard" link
    when the visitor is already authenticated. Authenticated app pages
    can also rely on these without re-reading the session in every view.

    Always returns the keys (with falsy defaults) so templates can use
    `{% if user_logged_in %}` without worrying about missing variables.
    """
    s = getattr(request, 'session', None)
    # Resolve the absolute-timeout cap once so the popup script in
    # base.html can render `data-max-age` for free on every page.
    from django.conf import settings as _s
    _max_age = int(getattr(_s, 'SESSION_ABSOLUTE_TIMEOUT_SECONDS', 3600))

    if s is None:
        return {
            'user_logged_in': False, 'user_id': None,
            'user_email': '', 'user_name': '',
            'user_initials': '', 'user_plan': '',
            'session_login_at': 0, 'session_max_age': _max_age,
        }
    uid = s.get('user_id')
    # `login_at` is an int unix-ts stamped at every successful login
    # (see core/views.py _stamp_session_login). May be missing on
    # legacy sessions minted before the absolute-timeout middleware
    # existed — the front-end script treats 0 as "not initialised
    # yet" and skips the countdown rather than firing immediately.
    login_at = s.get('login_at') if isinstance(s.get('login_at'), int) else 0
    return {
        'user_logged_in': bool(uid),
        'user_id': uid,
        'user_email': s.get('user_email', '') or '',
        'user_name': s.get('user_name', '') or '',
        'user_initials': s.get('user_initials', '') or '',
        'user_plan': s.get('user_plan', '') or '',
        'session_login_at': login_at,
        'session_max_age': _max_age,
    }


# ── Cron schedule (single source of truth for every "scheduled run"
#    label rendered anywhere in the site) ─────────────────────────────
#
# The real cron job is gated by ``scrapers_cron_at_utc`` and
# ``scrapers_cron_window_minutes`` in ``system_settings`` (see
# ``scripts/run_scrapers.py:_gate_by_schedule``). Multiple templates
# (homepage hero stat, dashboard "Data Refreshed" card, the empty-state
# banner that promises when the next batch lands, the comparison table,
# etc.) historically hard-coded ``6 AM`` / ``6:02 AM`` / ``6 AM – 9 AM``
# strings that drifted away from whatever the admin actually configured
# on the Cron page. Exposing one ``cron_schedule`` dict to every
# template lets those surfaces stay in sync automatically.
_FALLBACK_AT_UTC          = '08:00'
_FALLBACK_WINDOW_MINUTES  = 30


def _fmt_12h(h: int, m: int) -> str:
    """Render an integer 24h hour/minute as '6 AM' or '6:02 AM'."""
    h = max(0, min(23, int(h)))
    m = max(0, min(59, int(m)))
    suffix = 'AM' if h < 12 else 'PM'
    h12 = h % 12 or 12
    return f'{h12} {suffix}' if m == 0 else f'{h12}:{m:02d} {suffix}'


def cron_schedule(request):
    """Expose the configured scraper cron time to every template.

    Returns a dict with both 24h and 12h-clock labels plus a
    pre-rendered "window" range so the dashboard empty-state banner
    can say "between 8 AM – 8:30 AM" without re-doing the math.
    All times are UTC — the admin Cron page enters them as UTC and
    the cron entrypoint compares them as UTC, so we render them as
    UTC end-to-end to avoid timezone surprises.
    """
    at_utc = _FALLBACK_AT_UTC
    window = _FALLBACK_WINDOW_MINUTES
    try:
        from .db import get_system_setting
        raw_at = (get_system_setting('scrapers_cron_at_utc') or '').strip()
        if raw_at and ':' in raw_at:
            at_utc = raw_at
        try:
            window = int(get_system_setting('scrapers_cron_window_minutes')
                         or _FALLBACK_WINDOW_MINUTES)
        except (TypeError, ValueError):
            window = _FALLBACK_WINDOW_MINUTES
    except Exception:
        # DB unreachable / settings table missing — fall through with
        # the documented defaults so public pages still render.
        pass
    try:
        h, m = at_utc.split(':', 1)
        hour, minute = int(h), int(m)
    except (TypeError, ValueError):
        hour, minute = 8, 0
    window = max(1, min(int(window), 720))

    # Window end = at_utc + window_minutes, wrapped at 24h.
    end_total = (hour * 60 + minute + window) % (24 * 60)
    end_h, end_m = divmod(end_total, 60)

    hour_label   = _fmt_12h(hour, 0)              # '8 AM'
    time_label   = _fmt_12h(hour, minute)         # '8:00 AM'
    end_label    = _fmt_12h(end_h, end_m)         # '8:30 AM'
    window_label = (f'{hour_label} – {end_label}'
                    if minute == 0 and end_m == 0 and window >= 60
                    else f'{time_label} – {end_label}')

    return {
        'cron_schedule': {
            'at_utc':       f'{hour:02d}:{minute:02d}',  # '08:00'
            'time_24':      f'{hour:02d}:{minute:02d}',
            'hour_label':   hour_label,    # '8 AM'           (hero stats)
            'time_label':   time_label,    # '8:00 AM'        (data-refreshed card)
            'end_label':    end_label,     # '8:30 AM'        (window upper bound)
            'window_label': window_label,  # '8 AM – 8:30 AM' (empty-state banner)
            'window_minutes': window,
            # Intentionally blank on customer-facing surfaces. The cron
            # itself fires at UTC, but exposing that to contractors on
            # the landing page reads as "not built for me" (UTC = 1-2
            # AM US time). The per-user digest email is delivered at
            # whatever local time the user picks in notification
            # preferences, so "Daily 6 AM" alone is the honest copy.
            'tz_label':     '',
        },
    }
