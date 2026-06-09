import os
import json
import hashlib
import hmac
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime


BASE = 'https://api.whop.com/api/v5'

DEFAULT_CHECKOUT_URLS = {
    'starter_monthly': 'https://whop.com/goldenproxies/starter-monthly/',
    'starter_annual':  'https://whop.com/goldenproxies/starter-annual/',
    'pro_monthly':     'https://whop.com/goldenproxies/pro-monthly/',
    'pro_annual':      'https://whop.com/goldenproxies/pro-annual/',
    'agency_monthly':  'https://whop.com/goldenproxies/business-monthly/',
    'agency_annual':   'https://whop.com/goldenproxies/business-annual/',
}

_PLAN_NAME_MAP = {
    'agency':   'agency',
    'business': 'agency',
    'pro':      'pro',
    'starter':  'starter',
}

_PLAN_ID_MAP: dict = {}
_PRODUCT_ID_MAP: dict = {}
_ANNUAL_PRODUCT_IDS: set = set()
_ANNUAL_PLAN_IDS: set = set()

import time as _time
import threading as _threading
_SETTINGS_TTL  = 60.0
_settings_cache: dict = {}
_settings_lock  = _threading.Lock()


def clear_settings_cache(key=None):
    with _settings_lock:
        if key is None:
            _settings_cache.clear()
        else:
            _settings_cache.pop(key, None)


def _db_setting(key: str, default: str = '') -> str:
    now = _time.monotonic()
    hit = _settings_cache.get(key)
    if hit and (now - hit[0]) < _SETTINGS_TTL:
        return hit[1] or default
    try:
        from .models import SystemSetting
        obj = SystemSetting.objects.filter(key=key).first()
        s = str(obj.value) if obj and obj.value else ''
    except Exception:
        return default
    with _settings_lock:
        _settings_cache[key] = (now, s)
    return s or default


def set_db_setting(key: str, value: str) -> None:
    try:
        from .models import SystemSetting
        SystemSetting.objects.update_or_create(key=key, defaults={'value': value})
        clear_settings_cache(key)
    except Exception:
        pass


def _api_key() -> str:
    return _db_setting('whop_api_key', os.environ.get('WHOP_API_KEY', ''))


def _company_id() -> str:
    return _db_setting('whop_company_id', os.environ.get('WHOP_COMPANY_ID', ''))


def _webhook_secret() -> str:
    return _db_setting('whop_webhook_secret', os.environ.get('WHOP_WEBHOOK_SECRET', ''))


def _whop_mode() -> str:
    return _db_setting('whop_mode', 'prod') or 'prod'


def mode_for_user(user) -> str:
    try:
        from .models import UserProfile
        if hasattr(user, 'profile'):
            m = user.profile.whop_mode
        else:
            m = getattr(user, 'whop_mode', None)
    except Exception:
        m = None
    if m in ('dev', 'prod'):
        return m
    return 'prod'


def get_checkout_url(plan: str, period: str) -> str:
    key = f'whop_checkout_{plan}_{period}'
    val = _db_setting(key, '')
    return val or DEFAULT_CHECKOUT_URLS.get(f'{plan}_{period}', '')


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


def get_plan_id(plan: str, period: str, mode: str = None) -> str:
    key = f'{plan.lower()}_{period.lower()}'
    effective_mode = mode if mode in ('dev', 'prod') else _whop_mode()
    if effective_mode == 'dev':
        return _db_setting(f'whop_plan_id_dev_{key}', '')
    return _db_setting(f'whop_plan_id_{key}', '')


_DEFAULT_DISPLAY_PRICES = {
    'prod': {
        ('starter', 'monthly'):  29,
        ('starter', 'annual'):   23,
        ('pro',     'monthly'):  99,
        ('pro',     'annual'):   79,
        ('agency',  'monthly'): 249,
        ('agency',  'annual'):  199,
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


def get_plan_price(plan: str, period: str, mode: str = None) -> int:
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


def get_pricing_dict(mode: str = None) -> dict:
    if mode not in ('dev', 'prod'):
        mode = _whop_mode()
    out = {'mode': mode, 'is_dev': mode == 'dev'}
    for plan in ('starter', 'pro', 'agency'):
        m = get_plan_price(plan, 'monthly', mode)
        a = get_plan_price(plan, 'annual',  mode)
        out[f'{plan}_monthly']       = m
        out[f'{plan}_annual']        = a
        out[f'{plan}_monthly_total'] = m * 12
        out[f'{plan}_annual_total']  = a * 12
        out[f'{plan}_annual_save']   = max((m - a) * 12, 0)
    return out


def create_checkout_url(plan: str, email: str, user_id: int,
                        success_url: str, period: str = 'monthly') -> str:
    plan_id = get_plan_id(plan, period)
    if plan_id:
        params = urllib.parse.urlencode({
            'redirect_url':      success_url,
            'prefill_email':     email,
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


def get_membership(membership_id: str, timeout: int = 15) -> dict | None:
    try:
        req = urllib.request.Request(f'{BASE}/memberships/{membership_id}', headers=_headers())
        r = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(r.read())
        if result:
            return result
    except Exception:
        pass
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
    e_lc = (email or '').strip().lower()

    def _mem_email(m: dict) -> str:
        u = m.get('user') or {}
        u_email = u.get('email') if isinstance(u, dict) else ''
        return (m.get('email') or m.get('user_email') or u_email or '').strip().lower()

    try:
        params = urllib.parse.urlencode({'email': email, 'per': 25})
        req = urllib.request.Request(
            f'https://api.whop.com/api/v2/memberships?{params}',
            headers=_headers(),
        )
        r = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(r.read())
        items = data.get('data', [])
        if e_lc:
            items = [m for m in items if (not _mem_email(m)) or _mem_email(m) == e_lc]
        if items:
            items.sort(key=lambda m: (0 if m.get('valid') else 1, -int(m.get('created_at') or 0)))
            return items
    except Exception:
        pass

    try:
        req = urllib.request.Request(
            'https://api.whop.com/api/v5/company/memberships',
            headers=_headers(),
        )
        r = urllib.request.urlopen(req, timeout=timeout)
        data  = json.loads(r.read())
        items = data.get('data', [])
        if e_lc:
            items = [m for m in items if _mem_email(m) == e_lc]
        items.sort(key=lambda m: (0 if m.get('valid') else 1, -int(m.get('created_at') or 0)))
        return items
    except Exception:
        return []


def cancel_membership(membership_id: str, immediate: bool = False) -> bool:
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
                                extra_membership_ids: list | None = None,
                                timeout: int = 15) -> dict:
    seen: set = set()
    targets: list = []

    try:
        mems = get_memberships_by_email(email or '', timeout=timeout) or []
    except Exception:
        mems = []
    for m in mems:
        status = (m.get('status') or '').lower()
        if not (m.get('valid') or status in ('active', 'trialing', 'completed')):
            continue
        mid = m.get('id')
        if mid and mid not in seen:
            seen.add(mid)
            targets.append(mid)

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

    cancelled: list = []
    errors: dict = {}
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


def verify_webhook_signature(payload_bytes: bytes, signature: str) -> bool:
    secret = _webhook_secret()
    if not secret:
        return True
    digest = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    clean  = signature.replace('sha256=', '').strip()
    return hmac.compare_digest(digest, clean)


def get_billing_dates(membership: dict) -> dict:
    from datetime import timezone

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


def _is_annual_membership(membership: dict) -> bool:
    start = membership.get('renewal_period_start')
    end   = membership.get('renewal_period_end')
    try:
        if start and end and (int(end) - int(start)) > 60 * 86400:
            return True
    except Exception:
        pass
    return False


def plan_from_membership(membership: dict, default: str = 'starter') -> str:
    product = membership.get('product')
    if isinstance(product, dict):
        name = (product.get('name') or product.get('title') or '').lower()
        for key in ('agency', 'business', 'pro', 'starter'):
            if key in name:
                return _PLAN_NAME_MAP.get(key, key)

    plan_id = membership.get('plan_id', '') or membership.get('plan', '')
    admin_map = _admin_plan_id_map()
    if plan_id and plan_id in admin_map:
        return admin_map[plan_id]

    sources = [
        membership.get('affiliate_page_url', ''),
        plan_id,
        membership.get('id', ''),
    ]
    for src in sources:
        s = (src or '').lower()
        for key in ('agency', 'business', 'pro', 'starter'):
            if key in s:
                return _PLAN_NAME_MAP.get(key, key)
    return default


def _admin_plan_id_map() -> dict:
    out: dict = {}
    for plan in ('starter', 'pro', 'agency'):
        for period in ('monthly', 'annual'):
            key = f'{plan}_{period}'
            for prefix in ('whop_plan_id_', 'whop_plan_id_dev_'):
                pid = _db_setting(f'{prefix}{key}', '').strip()
                if pid:
                    out[pid] = plan
    return out


def format_membership_for_ui(membership: dict) -> dict:
    if not membership:
        return {}
    status = (membership.get('status') or 'active').lower()
    valid  = bool(membership.get('valid'))
    dates  = get_billing_dates(membership)
    plan   = plan_from_membership(membership)
    annual = _is_annual_membership(membership)

    cancelled = status in ('cancelled', 'canceled') or bool(membership.get('cancel_at_period_end'))
    paused    = status == 'paused'

    return {
        'id':         membership.get('id', ''),
        'plan':       plan.title(),
        'status':     'active' if valid else status,
        'renews_at':  dates.get('period_end', ''),
        'portal_url': 'https://whop.com/hub/',
        'update_url': 'https://whop.com/hub/',
        'cancelled':  cancelled,
        'paused':     paused,
        'trial_ends': '',
        'annual':     annual,
        'card_brand': '',
        'card_last4': '',
    }
