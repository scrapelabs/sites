import os
import json
import hashlib
import hmac
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

LEMONSQUEEZY_API_KEY    = os.environ.get('LEMONSQUEEZY_API_KEY', '')
LEMONSQUEEZY_STORE_ID   = os.environ.get('LEMONSQUEEZY_STORE_ID', '339564')
LEMONSQUEEZY_STORE_SLUG = os.environ.get('LEMONSQUEEZY_STORE_SLUG', 'permitlify')
LEMONSQUEEZY_WEBHOOK_SECRET = os.environ.get('LEMONSQUEEZY_WEBHOOK_SECRET', '')

VARIANT_IDS = {
    'starter': os.environ.get('LEMONSQUEEZY_VARIANT_STARTER', '1553272'),
    'pro':     os.environ.get('LEMONSQUEEZY_VARIANT_PRO',     '1553293'),
    'agency':  os.environ.get('LEMONSQUEEZY_VARIANT_AGENCY',  '1553302'),
}

VARIANT_IDS_ANNUAL = {
    'starter': os.environ.get('LEMONSQUEEZY_VARIANT_STARTER_ANNUAL', '990125'),
    'pro':     os.environ.get('LEMONSQUEEZY_VARIANT_PRO_ANNUAL',     '990123'),
    'agency':  os.environ.get('LEMONSQUEEZY_VARIANT_AGENCY_ANNUAL',  '990116'),
}

# Combined map: variant_id -> plan name (both monthly and annual).
# Build separately so monthly and annual IDs are all included —
# merging dicts with the same key causes annual to overwrite monthly.
ALL_VARIANT_TO_PLAN: dict[str, str] = {}
for _plan, _vid in VARIANT_IDS.items():
    ALL_VARIANT_TO_PLAN[_vid] = _plan
for _plan, _vid in VARIANT_IDS_ANNUAL.items():
    ALL_VARIANT_TO_PLAN[_vid] = _plan

PLAN_PRICES          = {'starter': 7900,   'pro': 14900,  'agency': 34900}
PLAN_PRICES_ANNUAL   = {'starter': 75600,  'pro': 142800, 'agency': 334800}
PLAN_TRIAL_DAYS      = {'starter': 3,      'pro': 5,      'agency': 7}

BASE = 'https://api.lemonsqueezy.com/v1'


def _headers():
    return {
        'Authorization': f'Bearer {LEMONSQUEEZY_API_KEY}',
        'Accept':        'application/vnd.api+json',
        'Content-Type':  'application/vnd.api+json',
    }


def _get(path: str) -> dict:
    req = urllib.request.Request(f'{BASE}{path}', headers=_headers())
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'LS GET {path} -> {e.code}: {body}')


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f'{BASE}{path}', data=data, headers=_headers(), method='POST')
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'LS POST {path} -> {e.code}: {body}')


def _patch(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f'{BASE}{path}', data=data, headers=_headers(), method='PATCH')
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'LS PATCH {path} -> {e.code}: {body}')


def _delete(path: str) -> bool:
    req = urllib.request.Request(f'{BASE}{path}', headers=_headers(), method='DELETE')
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'LS DELETE {path} -> {e.code}: {body}')


def create_checkout(plan: str, email: str, user_id: int, success_url: str, cancel_url: str, period: str = 'monthly') -> str:
    id_map = VARIANT_IDS_ANNUAL if period == 'annual' else VARIANT_IDS
    variant_id = id_map.get(plan.lower())
    if not variant_id:
        raise ValueError(f'Unknown plan: {plan}')
    trial_days = PLAN_TRIAL_DAYS.get(plan.lower(), 3)
    payload = {
        'data': {
            'type': 'checkouts',
            'attributes': {
                'test_mode':       True,
                'checkout_options': {'button_color': '#1d4ed8'},
                'checkout_data': {
                    'email':              email,
                    'custom':             {'user_id': str(user_id)},
                    'trial_period_days':  trial_days,
                },
                'product_options': {
                    'redirect_url':    success_url,
                },
            },
            'relationships': {
                'store': {
                    'data': {'type': 'stores', 'id': str(LEMONSQUEEZY_STORE_ID)}
                },
                'variant': {
                    'data': {'type': 'variants', 'id': str(variant_id)}
                },
            },
        }
    }
    result = _post('/checkouts', payload)
    return result['data']['attributes']['url']


def get_customer_by_email(email: str) -> dict | None:
    try:
        params = urllib.parse.urlencode({
            'filter[store_id]': LEMONSQUEEZY_STORE_ID,
            'filter[email]':    email,
        })
        data = _get(f'/customers?{params}')
        customers = data.get('data', [])
        return customers[0] if customers else None
    except Exception:
        return None


def get_subscriptions_for_customer(customer_id: str) -> list:
    """Legacy – customer_id filter not supported by LS API; use get_subscriptions_by_email."""
    return get_subscriptions_by_email_from_store(customer_id=str(customer_id))


def get_subscriptions_by_email_from_store(email: str = '', customer_id: str = '') -> list:
    """
    Fetch all store subscriptions and filter client-side by email or customer_id.
    LS v1 does not support filter[customer_id] on the subscriptions endpoint.
    """
    try:
        params = urllib.parse.urlencode({'filter[store_id]': LEMONSQUEEZY_STORE_ID})
        data = _get(f'/subscriptions?{params}')
        subs = data.get('data', [])
        result = []
        for s in subs:
            a = s.get('attributes', {})
            if email and a.get('user_email', '').lower() == email.lower():
                result.append(s)
            elif customer_id and str(a.get('customer_id', '')) == str(customer_id):
                result.append(s)
        # Sort newest first
        result.sort(key=lambda x: x.get('attributes', {}).get('created_at', ''), reverse=True)
        return result
    except Exception:
        return []


def get_subscription(subscription_id: str) -> dict | None:
    try:
        data = _get(f'/subscriptions/{subscription_id}')
        return data.get('data')
    except Exception:
        return None


def get_customer_portal_url(subscription_id: str) -> str | None:
    try:
        sub = get_subscription(subscription_id)
        if sub:
            return sub['attributes'].get('urls', {}).get('customer_portal')
        return None
    except Exception:
        return None


def update_subscription_plan(subscription_id: str, new_plan: str,
                              period: str = 'monthly',
                              new_trial_ends_at: str = None) -> dict:
    id_map     = VARIANT_IDS_ANNUAL if period == 'annual' else VARIANT_IDS
    variant_id = id_map.get(new_plan.lower())
    if not variant_id:
        raise ValueError(f'Unknown plan: {new_plan}')
    attrs = {
        'variant_id':          int(variant_id),
        'invoice_immediately': True,
    }
    if new_trial_ends_at:
        attrs['trial_ends_at'] = new_trial_ends_at
    payload = {
        'data': {
            'type': 'subscriptions',
            'id':   str(subscription_id),
            'attributes': attrs,
        }
    }
    return _patch(f'/subscriptions/{subscription_id}', payload)


def cancel_subscription(subscription_id: str) -> dict:
    return _delete(f'/subscriptions/{subscription_id}')


def resume_subscription(subscription_id: str) -> dict:
    payload = {
        'data': {
            'type': 'subscriptions',
            'id':   str(subscription_id),
            'attributes': {'cancelled': False},
        }
    }
    return _patch(f'/subscriptions/{subscription_id}', payload)


def pause_subscription(subscription_id: str) -> dict:
    payload = {
        'data': {
            'type': 'subscriptions',
            'id':   str(subscription_id),
            'attributes': {'pause': {'mode': 'void'}},
        }
    }
    return _patch(f'/subscriptions/{subscription_id}', payload)


def get_invoices_for_subscription(subscription_id: str) -> list:
    try:
        data = _get(f'/subscription-invoices?filter[subscription_id]={subscription_id}')
        return data.get('data', [])
    except Exception:
        return []


def verify_webhook_signature(payload_bytes: bytes, signature: str) -> bool:
    secret = LEMONSQUEEZY_WEBHOOK_SECRET
    if not secret:
        return True
    digest = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


def correct_trial_end_if_wrong(sub: dict) -> dict:
    """
    Dynamically recalculate the correct trial_ends_at as
        created_at  +  PLAN_TRIAL_DAYS[plan]
    and PATCH the LS subscription if the stored value differs.
    Returns the (possibly updated) subscription dict.
    Nothing is hardcoded — everything is derived from LS-stored created_at.
    """
    try:
        a        = sub.get('attributes', {})
        status   = a.get('status', '')
        if status not in ('active', 'on_trial'):
            return sub   # only touch live subscriptions

        vid   = str(a.get('variant_id', ''))
        plan  = ALL_VARIANT_TO_PLAN.get(vid)
        if not plan:
            return sub   # unknown plan — don't touch

        trial_days = PLAN_TRIAL_DAYS.get(plan)
        if not trial_days:
            return sub

        created_raw = a.get('created_at', '')
        if not created_raw:
            return sub

        created_dt  = datetime.fromisoformat(created_raw.replace('Z', '+00:00'))
        correct_end = created_dt + timedelta(days=trial_days)
        correct_str = correct_end.strftime('%Y-%m-%dT%H:%M:%S+00:00')

        current_raw = a.get('trial_ends_at', '') or ''
        if current_raw:
            try:
                current_dt = datetime.fromisoformat(current_raw.replace('Z', '+00:00'))
                # Allow ±1 second tolerance; anything else is a mismatch
                if abs((correct_end - current_dt).total_seconds()) < 2:
                    return sub   # already correct — nothing to do
            except Exception:
                pass

        # Mismatch detected — patch LS with the dynamically calculated date
        payload = {
            'data': {
                'type': 'subscriptions',
                'id':   str(sub['id']),
                'attributes': {'trial_ends_at': correct_str},
            }
        }
        updated = _patch(f'/subscriptions/{sub["id"]}', payload)
        return updated.get('data', sub)
    except Exception:
        return sub   # best-effort; never break the caller


def format_subscription_for_ui(sub: dict) -> dict:
    a   = sub.get('attributes', {})
    vid = str(a.get('variant_id', ''))
    plan = ALL_VARIANT_TO_PLAN.get(vid, 'starter').title()
    # Detect annual billing
    annual = vid in VARIANT_IDS_ANNUAL.values()
    renews = a.get('renews_at') or a.get('ends_at') or ''
    if renews:
        try:
            renews = datetime.fromisoformat(renews.replace('Z', '+00:00')).strftime('%b %d, %Y')
        except Exception:
            pass
    # Format trial end date
    trial_ends = a.get('trial_ends_at', '') or ''
    if trial_ends:
        try:
            trial_ends = datetime.fromisoformat(trial_ends.replace('Z', '+00:00')).strftime('%b %d, %Y')
        except Exception:
            pass
    return {
        'id':          sub['id'],
        'plan':        plan,
        'status':      a.get('status', 'active'),
        'renews_at':   renews,
        'portal_url':  a.get('urls', {}).get('customer_portal', ''),
        'update_url':  a.get('urls', {}).get('update_payment_method', ''),
        'cancelled':   a.get('cancelled', False),
        'paused':      bool(a.get('pause')),
        'trial_ends':  trial_ends,
        'annual':      annual,
        'card_brand':  a.get('card_brand', ''),
        'card_last4':  a.get('card_last_four', ''),
    }
