from functools import wraps
from django.shortcuts import redirect
from .db import get_user_by_id

ADMIN_EMAILS = {'admin@permitlify.com', 'khemiri@permitlify.com'}

_ONBOARDING_EXEMPT_PATHS = {
    '/onboarding/',
    '/logout/',
    '/paywall/',
    '/billing/embed/',
    '/billing/success/',
    '/billing/sync/',
    '/billing/webhook/',
}


def login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user_id = request.session.get('user_id')
        if not user_id:
            return redirect('login')

        if request.path not in _ONBOARDING_EXEMPT_PATHS:
            user = get_user_by_id(user_id) or {}
            if not user.get('onboarding_complete'):
                return redirect('onboarding')

        return view_func(request, *args, **kwargs)
    return wrapper


def admin_required(view_func):
    """Require login + admin email. Use on every /admin-panel/* view."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user_id = request.session.get('user_id')
        if not user_id:
            return redirect('login')
        user = get_user_by_id(user_id) or {}
        email = (user.get('email') or request.session.get('user_email') or '').lower().strip()
        if email not in ADMIN_EMAILS:
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return wrapper


def subscription_required(view_func):
    """
    Soft-gate a view behind an active paid subscription.

    The previous behaviour was a hard redirect to ``/paywall/`` whenever
    ``subscription_active`` wasn't a literal True. That was harsh for
    brand-new signups: they could not even see what the dashboard looked
    like before being asked for money.

    New behaviour: let every logged-in user reach the page, but stamp
    ``request.is_locked = True`` when there is no active subscription.
    Views surface that flag through ``_user_ctx`` and templates show a
    "Preview Mode" banner with an Upgrade CTA instead of redirecting.
    The hard ``/paywall/`` page is still reachable via direct link
    (e.g. from the marketing site or after a payment failure).
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user_id = request.session.get('user_id')
        if not user_id:
            return redirect('login')
        user = get_user_by_id(user_id) or {}
        request.is_locked = not user.get('subscription_active')
        return view_func(request, *args, **kwargs)
    return wrapper
