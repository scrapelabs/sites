import os
import ipaddress
import secrets
import string

from django.conf import settings
from django.utils import timezone

from .models import CheckoutConsent, ProxyCredential, UserProfile


PAID_PLANS = ('starter', 'pro', 'agency')
CHECKOUT_TERMS_VERSION = '2026-07-07'
CHECKOUT_TERMS_TEXT = (
    'I request immediate access to this digital proxy service and understand that '
    'proxy credentials and bandwidth access are delivered immediately. I acknowledge '
    'that payments are non-refundable once access is delivered, except where required by law.'
)
_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def _setting(key: str, env_key: str, default: str = '') -> str:
    from . import whop as wp
    return wp._db_setting(key, os.environ.get(env_key, default))


def gateway_config() -> dict:
    return {
        'host': _setting('proxy_gateway_host', 'PROXY_GATEWAY_HOST', 'proxy.goldenproxies.com'),
        'http_port': _setting('proxy_gateway_http_port', 'PROXY_GATEWAY_HTTP_PORT', '8001'),
        'socks5_port': _setting('proxy_gateway_socks5_port', 'PROXY_GATEWAY_SOCKS5_PORT', '8002'),
    }


def _random_password(length: int = 28) -> str:
    return ''.join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def _new_username(user) -> str:
    prefix = f'gp{user.pk}'
    for _ in range(20):
        candidate = f'{prefix}_{secrets.token_hex(4)}'
        if not ProxyCredential.objects.filter(username=candidate).exists():
            return candidate
    raise RuntimeError('Could not allocate a unique proxy username')


def ensure_proxy_credential(profile: UserProfile) -> ProxyCredential:
    if profile.plan not in PAID_PLANS:
        raise ValueError('Proxy credentials require a paid plan')

    now = timezone.now()
    try:
        credential = profile.user.proxy_credential
        created = False
    except ProxyCredential.DoesNotExist:
        credential = ProxyCredential.objects.create(
            user=profile.user,
            username=_new_username(profile.user),
            password=_random_password(),
            plan=profile.plan,
            whop_membership_id=profile.whop_membership_id,
            is_active=True,
            activated_at=now,
        )
        created = True

    update_fields = []
    for field, value in (
        ('plan', profile.plan),
        ('whop_membership_id', profile.whop_membership_id),
    ):
        if getattr(credential, field) != value:
            setattr(credential, field, value)
            update_fields.append(field)

    if not credential.password:
        credential.password = _random_password()
        update_fields.append('password')
    if not credential.is_active:
        credential.is_active = True
        credential.activated_at = now
        update_fields.extend(['is_active', 'activated_at'])
    if credential.disabled_at is not None or credential.disabled_reason:
        credential.disabled_at = None
        credential.disabled_reason = ''
        update_fields.extend(['disabled_at', 'disabled_reason'])

    if update_fields:
        credential.save(update_fields=sorted(set(update_fields)))

    if created or profile.proxies_generated < 1:
        profile.proxies_generated = max(profile.proxies_generated, 1)
        profile.save(update_fields=['proxies_generated'])

    return credential


def disable_proxy_credential(profile: UserProfile, reason: str = '') -> None:
    try:
        credential = profile.user.proxy_credential
    except ProxyCredential.DoesNotExist:
        return
    if not credential.is_active and credential.disabled_reason == reason:
        return
    credential.is_active = False
    credential.disabled_at = timezone.now()
    credential.disabled_reason = reason[:120]
    credential.save(update_fields=['is_active', 'disabled_at', 'disabled_reason'])


def sync_proxy_access(profile: UserProfile, reason: str = '') -> ProxyCredential | None:
    if profile.user.is_active and profile.has_active_subscription:
        return ensure_proxy_credential(profile)
    disable_proxy_credential(profile, reason=reason or 'inactive subscription')
    return None


def connection_line(credential: ProxyCredential, protocol: str, fmt: str) -> str:
    cfg = gateway_config()
    port = cfg['socks5_port'] if protocol == 'socks5' else cfg['http_port']
    host = cfg['host']
    if fmt == 'user_pass_at_ip_port':
        return f'{credential.username}:{credential.password}@{host}:{port}'
    if fmt == 'url':
        return f'{protocol}://{credential.username}:{credential.password}@{host}:{port}'
    if fmt == 'ip_port':
        return f'{host}:{port}'
    return f'{host}:{port}:{credential.username}:{credential.password}'


def client_ip(request) -> str | None:
    remote = request.META.get('REMOTE_ADDR', '').strip()
    candidate = remote
    try:
        remote_ip = ipaddress.ip_address(remote)
    except ValueError:
        remote_ip = None
    trusted_proxy = bool(remote_ip and (remote_ip.is_loopback or remote_ip.is_private))
    trusted_proxy = trusted_proxy or remote in getattr(settings, 'TRUSTED_PROXY_IPS', [])
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '') if trusted_proxy else ''
    if forwarded_for:
        candidate = forwarded_for.split(',', 1)[0].strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def create_checkout_consent(request, plan: str, period: str) -> CheckoutConsent:
    return CheckoutConsent.objects.create(
        user=request.user,
        plan=plan,
        period=period,
        terms_version=CHECKOUT_TERMS_VERSION,
        ip_address=client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:2000],
    )


def attach_consent_to_membership(user, membership_id: str, consent_id: str = '') -> None:
    if not membership_id:
        return
    qs = CheckoutConsent.objects.filter(user=user)
    if consent_id:
        qs = qs.filter(pk=consent_id)
    else:
        qs = qs.filter(whop_membership_id='')
    consent = qs.order_by('-accepted_at').first()
    if consent:
        consent.whop_membership_id = membership_id
        consent.save(update_fields=['whop_membership_id'])
