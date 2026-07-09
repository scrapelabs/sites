import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timezone as dt_timezone, timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.html import escape
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .forms import AdminReplyForm, LoginCodeForm, LoginForm, RegisterForm, SupportForm
from .models import BlogPost, CheckoutConsent, EmailLoginCode, Invoice, ProxyCredential, Purchase, SupportMessage, UserProfile
from . import proxy_access

log = logging.getLogger(__name__)

SUPER_ADMIN_EMAIL = getattr(settings, 'SUPER_ADMIN_EMAIL', 'khemiri.mohamed.ensi@gmail.com')

PROXY_PLANS = [
    {'id': 'res-starter',  'name': 'Starter',      'type': 'residential', 'price': 9.99,  'gb': 5,   'threads': 100},
    {'id': 'res-pro',      'name': 'Professional', 'type': 'residential', 'price': 29.99, 'gb': 20,  'threads': 500},
    {'id': 'res-biz',      'name': 'Business',     'type': 'residential', 'price': 79.99, 'gb': 80,  'threads': 2000},
    {'id': 'res-ent',      'name': 'Enterprise',   'type': 'residential', 'price': 199.99,'gb': 250, 'threads': 10000},
    {'id': 'dc-starter',   'name': 'Starter',      'type': 'datacenter',  'price': 7.99,  'ips': 25,   'threads': 50},
    {'id': 'dc-pro',       'name': 'Professional', 'type': 'datacenter',  'price': 19.99, 'ips': 100,  'threads': 200},
    {'id': 'dc-biz',       'name': 'Business',     'type': 'datacenter',  'price': 49.99, 'ips': 500,  'threads': 1000},
    {'id': 'ipv6-starter', 'name': 'Starter',      'type': 'ipv6',        'price': 4.99,  'ips': 500,   'threads': 100},
    {'id': 'ipv6-pro',     'name': 'Professional', 'type': 'ipv6',        'price': 14.99, 'ips': 5000,  'threads': 500},
    {'id': 'ipv6-biz',     'name': 'Business',     'type': 'ipv6',        'price': 39.99, 'ips': 50000, 'threads': 2000},
]

WHOP_PLAN_FEATURES = {
    'starter': {
        'label': 'Starter', 'color': 'gray',
        'residential_gb': 5, 'datacenter_ips': 25, 'ipv6_ips': 500,
        'threads': 100, 'countries': 20,
    },
    'pro': {
        'label': 'Pro', 'color': 'gold',
        'residential_gb': 25, 'datacenter_ips': 250, 'ipv6_ips': 5000,
        'threads': 1000, 'countries': 50,
    },
    'agency': {
        'label': 'Business', 'color': 'purple',
        'residential_gb': 100, 'datacenter_ips': 2000, 'ipv6_ips': 50000,
        'threads': 10000, 'countries': 195,
    },
}


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if request.user.email != SUPER_ADMIN_EMAIL:
            raise Http404
        return view_func(request, *args, **kwargs)
    return wrapper


def _get_or_create_profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def _membership_has_active_access(membership: dict) -> bool:
    status = (membership.get('status') or '').lower()
    return bool(membership.get('valid')) or status in ('active', 'trialing', 'completed')


def _membership_metadata(membership: dict) -> dict:
    metadata = membership.get('metadata') or membership.get('custom_metadata') or {}
    return metadata if isinstance(metadata, dict) else {}


def _membership_metadata_value(membership: dict, key: str) -> str:
    metadata = _membership_metadata(membership)
    return str(metadata.get(key) or metadata.get(f'metadata[{key}]') or '').strip()


def _membership_email(membership: dict) -> str:
    user_obj = membership.get('user') or {}
    user_email = user_obj.get('email') if isinstance(user_obj, dict) else ''
    return (membership.get('email') or membership.get('user_email') or user_email or '').strip().lower()


def _membership_belongs_to_user(membership: dict, user: User, consent_id: str = '') -> bool:
    if not membership or not user:
        return False
    mem_id = str(membership.get('id') or '').strip()
    try:
        profile = _get_or_create_profile(user)
        if mem_id and profile.whop_membership_id == mem_id:
            return True
    except Exception:
        pass

    metadata_user_id = _membership_metadata_value(membership, 'user_id')
    if metadata_user_id and constant_time_compare(metadata_user_id, str(user.pk)):
        return True

    membership_consent_id = _membership_metadata_value(membership, 'consent_id')
    if consent_id and membership_consent_id and constant_time_compare(str(consent_id), membership_consent_id):
        try:
            if str(consent_id).isdigit() and CheckoutConsent.objects.filter(pk=consent_id, user=user).exists():
                return True
        except (TypeError, ValueError):
            pass

    email = _membership_email(membership)
    return bool(email and user.email and constant_time_compare(email, user.email.strip().lower()))


# ── Public views ──────────────────────────────────────────────────────────

def home(request):
    recent_posts = BlogPost.objects.filter(status='published').order_by('-published_at')[:3]
    return render(request, 'home.html', {'recent_posts': recent_posts})


def pricing(request):
    from . import whop as wp
    pricing_data = wp.get_pricing_dict()
    residential = [p for p in PROXY_PLANS if p['type'] == 'residential']
    datacenter  = [p for p in PROXY_PLANS if p['type'] == 'datacenter']
    ipv6        = [p for p in PROXY_PLANS if p['type'] == 'ipv6']
    return render(request, 'pricing.html', {
        'residential': residential,
        'datacenter': datacenter,
        'ipv6': ipv6,
        'pricing': pricing_data,
    })


def contact(request):
    if request.method == 'POST':
        messages.success(request, 'Your message has been sent! We\'ll get back to you shortly.')
        return redirect('contact')
    return render(request, 'contact.html')


LOGIN_CODE_TTL_MINUTES = 10
LOGIN_CODE_MAX_ATTEMPTS = 5
LOGIN_CODE_RESEND_COOLDOWN_SECONDS = 60
LOGIN_CODE_MAX_SENDS_PER_HOUR = 5
LOGIN_CODE_MAX_IP_SENDS_PER_HOUR = 20
GOOGLE_OAUTH_STATE_TTL_SECONDS = 10 * 60
GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'


def _google_oauth_config() -> dict:
    from . import whop as wp
    return {
        'client_id': wp._db_setting('google_oauth_client_id', '') or os.environ.get('GOOGLE_OAUTH_CLIENT_ID', ''),
        'client_secret': wp._db_setting('google_oauth_client_secret', '') or os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', ''),
        'enabled': wp._db_setting('google_oauth_enabled', '0') == '1',
    }


def _google_auto_origin(request) -> str:
    return request.build_absolute_uri('/').rstrip('/')


def _google_effective_origin(request) -> str:
    from . import whop as wp
    return (wp._db_setting('google_authorized_origin', '') or _google_auto_origin(request)).rstrip('/')


def _login_code_hash(code: str) -> str:
    return salted_hmac('goldenproxies.email_login_code', code).hexdigest()


def _safe_next_url(request, raw_next: str = '') -> str:
    if raw_next and url_has_allowed_host_and_scheme(
        raw_next,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return raw_next
    return 'dashboard'


def _google_redirect_uri(request) -> str:
    from . import whop as wp
    return wp._db_setting('google_redirect_uri', '') or request.build_absolute_uri('/auth/google/callback/')


def _google_oauth_available() -> bool:
    cfg = _google_oauth_config()
    return bool(cfg['enabled'] and cfg['client_id'] and cfg['client_secret'])


def _login_email_codes_configured() -> bool:
    from . import whop as wp
    return bool(wp._db_setting('resend_api_key', ''))


def _pending_login_key(user_id: int, code_id: int) -> str:
    return salted_hmac('goldenproxies.pending_login', f'{user_id}:{code_id}').hexdigest()


def _set_pending_login_session(request, user: User, code: EmailLoginCode, next_url: str = '') -> None:
    request.session['pending_login_user_id'] = user.pk
    request.session['pending_login_code_id'] = code.pk
    request.session['pending_login_next'] = next_url or 'dashboard'
    request.session['pending_login_key'] = _pending_login_key(user.pk, code.pk)
    request.session.set_expiry(LOGIN_CODE_TTL_MINUTES * 60)


def _clear_pending_login_session(request) -> None:
    for key in ('pending_login_user_id', 'pending_login_code_id', 'pending_login_next', 'pending_login_key'):
        request.session.pop(key, None)
    request.session.set_expiry(None)


def _pending_login(request):
    user_id = request.session.get('pending_login_user_id')
    code_id = request.session.get('pending_login_code_id')
    pending_key = request.session.get('pending_login_key', '')
    if not user_id or not code_id or not pending_key:
        return None, None
    if not constant_time_compare(pending_key, _pending_login_key(user_id, code_id)):
        _clear_pending_login_session(request)
        return None, None
    user = User.objects.filter(pk=user_id, is_active=True).first()
    code = EmailLoginCode.objects.filter(pk=code_id, user_id=user_id).first()
    if not user or not code:
        _clear_pending_login_session(request)
        return None, None
    return user, code


def _send_login_code(request, user: User, next_url: str = '') -> tuple[bool, str]:
    email = (user.email or '').strip()
    if not email:
        return False, 'This account does not have an email address.'

    now = timezone.now()
    since_hour = now - timedelta(hours=1)
    latest = EmailLoginCode.objects.filter(user=user).order_by('-created_at').first()
    if latest and (now - latest.created_at).total_seconds() < LOGIN_CODE_RESEND_COOLDOWN_SECONDS:
        return False, 'Please wait a minute before requesting another login code.'
    if EmailLoginCode.objects.filter(user=user, created_at__gte=since_hour).count() >= LOGIN_CODE_MAX_SENDS_PER_HOUR:
        return False, 'Too many login codes were requested. Please try again later.'
    ip_addr = proxy_access.client_ip(request)
    if ip_addr and EmailLoginCode.objects.filter(ip_address=ip_addr, created_at__gte=since_hour).count() >= LOGIN_CODE_MAX_IP_SENDS_PER_HOUR:
        return False, 'Too many login codes were requested from this network. Please try again later.'

    EmailLoginCode.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
    raw_code = f'{secrets.randbelow(1000000):06d}'
    code = EmailLoginCode.objects.create(
        user=user,
        code_hash=_login_code_hash(raw_code),
        expires_at=timezone.now() + timedelta(minutes=LOGIN_CODE_TTL_MINUTES),
        sent_to=email,
        ip_address=ip_addr,
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:2000],
    )
    subject = 'Your GoldenProxies login code'
    body = (
        f'Your GoldenProxies login code is {raw_code}.\n\n'
        f'This code expires in {LOGIN_CODE_TTL_MINUTES} minutes. If you did not try to sign in, ignore this email.'
    )
    html = (
        '<p>Your GoldenProxies login code is:</p>'
        f'<p style="font-size:28px;font-weight:700;letter-spacing:6px;margin:16px 0;">{raw_code}</p>'
        f'<p>This code expires in {LOGIN_CODE_TTL_MINUTES} minutes. If you did not try to sign in, ignore this email.</p>'
    )
    ok, info = _send_resend_email(email, subject, body, html_body=html, service='system')
    if not ok:
        code.used_at = timezone.now()
        code.save(update_fields=['used_at'])
        return False, info
    _set_pending_login_session(request, user, code, next_url)
    return True, email


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        next_url = _safe_next_url(request, request.GET.get('next', 'dashboard'))
        if not _login_email_codes_configured():
            login(request, form.get_user())
            messages.warning(request, 'Email login codes are disabled until Resend is configured.')
            return redirect(next_url)
        ok, info = _send_login_code(request, form.get_user(), next_url)
        if ok:
            messages.success(request, f'We sent a verification code to {info}.')
            return redirect('login_verify')
        form.add_error(None, f'Could not send verification email: {info}')
    return render(request, 'auth/login.html', {
        'form': form,
        'google_oauth_available': _google_oauth_available(),
    })


def login_verify(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    user, code = _pending_login(request)
    if not user or not code:
        messages.error(request, 'Your login verification expired. Please sign in again.')
        return redirect('login')

    form = LoginCodeForm(request.POST or None)
    if request.method == 'POST':
        action = request.POST.get('action', 'verify')
        if action == 'resend':
            ok, info = _send_login_code(request, user, _safe_next_url(request, request.session.get('pending_login_next', 'dashboard')))
            if ok:
                messages.success(request, f'A new verification code was sent to {info}.')
            else:
                messages.error(request, f'Could not resend verification email: {info}')
            return redirect('login_verify')

        if form.is_valid():
            code.refresh_from_db()
            if code.used_at or code.expires_at <= timezone.now():
                _clear_pending_login_session(request)
                messages.error(request, 'Your login verification expired. Please sign in again.')
                return redirect('login')
            if code.attempts >= LOGIN_CODE_MAX_ATTEMPTS:
                _clear_pending_login_session(request)
                messages.error(request, 'Too many incorrect codes. Please sign in again.')
                return redirect('login')

            submitted = form.cleaned_data['code']
            if constant_time_compare(code.code_hash, _login_code_hash(submitted)):
                code.used_at = timezone.now()
                code.save(update_fields=['used_at'])
                next_url = _safe_next_url(request, request.session.get('pending_login_next') or 'dashboard')
                _clear_pending_login_session(request)
                login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                return redirect(next_url)

            code.attempts += 1
            code.save(update_fields=['attempts'])
            form.add_error('code', 'Incorrect code. Check your email and try again.')

    return render(request, 'auth/login_verify.html', {
        'form': form,
        'email': user.email,
        'expires_at': code.expires_at,
    })


def google_login_start(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    cfg = _google_oauth_config()
    if not cfg['enabled']:
        messages.error(request, 'Google sign-in is currently disabled.')
        return redirect('login')
    if not cfg['client_id'] or not cfg['client_secret']:
        messages.error(request, 'Google sign-in is not configured yet.')
        return redirect('login')

    state = secrets.token_urlsafe(32)
    request.session['google_oauth_state'] = state
    request.session['google_oauth_started_at'] = int(time.time())
    request.session['google_oauth_next'] = _safe_next_url(request, request.GET.get('next', 'dashboard'))
    params = urllib.parse.urlencode({
        'client_id': cfg['client_id'],
        'redirect_uri': _google_redirect_uri(request),
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'prompt': 'select_account',
    })
    return redirect(f'{GOOGLE_AUTH_URL}?{params}')


def google_login_callback(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    expected_state = request.session.get('google_oauth_state', '')
    received_state = request.GET.get('state', '')
    started_at = int(request.session.get('google_oauth_started_at') or 0)
    if (
        not expected_state
        or not received_state
        or not constant_time_compare(expected_state, received_state)
        or not started_at
        or int(time.time()) - started_at > GOOGLE_OAUTH_STATE_TTL_SECONDS
    ):
        messages.error(request, 'Google sign-in expired. Please try again.')
        return redirect('login')

    code = request.GET.get('code', '')
    if not code:
        messages.error(request, 'Google sign-in was cancelled or failed.')
        return redirect('login')

    cfg = _google_oauth_config()
    try:
        import requests as _req
        token_resp = _req.post(
            GOOGLE_TOKEN_URL,
            data={
                'code': code,
                'client_id': cfg['client_id'],
                'client_secret': cfg['client_secret'],
                'redirect_uri': _google_redirect_uri(request),
                'grant_type': 'authorization_code',
            },
            timeout=20,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get('access_token', '')
        if not access_token:
            raise RuntimeError('Google did not return an access token')

        userinfo_resp = _req.get(
            GOOGLE_USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=20,
        )
        userinfo_resp.raise_for_status()
        info = userinfo_resp.json()
    except Exception:
        log.exception('Google sign-in failed')
        messages.error(request, 'Google sign-in failed. Please try again.')
        return redirect('login')

    email = (info.get('email') or '').strip().lower()
    if not email or not info.get('email_verified'):
        messages.error(request, 'Google did not confirm a verified Gmail address.')
        return redirect('login')

    user = User.objects.filter(email__iexact=email).first() or User.objects.filter(username__iexact=email).first()
    if user and not user.is_active:
        messages.error(request, 'This account is suspended. Contact support.')
        return redirect('login')
    if not user:
        user = User.objects.create_user(
            username=email,
            email=email,
            first_name=(info.get('given_name') or '')[:150],
            last_name=(info.get('family_name') or '')[:150],
        )
    else:
        changed = []
        if not user.email:
            user.email = email
            changed.append('email')
        if info.get('given_name') and not user.first_name:
            user.first_name = info.get('given_name', '')[:150]
            changed.append('first_name')
        if info.get('family_name') and not user.last_name:
            user.last_name = info.get('family_name', '')[:150]
            changed.append('last_name')
        if changed:
            user.save(update_fields=changed)

    next_url = _safe_next_url(request, request.session.get('google_oauth_next', 'dashboard'))
    for key in ('google_oauth_state', 'google_oauth_started_at', 'google_oauth_next'):
        request.session.pop(key, None)
    _get_or_create_profile(user)
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    return redirect(next_url)


def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            user = form.save()
        except IntegrityError:
            form.add_error('email', 'An account with this email already exists. Please sign in instead.')
        else:
            if not _login_email_codes_configured():
                login(request, user)
                messages.warning(request, 'Email login codes are disabled until Resend is configured.')
                return redirect('dashboard')
            ok, info = _send_login_code(request, user, 'dashboard')
            if ok:
                messages.success(request, f'Account created. We sent a verification code to {info}.')
                return redirect('login_verify')
            user.delete()
            form.add_error(None, f'Could not send verification email: {info}')
    return render(request, 'auth/register.html', {
        'form': form,
        'google_oauth_available': _google_oauth_available(),
    })


def logout_view(request):
    logout(request)
    return redirect('home')


# ── Dashboard views ────────────────────────────────────────────────────────

@login_required
def dashboard_overview(request):
    profile    = _get_or_create_profile(request.user)
    active_purchases = Purchase.objects.filter(user=request.user, status='active')
    is_admin   = request.user.email == SUPER_ADMIN_EMAIL
    invoices   = Invoice.objects.filter(user=request.user)[:5]
    return render(request, 'dashboard/overview.html', {
        'profile': profile,
        'active_purchases': active_purchases,
        'invoices': invoices,
        'is_admin': is_admin,
        'features': WHOP_PLAN_FEATURES.get(profile.plan, WHOP_PLAN_FEATURES.get('free', {})),
    })


@login_required
def dashboard_generator(request):
    profile = _get_or_create_profile(request.user)
    protocol = request.POST.get('protocol', 'http') if request.method == 'POST' else 'http'
    fmt = request.POST.get('format', 'ip_port_user_pass') if request.method == 'POST' else 'ip_port_user_pass'
    if protocol not in ('http', 'socks5'):
        protocol = 'http'
    if fmt not in ('ip_port_user_pass', 'user_pass_at_ip_port', 'url', 'ip_port'):
        fmt = 'ip_port_user_pass'

    credential = None
    connection_line = ''
    if profile.has_active_subscription:
        credential = proxy_access.ensure_proxy_credential(profile)
        connection_line = proxy_access.connection_line(credential, protocol, fmt)
    elif request.method == 'POST':
        messages.error(request, 'An active paid subscription is required before proxy credentials are issued.')

    is_admin = request.user.email == SUPER_ADMIN_EMAIL
    resp = render(request, 'dashboard/generator.html', {
        'credential': credential,
        'connection_line': connection_line,
        'proxy_gateway': proxy_access.gateway_config(),
        'protocol': protocol,
        'fmt': fmt,
        'is_admin': is_admin,
        'profile': profile,
    })
    resp['Cache-Control'] = 'no-store'
    return resp


@login_required
def dashboard_stats(request):
    profile   = _get_or_create_profile(request.user)
    purchases = Purchase.objects.filter(user=request.user)
    is_admin  = request.user.email == SUPER_ADMIN_EMAIL
    return render(request, 'dashboard/stats.html', {
        'profile': profile, 'purchases': purchases, 'is_admin': is_admin,
    })


@login_required
def dashboard_support(request):
    form = SupportForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        ticket = form.save(commit=False)
        ticket.user = request.user
        ticket.save()
        messages.success(request, 'Ticket submitted successfully.')
        return redirect('dashboard_support_detail', ticket_id=ticket.pk)
    my_tickets = SupportMessage.objects.filter(user=request.user)
    is_admin   = request.user.email == SUPER_ADMIN_EMAIL
    return render(request, 'dashboard/support.html', {
        'form': form, 'my_tickets': my_tickets, 'is_admin': is_admin,
    })


@login_required
def dashboard_support_detail(request, ticket_id):
    ticket   = get_object_or_404(SupportMessage, pk=ticket_id, user=request.user)
    is_admin = request.user.email == SUPER_ADMIN_EMAIL
    return render(request, 'dashboard/support_detail.html', {
        'ticket': ticket, 'is_admin': is_admin,
    })


@login_required
def dashboard_support_edit(request, ticket_id):
    ticket = get_object_or_404(SupportMessage, pk=ticket_id, user=request.user)
    form   = SupportForm(request.POST or None, instance=ticket)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Ticket updated.')
        return redirect('dashboard_support_detail', ticket_id=ticket_id)
    is_admin = request.user.email == SUPER_ADMIN_EMAIL
    return render(request, 'dashboard/support_detail.html', {
        'ticket': ticket, 'form': form, 'edit_mode': True, 'is_admin': is_admin,
    })


@login_required
def dashboard_support_delete(request, ticket_id):
    ticket = get_object_or_404(SupportMessage, pk=ticket_id, user=request.user)
    if request.method == 'POST':
        ticket.delete()
        messages.success(request, 'Ticket deleted.')
        return redirect('dashboard_support')
    return redirect('dashboard_support_detail', ticket_id=ticket_id)


@login_required
def dashboard_settings(request):
    profile  = _get_or_create_profile(request.user)
    if request.method == 'POST':
        action = request.POST.get('action', 'update_profile')
        if action == 'update_profile':
            user            = request.user
            user.first_name = request.POST.get('first_name', user.first_name)
            user.last_name  = request.POST.get('last_name', user.last_name)
            user.save()
            messages.success(request, 'Profile saved.')
        return redirect('dashboard_settings')
    is_admin = request.user.email == SUPER_ADMIN_EMAIL
    return render(request, 'dashboard/settings.html', {
        'is_admin': is_admin, 'profile': profile,
    })


@login_required
def change_password(request):
    if request.method != 'POST':
        return redirect('dashboard_settings')
    from django.contrib.auth import update_session_auth_hash
    old_password     = request.POST.get('old_password', '').strip()
    new_password     = request.POST.get('new_password', '').strip()
    confirm_password = request.POST.get('confirm_password', '').strip()
    user             = request.user
    if not user.check_password(old_password):
        messages.error(request, 'Current password is incorrect.')
        return redirect('dashboard_settings')
    if len(new_password) < 8:
        messages.error(request, 'New password must be at least 8 characters.')
        return redirect('dashboard_settings')
    if new_password != confirm_password:
        messages.error(request, 'Passwords do not match.')
        return redirect('dashboard_settings')
    user.set_password(new_password)
    user.save()
    update_session_auth_hash(request, user)
    messages.success(request, 'Password changed successfully.')
    return redirect('dashboard_settings')


@login_required
def generate_api_key(request):
    if request.method != 'POST':
        return redirect('dashboard_settings')
    import secrets
    profile = _get_or_create_profile(request.user)
    profile.proxy_api_key = secrets.token_hex(32)
    profile.save()
    messages.success(request, 'New API key generated.')
    return redirect('dashboard_settings')


def _email_proxy_credentials_if_needed(user: User, credential: ProxyCredential | None) -> None:
    if not credential or credential.credentials_emailed_at or not credential.is_active:
        return
    if not user.email:
        log.warning('proxy credential email skipped for user %s: no email', user.pk)
        return

    gateway = proxy_access.gateway_config()
    http_line = proxy_access.connection_line(credential, 'http', 'ip_port_user_pass')
    socks_line = proxy_access.connection_line(credential, 'socks5', 'url')
    subject = 'Your GoldenProxies proxy login is ready'
    body = (
        'Your GoldenProxies proxy login is ready.\n\n'
        f'Host: {gateway["host"]}\n'
        f'HTTP port: {gateway["http_port"]}\n'
        f'SOCKS5 port: {gateway["socks5_port"]}\n'
        f'Username: {credential.username}\n'
        f'Password: {credential.password}\n\n'
        f'HTTP format: {http_line}\n'
        f'SOCKS5 URL: {socks_line}\n\n'
        'You can also view these credentials in your GoldenProxies dashboard under Proxy Access.'
    )
    html = (
        '<p>Your GoldenProxies proxy login is ready.</p>'
        '<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;">'
        f'<tr><td><strong>Host</strong></td><td><code>{escape(gateway["host"])}</code></td></tr>'
        f'<tr><td><strong>HTTP port</strong></td><td><code>{escape(gateway["http_port"])}</code></td></tr>'
        f'<tr><td><strong>SOCKS5 port</strong></td><td><code>{escape(gateway["socks5_port"])}</code></td></tr>'
        f'<tr><td><strong>Username</strong></td><td><code>{escape(credential.username)}</code></td></tr>'
        f'<tr><td><strong>Password</strong></td><td><code>{escape(credential.password)}</code></td></tr>'
        '</table>'
        f'<p><strong>HTTP format:</strong><br><code>{escape(http_line)}</code></p>'
        f'<p><strong>SOCKS5 URL:</strong><br><code>{escape(socks_line)}</code></p>'
        '<p>You can also view these credentials in your GoldenProxies dashboard under Proxy Access.</p>'
    )
    ok, info = _send_resend_email(user.email, subject, body, html_body=html, service='billing')
    if ok:
        credential.credentials_emailed_at = timezone.now()
        credential.save(update_fields=['credentials_emailed_at'])
        return
    log.warning('proxy credential email failed for user %s: %s', user.pk, info)


@login_required
def close_account(request):
    if request.method != 'POST':
        return redirect('dashboard_settings')
    user = request.user
    if user.email == SUPER_ADMIN_EMAIL:
        messages.error(request, 'Cannot close the super admin account.')
        return redirect('dashboard_settings')
    logout(request)
    user.delete()
    return redirect('home')


# ── Billing views ──────────────────────────────────────────────────────────

@login_required
def billing_dashboard(request):
    profile  = _get_or_create_profile(request.user)
    invoices = Invoice.objects.filter(user=request.user).order_by('-created_at')
    is_admin = request.user.email == SUPER_ADMIN_EMAIL

    from . import whop as wp
    pricing_data = wp.get_pricing_dict()

    whop_info = {}
    if profile.whop_membership_id:
        whop_info = {
            'id':         profile.whop_membership_id,
            'plan':       profile.plan_display,
            'status':     profile.whop_status or 'active',
            'renews_at':  profile.billing_period_end or profile.whop_renews_at,
            'portal_url': 'https://whop.com/hub/',
            'cancelled':  profile.whop_cancelled,
            'paused':     profile.whop_paused,
        }

    features = WHOP_PLAN_FEATURES.get(profile.plan, {})

    resp = render(request, 'dashboard/billing.html', {
        'profile':   profile,
        'invoices':  invoices,
        'whop_info': whop_info,
        'pricing':   pricing_data,
        'features':  features,
        'is_admin':  is_admin,
        'plans':     WHOP_PLAN_FEATURES,
        'checkout_terms_text': proxy_access.CHECKOUT_TERMS_TEXT,
        'checkout_terms_version': proxy_access.CHECKOUT_TERMS_VERSION,
    })
    resp['Cache-Control'] = 'no-store'
    return resp


@login_required
def billing_checkout(request, plan, period='monthly'):
    VALID_PLANS   = ('starter', 'pro', 'agency')
    VALID_PERIODS = ('monthly', 'annual')
    if plan not in VALID_PLANS or period not in VALID_PERIODS:
        messages.error(request, 'Invalid plan or billing period.')
        return redirect('billing_dashboard')
    if request.method != 'POST':
        messages.error(request, 'Please review and accept the digital service terms before checkout.')
        return redirect('billing_dashboard')
    if request.POST.get('accept_terms') != '1':
        messages.error(request, 'You must accept the digital service terms before checkout.')
        return redirect('billing_dashboard')

    from . import whop as wp
    consent = proxy_access.create_checkout_consent(request, plan, period)
    success_url = request.build_absolute_uri(
        f'/billing/success/?plan={plan}&period={period}&consent_id={consent.pk}'
    )

    try:
        checkout_url = wp.create_checkout_url(
            plan=plan,
            email=request.user.email,
            user_id=request.user.pk,
            success_url=success_url,
            period=period,
            metadata={
                'consent_id': consent.pk,
                'terms_version': consent.terms_version,
                'period': period,
            },
        )
        consent.checkout_url = checkout_url
        consent.save(update_fields=['checkout_url'])
        return redirect(checkout_url)
    except ValueError as e:
        messages.error(request, f'Checkout not configured yet: {e}')
        return redirect('billing_dashboard')
    except Exception as e:
        log.exception('billing_checkout error for user %s', request.user.pk)
        messages.error(request, 'Could not start checkout. Please try again.')
        return redirect('billing_dashboard')


@login_required
def billing_success(request):
    membership_id = request.GET.get('membership_id', '').strip()
    plan_hint     = request.GET.get('plan', '').strip().lower()
    consent_id    = request.GET.get('consent_id', '').strip()
    profile       = _get_or_create_profile(request.user)
    credential    = None

    if not membership_id:
        messages.warning(request, 'Payment received! Your subscription will be activated shortly.')
        return redirect('billing_dashboard')

    from . import whop as wp
    try:
        membership = wp.get_membership(membership_id, timeout=10)
    except Exception:
        membership = None

    if membership and _membership_has_active_access(membership) and _membership_belongs_to_user(membership, request.user, consent_id):
        detected_plan = wp.plan_from_membership(membership, default=plan_hint or 'starter')
        final_plan    = detected_plan or plan_hint or 'starter'
        ui            = wp.format_membership_for_ui(membership)
        dates         = wp.get_billing_dates(membership)

        profile.plan                 = final_plan
        profile.whop_membership_id   = membership_id
        profile.whop_status          = ui.get('status', 'active')
        profile.whop_renews_at       = ui.get('renews_at', '')
        profile.whop_cancelled       = False
        profile.whop_paused          = False
        profile.billing_period_start = dates.get('period_start', '')
        profile.billing_period_end   = dates.get('period_end', '')
        profile.subscription_start   = dates.get('subscription_start', '')
        profile.last_whop_sync_at    = timezone.now()
        profile.save()

        _upsert_invoice_from_membership(request.user, final_plan, membership)
        proxy_access.attach_consent_to_membership(request.user, membership_id, consent_id)
        credential = proxy_access.sync_proxy_access(profile, reason='billing success')
        _email_proxy_credentials_if_needed(request.user, credential)

        messages.success(request, f'Welcome to GoldenProxies {profile.plan_display}! Your subscription is now active.')
    else:
        if membership and _membership_has_active_access(membership):
            log.warning('billing_success ownership check failed user=%s membership=%s', request.user.pk, membership_id)
        messages.success(request, 'Payment successful! Your proxy access will be activated after Whop verifies the membership.')

    resp = render(request, 'billing/success.html', {
        'profile':       profile,
        'membership_id': membership_id,
        'credential':    credential,
        'proxy_gateway': proxy_access.gateway_config(),
        'is_admin':      request.user.email == SUPER_ADMIN_EMAIL,
    })
    resp['Cache-Control'] = 'no-store'
    return resp


@login_required
def billing_cancel(request):
    if request.method != 'POST':
        return redirect('billing_dashboard')
    profile = _get_or_create_profile(request.user)
    if not profile.whop_membership_id:
        messages.error(request, 'No active subscription found.')
        return redirect('billing_dashboard')

    from . import whop as wp
    try:
        result = wp.cancel_all_active_for_email(
            email=request.user.email,
            immediate=False,
            extra_membership_ids=[profile.whop_membership_id],
        )
        if result['cancelled']:
            profile.whop_cancelled = True
            profile.whop_status    = 'cancelled'
            profile.save()
            proxy_access.sync_proxy_access(profile, reason='billing cancelled')
            messages.success(request, 'Your subscription has been cancelled. You retain access until the end of the billing period.')
        else:
            messages.warning(request, 'Cancellation request sent. Your account will be updated shortly.')
    except Exception as e:
        log.exception('billing_cancel error for user %s', request.user.pk)
        messages.error(request, f'Could not cancel subscription: {e}')

    return redirect('billing_dashboard')


@login_required
def billing_portal(request):
    return redirect('https://whop.com/hub/')


@csrf_exempt
def billing_webhook(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    payload   = request.body
    signature = request.META.get('HTTP_X_WHOP_SIGNATURE', '') or request.META.get('HTTP_SIGNATURE', '')

    from . import whop as wp
    if not wp.verify_webhook_signature(payload, signature):
        log.warning('Whop webhook: invalid signature')
        return HttpResponse('Invalid signature', status=401)

    try:
        data   = json.loads(payload)
        action = data.get('action', '')
        mem    = data.get('data', {})
        mem_id = mem.get('id', '')
    except Exception:
        return HttpResponse('Bad JSON', status=400)

    log.info('Whop webhook: action=%s membership=%s', action, mem_id)

    if action == 'membership.went_valid' or (action == 'membership.was_created' and _membership_has_active_access(mem)):
        _webhook_activate(mem, wp)
    elif action in ('membership.was_revoked', 'membership.expired',
                    'membership.was_cancelled', 'membership.was_paused'):
        _webhook_deactivate(mem, action, wp)
    elif action == 'membership.updated':
        _webhook_update(mem, wp)

    return HttpResponse('ok', status=200)


def _webhook_activate(mem: dict, wp) -> None:
    mem_id = mem.get('id', '')
    plan   = wp.plan_from_membership(mem, default='')
    if not plan or not mem_id:
        return
    try:
        profile = UserProfile.objects.filter(whop_membership_id=mem_id).first()
        if not profile:
            metadata_user_id = _membership_metadata_value(mem, 'user_id')
            if metadata_user_id:
                user = User.objects.filter(pk=metadata_user_id).first()
                if user:
                    profile = _get_or_create_profile(user)
        if not profile:
            u_email = (mem.get('user') or {}).get('email') or mem.get('email', '')
            if u_email:
                user = User.objects.filter(email=u_email).first()
                if user:
                    profile = _get_or_create_profile(user)
        if not profile:
            return
        ui    = wp.format_membership_for_ui(mem)
        dates = wp.get_billing_dates(mem)
        profile.plan                 = plan
        profile.whop_membership_id   = mem_id
        profile.whop_status          = 'active'
        profile.whop_cancelled       = False
        profile.whop_paused          = False
        profile.whop_renews_at       = ui.get('renews_at', '')
        profile.billing_period_start = dates.get('period_start', '')
        profile.billing_period_end   = dates.get('period_end', '')
        profile.last_whop_sync_at    = timezone.now()
        profile.save()
        _upsert_invoice_from_membership(profile.user, plan, mem)
        proxy_access.attach_consent_to_membership(
            profile.user,
            mem_id,
            _membership_metadata_value(mem, 'consent_id'),
        )
        credential = proxy_access.sync_proxy_access(profile, reason='webhook activation')
        _email_proxy_credentials_if_needed(profile.user, credential)
    except Exception:
        log.exception('webhook_activate failed for membership %s', mem_id)


def _webhook_deactivate(mem: dict, action: str, wp) -> None:
    mem_id = mem.get('id', '')
    if not mem_id:
        return
    try:
        profile = UserProfile.objects.filter(whop_membership_id=mem_id).first()
        if not profile:
            return
        is_paused    = 'paused' in action
        is_cancelled = 'cancel' in action
        is_terminal  = 'revoked' in action or 'expired' in action
        profile.whop_status    = 'paused' if is_paused else 'cancelled'
        profile.whop_cancelled = is_cancelled or is_terminal
        profile.whop_paused    = is_paused
        if is_terminal:
            profile.plan = 'free'
        profile.last_whop_sync_at = timezone.now()
        profile.save()
        proxy_access.sync_proxy_access(profile, reason=action or 'webhook deactivation')
    except Exception:
        log.exception('webhook_deactivate failed for membership %s', mem_id)


def _webhook_update(mem: dict, wp) -> None:
    mem_id = mem.get('id', '')
    if not mem_id:
        return
    try:
        profile = UserProfile.objects.filter(whop_membership_id=mem_id).first()
        if not profile:
            return
        ui    = wp.format_membership_for_ui(mem)
        dates = wp.get_billing_dates(mem)
        plan  = wp.plan_from_membership(mem, default=profile.plan)
        active_access = _membership_has_active_access(mem)
        profile.plan                 = plan if active_access else 'free'
        profile.whop_status          = ui.get('status', profile.whop_status)
        profile.whop_cancelled       = ui.get('cancelled', profile.whop_cancelled) or not active_access
        profile.whop_paused          = ui.get('paused', profile.whop_paused)
        profile.whop_renews_at       = ui.get('renews_at', profile.whop_renews_at)
        profile.billing_period_end   = dates.get('period_end', profile.billing_period_end)
        profile.last_whop_sync_at    = timezone.now()
        profile.save()
        proxy_access.sync_proxy_access(profile, reason='webhook update')
    except Exception:
        log.exception('webhook_update failed for membership %s', mem_id)


def _upsert_invoice_from_membership(user: User, plan: str, mem: dict) -> None:
    try:
        from . import whop as wp
        dates = wp.get_billing_dates(mem)
        if not dates.get('period_start_ts'):
            return
        period_start_ts = dates['period_start_ts']
        period_end_ts   = dates.get('period_end_ts', 0)
        is_annual       = bool(period_end_ts and (period_end_ts - period_start_ts) > 60 * 86400)
        period_key      = 'annual' if is_annual else 'monthly'
        price           = wp.get_plan_price(plan, period_key)
        amount_dollars  = price * 12 if is_annual else price
        amount_str      = f'${amount_dollars}.00'
        inv_key         = f'{user.pk}_{period_start_ts}'
        inv_id          = 'INV-' + hashlib.md5(inv_key.encode()).hexdigest()[:8].upper()

        def _fmt(ts):
            try:
                return datetime.fromtimestamp(int(ts), tz=dt_timezone.utc).strftime('%b %d, %Y')
            except Exception:
                return ''

        Invoice.objects.update_or_create(
            invoice_id=inv_id,
            defaults=dict(
                user=user,
                plan=plan,
                plan_label=plan.title() + (' Annual' if is_annual else ' Monthly'),
                amount=amount_str,
                amount_cents=amount_dollars * 100,
                date=_fmt(period_start_ts),
                period=f'{_fmt(period_start_ts)} – {_fmt(period_end_ts)}' if period_end_ts else _fmt(period_start_ts),
                period_start=dates.get('period_start', ''),
                period_end=dates.get('period_end', ''),
                status='paid',
                payment='Whop',
                billing_reason='Annual' if is_annual else 'Monthly',
                whop_membership_id=mem.get('id', ''),
            )
        )
    except Exception:
        log.exception('_upsert_invoice_from_membership failed for user %s', user.pk)


# ── Admin views ────────────────────────────────────────────────────────────

@admin_required
def admin_overview(request):
    total_users     = User.objects.count()
    active_subs     = UserProfile.objects.filter(plan__in=('starter', 'pro', 'agency'), whop_cancelled=False).count()
    total_revenue   = sum(i.amount_cents for i in Invoice.objects.all()) / 100
    mrr             = _calculate_mrr()
    open_tickets    = SupportMessage.objects.filter(status='open').count()
    recent_invoices = Invoice.objects.select_related('user').order_by('-created_at')[:8]
    recent_users    = User.objects.order_by('-date_joined')[:6]

    from . import whop as wp
    pricing = wp.get_pricing_dict()

    plan_counts = {
        'free':    UserProfile.objects.filter(plan='free').count(),
        'starter': UserProfile.objects.filter(plan='starter').count(),
        'pro':     UserProfile.objects.filter(plan='pro').count(),
        'agency':  UserProfile.objects.filter(plan='agency').count(),
    }

    return render(request, 'admin/overview.html', {
        'total_users':     total_users,
        'active_subs':     active_subs,
        'total_revenue':   round(total_revenue, 2),
        'mrr':             round(mrr, 2),
        'arr':             round(mrr * 12, 2),
        'open_tickets':    open_tickets,
        'recent_invoices': recent_invoices,
        'recent_users':    recent_users,
        'plan_counts':     plan_counts,
        'pricing':         pricing,
        'is_admin':        True,
    })


def _calculate_mrr() -> float:
    from . import whop as wp
    mrr = 0.0
    for profile in UserProfile.objects.filter(plan__in=('starter', 'pro', 'agency'), whop_cancelled=False):
        price = wp.get_plan_price(profile.plan, 'monthly')
        mrr += price
    return mrr


@admin_required
def admin_users(request):
    search        = request.GET.get('q', '').strip()
    plan_filter   = request.GET.get('plan', '')
    status_filter = request.GET.get('status', '')

    users = User.objects.select_related('profile').all()
    if search:
        users = (users.filter(email__icontains=search) |
                 users.filter(first_name__icontains=search) |
                 users.filter(last_name__icontains=search))
    if plan_filter == 'free':
        users = users.filter(profile__plan='free') | users.exclude(profile__isnull=False)
    elif plan_filter in ('starter', 'pro', 'agency'):
        users = users.filter(profile__plan=plan_filter)
    if status_filter == 'active':
        users = users.filter(is_active=True)
    elif status_filter == 'inactive':
        users = users.filter(is_active=False)

    users       = users.order_by('-date_joined').distinct()
    total_count = User.objects.count()
    active_count= User.objects.filter(is_active=True).count()
    sub_count   = UserProfile.objects.filter(plan__in=('starter', 'pro', 'agency'), whop_cancelled=False).count()

    plan_filters = [
        ('All', ''), ('Free', 'free'), ('Starter', 'starter'),
        ('Pro', 'pro'), ('Business', 'agency'),
    ]

    return render(request, 'admin/users.html', {
        'users':         users,
        'search':        search,
        'plan_filter':   plan_filter,
        'status_filter': status_filter,
        'plan_filters':  plan_filters,
        'total_count':   total_count,
        'active_count':  active_count,
        'sub_count':     sub_count,
        'is_admin':      True,
    })


@admin_required
def admin_toggle_ban(request, user_id):
    if request.method != 'POST':
        return redirect('admin_users')
    target = get_object_or_404(User, pk=user_id)
    if target.email == SUPER_ADMIN_EMAIL:
        messages.error(request, 'Cannot suspend the super admin account.')
        return redirect('admin_users')
    target.is_active = not target.is_active
    target.save()
    action = 'reactivated' if target.is_active else 'suspended'
    messages.success(request, f'{target.email} has been {action}.')
    return redirect(request.POST.get('next', 'admin_users'))


@admin_required
def admin_delete_user(request, user_id):
    if request.method != 'POST':
        return redirect('admin_users')
    target = get_object_or_404(User, pk=user_id)
    if target.email == SUPER_ADMIN_EMAIL:
        messages.error(request, 'Cannot delete the super admin account.')
        return redirect('admin_users')
    email = target.email
    target.delete()
    messages.success(request, f'{email} has been permanently deleted.')
    return redirect('admin_users')


@admin_required
def admin_user_detail(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    profile     = _get_or_create_profile(target_user)
    invoices    = Invoice.objects.filter(user=target_user).order_by('-created_at')
    purchases   = Purchase.objects.filter(user=target_user).order_by('-created_at')
    tickets     = SupportMessage.objects.filter(user=target_user).order_by('-created_at')

    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'set_plan':
            new_plan = request.POST.get('plan', '').strip()
            if new_plan in ('free', 'starter', 'pro', 'agency'):
                profile.plan = new_plan
                if new_plan == 'free':
                    profile.whop_cancelled = True
                else:
                    profile.whop_cancelled = False
                    profile.whop_paused = False
                profile.save()
                proxy_access.sync_proxy_access(profile, reason='admin plan update')
                messages.success(request, f'Plan updated to {new_plan}.')
        elif action == 'set_mode':
            new_mode = request.POST.get('mode', 'prod')
            if new_mode in ('dev', 'prod'):
                profile.whop_mode = new_mode
                profile.save()
                messages.success(request, f'Billing mode set to {new_mode}.')
        elif action == 'whop_resync':
            _admin_resync_user(profile)
            messages.success(request, 'Whop membership resynced.')
        elif action == 'proxy_sync':
            proxy_access.sync_proxy_access(profile, reason='admin user proxy sync')
            messages.success(request, 'Proxy credentials synced for this user.')
        return redirect('admin_user_detail', user_id=user_id)

    credential = _user_proxy_credential(target_user)

    resp = render(request, 'admin/user_detail.html', {
        'target_user': target_user,
        'profile':     profile,
        'credential':  credential,
        'proxy_gateway': proxy_access.gateway_config(),
        'http_proxy_line': proxy_access.connection_line(credential, 'http', 'ip_port_user_pass') if credential else '',
        'socks_proxy_line': proxy_access.connection_line(credential, 'socks5', 'url') if credential else '',
        'invoices':    invoices,
        'purchases':   purchases,
        'tickets':     tickets,
        'is_admin':    True,
    })
    resp['Cache-Control'] = 'no-store'
    return resp


def _admin_resync_user(profile: UserProfile) -> bool:
    from . import whop as wp
    if not profile.whop_membership_id:
        return False
    try:
        mem = wp.get_membership(profile.whop_membership_id, timeout=10)
        if not mem:
            return False
        ui    = wp.format_membership_for_ui(mem)
        dates = wp.get_billing_dates(mem)
        plan  = wp.plan_from_membership(mem, default=profile.plan)
        active_access = _membership_has_active_access(mem)
        profile.plan                 = plan if active_access else 'free'
        profile.whop_status          = ui.get('status', 'active')
        profile.whop_cancelled       = ui.get('cancelled', False) or not active_access
        profile.whop_paused          = ui.get('paused', False)
        profile.whop_renews_at       = ui.get('renews_at', '')
        profile.billing_period_start = dates.get('period_start', '')
        profile.billing_period_end   = dates.get('period_end', '')
        profile.last_whop_sync_at    = timezone.now()
        profile.save()
        _upsert_invoice_from_membership(profile.user, plan, mem)
        proxy_access.sync_proxy_access(profile, reason='admin whop resync')
        return True
    except Exception:
        log.exception('admin_resync_user failed for profile %s', profile.pk)
        return False


def _user_proxy_credential(user):
    try:
        return user.proxy_credential
    except ProxyCredential.DoesNotExist:
        return None


def _admin_proxy_rows(users):
    rows = []
    for user in users:
        profile = _get_or_create_profile(user)
        credential = _user_proxy_credential(user)
        rows.append({
            'user': user,
            'profile': profile,
            'credential': credential,
            'http_line': proxy_access.connection_line(credential, 'http', 'ip_port_user_pass') if credential else '',
            'socks_line': proxy_access.connection_line(credential, 'socks5', 'url') if credential else '',
        })
    return rows


@admin_required
def admin_proxy_settings(request):
    from . import whop as wp

    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'save_gateway':
            host = request.POST.get('proxy_gateway_host', '').strip() or 'proxy.goldenproxies.com'
            http_port = request.POST.get('proxy_gateway_http_port', '').strip() or '8001'
            socks5_port = request.POST.get('proxy_gateway_socks5_port', '').strip() or '8002'
            wp.set_db_setting('proxy_gateway_host', host)
            wp.set_db_setting('proxy_gateway_http_port', http_port)
            wp.set_db_setting('proxy_gateway_socks5_port', socks5_port)
            messages.success(request, 'Global proxy gateway settings saved.')
        elif action == 'sync_active':
            issued = 0
            disabled = 0
            failed = 0
            for user in User.objects.all().order_by('pk'):
                profile = _get_or_create_profile(user)
                before = _user_proxy_credential(user)
                was_active = bool(before and before.is_active)
                try:
                    credential = proxy_access.sync_proxy_access(profile, reason='admin proxy sync')
                    if credential and not was_active:
                        issued += 1
                    if not credential and was_active:
                        disabled += 1
                except Exception:
                    failed += 1
                    log.exception('admin_proxy_settings sync failed for user %s', user.pk)
            msg = f'Proxy credentials synced. Issued/enabled: {issued}. Disabled: {disabled}.'
            if failed:
                msg += f' Failed: {failed}.'
            messages.success(request, msg)
        return redirect('admin_proxy_settings')

    users = User.objects.select_related('profile').order_by('-date_joined')
    rows = _admin_proxy_rows(users)
    active_credentials = sum(1 for row in rows if row['credential'] and row['credential'].is_active)
    issued_credentials = sum(1 for row in rows if row['credential'])
    active_subscriptions = sum(1 for row in rows if row['profile'].has_active_subscription and row['user'].is_active)

    resp = render(request, 'admin/proxy_settings.html', {
        'is_admin': True,
        'proxy_gateway': proxy_access.gateway_config(),
        'rows': rows,
        'total_users': len(rows),
        'issued_credentials': issued_credentials,
        'active_credentials': active_credentials,
        'active_subscriptions': active_subscriptions,
    })
    resp['Cache-Control'] = 'no-store'
    return resp


@admin_required
def admin_purchases(request):
    status_filter = request.GET.get('status', 'all')
    purchases     = Purchase.objects.select_related('user').all()
    if status_filter != 'all':
        purchases = purchases.filter(status=status_filter)
    purchases = purchases.order_by('-created_at')
    return render(request, 'admin/purchases.html', {
        'purchases': purchases, 'status_filter': status_filter, 'is_admin': True,
    })


@admin_required
def admin_messages(request):
    status_filter = request.GET.get('status', 'all')
    selected_id   = request.GET.get('msg')
    msgs          = SupportMessage.objects.select_related('user').all()
    if status_filter != 'all':
        msgs = msgs.filter(status=status_filter)
    selected = None
    if selected_id:
        try:
            selected = SupportMessage.objects.select_related('user').get(pk=selected_id)
        except SupportMessage.DoesNotExist:
            pass
    reply_form = AdminReplyForm()
    open_count = SupportMessage.objects.filter(status='open').count()
    return render(request, 'admin/messages.html', {
        'msgs': msgs, 'selected': selected, 'reply_form': reply_form,
        'status_filter': status_filter, 'open_count': open_count, 'is_admin': True,
    })


@admin_required
def admin_reply(request, msg_id):
    if request.method != 'POST':
        return redirect('admin_messages')
    msg  = get_object_or_404(SupportMessage, pk=msg_id)
    form = AdminReplyForm(request.POST)
    if form.is_valid():
        msg.reply_body = form.cleaned_data['reply_body']
        msg.replied_at = timezone.now()
        msg.status     = 'replied'
        msg.save()
        messages.success(request, 'Reply sent.')
    return redirect(f'/admin-panel/messages/?msg={msg_id}&status=all')


@admin_required
def admin_message_status(request, msg_id):
    if request.method != 'POST':
        return redirect('admin_messages')
    msg        = get_object_or_404(SupportMessage, pk=msg_id)
    new_status = request.POST.get('status')
    if new_status in ('open', 'replied', 'closed'):
        msg.status = new_status
        msg.save()
    return redirect(f'/admin-panel/messages/?msg={msg_id}&status=all')


@admin_required
def admin_invoices(request):
    invoices = Invoice.objects.select_related('user').order_by('-created_at')
    total_revenue = sum(i.amount_cents for i in invoices) / 100
    return render(request, 'admin/invoices.html', {
        'invoices': invoices, 'total_revenue': round(total_revenue, 2), 'is_admin': True,
    })


@admin_required
def admin_whop_settings(request):
    from . import whop as wp

    SETTING_KEYS = [
        ('whop_api_key',        'Whop API Key',            'text',    'Bearer token from Whop developer settings'),
        ('whop_company_id',     'Company ID',              'text',    'Your Whop company ID (biz_...)'),
        ('whop_webhook_secret', 'Webhook Secret',          'text',    'Secret for verifying Whop webhook signatures'),
        ('whop_mode',           'Billing Mode',            'select',  'prod or dev — affects which plan IDs are used'),
        ('whop_plan_id_starter_monthly', 'Starter Monthly Plan ID', 'text', 'plan_... from Whop'),
        ('whop_plan_id_starter_annual',  'Starter Annual Plan ID',  'text', 'plan_... from Whop'),
        ('whop_plan_id_pro_monthly',     'Pro Monthly Plan ID',     'text', 'plan_... from Whop'),
        ('whop_plan_id_pro_annual',      'Pro Annual Plan ID',      'text', 'plan_... from Whop'),
        ('whop_plan_id_agency_monthly',  'Business Monthly Plan ID','text', 'plan_... from Whop'),
        ('whop_plan_id_agency_annual',   'Business Annual Plan ID', 'text', 'plan_... from Whop'),
        ('whop_checkout_starter_monthly','Starter Monthly Checkout URL','text','https://whop.com/...'),
        ('whop_checkout_starter_annual', 'Starter Annual Checkout URL', 'text','https://whop.com/...'),
        ('whop_checkout_pro_monthly',    'Pro Monthly Checkout URL',    'text','https://whop.com/...'),
        ('whop_checkout_pro_annual',     'Pro Annual Checkout URL',     'text','https://whop.com/...'),
        ('whop_checkout_agency_monthly', 'Business Monthly Checkout URL','text','https://whop.com/...'),
        ('whop_checkout_agency_annual',  'Business Annual Checkout URL', 'text','https://whop.com/...'),
    ]

    if request.method == 'POST':
        for key, *_ in SETTING_KEYS:
            val = request.POST.get(key, '').strip()
            if val or request.POST.get(f'{key}_clear'):
                wp.set_db_setting(key, val)
        messages.success(request, 'Whop settings saved.')
        return redirect('admin_whop_settings')

    current = {}
    for key, *_ in SETTING_KEYS:
        current[key] = wp._db_setting(key, '')

    return render(request, 'admin/whop_settings.html', {
        'setting_keys': SETTING_KEYS,
        'current':      current,
        'is_admin':     True,
    })


@admin_required
def admin_whop_resync_all(request):
    if request.method != 'POST':
        return redirect('admin_whop_settings')
    synced = 0
    failed = 0
    for profile in UserProfile.objects.filter(whop_membership_id__gt='').exclude(whop_membership_id=''):
        if _admin_resync_user(profile):
            synced += 1
        else:
            failed += 1
    messages.success(request, f'Resynced {synced} users. {failed} failed.')
    return redirect('admin_users')


# ── Blog helpers ────────────────────────────────────────────────────────────

def _proxy_scrape(url, proxy_url=''):
    """Fetch a URL through a datacenter proxy (string form, e.g.
    'user:pass@host:port' or 'http://user:pass@host:port') and return the
    cleaned main-content text. Returns '' on any failure."""
    try:
        import requests as _req
        from bs4 import BeautifulSoup
        proxies = None
        proxy_url = (proxy_url or '').strip()
        if proxy_url:
            if '://' not in proxy_url:
                proxy_url = 'http://' + proxy_url
            proxies = {'http': proxy_url, 'https': proxy_url}
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; GoldenProxiesBot/1.0)'}
        resp = _req.get(url, headers=headers, proxies=proxies, timeout=30)
        if not resp.ok:
            return ''
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'form', 'aside', 'noscript']):
            tag.decompose()
        article = (
            soup.find('article')
            or soup.find('main')
            or soup.find('div', class_=lambda c: c and ('content' in c.lower() or 'post' in c.lower() or 'article' in c.lower()))
            or soup.body
        )
        if article is None:
            return ''
        return article.get_text('\n', strip=True)[:18000]
    except Exception:
        return ''


LOCAL_GPT_DEFAULT_BASE_URL = 'http://127.0.0.1:8010/v1'
LOCAL_GPT_DEFAULT_MODEL = 'gpt-oss-20b-mxfp4'

# Strict allow-list for blog post HTML — matches the JSON_SCHEMA_HINT structure.
# Anything outside these tags/attrs is stripped, preventing stored XSS from
# LLM hallucinations or pasted content.
_BLOG_ALLOWED_TAGS = [
    'h2', 'h3', 'h4', 'p', 'br', 'hr',
    'ul', 'ol', 'li',
    'strong', 'em', 'b', 'i', 'u', 'blockquote', 'code', 'pre',
    'a', 'img',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
]
_BLOG_ALLOWED_ATTRS = {
    'a': ['href', 'title', 'rel', 'target'],
    'img': ['src', 'alt', 'title', 'width', 'height'],
}
_BLOG_ALLOWED_PROTOCOLS = ['http', 'https', 'mailto']


def _sanitize_blog_html(html):
    """Strip any HTML outside the blog allow-list — defends against stored XSS."""
    try:
        import bleach
    except ImportError:
        return escape(html or '')
    return bleach.clean(
        html or '',
        tags=_BLOG_ALLOWED_TAGS,
        attributes=_BLOG_ALLOWED_ATTRS,
        protocols=_BLOG_ALLOWED_PROTOCOLS,
        strip=True,
    )


def _sanitize_text(value, maxlen=None):
    """Strip ALL HTML — for short text fields like title/excerpt/meta/tags/slug."""
    try:
        import bleach
        cleaned = bleach.clean(value or '', tags=[], attributes={}, strip=True)
    except ImportError:
        cleaned = value or ''
    cleaned = cleaned.strip()
    if maxlen:
        cleaned = cleaned[:maxlen]
    return cleaned


def _local_gpt_settings() -> dict:
    from . import whop as wp
    base_url = (
        wp._db_setting('local_gpt_base_url', '')
        or os.environ.get('LOCAL_GPT_BASE_URL')
        or os.environ.get('GPT_OSS_BASE_URL')
        or LOCAL_GPT_DEFAULT_BASE_URL
    ).rstrip('/')
    model = (
        wp._db_setting('local_gpt_model', '')
        or os.environ.get('LOCAL_GPT_MODEL')
        or os.environ.get('GPT_OSS_MODEL')
        or LOCAL_GPT_DEFAULT_MODEL
    )
    return {'base_url': base_url, 'model': model}


def _call_local_gpt(base_url, model, prompt, system=None, json_mode=True):
    """Call the local OpenAI-compatible GPT-OSS chat/completions endpoint."""
    import requests as _req
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})
    payload = {
        'model': model or LOCAL_GPT_DEFAULT_MODEL,
        'messages': messages,
        'max_tokens': 4000,
        'temperature': 0.7,
    }
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    base_url = (base_url or LOCAL_GPT_DEFAULT_BASE_URL).rstrip('/')
    headers = {'Content-Type': 'application/json'}
    resp = _req.post(
        f'{base_url}/chat/completions',
        headers=headers,
        json=payload,
        timeout=240,
    )
    if not resp.ok:
        # If JSON mode isn't supported by the model, retry without it
        if json_mode and resp.status_code in (400, 422):
            payload.pop('response_format', None)
            resp = _req.post(
                f'{base_url}/chat/completions',
                headers=headers,
                json=payload,
                timeout=240,
            )
    resp.raise_for_status()
    data = resp.json()
    return data['choices'][0]['message']['content']


def _extract_json(raw):
    """Extract a JSON object from a model response, tolerating ```json fences."""
    raw = (raw or '').strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[1] if '\n' in raw else raw[3:]
        raw = raw.rsplit('```', 1)[0]
    raw = raw.strip()
    # Find first { and last } if there's leading/trailing prose
    if not raw.startswith('{'):
        i = raw.find('{')
        j = raw.rfind('}')
        if i != -1 and j != -1 and j > i:
            raw = raw[i:j + 1]
    return json.loads(raw)


def _save_generated_post(data, source_url, author):
    """Persist a model-generated JSON blob as a draft BlogPost. Returns the post."""
    from django.utils.text import slugify
    title = _sanitize_text(data.get('title', 'Untitled'), maxlen=200) or 'Untitled'
    slug = slugify(_sanitize_text(data.get('slug') or title or 'post', maxlen=200))[:180] or 'post'
    base_slug = slug
    n = 1
    while BlogPost.objects.filter(slug=slug).exists():
        slug = f'{base_slug}-{n}'
        n += 1
    return BlogPost.objects.create(
        title=title,
        slug=slug,
        excerpt=_sanitize_text(data.get('excerpt', ''), maxlen=500),
        content=_sanitize_blog_html(data.get('content', '')),
        meta_description=_sanitize_text(data.get('meta_description', ''), maxlen=200),
        tags=_sanitize_text(data.get('tags', ''), maxlen=200),
        status='draft',
        ai_generated=True,
        source_url=_sanitize_text(source_url, maxlen=500),
        author=author,
    )


# ── Public blog views ────────────────────────────────────────────────────────

def blog_list(request):
    posts = BlogPost.objects.filter(status='published').order_by('-published_at')
    return render(request, 'public/blog_list.html', {'posts': posts})


def blog_detail(request, slug):
    post = get_object_or_404(BlogPost, slug=slug, status='published')
    return render(request, 'public/blog_post.html', {'post': post})


# ── Admin blog views ─────────────────────────────────────────────────────────

@admin_required
def admin_blog_list(request):
    posts = BlogPost.objects.all().order_by('-created_at')
    published = posts.filter(status='published').count()
    drafts    = posts.filter(status='draft').count()
    return render(request, 'admin/blog_list.html', {
        'posts': posts, 'is_admin': True,
        'published': published, 'drafts': drafts,
    })


@admin_required
def admin_blog_generate(request):
    from . import whop as wp
    ai_settings  = _local_gpt_settings()
    ai_base_url  = ai_settings['base_url']
    ai_model     = ai_settings['model']
    ai_configured = bool(ai_base_url and ai_model)
    proxy_url    = wp._db_setting('proxy_url', '')
    proxy_configured = bool(proxy_url)
    error = None

    SYSTEM_PROMPT = (
        'You are a senior content writer for GoldenProxies, a premium residential and '
        'datacenter proxy network used by professionals for web scraping, market research, '
        'SEO monitoring, and data collection. You always respond with a single valid JSON '
        'object — no markdown fences, no commentary, no leading/trailing text.'
    )
    JSON_SCHEMA_HINT = (
        '{\n'
        '  "title": "Compelling, SEO-friendly title (60-70 chars ideal)",\n'
        '  "slug": "url-friendly-slug-lowercase-hyphens",\n'
        '  "meta_description": "150-160 character meta description",\n'
        '  "excerpt": "2-3 sentence preview for listing pages",\n'
        '  "tags": "comma-separated relevant tags",\n'
        '  "content": "Full article as HTML using <h2>, <h3>, <p>, <ul>, <li>, <strong>, <em>, <blockquote>. At least 800 words. Authoritative and practical."\n'
        '}'
    )

    if request.method == 'POST':
        mode       = request.POST.get('mode', 'generate')
        topic      = request.POST.get('topic', '').strip()
        keywords   = request.POST.get('keywords', '').strip()
        source_url = request.POST.get('source_url', '').strip()

        if not ai_configured:
            error = 'Local GPT-OSS endpoint is not configured. Go to API Settings first.'
        elif mode == 'rewrite':
            if not source_url:
                error = 'A source URL is required to scrape & rewrite.'
            else:
                scraped = _proxy_scrape(source_url, proxy_url)
                if not scraped:
                    error = 'Could not fetch the source URL. Check the URL and your proxy settings.'
                else:
                    try:
                        prompt = (
                            f'Below is the full text of an article scraped from {source_url}.\n\n'
                            f'Rewrite it as an original, SEO-optimized blog post for GoldenProxies. '
                            f'Do NOT copy sentences verbatim — restructure, rephrase, add proxy/scraping '
                            f'expertise, remove anything brand-specific to the source.\n\n'
                            f'Respond ONLY with a single JSON object matching this shape:\n{JSON_SCHEMA_HINT}\n\n'
                            f'Source article:\n---\n{scraped[:14000]}\n---'
                        )
                        raw = _call_local_gpt(ai_base_url, ai_model, prompt, system=SYSTEM_PROMPT)
                        data = _extract_json(raw)
                        post = _save_generated_post(data, source_url, request.user)
                        messages.success(request, f'Rewrote source URL into draft "{post.title}".')
                        return redirect('admin_blog_edit', post_id=post.pk)
                    except Exception as e:
                        error = f'Rewrite failed: {e}'
        else:  # mode == 'generate'
            if not topic:
                error = 'Topic is required.'
            else:
                research = ''
                if source_url:
                    research = _proxy_scrape(source_url, proxy_url)
                    if not research:
                        error = 'Could not scrape the research URL. Check the URL and your proxy settings.'
                if not error:
                    try:
                        kw_line = f'\nTarget keywords: {keywords}' if keywords else ''
                        research_block = (
                            f'\n\nResearch context from {source_url}:\n---\n{research[:4000]}\n---'
                            if research else ''
                        )
                        prompt = (
                            f'Write a detailed, engaging, SEO-optimized blog post about: '
                            f'{topic}{kw_line}{research_block}\n\n'
                            f'Respond ONLY with a single JSON object matching this shape:\n{JSON_SCHEMA_HINT}'
                        )
                        raw = _call_local_gpt(ai_base_url, ai_model, prompt, system=SYSTEM_PROMPT)
                        data = _extract_json(raw)
                        post = _save_generated_post(data, source_url, request.user)
                        messages.success(request, f'Blog post "{post.title}" generated and saved as draft.')
                        return redirect('admin_blog_edit', post_id=post.pk)
                    except Exception as e:
                        error = f'Generation failed: {e}'

    return render(request, 'admin/blog_generate.html', {
        'is_admin': True,
        'ai_configured': ai_configured,
        'ai_model': ai_model,
        'ai_base_url': ai_base_url,
        'proxy_configured': proxy_configured,
        'error': error,
    })


# ── AJAX endpoints for the 3-step Scrape → Rewrite → Publish wizard ──────────

@admin_required
@require_http_methods(['POST'])
def admin_blog_scrape(request):
    """AJAX: fetch a URL through the configured proxy and return cleaned text."""
    from . import whop as wp
    url = (request.POST.get('url') or '').strip()
    if not url:
        return JsonResponse({'ok': False, 'error': 'URL is required.'}, status=400)
    text = _proxy_scrape(url, wp._db_setting('proxy_url', ''))
    if not text:
        return JsonResponse({
            'ok': False,
            'error': 'Could not fetch the URL. Check the URL and your proxy settings.',
        }, status=502)
    return JsonResponse({'ok': True, 'text': text, 'length': len(text)})


@admin_required
@require_http_methods(['POST'])
def admin_blog_rewrite(request):
    """AJAX: rewrite the supplied text into a structured blog post via local GPT-OSS."""
    text = (request.POST.get('text') or '').strip()
    source_url = (request.POST.get('source_url') or '').strip()
    if not text:
        return JsonResponse({'ok': False, 'error': 'Source text is required.'}, status=400)
    ai_settings = _local_gpt_settings()
    ai_base_url = ai_settings['base_url']
    ai_model = ai_settings['model']
    if not ai_base_url or not ai_model:
        return JsonResponse({
            'ok': False,
            'error': 'Local GPT-OSS endpoint is not configured. Go to API Settings.',
        }, status=400)
    SYSTEM_PROMPT = (
        'You are a senior content writer for GoldenProxies, a premium residential and '
        'datacenter proxy network used by professionals for web scraping, market research, '
        'SEO monitoring, and data collection. You always respond with a single valid JSON '
        'object — no markdown fences, no commentary, no leading/trailing text.'
    )
    JSON_SCHEMA_HINT = (
        '{\n'
        '  "title": "Compelling, SEO-friendly title (60-70 chars ideal)",\n'
        '  "slug": "url-friendly-slug-lowercase-hyphens",\n'
        '  "meta_description": "150-160 character meta description",\n'
        '  "excerpt": "2-3 sentence preview for listing pages",\n'
        '  "tags": "comma-separated relevant tags",\n'
        '  "content": "Full article as HTML using <h2>, <h3>, <p>, <ul>, <li>, <strong>, <em>, <blockquote>. At least 800 words. Authoritative and practical."\n'
        '}'
    )
    try:
        src_line = f' scraped from {source_url}' if source_url else ''
        prompt = (
            f'Below is the cleaned text of an article{src_line} (it may have been edited '
            f'by the user before sending).\n\n'
            f'Rewrite it as an original, SEO-optimized blog post for GoldenProxies. '
            f'Do NOT copy sentences verbatim — restructure, rephrase, add proxy/scraping '
            f'expertise, remove anything brand-specific to the source.\n\n'
            f'Respond ONLY with a single JSON object matching this shape:\n{JSON_SCHEMA_HINT}\n\n'
            f'Source article:\n---\n{text[:14000]}\n---'
        )
        raw = _call_local_gpt(ai_base_url, ai_model, prompt, system=SYSTEM_PROMPT)
        data = _extract_json(raw)
        # Sanitize the LLM output before sending it back to the browser:
        # text fields stripped of all HTML, content stripped to allow-list.
        return JsonResponse({
            'ok': True,
            'title':            _sanitize_text(data.get('title', ''),            maxlen=200),
            'slug':             _sanitize_text(data.get('slug', ''),             maxlen=200),
            'excerpt':          _sanitize_text(data.get('excerpt', ''),          maxlen=500),
            'meta_description': _sanitize_text(data.get('meta_description', ''), maxlen=200),
            'tags':             _sanitize_text(data.get('tags', ''),             maxlen=200),
            'content':          _sanitize_blog_html(data.get('content', '')),
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Rewrite failed: {e}'}, status=500)


@admin_required
@require_http_methods(['POST'])
def admin_blog_publish(request):
    """AJAX: persist the finalized (possibly user-edited) post."""
    from django.utils.text import slugify
    from django.db import IntegrityError, transaction
    title = _sanitize_text(request.POST.get('title', ''), maxlen=200)
    if not title:
        return JsonResponse({'ok': False, 'error': 'Title is required.'}, status=400)
    raw_slug = _sanitize_text(request.POST.get('slug', '') or title, maxlen=200)
    base_slug = slugify(raw_slug)[:180] or 'post'
    status = request.POST.get('status', 'published')
    if status not in ('published', 'draft'):
        status = 'draft'

    fields = dict(
        title=title,
        excerpt=         _sanitize_text(request.POST.get('excerpt', ''),          maxlen=500),
        content=         _sanitize_blog_html(request.POST.get('content', '')),
        meta_description=_sanitize_text(request.POST.get('meta_description', ''), maxlen=200),
        tags=            _sanitize_text(request.POST.get('tags', ''),             maxlen=200),
        cover_image_url= _sanitize_text(request.POST.get('cover_image_url', ''),  maxlen=500),
        source_url=      _sanitize_text(request.POST.get('source_url', ''),       maxlen=500),
        status=status,
        ai_generated=True,
        author=request.user,
        published_at=timezone.now() if status == 'published' else None,
    )

    # Race-safe slug uniqueness: try base, then -2, -3, … catching IntegrityError.
    slug = base_slug
    for n in range(0, 50):
        candidate = base_slug if n == 0 else f'{base_slug}-{n + 1}'
        try:
            with transaction.atomic():
                post = BlogPost.objects.create(slug=candidate, **fields)
            slug = candidate
            break
        except IntegrityError:
            continue
    else:
        return JsonResponse({'ok': False, 'error': 'Could not generate a unique slug.'}, status=500)

    return JsonResponse({
        'ok': True,
        'post_id': post.pk,
        'status': status,
        'edit_url': f'/admin-panel/blog/{post.pk}/edit/',
        'view_url': f'/blog/{post.slug}/' if status == 'published' else None,
    })


@admin_required
def admin_blog_edit(request, post_id=None):
    post = get_object_or_404(BlogPost, pk=post_id) if post_id else None

    if request.method == 'POST':
        from django.utils.text import slugify
        title            = _sanitize_text(request.POST.get('title', ''), maxlen=200)
        slug             = slugify(_sanitize_text(request.POST.get('slug', '') or title, maxlen=200)) or 'post'
        excerpt          = _sanitize_text(request.POST.get('excerpt', ''), maxlen=500)
        content          = _sanitize_blog_html(request.POST.get('content', ''))
        meta_description = _sanitize_text(request.POST.get('meta_description', ''), maxlen=200)
        tags             = _sanitize_text(request.POST.get('tags', ''), maxlen=200)
        cover_image_url  = _sanitize_text(request.POST.get('cover_image_url', ''), maxlen=500)
        status           = request.POST.get('status', 'draft')

        if not title:
            messages.error(request, 'Title is required.')
            return render(request, 'admin/blog_edit.html', {'post': post, 'is_admin': True})

        if post:
            if BlogPost.objects.filter(slug=slug).exclude(pk=post.pk).exists():
                slug = f'{slug}-{post.pk}'
            post.title = title; post.slug = slug; post.excerpt = excerpt
            post.content = content; post.meta_description = meta_description
            post.tags = tags; post.cover_image_url = cover_image_url; post.status = status
            if status == 'published' and not post.published_at:
                post.published_at = timezone.now()
            post.save()
            messages.success(request, 'Post saved.')
        else:
            base_slug = slug; n = 1
            while BlogPost.objects.filter(slug=slug).exists():
                slug = f'{base_slug}-{n}'; n += 1
            post = BlogPost.objects.create(
                title=title, slug=slug, excerpt=excerpt, content=content,
                meta_description=meta_description, tags=tags,
                cover_image_url=cover_image_url, status=status,
                author=request.user,
                published_at=timezone.now() if status == 'published' else None,
            )
            messages.success(request, 'Post created.')

        return redirect('admin_blog_edit', post_id=post.pk)

    return render(request, 'admin/blog_edit.html', {'post': post, 'is_admin': True})


@admin_required
def admin_blog_toggle_status(request, post_id):
    if request.method != 'POST':
        return redirect('admin_blog_list')
    post = get_object_or_404(BlogPost, pk=post_id)
    if post.status == 'published':
        post.status = 'draft'
    else:
        post.title = _sanitize_text(post.title, maxlen=200)
        post.slug = slugify(_sanitize_text(post.slug or post.title, maxlen=200)) or post.slug
        post.excerpt = _sanitize_text(post.excerpt, maxlen=500)
        post.content = _sanitize_blog_html(post.content)
        post.meta_description = _sanitize_text(post.meta_description, maxlen=200)
        post.tags = _sanitize_text(post.tags, maxlen=200)
        post.status = 'published'
        if not post.published_at:
            post.published_at = timezone.now()
    post.save()
    verb = 'published' if post.status == 'published' else 'moved to draft'
    messages.success(request, f'"{post.title}" {verb}.')
    return redirect('admin_blog_list')


@admin_required
def admin_blog_delete(request, post_id):
    if request.method != 'POST':
        return redirect('admin_blog_list')
    post = get_object_or_404(BlogPost, pk=post_id)
    title = post.title
    post.delete()
    messages.success(request, f'"{title}" deleted.')
    return redirect('admin_blog_list')


@admin_required
def admin_api_settings(request):
    from . import whop as wp
    if request.method == 'POST':
        base_url = request.POST.get('local_gpt_base_url', '').strip() or LOCAL_GPT_DEFAULT_BASE_URL
        model = request.POST.get('local_gpt_model', '').strip() or LOCAL_GPT_DEFAULT_MODEL
        wp.set_db_setting('local_gpt_base_url', base_url.rstrip('/'))
        wp.set_db_setting('local_gpt_model', model)
        wp.set_db_setting('proxy_url', request.POST.get('proxy_url', '').strip())
        messages.success(request, 'API settings saved.')
        return redirect('admin_api_settings')

    ai_settings = _local_gpt_settings()

    return render(request, 'admin/api_settings.html', {
        'is_admin': True,
        'ai_base_url': ai_settings['base_url'],
        'ai_model': ai_settings['model'],
        'ai_configured': bool(ai_settings['base_url'] and ai_settings['model']),
        'ai_default_base_url': LOCAL_GPT_DEFAULT_BASE_URL,
        'ai_default_model': LOCAL_GPT_DEFAULT_MODEL,
        'proxy_url': wp._db_setting('proxy_url', ''),
        'proxy_set': bool(wp._db_setting('proxy_url', '')),
    })


def _google_settings_response(request, payload: dict, status: int = 200):
    if request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest':
        return JsonResponse(payload, status=status)
    if payload.get('ok'):
        messages.success(request, payload.get('message', 'Google Sign-In settings saved.'))
    else:
        messages.error(request, payload.get('error', 'Could not save Google Sign-In settings.'))
    return redirect('admin_google_settings')


def _truncate_middle(value: str, left: int = 12, right: int = 16) -> str:
    value = value or ''
    if len(value) <= left + right + 1:
        return value
    return f'{value[:left]}...{value[-right:]}'


def _validate_google_origin(value: str) -> str:
    value = (value or '').strip().rstrip('/')
    if not value:
        return ''
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('Authorized origin must start with http:// or https://.')
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('Authorized origin must be only the scheme and domain, for example https://goldenproxies.com.')
    return f'{parsed.scheme}://{parsed.netloc}'


def _validate_google_redirect(value: str) -> str:
    value = (value or '').strip()
    if not value:
        return ''
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('Redirect URI must start with http:// or https://.')
    if not parsed.path.endswith('/auth/google/callback/'):
        raise ValueError('Redirect URI must end with /auth/google/callback/.')
    return value


@admin_required
def admin_google_settings(request):
    from . import whop as wp

    if request.method == 'POST':
        section = request.POST.get('section', '')
        try:
            if section == 'credentials':
                client_id = request.POST.get('google_client_id', '').strip()
                client_secret = request.POST.get('google_client_secret', '').strip()
                wp.set_db_setting('google_oauth_client_id', client_id)
                if client_secret:
                    wp.set_db_setting('google_oauth_client_secret', client_secret)
                return _google_settings_response(request, {'ok': True, 'message': 'Google OAuth credentials saved.'})

            if section == 'urls':
                origin = _validate_google_origin(request.POST.get('google_authorized_origin', ''))
                redirect_uri = _validate_google_redirect(request.POST.get('google_redirect_uri', ''))
                wp.set_db_setting('google_authorized_origin', origin)
                wp.set_db_setting('google_redirect_uri', redirect_uri)
                return _google_settings_response(request, {'ok': True, 'message': 'Google OAuth URLs saved.'})

            if section == 'enabled':
                enabled = request.POST.get('google_oauth_enabled') == '1'
                cfg = _google_oauth_config()
                if enabled and not (cfg['client_id'] and cfg['client_secret']):
                    return _google_settings_response(request, {'ok': False, 'error': 'Save a Google Client ID and Secret before enabling Google sign-in.'}, status=400)
                wp.set_db_setting('google_oauth_enabled', '1' if enabled else '0')
                return _google_settings_response(request, {'ok': True, 'enabled': enabled, 'message': f'Google sign-in {"enabled" if enabled else "disabled"}.'})

            if section == 'clear_secret':
                wp.set_db_setting('google_oauth_client_secret', '')
                wp.set_db_setting('google_oauth_enabled', '0')
                return _google_settings_response(request, {'ok': True, 'message': 'Google OAuth secret cleared and sign-in disabled.'})

            if section == 'reset_urls':
                wp.set_db_setting('google_authorized_origin', '')
                wp.set_db_setting('google_redirect_uri', '')
                return _google_settings_response(request, {'ok': True, 'message': 'Google OAuth URLs reset to auto-detect.'})
        except ValueError as e:
            return _google_settings_response(request, {'ok': False, 'error': str(e)}, status=400)

        return _google_settings_response(request, {'ok': False, 'error': 'Unknown settings section.'}, status=400)

    cfg = _google_oauth_config()
    configured = bool(cfg['client_id'] and cfg['client_secret'])
    auto_origin = _google_auto_origin(request)
    auto_redirect = request.build_absolute_uri('/auth/google/callback/')
    origin_override = wp._db_setting('google_authorized_origin', '')
    redirect_override = wp._db_setting('google_redirect_uri', '')

    resp = render(request, 'admin/google_settings.html', {
        'is_admin': True,
        'client_id': cfg['client_id'],
        'client_id_short': _truncate_middle(cfg['client_id']),
        'secret_set': bool(cfg['client_secret']),
        'secret_short': 'GOCSPX...' if cfg['client_secret'] else '',
        'enabled': cfg['enabled'],
        'configured': configured,
        'ready': configured and cfg['enabled'],
        'auto_origin': auto_origin,
        'auto_redirect_uri': auto_redirect,
        'origin_override': origin_override,
        'redirect_override': redirect_override,
        'effective_origin': origin_override or auto_origin,
        'effective_redirect_uri': redirect_override or auto_redirect,
    })
    resp['Cache-Control'] = 'no-store'
    return resp


# ── Email settings ──────────────────────────────────────────────────────────

EMAIL_SERVICES = [
    ('billing',   'Billing',                  'Receipts, invoices, subscription updates, payment failures.', '💳', 'billing@goldenproxies.com'),
    ('support',   'Support',                  'Customer support replies and ticket notifications.',          '🎧', 'support@goldenproxies.com'),
    ('alerts',    'Alerts & Notifications',   'Service alerts, account changes, security notifications.',    '🔔', 'alerts@goldenproxies.com'),
    ('marketing', 'Marketing & Onboarding',   'Product announcements, onboarding sequences, newsletters.',   '📣', 'hello@goldenproxies.com'),
    ('system',    'System & Security',        'Password resets, login alerts, 2FA codes, account security.', '🔐', 'security@goldenproxies.com'),
]


def _email_service_config(service: str) -> dict:
    """Return the per-service sender config (from / display name / reply-to).
    Falls back to the global SMTP defaults when a service field is empty."""
    from . import whop as wp
    return {
        'from_email': (
            wp._db_setting(f'email_{service}_from', '')
            or wp._db_setting('resend_from_email', '')
            or wp._db_setting('smtp_from_email', '')
            or wp._db_setting('smtp_username', '')
            or RESEND_DEFAULT_FROM
        ),
        'from_name':  wp._db_setting(f'email_{service}_name', '') or wp._db_setting('smtp_from_name', 'GoldenProxies'),
        'reply_to':   wp._db_setting(f'email_{service}_replyto', ''),
    }


RESEND_DEFAULT_FROM = 'onboarding@resend.dev'  # Resend's sandbox sender (always works for testing)


def _email_default_from() -> str:
    """The fallback From address used when no per-service override is set."""
    from . import whop as wp
    return wp._db_setting('resend_from_email', '') or RESEND_DEFAULT_FROM


@admin_required
def admin_email_settings(request):
    from . import whop as wp
    if request.method == 'POST':
        # ── Step 1: Resend API key + global default sender ──
        api_key = request.POST.get('resend_api_key', '').strip()
        if api_key:
            wp.set_db_setting('resend_api_key', api_key)
        for key in ('resend_from_email', 'resend_from_name'):
            val = request.POST.get(key, '').strip()
            if val:
                wp.set_db_setting(key, val)

        # ── Step 2: per-service senders ──
        for slug, *_ in EMAIL_SERVICES:
            for field in ('from', 'name', 'replyto'):
                key = f'email_{slug}_{field}'
                val = request.POST.get(key, '').strip()
                # Always write (so users can clear a per-service override and fall back to default)
                wp.set_db_setting(key, val)

        messages.success(request, 'Email settings saved.')
        return redirect('admin_email_settings')

    services = []
    for slug, label, desc, icon, default_from in EMAIL_SERVICES:
        cfg = _email_service_config(slug)
        services.append({
            'slug': slug, 'label': label, 'desc': desc, 'icon': icon,
            'default_from': default_from,
            'from_email': wp._db_setting(f'email_{slug}_from', ''),
            'from_name':  wp._db_setting(f'email_{slug}_name', ''),
            'reply_to':   wp._db_setting(f'email_{slug}_replyto', ''),
            'effective_from': cfg['from_email'],
        })

    return render(request, 'admin/email_settings.html', {
        'is_admin': True,
        'resend_set': bool(wp._db_setting('resend_api_key', '')),
        'resend_from_email': wp._db_setting('resend_from_email', ''),
        'resend_from_name': wp._db_setting('resend_from_name', 'GoldenProxies'),
        'resend_default_from': RESEND_DEFAULT_FROM,
        'services': services,
    })


def _send_resend_email(to_email, subject, body, html_body=None, service: str = ''):
    """Send an email through the Resend HTTP API
    (https://resend.com/docs/api-reference/emails/send-email).
    When `service` is set ('billing', 'support', etc.) uses that service's
    sender overrides, falling back to the global default From.
    Returns (ok: bool, message: str)."""
    from . import whop as wp
    import requests as _req

    api_key = wp._db_setting('resend_api_key', '')
    if not api_key:
        return False, 'Resend API key is not configured.'

    if service:
        cfg = _email_service_config(service)
        from_email = cfg['from_email']
        from_name  = cfg['from_name']
        reply_to   = cfg['reply_to']
    else:
        from_email = wp._db_setting('resend_from_email', '') or RESEND_DEFAULT_FROM
        from_name  = wp._db_setting('resend_from_name', 'GoldenProxies')
        reply_to   = ''

    if not from_email:
        return False, 'No From address configured.'

    payload = {
        'from': f'{from_name} <{from_email}>' if from_name else from_email,
        'to': [to_email],
        'subject': subject,
        'text': body,
    }
    if html_body:
        payload['html'] = html_body
    if reply_to:
        payload['reply_to'] = reply_to

    try:
        resp = _req.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=20,
        )
    except Exception as e:
        return False, f'Network error: {e}'

    if resp.status_code in (200, 201, 202):
        try:
            return True, resp.json().get('id', 'sent')
        except Exception:
            return True, 'sent'

    # Surface Resend's error message verbatim so the admin sees what's wrong
    try:
        err = resp.json()
        return False, err.get('message') or err.get('error') or resp.text[:300]
    except Exception:
        return False, f'HTTP {resp.status_code}: {resp.text[:300]}'


@admin_required
def admin_send_test_email(request):
    if request.method != 'POST':
        return redirect('admin_email_settings')
    to = request.POST.get('to_email', '').strip() or request.user.email
    service = request.POST.get('service', '').strip()
    label = next((lbl for slug, lbl, *_ in EMAIL_SERVICES if slug == service), 'Default')
    if not to:
        messages.error(request, 'No recipient email — provide one or set an email on your account.')
        return redirect('admin_email_settings')
    ok, info = _send_resend_email(
        to_email=to,
        subject=f'GoldenProxies — {label} test email',
        body=f'This is a test email from your GoldenProxies {label} sender. Delivery works.',
        html_body=f'<p>This is a <strong>{label}</strong> test email from your GoldenProxies admin panel. ✓ Delivery works.</p>',
        service=service,
    )
    if ok:
        messages.success(request, f'{label} test email sent to {to} (Resend id: {info}).')
    else:
        messages.error(request, f'{label} test email failed: {info}')
    return redirect('admin_email_settings')
