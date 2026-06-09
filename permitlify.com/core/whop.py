import os
import json
import hashlib
import hmac
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime


BASE = 'https://api.whop.com/api/v5'

# ── Default checkout URLs ──────────────────────────────────────────────────
DEFAULT_CHECKOUT_URLS = {
    'starter_monthly': 'https://whop.com/permitlify/permit-starter-monthly/',
    'starter_annual':  'https://whop.com/permitlify/permit-starter-yearly/',
    'pro_monthly':     'https://whop.com/permitlify/permit-pro-monthly/',
    'pro_annual':      'https://whop.com/permitlify/permit-pro-yearly/',
    'agency_monthly':  'https://whop.com/permitlify/permit-agency-monthly/',
    'agency_annual':   'https://whop.com/permitlify/permit-agency-year/',
}

PLAN_PRICES        = {'starter': 7900,  'pro': 14900,  'agency': 34900}
PLAN_PRICES_ANNUAL = {'starter': 75600, 'pro': 142800, 'agency': 334800}
PLAN_TRIAL_DAYS    = {'starter': 3,     'pro': 5,     'agency': 7}


def annual_billing_enabled() -> bool:
    """Feature flag — annual plans are temporarily disabled because Whop
    requires a real, registered business entity per country before
    annual pricing can be configured at the gateway. The data layer
    (PLAN_PRICES_ANNUAL, DEFAULT_CHECKOUT_URLS, _is_annual_membership,
    pricing-dict ``*_annual*`` keys, webhook sync) is kept intact so
    existing annual subscribers continue to be billed and renewed
    correctly — only the user-facing toggles + new-checkout entry
    points are gated.

    Flip on by setting ``billing_annual_enabled = 1`` in system_settings
    once Whop annual plans are live for your registered entity.
    """
    try:
        from .db import get_system_setting
        return str(get_system_setting('billing_annual_enabled') or '').lower() in (
            '1', 'true', 'yes', 'on')
    except Exception:
        return False

# Map known product/plan name fragments → plan key
_PLAN_NAME_MAP = {
    'agency':  'agency',
    'pro':     'pro',
    'starter': 'starter',
}

# Direct plan-ID → plan key map (prod + known dev plan IDs)
# Prevents extra API call during sync/success flows
_PLAN_ID_MAP = {
    # Production plan IDs (from Whop products API)
    'plan_sOwfewYyE2Jy0': 'starter',   # Starter Monthly
    'plan_Jk8ArUo7uXsGo': 'starter',   # Starter Yearly
    'plan_zDUbG0HDTquRW': 'pro',        # Pro Monthly
    'plan_VPtCmJssZqSNK': 'pro',        # Pro Yearly
    'plan_LPNMOAqVUKMIz': 'agency',     # Agency Monthly
    'plan_s8dxitixlonFN': 'agency',     # Agency Yearly
    # Dev / test plan IDs
    'plan_RmlVwr9t4gRC0': 'starter',    # Dev Starter Monthly
    'plan_NmuWZl5rMAMYB': 'starter',    # Dev Starter Yearly
    'plan_aBfVbcYzVXHmt': 'pro',        # Dev Pro Monthly
    'plan_ERrp3boWrHcH6': 'pro',        # Dev Pro Yearly
}

# Direct product-ID → plan key map (prod)
_PRODUCT_ID_MAP = {
    'prod_xs5CNxtzXT0lQ': 'starter',    # Permit Starter Monthly
    'prod_7yIyBBaNnoTAf': 'starter',    # Permit Starter Yearly
    'prod_tAd8FwqHh88M8': 'pro',        # Permit Pro Monthly
    'prod_l0750Yi5r6ZuU': 'pro',        # Permit Pro Yearly
    'prod_I2O7XJkRMQp6g': 'agency',     # Permit Agency Monthly
    'prod_2yHpos2hGtwtP': 'agency',     # Permit Agency Yearly
}

# Subset of the maps above whose membership is billed annually. Used by the
# revenue aggregator to amortize annual subscriptions into monthly MRR
# (e.g. a $948/yr Pro sub contributes $79/mo MRR, not $99/mo). Anything not
# in here is assumed monthly — that's the safer default because mistakenly
# treating a monthly sub as annual would *under*-count MRR by 12×.
_ANNUAL_PRODUCT_IDS = {
    'prod_7yIyBBaNnoTAf',  # Permit Starter Yearly
    'prod_l0750Yi5r6ZuU',  # Permit Pro Yearly
    'prod_2yHpos2hGtwtP',  # Permit Agency Yearly
}
_ANNUAL_PLAN_IDS = {
    'plan_Jk8ArUo7uXsGo',  # Starter Yearly
    'plan_VPtCmJssZqSNK',  # Pro Yearly
    'plan_s8dxitixlonFN',  # Agency Yearly
    'plan_NmuWZl5rMAMYB',  # Dev Starter Yearly
    'plan_ERrp3boWrHcH6',  # Dev Pro Yearly
}


def _is_annual_membership(membership: dict) -> bool:
    """True when the membership is on a yearly billing plan.

    Falls back to a renewal-period heuristic when neither the plan nor
    product id is in the hardcoded annual set — e.g. legacy / dev plan
    ids we haven't seen yet. A renewal window > 60 days is treated as
    annual; anything shorter is monthly. 60d threshold safely separates
    monthly (~30d) from yearly (~365d) without false-positives on the
    occasional 31-day month.
    """
    pid = membership.get('plan_id')
    if isinstance(pid, str) and pid in _ANNUAL_PLAN_IDS:
        return True
    prod = membership.get('product_id') or membership.get('product')
    if isinstance(prod, dict):
        prod = prod.get('id')
    if isinstance(prod, str) and prod in _ANNUAL_PRODUCT_IDS:
        return True
    start = membership.get('renewal_period_start')
    end   = membership.get('renewal_period_end')
    try:
        if start and end and (int(end) - int(start)) > 60 * 86400:
            return True
    except Exception:
        pass
    return False


# ── Settings cache ───────────────────────────────────────────────────────
# ``_db_setting`` used to round-trip Supabase on every call, which meant a
# single homepage render did 30 sequential ~85ms queries (one per pricing
# field) and burned ~1.3s of pure DB wait per request. We now cache values
# in-process for ``_SETTINGS_TTL`` seconds so repeat lookups inside the same
# request — and across requests within the TTL — are O(1) dict reads.
#
# Invalidation: ``core.db.set_system_setting`` calls ``clear_settings_cache``
# whenever a key is written, so admin edits to pricing / Whop mode / API
# keys take effect on the next request without waiting for the TTL.
import time as _time
import threading as _threading
_SETTINGS_TTL    = 60.0           # seconds
_settings_cache: dict[str, tuple[float, str]] = {}
_settings_lock   = _threading.Lock()


def clear_settings_cache(key: str | None = None) -> None:
    """Drop a single key (or the whole cache when ``key`` is None) so the
    next read reloads from Postgres. Safe to call from any thread."""
    with _settings_lock:
        if key is None:
            _settings_cache.clear()
        else:
            _settings_cache.pop(key, None)
    # Any settings change can affect the memoised pricing dict (mode,
    # plan_price_*, etc.), so drop it too — cheap to rebuild and avoids
    # stale prices lingering for up to 60 s after an admin update.
    try:
        _clear_pricing_dict_cache()
    except NameError:
        pass


def _db_setting(key: str, default: str = '') -> str:
    """Read a system_settings value with a 60s in-process cache.

    Returns the cached value when fresh; on miss/stale, queries Postgres
    once and stores the result. Empty/missing rows are also cached (as
    ``''``) so a missing key doesn't keep hitting the DB. The ``default``
    is returned only when the cache + DB both have no value, so cached
    misses still respect caller-supplied fallbacks."""
    now = _time.monotonic()
    hit = _settings_cache.get(key)
    if hit and (now - hit[0]) < _SETTINGS_TTL:
        return hit[1] or default
    try:
        from .db import get_system_setting
        val = get_system_setting(key)
        s = str(val) if val else ''
    except Exception:
        # On DB failure, fall back to default *without* polluting the
        # cache — we want the next request to retry, not serve empty
        # pricing for a full minute because of one transient blip.
        return default
    with _settings_lock:
        _settings_cache[key] = (now, s)
    return s or default


def _api_key() -> str:
    return _db_setting('whop_api_key', os.environ.get('WHOP_API_KEY', ''))


def _company_id() -> str:
    return _db_setting('whop_company_id', os.environ.get('WHOP_COMPANY_ID', 'biz_ZuaWu6MxVXoVTD'))


def _webhook_secret() -> str:
    return _db_setting('whop_webhook_secret', os.environ.get('WHOP_WEBHOOK_SECRET', ''))


def get_checkout_url(plan: str, period: str) -> str:
    """Return the checkout URL for a plan/period, checking TinyDB first."""
    key = f'whop_checkout_{plan}_{period}'
    val = _db_setting(key, '')
    return val or DEFAULT_CHECKOUT_URLS.get(f'{plan}_{period}', '')


# ── HTTP helpers ───────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        'Authorization': f'Bearer {_api_key()}',
        'Content-Type':  'application/json',
    }


def _get(path: str) -> dict:
    req = urllib.request.Request(f'{BASE}{path}', headers=_headers())
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Whop GET {path} → {e.code}: {body}')


def _post(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(f'{BASE}{path}', data=data, headers=_headers(), method='POST')
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Whop POST {path} → {e.code}: {body}')


# ── Checkout ───────────────────────────────────────────────────────────────

def _whop_mode() -> str:
    """Return 'dev' or 'prod' based on the global admin setting.

    This is the *fallback* mode used when no per-user override applies
    (for unauthenticated checkout, scripts, etc.). For per-user billing
    use ``mode_for_user()`` and pass the result as ``mode=`` to the
    plan-id / pricing helpers.
    """
    return _db_setting('whop_mode', 'prod') or 'prod'


def mode_for_user(user) -> str:
    """
    Return the Whop billing mode ('dev' or 'prod') to use for a given user.

    Each user document carries its own ``whop_mode`` field (defaulted to
    'prod' on signup, see core/db.create_user). Admins can flip individual
    users — or many at once — to 'dev' from /admin-panel/users/, which
    routes that user's checkout to the $1 Whop test plans without
    affecting anybody else.

    Defaults to **'prod'** when the user has no field set (legacy accounts
    created before this field existed) so we never accidentally bill a
    real user against $1 dev plans because the global mode happens to be
    flipped. Admins must explicitly opt a user into 'dev' from the
    dashboard. Same default is used when no user is provided at all.
    """
    try:
        m = (user or {}).get('whop_mode')
    except AttributeError:
        m = None
    if m in ('dev', 'prod'):
        return m
    return 'prod'


def get_plan_id(plan: str, period: str, mode: str = None) -> str:
    """Return the Whop plan_id for the given mode (dev or prod).

    When ``mode`` is omitted, falls back to the global setting so callers
    that don't have a user context (webhooks, batch jobs) keep working
    exactly as before.
    """
    key = f'{plan.lower()}_{period.lower()}'
    effective_mode = mode if mode in ('dev', 'prod') else _whop_mode()
    if effective_mode == 'dev':
        return _db_setting(f'whop_plan_id_dev_{key}', '')
    return _db_setting(f'whop_plan_id_{key}', '')


# ── Plan pricing (mode-aware) ─────────────────────────────────────────────
# Default monthly-rate prices (the displayed "$X / mo" number).
# In dev mode, every plan defaults to $1 to mirror $1 Whop test plans.
# Admins can override any value via system_settings:
#   plan_price_<mode>_<plan>_<period>   (e.g. plan_price_dev_pro_monthly = 1)
_DEFAULT_DISPLAY_PRICES = {
    'prod': {
        # State-based pricing (May 2026 onward):
        #   starter = 1 state,  pro = 2 states,  agency = 5 states.
        # Annual rates are ~20% off the monthly equivalent.
        ('starter', 'monthly'):  79,
        ('starter', 'annual'):   63,
        ('pro',     'monthly'): 149,
        ('pro',     'annual'):  119,
        ('agency',  'monthly'): 349,
        ('agency',  'annual'):  279,
    },
    'dev': {
        ('starter', 'monthly'): 1,
        ('starter', 'annual'):  1,
        ('pro',     'monthly'): 1,
        ('pro',     'annual'):  1,
        ('agency',  'monthly'): 1,
        ('agency',  'annual'):  1,
    },
}


# Per-plan free-trial length in days. Admin can override per mode via
# system_settings key  plan_trial_<mode>_<plan>  (whole integer days).
_DEFAULT_TRIAL_DAYS = {
    'prod': {'starter': 3, 'pro': 5, 'agency': 7},
    'dev':  {'starter': 3, 'pro': 5, 'agency': 7},
}


def get_plan_trial_days(plan: str, mode: str = None) -> int:
    """Return the free-trial length (in days) for the given plan.

    Reads the admin-configurable ``plan_trial_<mode>_<plan>`` system setting;
    falls back to :data:`_DEFAULT_TRIAL_DAYS` (3/5/7 for starter/pro/agency).
    Clamped to ``>= 0`` — an empty or invalid value falls back to the default
    rather than producing a negative trial.
    """
    plan = (plan or '').lower()
    if mode is None:
        mode = _whop_mode()
    if mode not in ('prod', 'dev'):
        mode = 'prod'
    raw = _db_setting(f'plan_trial_{mode}_{plan}', '')
    if raw:
        try:
            n = int(float(raw))
            if n >= 0:
                return n
        except (ValueError, TypeError):
            pass
    return _DEFAULT_TRIAL_DAYS.get(mode, _DEFAULT_TRIAL_DAYS['prod']).get(plan, 7)


def get_plan_price(plan: str, period: str, mode: str = None) -> int:
    """Return the displayed monthly-equivalent price for a plan/period."""
    plan   = (plan or '').lower()
    period = (period or '').lower()
    if mode is None:
        mode = _whop_mode()
    if mode not in ('prod', 'dev'):
        mode = 'prod'
    raw = _db_setting(f'plan_price_{mode}_{plan}_{period}', '')
    if raw:
        try:
            return int(float(raw))
        except (ValueError, TypeError):
            pass
    return _DEFAULT_DISPLAY_PRICES.get(mode, _DEFAULT_DISPLAY_PRICES['prod']).get((plan, period), 0)


_pricing_dict_cache: dict[str, tuple[float, dict]] = {}
_pricing_dict_lock  = _threading.Lock()


def _clear_pricing_dict_cache() -> None:
    """Drop the memoised pricing dict for both modes. Called from
    ``clear_settings_cache`` so an admin price/mode change is reflected
    immediately instead of waiting up to 60 s."""
    with _pricing_dict_lock:
        _pricing_dict_cache.clear()


def get_pricing_dict(mode: str = None) -> dict:
    """
    Build a pricing dict for templates. Returns flat keys for the 6 plan/period
    combos plus the annual-billed-once amounts (price * 12) and the current mode.

    When ``mode`` is provided ('dev' or 'prod') the dict is built for that
    mode — used by views that have a logged-in user with their own
    ``whop_mode`` override. Otherwise falls back to the global setting.

    Memoised for ``_SETTINGS_TTL`` seconds per mode. Without this cache,
    the function does 12 sequential ``system_settings`` lookups (six
    ``plan_price_<mode>_<plan>_<period>`` reads + the implicit
    ``whop_mode`` read inside ``_whop_mode``), each ~170 ms RTT to
    Supabase — a cold ``/dashboard/`` render was paying ~2 s here on
    every fresh worker / every 60-second window before the per-key
    cache warmed up. Caching the *result* dict collapses that to a
    single dict lookup on the hot path; admin price changes still
    invalidate immediately via ``clear_settings_cache``.

    Keys:
      mode                          → 'prod' or 'dev'
      starter_monthly, starter_annual
      pro_monthly,     pro_annual
      agency_monthly,  agency_annual
      starter_annual_total, pro_annual_total, agency_annual_total   (price * 12)
      starter_monthly_total, …                                       (price * 12 of monthly, for "save X" math)
    """
    if mode not in ('dev', 'prod'):
        mode = _whop_mode()
    now = _time.monotonic()
    hit = _pricing_dict_cache.get(mode)
    if hit and (now - hit[0]) < _SETTINGS_TTL:
        return hit[1]
    out  = {'mode': mode, 'is_dev': mode == 'dev',
            # Pin the annual-billing flag directly onto the pricing
            # dict so views that build their context with
            # ``{'pricing': wp.get_pricing_dict()}`` — bypassing the
            # ``context_processors.pricing`` processor that would
            # otherwise inject it — still see the correct gating value.
            # Without this, /pricing/, /onboarding/, /paywall/, and
            # /settings/ would always show the annual UI even with the
            # flag flipped on, because their view-supplied ``pricing``
            # context overrides the processor.
            'annual_billing_enabled': annual_billing_enabled()}
    for plan in ('starter', 'pro', 'agency'):
        m = get_plan_price(plan, 'monthly', mode)
        a = get_plan_price(plan, 'annual',  mode)
        out[f'{plan}_monthly']        = m
        out[f'{plan}_annual']         = a
        out[f'{plan}_monthly_total']  = m * 12
        out[f'{plan}_annual_total']   = a * 12
        out[f'{plan}_annual_save']    = max((m - a) * 12, 0)
        out[f'{plan}_trial_days']     = get_plan_trial_days(plan, mode)
    with _pricing_dict_lock:
        _pricing_dict_cache[mode] = (now, out)
    return out


def _create_checkout_session(plan_id: str, email: str, user_id: int,
                              success_url: str) -> str:
    """
    Call Whop's Checkout Sessions API.
    Returns a direct card-payment URL that skips the company/plan pages.
    """
    payload = json.dumps({
        'plan_id':      plan_id,
        'redirect_url': success_url,
        'email':        email,
        'metadata':     {'user_id': str(user_id)},
    }).encode()
    req = urllib.request.Request(
        f'{BASE}/checkout_sessions',
        data=payload,
        headers=_headers(),
        method='POST',
    )
    try:
        r = urllib.request.urlopen(req, timeout=15)
        data = json.loads(r.read())
        # Whop returns { "url": "https://checkout.whop.com/..." }
        return data.get('url') or data.get('checkout_url', '')
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Whop checkout session error {e.code}: {e.read().decode()}')


def create_checkout_url(plan: str, email: str, user_id: int,
                        success_url: str, period: str = 'monthly') -> str:
    """
    Return a checkout URL for the given plan/period.
    If a Whop plan_id is configured, uses whop.com/checkout/{plan_id} which
    goes directly to the card payment form, skipping the company/landing page.
    Otherwise falls back to the plan page URL + d2c=true.
    """
    plan_id = get_plan_id(plan, period)
    if plan_id:
        params = urllib.parse.urlencode({
            'redirect_url':  success_url,
            'prefill_email': email,
            'metadata[user_id]': str(user_id),
        })
        return f'https://whop.com/checkout/{plan_id}/?{params}'

    base = get_checkout_url(plan.lower(), period.lower())
    if not base:
        raise ValueError(f'No checkout URL configured for {plan}/{period}')
    params = urllib.parse.urlencode({
        'd2c':               'true',
        'redirect_url':      success_url,
        'prefill_email':     email,
        'metadata[user_id]': str(user_id),
    })
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}{params}'


# ── Membership API ─────────────────────────────────────────────────────────

def get_membership(membership_id: str, timeout: int = 15) -> dict | None:
    """Fetch a single membership. Tries v5 first, falls back to v2.

    The default ``timeout`` of 15s matches every other Whop call in this
    module, but callers on the user's hot path (login sync, page renders)
    should pass a tight value like ``timeout=3`` so a slow Whop response
    can never wedge the request.
    """
    # Try v5 first
    try:
        req = urllib.request.Request(f'{BASE}/memberships/{membership_id}',
                                     headers=_headers())
        r = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(r.read())
        if result:
            return result
    except Exception:
        pass
    # Fall back to v2
    try:
        req = urllib.request.Request(
            f'https://api.whop.com/api/v2/memberships/{membership_id}',
            headers=_headers(),
        )
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read())
    except Exception:
        return None


def get_memberships_by_email(email: str, timeout: int = 15) -> list:
    """
    Fetch Whop memberships for a given email.
    Uses v2 API (which includes the email field) and falls back to listing
    all company memberships if no exact email match is found.

    The default ``timeout`` of 15s matches every other Whop call here,
    but callers on the user's hot path (login sync, settings render)
    should pass a tight value like ``timeout=3`` so a slow Whop response
    can never wedge the request. Applies to BOTH the v2 call and the
    v5 fallback so worst-case wait is ``2 * timeout``.
    """
    e_lc = (email or '').strip().lower()

    def _mem_email(m: dict) -> str:
        u = m.get('user') or {}
        u_email = u.get('email') if isinstance(u, dict) else ''
        return (m.get('email') or m.get('user_email') or u_email or '').strip().lower()

    try:
        # v2 supports email filter and includes membership email field.
        # ⚠️  Whop's v2 ?email= filter is FUZZY: a query for
        # "khemiri.mohamed.ensi@gmail.com" returns memberships for
        # "khamiri.hafed.ensi@gmail.com" too. Without the exact-email
        # post-filter below, a user can be silently bound to ANOTHER
        # paying user's membership if their emails are similar enough —
        # cross-account data leakage. The v5 fallback already does this
        # filter; v2 must do it too.
        params = urllib.parse.urlencode({'email': email, 'per': 25})
        req = urllib.request.Request(
            f'https://api.whop.com/api/v2/memberships?{params}',
            headers=_headers(),
        )
        r = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(r.read())
        items = data.get('data', [])
        if e_lc:
            # Only drop items where we have a populated email field that
            # demonstrably doesn't match. If the field is missing/blank
            # we keep the row — Whop sometimes omits it on edge formats
            # and we don't want to drop the user's only real membership.
            items = [m for m in items if (not _mem_email(m)) or _mem_email(m) == e_lc]
        if items:
            # Sort: valid/trialing first, then newest created_at first
            items.sort(key=lambda m: (0 if m.get('valid') else 1, -int(m.get('created_at') or 0)))
            return items
    except Exception:
        pass

    # Fallback: list all company memberships (v5) and return all active ones.
    # Filter to the requested email so we never accidentally return another
    # user's membership when the v2 endpoint is unavailable.
    try:
        req = urllib.request.Request(
            'https://api.whop.com/api/v5/company/memberships',
            headers=_headers(),
        )
        r = urllib.request.urlopen(req, timeout=timeout)
        data  = json.loads(r.read())
        items = data.get('data', [])
        e_lc  = (email or '').strip().lower()
        if e_lc:
            def _mem_email(m):
                u = m.get('user') or {}
                return (m.get('email') or u.get('email') or '').strip().lower()
            items = [m for m in items if _mem_email(m) == e_lc]
        items.sort(key=lambda m: (0 if m.get('valid') else 1, -int(m.get('created_at') or 0)))
        return items
    except Exception:
        return []


def cancel_membership(membership_id: str, immediate: bool = False) -> bool:
    """Cancel a Whop membership via the v2 API.

    Args:
        membership_id: The Whop membership id to cancel.
        immediate: When True, terminate the membership right now (no further
            access, no future renewal — used by the change-plan flow so the
            user is never billed for two plans at once). When False (default),
            schedule cancel-at-period-end so the user keeps access until the
            paid period would otherwise have renewed.

    Background — what the v5 attempt was about:
        PR #84 tried v5 ``/company/memberships/{id}/{cancel|terminate}``
        first, falling back to v2 ``/cancel`` only for the non-immediate
        path. Probing those v5 paths against a real account returns
        **404 with empty body for every variation** (``/memberships``,
        ``/company/memberships``, ``/me/memberships`` × ``cancel`` /
        ``terminate``) — i.e. the v5 routes simply do not exist for our
        company-scoped key. Meanwhile v2 returns ``404 "No such
        Membership found with the provided ID: cancel"`` for a fake
        id at BOTH ``/cancel`` AND ``/terminate``, proving the v2
        routes exist (404 is the membership lookup, not the path).

        Net effect of PR #84: cancel-at-period-end worked (v2 fallback
        kicked in), but the change-plan flow's ``immediate=True`` always
        raised because nothing fell back. That's exactly the
        "Request failed" / 502 the user just hit.

    This version goes straight to v2 for both ops. Single network call,
    no doomed v5 attempts, fast enough to never approach the proxy
    timeout even with multiple memberships in the same request (the
    sweep helper above can cancel several in a row).

    Raises ``RuntimeError`` on failure so callers can surface the error
    to the user instead of silently marking them cancelled locally
    while the subscription keeps renewing in Whop.
    """
    op = 'terminate' if immediate else 'cancel'
    url = f'https://api.whop.com/api/v2/memberships/{membership_id}/{op}'
    try:
        req = urllib.request.Request(url, data=b'{}', headers=_headers(), method='POST')
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Whop cancel ({op}) failed for {membership_id}: HTTP {e.code} {body}')
    except Exception as e:
        raise RuntimeError(f'Whop cancel ({op}) failed for {membership_id}: {e}')


def cancel_all_active_for_email(email: str, immediate: bool = False,
                                extra_membership_ids: list[str] | None = None,
                                timeout: int = 15) -> dict:
    """Cancel every still-active Whop membership tied to ``email``.

    This is what the user-facing "cancel" and "change plan" flows should
    call. Cancelling only the single ``whop_membership_id`` we have on file
    leaves orphans in Whop whenever a previous flow created multiple
    memberships (the recent ``cancel_membership`` 404-silent bug, manual
    re-checkouts after a failed payment, dev/prod-mode swaps, etc.) — and
    those orphans keep renewing and billing the user every month.

    Args:
        email: User's email — passed to ``get_memberships_by_email`` to
            discover every active membership Whop has on file.
        immediate: ``True`` to terminate now, ``False`` for cancel-at-period-end.
        extra_membership_ids: Optional ids that MUST also be cancelled even
            if Whop's email lookup doesn't return them (e.g. a stale id we
            recorded against this user account historically).
        timeout: Per-API-call timeout in seconds.

    Returns:
        ``{
            'cancelled':  [ids successfully cancelled],
            'attempted':  [all ids we tried],
            'errors':     {membership_id: error_str},
            'discovered': total active memberships found,
        }``

    Does NOT raise. The caller decides whether to surface a partial-failure
    or full-failure to the user (see ``ls_cancel`` / ``ls_change_plan``).
    """
    seen: set[str] = set()
    targets: list[str] = []

    # 1) All active memberships Whop returns for this email.
    try:
        mems = get_memberships_by_email(email or '', timeout=timeout) or []
    except Exception:
        mems = []
    for m in mems:
        # ``valid`` is the Whop-side "still entitled" flag. We also accept
        # status==active|trialing as a belt-and-braces check for v2 rows
        # where ``valid`` may be missing.
        status = (m.get('status') or '').lower()
        if not (m.get('valid') or status in ('active', 'trialing', 'completed')):
            continue
        mid = m.get('id')
        if mid and mid not in seen:
            seen.add(mid)
            targets.append(mid)

    # 2) Any extra ids the caller wants us to make sure we cover (e.g. the
    #    one we have stored on the user row). Verify each is actually still
    #    active before adding — no point cancelling something already ended.
    for mid in (extra_membership_ids or []):
        if not mid or mid in seen:
            continue
        try:
            m = get_membership(mid, timeout=timeout) or {}
        except Exception:
            m = {}
        status = (m.get('status') or '').lower()
        if m.get('valid') or status in ('active', 'trialing', 'completed'):
            seen.add(mid)
            targets.append(mid)

    cancelled: list[str] = []
    errors: dict[str, str] = {}
    for mid in targets:
        try:
            cancel_membership(mid, immediate=immediate)
            cancelled.append(mid)
        except Exception as e:
            errors[mid] = str(e)

    return {
        'cancelled':  cancelled,
        'attempted':  targets,
        'errors':     errors,
        'discovered': len(targets),
    }


# NOTE: ``resume_membership`` was removed. Whop does not expose any
# un-cancel endpoint to our company-scoped API key — every v5
# ``/uncancel`` path returns 404 and v2 returns 401 — so the wrapper
# could never succeed and the settings UI no longer offers a
# Reactivate flow. Cancelled users buy a fresh subscription via the
# normal checkout instead.


# ── Webhook ────────────────────────────────────────────────────────────────

def verify_webhook_signature(payload_bytes: bytes, signature: str) -> bool:
    secret = _webhook_secret()
    if not secret:
        return True
    digest = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    clean  = signature.replace('sha256=', '').strip()
    return hmac.compare_digest(digest, clean)


# ── Billing dates ──────────────────────────────────────────────────────────

def get_billing_dates(membership: dict) -> dict:
    """
    Extract subscription and billing-period dates from a Whop membership.
    Returns dict with ISO date strings and Unix timestamps.
    """
    from datetime import datetime, timezone

    def _to_iso(ts):
        if not ts:
            return ''
        try:
            if isinstance(ts, (int, float)) and int(ts) > 0:
                return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime('%Y-%m-%d')
            return str(ts)[:10]
        except Exception:
            return ''

    def _to_ts(val):
        if not val:
            return 0
        try:
            if isinstance(val, (int, float)):
                return int(val)
            return int(datetime.fromisoformat(str(val).replace('Z', '+00:00')).timestamp())
        except Exception:
            return 0

    created_at       = membership.get('created_at', 0)
    period_start_raw = membership.get('renewal_period_start') or created_at or 0
    period_end_raw   = membership.get('renewal_period_end') or 0
    expires_at_raw   = membership.get('expires_at') or 0

    period_start_ts  = _to_ts(period_start_raw)
    period_end_ts    = _to_ts(period_end_raw) or _to_ts(expires_at_raw)
    created_ts       = _to_ts(created_at)

    return {
        'subscription_start':    _to_iso(created_ts),
        'subscription_start_ts': created_ts,
        'period_start':          _to_iso(period_start_ts),
        'period_start_ts':       period_start_ts,
        'period_end':            _to_iso(period_end_ts),
        'period_end_ts':         period_end_ts,
    }


# ── Helpers ────────────────────────────────────────────────────────────────

def _admin_plan_id_map() -> dict:
    """Build a {plan_id: plan_key} map from admin-configured Whop plan IDs.
    Reads both prod (whop_plan_id_<plan>_<period>) and dev
    (whop_plan_id_dev_<plan>_<period>) settings so detection works
    regardless of which mode the admin is currently running in. This is
    what closes the gap left by the hardcoded _PLAN_ID_MAP, which only
    contains a fixed snapshot of known IDs and cannot keep up with
    custom plan IDs admins paste into /admin-panel/whop-settings/."""
    out: dict = {}
    for plan in ('starter', 'pro', 'agency'):
        for period in ('monthly', 'annual'):
            key = f'{plan}_{period}'
            for prefix in ('whop_plan_id_', 'whop_plan_id_dev_'):
                pid = _db_setting(f'{prefix}{key}', '').strip()
                if pid:
                    out[pid] = plan
    return out


def plan_from_membership(membership: dict, default: str = 'starter') -> str:
    """
    Detect plan key (starter/pro/agency) from a Whop membership object.
    Handles both v5 (embedded product object) and v2 (flat product ID) formats.
    Uses direct ID maps first to avoid extra API calls.

    Lookup precedence:
      1. Hardcoded _PRODUCT_ID_MAP (product-level — most authoritative,
         a Whop product is one tier by design)
      2. Hardcoded _PLAN_ID_MAP (known stable plan IDs)
      3. Admin-configured plan IDs from system_settings (so custom IDs
         pasted into /admin-panel/whop-settings/ resolve correctly)
      4. Text-based fallback over product name / affiliate URL slugs
      5. ``default`` (defaults to 'starter' for backward compat).
         Callers that need to distinguish "detected starter" from
         "couldn't detect anything" should pass ``default=''`` and
         treat the empty string as "unknown — apply your own hint or
         skip the update". This prevents the long-standing bug where
         an Agency upgrade webhook with an unrecognised plan_id would
         silently downgrade the user to starter.

    Why product_id is checked before plan_id: in production they always
    agree, but in dev/test the same plan_id is sometimes attached to
    multiple products (the team reuses fixtures across starter/agency).
    The product is the authoritative tier; the plan is just a billing
    cadence under that product. Picking product first means an Agency
    membership with a re-used dev plan_id is still correctly recognised
    as Agency — without this, the user gets bound to the right
    membership but the *plan* field stays at whatever the plan_id
    happens to map to (e.g. 'starter'), defeating the whole sync.
    """
    # 1. Direct product_id lookup (most authoritative)
    product = membership.get('product')
    if isinstance(product, str) and product in _PRODUCT_ID_MAP:
        return _PRODUCT_ID_MAP[product]
    if isinstance(product, dict) and product.get('id') in _PRODUCT_ID_MAP:
        return _PRODUCT_ID_MAP[product['id']]

    plan_id = membership.get('plan_id', '') or membership.get('plan', '')

    # 2. Hardcoded plan-id map
    if plan_id and plan_id in _PLAN_ID_MAP:
        return _PLAN_ID_MAP[plan_id]

    # 3. Admin-configured plan-id map — covers custom IDs pasted into
    #    /admin-panel/whop-settings/ that aren't in the hardcoded list.
    #    This is the path that fixes the "agency payment landed as
    #    starter" bug for the dev agency plan and any other admin-added
    #    plan ID.
    if plan_id:
        admin_map = _admin_plan_id_map()
        if plan_id in admin_map:
            return admin_map[plan_id]

    product_sources = []

    if isinstance(product, dict):
        # v5 embedded format — name fallback (id was already tried above)
        product_sources += [product.get('name', ''), product.get('title', '')]
    elif isinstance(product, str) and product:
        # v2 flat format — look up product name via v2 API
        try:
            req = urllib.request.Request(
                f'https://api.whop.com/api/v2/products/{product}',
                headers=_headers(),
            )
            r = urllib.request.urlopen(req, timeout=8)
            p = json.loads(r.read())
            product_sources += [p.get('name', ''), p.get('title', '')]
        except Exception:
            pass

    # 4. Text-based fallback: product names + URL slugs
    sources = product_sources + [
        membership.get('affiliate_page_url', ''),   # e.g. "https://whop.com/permit-pro-monthly/…"
        membership.get('plan_id', ''),
        membership.get('plan', ''),
        membership.get('id', ''),
    ]

    # Important: check 'agency' before 'pro' before 'starter' so that
    # a URL like '.../permit-agency-pro/' can never match 'pro' first.
    # Use a deterministic order rather than relying on dict iteration.
    keyword_order = (('agency', 'agency'), ('pro', 'pro'), ('starter', 'starter'))
    for src in sources:
        low = str(src).lower()
        for keyword, plan in keyword_order:
            if keyword in low:
                return plan
    return default


def format_membership_for_ui(membership: dict) -> dict:
    if not membership:
        return {}
    plan           = plan_from_membership(membership)
    status         = membership.get('status', 'active')
    valid          = membership.get('valid', True)
    cancel_at_end  = membership.get('cancel_at_period_end', False)
    renewal_end = membership.get('renewal_period_end', '') or ''
    if renewal_end:
        try:
            if isinstance(renewal_end, (int, float)):
                renewal_end = datetime.fromtimestamp(int(renewal_end)).strftime('%b %d, %Y')
            else:
                renewal_end = datetime.fromisoformat(
                    str(renewal_end).replace('Z', '+00:00')).strftime('%b %d, %Y')
        except Exception:
            renewal_end = ''
    return {
        'id':         membership.get('id', ''),
        'plan':       plan.title(),
        'status':     status if valid else 'expired',
        'renews_at':  renewal_end,
        'portal_url': 'https://whop.com/hub/',
        'update_url': 'https://whop.com/hub/',
        'cancelled':  cancel_at_end or status in ('cancelled', 'expired'),
        'paused':     status == 'paused',
        'trial_ends': '',
        'annual':     False,
        'card_brand': '',
        'card_last4': '',
    }


# ── Admin revenue aggregator ────────────────────────────────────────────
# The /admin-panel/revenue/ page used to derive its KPIs (MRR, active subs,
# plan distribution, monthly trend) from our local Postgres ``users`` table.
# That worked, but the local `subscription_active` / `plan` columns drift
# out of sync with Whop whenever a webhook is missed, a refund is issued
# outside our flow, or someone signs up with the default `plan='starter'`
# but never actually pays — so the numbers were never quite right and the
# historical-month trend was hardcoded.
#
# Whop is the source of truth for who is paying us, so the revenue page now
# pulls everything from Whop:
#   * /api/v5/company/memberships  → active sub count, plan distribution, MRR
#   * /api/v2/payments              → real paid revenue, bucketed per month
#
# Both endpoints are paginated; we fetch up to ``_REV_MAX_PAGES`` pages each.
# The aggregated result is cached for 5 minutes in-process so back-to-back
# admin page loads don't burn through the rate limit.

_REV_CACHE_TTL  = 300.0          # seconds (5 min)
_REV_MAX_PAGES  = 50             # safety cap → up to 5,000 rows per endpoint
_REV_PER_PAGE   = 100
_revenue_cache: dict = {}        # {'ts': float, 'data': dict}
_revenue_lock   = _threading.Lock()


def clear_revenue_cache() -> None:
    """Drop the cached Whop revenue snapshot — used by tests / admin tools."""
    with _revenue_lock:
        _revenue_cache.clear()


def _whop_get_paginated(url_base: str, page_param: str, per_param: str,
                        timeout: int = 15,
                        max_wall_seconds: float = 10.0) -> tuple[list, bool]:
    """Page through a Whop list endpoint until exhausted (or capped).

    Returns ``(items, ok)``. ``ok`` is ``False`` when ANY page request
    failed — the caller MUST treat partial data as untrustworthy because
    revenue/subscriber KPIs computed from a partial list silently
    under-report. The caller decides whether to fall back or render with
    a warning.
    """
    out: list = []
    page = 1
    # Hard wall-clock budget so a slow Whop API can never lock the
    # admin dashboard for minutes. We still honour the per-request
    # ``timeout`` but bail out of the page loop as soon as the total
    # elapsed time crosses ``max_wall_seconds`` — partial fetch is
    # surfaced via ``ok=False`` so the caller falls back to local math
    # exactly like a real outage.
    _t_start = _time.time()
    while page <= _REV_MAX_PAGES:
        if (_time.time() - _t_start) >= max_wall_seconds:
            return out, False
        sep = '&' if '?' in url_base else '?'
        url = f'{url_base}{sep}{per_param}={_REV_PER_PAGE}&{page_param}={page}'
        try:
            req = urllib.request.Request(url, headers=_headers())
            r   = urllib.request.urlopen(req, timeout=timeout)
            d   = json.loads(r.read())
        except Exception:
            return out, False
        items = d.get('data') if isinstance(d, dict) else None
        if items is None:
            # Unexpected response shape — treat as failure, not "end of list",
            # so we don't silently report zero subs from a malformed page.
            return out, False
        if not items:
            break
        out.extend(items)
        pag = (d.get('pagination') or {}) if isinstance(d, dict) else {}
        # v5 uses ``next_page``; v2 sometimes only reports total_page —
        # bail when next_page is explicitly null OR we've passed total_page.
        next_page = pag.get('next_page')
        total_pg  = pag.get('total_page') or pag.get('total_pages')
        if next_page is None and (total_pg is None or page >= int(total_pg)):
            break
        page += 1
    return out, True


def list_company_memberships(timeout: int = 15) -> tuple[list, bool]:
    """All memberships for the configured company. Returns ``(items, ok)``."""
    return _whop_get_paginated(
        f'{BASE}/company/memberships', page_param='page',
        per_param='per_page', timeout=timeout,
    )


def list_company_payments(timeout: int = 15) -> tuple[list, bool]:
    """All payments for the configured company (v2). Returns ``(items, ok)``.

    v2 is used because it returns the actual ``paid`` status + ``final_amount``
    in dollars, which is what we need to chart historical revenue.
    """
    return _whop_get_paginated(
        'https://api.whop.com/api/v2/payments', page_param='page',
        per_param='per', timeout=timeout,
    )


# ── Real fee + balance helpers (v1 API) ────────────────────────────────────
# Whop's v2/v5 payment list endpoints do NOT include any fee or net field —
# only ``final_amount`` and ``refunded_amount``. To get the actual cut Whop
# took (platform fee + Stripe processing + cross-border + sales-tax
# remittance etc.) we must hit ``GET /api/v1/payments/{id}/fees`` per
# payment and sum the line items. That call requires only
# ``payment:basic:read`` which our key already has. Fees on a settled
# payment are immutable, so we cache them in-process forever keyed by
# payment id — only new payments incur API round-trips.

# Successful fee fetches are cached forever (fees on settled payments are
# immutable). Failed fetches go into a TTL'd negative cache so a persistent
# 403/404/timeout doesn't cause us to retry every paid payment on every
# 5-minute revenue refresh. The negative TTL is short enough that a fixed
# scope / restored endpoint recovers within minutes.
_fees_cache:     dict[str, float] = {}
_fees_negcache:  dict[str, float] = {}   # payment_id -> unix_ts of last failure
_FEES_NEG_TTL    = 600  # seconds
_fees_lock = _threading.Lock()


def list_payment_fees(payment_id: str, timeout: int = 10) -> float | None:
    """Sum of all fees Whop took on this payment (USD). Returns ``None``
    when the fees endpoint fails so callers can decide between treating
    it as 0 or falling back to the flat-rate approximation. Recent
    failures are negative-cached for ``_FEES_NEG_TTL`` seconds to avoid
    retry storms when the scope/endpoint is genuinely missing."""
    if not payment_id:
        return None
    now = _time.time()
    with _fees_lock:
        if payment_id in _fees_cache:
            return _fees_cache[payment_id]
        last_fail = _fees_negcache.get(payment_id, 0.0)
        if last_fail and (now - last_fail) < _FEES_NEG_TTL:
            return None
    url = f'https://api.whop.com/api/v1/payments/{urllib.parse.quote(payment_id)}/fees'
    try:
        req = urllib.request.Request(url, headers=_headers())
        r   = urllib.request.urlopen(req, timeout=timeout)
        d   = json.loads(r.read())
    except Exception:
        with _fees_lock:
            _fees_negcache[payment_id] = now
        return None
    items = d.get('data') if isinstance(d, dict) else None
    if items is None:
        with _fees_lock:
            _fees_negcache[payment_id] = now
        return None
    total = 0.0
    for f in items:
        try:
            total += float(f.get('amount') or 0)
        except Exception:
            continue
    total = round(total, 4)
    with _fees_lock:
        _fees_cache[payment_id] = total
        _fees_negcache.pop(payment_id, None)
    return total


def get_company_ledger(timeout: int = 10) -> dict | None:
    """Return the company's ledger account — current balance, pending
    balance, reserve, transfer fee. This is what Whop's dashboard shows
    as "Total balance" / "Payouts" and is the authoritative all-time
    Net figure (gross paid in, minus all fees, minus prior payouts).
    Returns ``None`` on failure or missing scope."""
    cid = _company_id()
    if not cid:
        return None
    url = f'https://api.whop.com/api/v1/ledger_accounts/{urllib.parse.quote(cid)}'
    try:
        req = urllib.request.Request(url, headers=_headers())
        r   = urllib.request.urlopen(req, timeout=timeout)
        d   = json.loads(r.read())
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    # Pick the USD balance row (companies can technically hold multi-currency).
    usd = None
    for b in (d.get('balances') or []):
        if (b.get('currency') or '').lower() == 'usd':
            usd = b; break
    return {
        'balance':         float((usd or {}).get('balance') or 0),
        'pending_balance': float((usd or {}).get('pending_balance') or 0),
        'reserve_balance': float((usd or {}).get('reserve_balance') or 0),
        'currency':        'usd',
        'transfer_fee':    float(d.get('transfer_fee') or 0),
    }


# Range labels supported by the dashboard (?range= query parameter). Maps
# to a number of UTC days back from "today" (inclusive of today). 0 = today
# only. The dashboard always compares vs the immediately preceding window
# of equal length ("compared to Previous period" in Whop's UI).
_RANGE_OPTIONS = {
    '1d':  ('Today',         1),
    '7d':  ('Last 7 days',   7),
    '30d': ('Last 30 days',  30),
    '90d': ('Last 90 days',  90),
    '365d':('Last 365 days', 365),
}

# Approximate Whop take-rate used for the Net revenue card. Whop's actual
# cut varies by plan/processor/country (Whop fee + Stripe + sales-tax
# remittance) and isn't exposed via API at our scope. Empirically, on the
# permitlify.com account it averages ~44 % of gross (Whop's own dashboard
# reported $14 gross → $7.84 net all-time, a 44 % effective rate), so we
# default there and let admins override per-environment via the
# ``whop_fee_pct`` system setting (float, e.g. 0.44 = 44 %).
_DEFAULT_FEE_PCT = 0.44


def _fee_pct() -> tuple[float, bool]:
    """Return (fee_pct, is_explicit). ``is_explicit`` is False when we fell
    back to the default — the template uses this to mark Net revenue as an
    estimate so admins don't read the number as exact."""
    raw = _db_setting('whop_fee_pct', '')
    if not raw:
        return _DEFAULT_FEE_PCT, False
    try:
        v = float(raw)
        return max(0.0, min(0.95, v)), True
    except Exception:
        return _DEFAULT_FEE_PCT, False


def _day_key(unix_ts: int) -> str:
    """UTC YYYY-MM-DD bucket for a unix timestamp (seconds)."""
    return datetime.utcfromtimestamp(int(unix_ts)).strftime('%Y-%m-%d')


def _hour_of_day(unix_ts: int) -> int:
    return datetime.utcfromtimestamp(int(unix_ts)).hour


def _compute_mrr_at(payments: list, memberships: list, as_of_ts: int) -> float:
    """Whop-style MRR at a point in time.

    Whop's dashboard MRR is the sum of monthly-recurring revenue from every
    currently active membership at its plan price (annuals amortized /12).
    Cancelled/expired memberships drop out the moment they churn — Whop
    does NOT require a successful renewal cycle, so trial signups that
    just paid their first $1 still contribute (which is what Whop's UI
    reports too).

    We can't read Whop's ``/balance``-style endpoint at our API scope, so
    we reconstruct it from raw ``/memberships`` + ``/payments``:

      * Iterate over memberships with ``valid=True`` AND
        ``status in ('active','trialing')`` as of ``as_of_ts``.
      * Find the most recent paid payment on that membership (cycle or
        create, whichever is newest at-or-before ``as_of_ts``) — that's
        the per-period price the user is actually being charged.
      * Annuals (``billing_period_in_days >= 60``) amortize ``/12``.
      * Memberships with no paid payment yet (still in a free preview
        before charge) are skipped — they contribute $0 anyway.
    """
    # Index memberships by id for O(1) lookup.
    mem_by_id: dict[str, dict] = {}
    for m in (memberships or []):
        mid = m.get('id')
        if mid:
            mem_by_id[mid] = m

    # Bucket all paid create/cycle payments by membership_id and keep the
    # latest one per membership at-or-before ``as_of_ts`` — that's the
    # current period's charged amount.
    latest_paid: dict[str, dict] = {}
    for p in payments or []:
        if p.get('status') != 'paid':
            continue
        reason = p.get('billing_reason')
        if reason not in ('subscription_create', 'subscription_cycle'):
            continue
        mid = p.get('membership') or p.get('membership_id')
        if not mid:
            continue
        try:
            ts = int(p.get('paid_at') or p.get('created_at') or 0)
        except Exception:
            continue
        if ts > as_of_ts:
            continue
        cur = latest_paid.get(mid)
        if cur is None or ts > cur['ts']:
            latest_paid[mid] = {'ts': ts, 'p': p}

    total = 0.0
    for mid, m in mem_by_id.items():
        if not m.get('valid'):
            continue
        if m.get('status') not in ('active', 'trialing'):
            continue
        # Whop's dashboard MRR drops memberships that are flagged to
        # cancel at the end of the current period — they're still
        # "active" today but won't renew, so they no longer contribute
        # to recurring revenue. Mirror that behaviour or our MRR drifts
        # high by ~$N for every churn-pending subscriber.
        if m.get('cancel_at_period_end'):
            continue
        entry = latest_paid.get(mid)
        if not entry:
            # Active membership with no paid payment yet (e.g. free trial
            # preview) — contributes $0.
            continue
        p = entry['p']
        amt = float(p.get('final_amount') or 0) - float(p.get('refunded_amount') or 0)
        if amt <= 0:
            continue
        if _is_annual_membership(m):
            amt = amt / 12.0
        total += amt
    return round(total, 2)


def _delta_pct(cur: float, prev: float) -> float | None:
    """Percent change (cur vs prev). Returns None when prev is 0 to avoid
    a misleading +∞%; the template shows the absolute delta in that case."""
    try:
        if not prev:
            return None
        return round((cur - prev) / prev * 100, 1)
    except Exception:
        return None


def get_revenue_stats(range_key: str = '7d',
                      timeout: int = 15,
                      force: bool = False) -> dict | None:
    """Aggregate Whop data into the shape the new dashboard expects.

    Mirrors the Whop merchant dashboard layout:

      * ``today`` block — gross today, gross yesterday, hourly chart for
        today (24 buckets), % change vs yesterday.
      * ``range`` block — totals + daily series for Gross, Net (approx),
        New users, MRR, ARR over the selected window, plus "previous
        period" deltas matching Whop's "compared to Previous period" UI.
      * ``payments_breakdown`` — count + sum + share for paid / failed /
        past_due / canceled / refunded statuses inside the window.

    Returns ``None`` when memberships fetch fails (caller falls back to the
    legacy local-DB calc). Partial payments failure leaves ``payments_ok``
    False so the view can hide degraded charts and skip caching.
    """
    if range_key not in _RANGE_OPTIONS:
        range_key = '7d'

    now = _time.time()
    cache_key = f'data:{range_key}'
    _ts_key   = f'ts:{range_key}'
    with _revenue_lock:
        ts        = _revenue_cache.get(_ts_key, 0.0)
        cached    = _revenue_cache.get(cache_key)
        has_entry = cache_key in _revenue_cache
    # Honour cached failures too (cached == None) so a Whop outage
    # doesn't make every admin-dashboard hit re-spend the full
    # wall-clock budget. Negative cache TTL is intentionally short.
    _NEG_TTL = 60.0
    if has_entry and not force:
        if cached is None and (now - ts) < _NEG_TTL:
            return None
        if cached is not None and (now - ts) < _REV_CACHE_TTL:
            return cached

    if not _api_key():
        return None

    # Memberships first with the full wall-clock budget; if they fail
    # we bail immediately so payments isn't even attempted (saves the
    # caller another N×timeout wait on the way to the local fallback).
    memberships, mem_ok = list_company_memberships(timeout=timeout)
    if not mem_ok:
        # Short-cache the failure so a Whop outage doesn't make every
        # admin-dashboard hit re-spend the full wall-clock budget.
        with _revenue_lock:
            _revenue_cache[cache_key]         = None
            _revenue_cache[f'ts:{range_key}'] = now
        return None
    payments,    pay_ok = list_company_payments(timeout=timeout)
    if not memberships and not payments:
        return None

    range_label, range_days = _RANGE_OPTIONS[range_key]
    fee_pct, fee_pct_explicit = _fee_pct()

    # ── Day boundaries (UTC) ──────────────────────────────────────────
    # NOTE: ``datetime.timestamp()`` interprets a naive datetime as *local*
    # time, which silently shifts day boundaries on non-UTC servers
    # (e.g. prod runs in UTC, dev container might not). ``calendar.timegm``
    # treats the input as UTC, giving us the same answer everywhere.
    import calendar as _cal
    now_dt   = datetime.utcnow()
    today_d  = datetime(now_dt.year, now_dt.month, now_dt.day)
    today_start_ts = _cal.timegm(today_d.timetuple())
    yday_start_ts  = today_start_ts - 86400

    # Range covers ``range_days`` UTC days ending today (inclusive).
    range_end_ts   = today_start_ts + 86400 - 1   # end of today
    range_start_ts = today_start_ts - (range_days - 1) * 86400
    # Previous period of equal length, immediately before the current one.
    prev_end_ts    = range_start_ts - 1
    prev_start_ts  = prev_end_ts - range_days * 86400 + 1

    paid = [p for p in (payments or []) if p.get('status') == 'paid']

    def _amt(p: dict) -> float:
        return float(p.get('final_amount') or 0) - float(p.get('refunded_amount') or 0)

    def _ts(p: dict) -> int:
        try:
            return int(p.get('paid_at') or p.get('created_at') or 0)
        except Exception:
            return 0

    # ── Real per-payment fees (Whop v1 `/payments/{id}/fees`) ─────────
    # Pull the actual Whop+Stripe+tax fee breakdown so Net revenue
    # reflects what Whop's own dashboard reports rather than a flat
    # ``fee_pct`` approximation. We only hydrate fees for payments
    # actually shown on this page — current window + previous window
    # (for the delta vs. previous) — so a company with 10k all-time
    # payments doesn't pay 10k round-trips for a 7-day view. Fees on
    # settled payments are immutable so successes cache forever;
    # failures hit a short TTL negative cache to prevent retry storms.
    # ``fees_real`` reflects coverage *of the displayed window* — the
    # all-time totals/ledger card is sourced from `get_company_ledger`
    # below, so partial all-time coverage doesn't taint the badge.
    fees_by_pid: dict[str, float] = {}
    fees_real = True
    fees_total = 0
    fees_covered = 0
    for p in paid:
        t = _ts(p)
        # Only payments within current OR previous window need real fees.
        in_cur  = range_start_ts <= t <= range_end_ts
        in_prev = prev_start_ts  <= t <= prev_end_ts
        if not (in_cur or in_prev):
            continue
        fees_total += 1
        pid = p.get('id')
        if not pid:
            fees_real = False
            continue
        f = list_payment_fees(pid)
        if f is None:
            fees_real = False
            continue
        fees_by_pid[pid] = f
        fees_covered += 1
    # Coverage as a fraction (0.0 – 1.0) so the UI can show "85 % real,
    # 15 % estimated" instead of a binary flag. 1.0 = fully real,
    # 0.0 = entirely flat-rate fallback. ``fees_real`` is kept for
    # backwards compatibility with the existing template badge.
    fees_coverage = (fees_covered / fees_total) if fees_total else 1.0

    def _fee(p: dict) -> float:
        """Whop's cut on this payment. Uses the real per-payment fee when
        we have it, otherwise the flat-rate approximation so we never
        crash on a transient fee-API blip."""
        pid = p.get('id') or ''
        if pid in fees_by_pid:
            return fees_by_pid[pid]
        return float(p.get('final_amount') or 0) * fee_pct

    def _net(p: dict) -> float:
        """Per-payment net = gross-after-refund minus Whop fees. Fees are
        not refunded by Whop, so a fully-refunded payment can go
        slightly negative — clamp at 0 to mirror Whop's dashboard."""
        return max(0.0, _amt(p) - _fee(p))

    # ── TODAY block ───────────────────────────────────────────────────
    gross_today     = round(sum(_amt(p) for p in paid if today_start_ts <= _ts(p) < today_start_ts + 86400), 2)
    gross_yesterday = round(sum(_amt(p) for p in paid if yday_start_ts  <= _ts(p) < today_start_ts), 2)
    today_hourly = [0.0] * 24
    for p in paid:
        t = _ts(p)
        if today_start_ts <= t < today_start_ts + 86400:
            today_hourly[_hour_of_day(t)] += _amt(p)
    today_hourly = [round(v, 2) for v in today_hourly]
    yday_hourly = [0.0] * 24
    for p in paid:
        t = _ts(p)
        if yday_start_ts <= t < today_start_ts:
            yday_hourly[_hour_of_day(t)] += _amt(p)
    yday_hourly = [round(v, 2) for v in yday_hourly]

    # ── Build daily series for the selected range ─────────────────────
    daily_gross:    list[float] = []
    daily_net:      list[float] = []
    daily_new:      list[int]   = []
    daily_mrr:      list[float] = []
    daily_labels:   list[str]   = []
    seen_user_ids:  set         = set()  # for cumulative new-user dedup

    # Pre-bucket payments by day for fast lookup.
    bucket_paid: dict[str, list[dict]] = {}
    for p in paid:
        t = _ts(p)
        if not t:
            continue
        bucket_paid.setdefault(_day_key(t), []).append(p)

    # New users per day = count of unique user_ids whose FIRST paid payment
    # falls on that day (so a returning customer doesn't count again).
    first_payment_day: dict[str, str] = {}  # user_id -> 'YYYY-MM-DD'
    for p in sorted(paid, key=_ts):
        u = p.get('user') if isinstance(p.get('user'), str) else (p.get('user') or {}).get('id') or p.get('user_id')
        if not u or u in first_payment_day:
            continue
        t = _ts(p)
        if t:
            first_payment_day[u] = _day_key(t)
    new_per_day: dict[str, int] = {}
    for d in first_payment_day.values():
        new_per_day[d] = new_per_day.get(d, 0) + 1

    cur_day_ts = range_start_ts
    while cur_day_ts <= today_start_ts:
        d_dt   = datetime.utcfromtimestamp(cur_day_ts)
        d_key  = d_dt.strftime('%Y-%m-%d')
        d_label = d_dt.strftime('%b %d')
        daily_labels.append(d_label)
        day_payments = bucket_paid.get(d_key, [])
        gross = round(sum(_amt(p) for p in day_payments), 2)
        daily_gross.append(gross)
        # Net = sum of real per-payment net (gross minus Whop's actual cut).
        # Falls back to flat-rate inside ``_net`` when fees API failed for
        # specific payments — see ``fees_real`` flag surfaced below.
        daily_net.append(round(sum(_net(p) for p in day_payments), 2))
        daily_new.append(new_per_day.get(d_key, 0))
        # MRR as of end-of-day = trailing-31-day recurring contribution
        eod_ts = cur_day_ts + 86400 - 1
        daily_mrr.append(_compute_mrr_at(payments, memberships, eod_ts))
        cur_day_ts += 86400

    daily_arr = [round(v * 12, 2) for v in daily_mrr]

    # ── Range totals (current period) ─────────────────────────────────
    gross_range = round(sum(daily_gross), 2)
    net_range   = round(sum(daily_net), 2)
    new_range   = sum(daily_new)
    mrr_now     = daily_mrr[-1] if daily_mrr else 0.0
    arr_now     = round(mrr_now * 12, 2)

    # ── Previous period totals (for delta vs previous) ────────────────
    prev_paid = [p for p in paid if prev_start_ts <= _ts(p) <= prev_end_ts]
    prev_gross = round(sum(_amt(p) for p in prev_paid), 2)
    prev_net   = round(sum(_net(p) for p in prev_paid), 2)
    prev_new_users = set()
    for p in prev_paid:
        u = p.get('user') if isinstance(p.get('user'), str) else (p.get('user') or {}).get('id') or p.get('user_id')
        if not u or not first_payment_day.get(u):
            continue
        first_d = datetime.strptime(first_payment_day[u], '%Y-%m-%d')
        first_ts = _cal.timegm(first_d.timetuple())
        if prev_start_ts <= first_ts <= prev_end_ts:
            prev_new_users.add(u)
    prev_new = len(prev_new_users)
    prev_mrr = _compute_mrr_at(payments, memberships, prev_end_ts)
    prev_arr = round(prev_mrr * 12, 2)

    # ── Payments status breakdown (in window) ─────────────────────────
    breakdown = {k: {'count': 0, 'amount': 0.0} for k in ('paid','failed','past_due','canceled','refunded')}
    for p in (payments or []):
        t = _ts(p)
        if not (range_start_ts <= t <= range_end_ts):
            continue
        st  = p.get('status')
        amt = float(p.get('final_amount') or 0)
        ref = float(p.get('refunded_amount') or 0)
        if st == 'paid' and ref > 0:
            # Whop dashboard counts refunded payments in the Refunded row,
            # not the Paid row. Move the refunded portion across.
            breakdown['paid']['count']   += 1
            breakdown['paid']['amount']  += amt - ref
            breakdown['refunded']['count']  += 1
            breakdown['refunded']['amount'] += ref
        elif st in breakdown:
            breakdown[st]['count']  += 1
            breakdown[st]['amount'] += amt
    bd_total_amt = sum(b['amount'] for b in breakdown.values()) or 1.0
    payments_breakdown = []
    for label, key in [('Paid','paid'), ('Failed','failed'), ('Past due','past_due'),
                       ('Canceled','canceled'), ('Refunded','refunded')]:
        b = breakdown[key]
        payments_breakdown.append({
            'label':  label,
            'key':    key,
            'count':  b['count'],
            'amount': round(b['amount'], 2),
            'pct':    round(b['amount'] / bd_total_amt * 100),
        })

    # ── All-time totals (for the secondary KPIs panel) ────────────────
    # ``total_paid_alltime`` = sum of gross paid - refunds across all
    # payments (cheap, no extra API calls). We DON'T compute an all-time
    # net by hydrating fees for every historical payment — that would be
    # O(N) cold-start cost for no extra accuracy because the ledger
    # endpoint below already gives the authoritative current-balance
    # figure that Whop's own "Total balance" tile shows.
    total_paid_alltime = round(sum(_amt(p) for p in paid), 2)
    ledger = get_company_ledger()

    range_start_label = datetime.utcfromtimestamp(range_start_ts).strftime('%b %d')
    range_end_label   = datetime.utcfromtimestamp(today_start_ts).strftime('%b %d, %Y')

    result = {
        'today': {
            'gross':           gross_today,
            'yesterday':       gross_yesterday,
            'pct_vs_yday':     _delta_pct(gross_today, gross_yesterday),
            'hourly':          today_hourly,        # 24-element list
            'hourly_yday':     yday_hourly,
            'as_of':           now_dt.strftime('%I:%M %p UTC').lstrip('0'),
        },
        'range': {
            'key':             range_key,
            'label':           range_label,
            'days':            range_days,
            'start':           range_start_label,
            'end':             range_end_label,
            'fee_pct':         fee_pct,
            'fee_pct_explicit': fee_pct_explicit,
            # True when Net was computed from Whop's real per-payment fee
            # breakdown (``/payments/{id}/fees``) for EVERY payment in
            # the displayed (current + previous) window. False when even
            # one payment fell back to the flat ``fee_pct`` factor.
            'fees_real':       fees_real,
            # 0.0–1.0 coverage of real-fee data within the displayed
            # window. 1.0 = fully real; lower = some payments used the
            # flat-rate fallback. Useful for a "X % real" UI hint.
            'fees_coverage':   round(fees_coverage, 3),
            'labels':          daily_labels,
            # Each card: current value + delta vs previous period + series
            'gross':      {'value': gross_range, 'prev': prev_gross,
                           'delta': round(gross_range - prev_gross, 2),
                           'delta_pct': _delta_pct(gross_range, prev_gross),
                           'series': daily_gross},
            'net':        {'value': net_range, 'prev': prev_net,
                           'delta': round(net_range - prev_net, 2),
                           'delta_pct': _delta_pct(net_range, prev_net),
                           'series': daily_net},
            'new_users':  {'value': new_range, 'prev': prev_new,
                           'delta': new_range - prev_new,
                           'delta_pct': _delta_pct(new_range, prev_new),
                           'series': daily_new},
            'mrr':        {'value': mrr_now, 'prev': prev_mrr,
                           'delta': round(mrr_now - prev_mrr, 2),
                           'delta_pct': _delta_pct(mrr_now, prev_mrr),
                           'series': daily_mrr},
            'arr':        {'value': arr_now, 'prev': prev_arr,
                           'delta': round(arr_now - prev_arr, 2),
                           'delta_pct': _delta_pct(arr_now, prev_arr),
                           'series': daily_arr},
            'payments_breakdown': payments_breakdown,
        },
        'totals': {
            'all_time_paid':   total_paid_alltime if pay_ok else None,
            'ledger':          ledger,   # {'balance','pending_balance',...} or None
            'memberships':     len(memberships),
            # Currently recurring (valid + active|trialing) — same predicate
            # as `_compute_mrr_at`, exposed here so callers (e.g. the admin
            # overview) don't need a second `/memberships` round-trip.
            'active_recurring': sum(
                1 for m in memberships
                if m.get('valid')
                and m.get('status') in ('active', 'trialing')
                and not m.get('cancel_at_period_end')
            ),
        },
        'range_options':       [{'key': k, 'label': v[0]} for k, v in _RANGE_OPTIONS.items()],
        'payments_ok':         pay_ok,
        'memberships_ok':      mem_ok,
    }
    if pay_ok:
        with _revenue_lock:
            _revenue_cache[cache_key]            = result
            _revenue_cache[f'ts:{range_key}']    = now
    return result
