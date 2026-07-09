from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import datetime, timedelta


def _default_expires_at():
    return timezone.now() + timedelta(days=30)


class SystemSetting(models.Model):
    key = models.CharField(max_length=200, unique=True)
    value = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'system_settings'

    def __str__(self):
        return f'{self.key} = {self.value[:60]}'


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    plan = models.CharField(max_length=50, default='free')
    bandwidth_used_gb = models.FloatField(default=0)
    proxies_generated = models.IntegerField(default=0)

    # Whop billing fields
    whop_membership_id = models.CharField(max_length=100, blank=True, default='')
    whop_status = models.CharField(max_length=50, blank=True, default='')
    whop_renews_at = models.CharField(max_length=50, blank=True, default='')
    whop_cancelled = models.BooleanField(default=False)
    whop_paused = models.BooleanField(default=False)
    whop_mode = models.CharField(max_length=10, default='prod')
    last_whop_sync_at = models.DateTimeField(null=True, blank=True)
    subscription_start = models.CharField(max_length=20, blank=True, default='')
    billing_period_start = models.CharField(max_length=20, blank=True, default='')
    billing_period_end = models.CharField(max_length=20, blank=True, default='')
    payment_email_last_membership = models.CharField(max_length=100, blank=True, default='')
    payment_email_last_plan = models.CharField(max_length=50, blank=True, default='')
    proxy_api_key = models.CharField(max_length=64, blank=True, default='')

    def __str__(self):
        return f'{self.user.email} — {self.plan}'

    @property
    def has_active_subscription(self):
        if self.plan not in ('starter', 'pro', 'agency') or self.whop_paused:
            return False
        if not self.whop_cancelled:
            return True
        try:
            period_end = datetime.strptime(self.billing_period_end[:10], '%Y-%m-%d').date()
        except (TypeError, ValueError):
            # A cancellation without a reliable period end should not
            # immediately cut off already-paid digital access. Whop sync or a
            # terminal webhook will disable access once the end date/status is
            # known.
            return True
        return period_end >= timezone.localdate()

    @property
    def plan_display(self):
        mapping = {'starter': 'Starter', 'pro': 'Pro', 'agency': 'Business', 'free': 'Free'}
        return mapping.get(self.plan, self.plan.title())

    @property
    def plan_limits(self):
        limits = {
            'free':    {'gb': 0,   'dc_ips': 0,    'threads': 0,     'ipv6': 0},
            'starter': {'gb': 5,   'dc_ips': 25,   'threads': 100,   'ipv6': 500},
            'pro':     {'gb': 25,  'dc_ips': 250,  'threads': 1000,  'ipv6': 5000},
            'agency':  {'gb': 100, 'dc_ips': 2000, 'threads': 10000, 'ipv6': 50000},
        }
        return limits.get(self.plan, limits['free'])


class ProxyCredential(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='proxy_credential')
    username = models.CharField(max_length=80, unique=True)
    password = models.CharField(max_length=128)
    plan = models.CharField(max_length=50, blank=True, default='')
    whop_membership_id = models.CharField(max_length=100, blank=True, default='')
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    rotated_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    credentials_emailed_at = models.DateTimeField(null=True, blank=True)
    disabled_reason = models.CharField(max_length=120, blank=True, default='')

    class Meta:
        indexes = [
            models.Index(fields=['is_active', 'plan']),
            models.Index(fields=['whop_membership_id']),
        ]

    def __str__(self):
        status = 'active' if self.is_active else 'disabled'
        return f'{self.user.email} — {self.username} ({status})'


class CheckoutConsent(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='checkout_consents')
    plan = models.CharField(max_length=50)
    period = models.CharField(max_length=20)
    terms_version = models.CharField(max_length=40)
    accepted_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    whop_membership_id = models.CharField(max_length=100, blank=True, default='')
    checkout_url = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-accepted_at']
        indexes = [
            models.Index(fields=['user', 'accepted_at']),
            models.Index(fields=['whop_membership_id']),
        ]

    def __str__(self):
        return f'{self.user.email} — {self.plan}/{self.period} terms {self.terms_version}'


class EmailLoginCode(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='email_login_codes')
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_to = models.EmailField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f'{self.user.email} login code at {self.created_at:%Y-%m-%d %H:%M}'


class Purchase(models.Model):
    STATUS_CHOICES = [('active', 'Active'), ('expired', 'Expired'), ('cancelled', 'Cancelled')]
    TYPE_CHOICES = [('residential', 'Residential'), ('datacenter', 'Datacenter'), ('ipv6', 'IPv6')]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='purchases')
    plan_id = models.CharField(max_length=100)
    plan_name = models.CharField(max_length=200)
    plan_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=_default_expires_at)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.email} — {self.plan_name}'


class Invoice(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='invoices')
    invoice_id = models.CharField(max_length=50, unique=True)
    plan = models.CharField(max_length=50)
    plan_label = models.CharField(max_length=100)
    amount = models.CharField(max_length=20)
    amount_cents = models.IntegerField(default=0)
    date = models.CharField(max_length=50)
    period = models.CharField(max_length=100, blank=True, default='')
    period_start = models.CharField(max_length=20, blank=True, default='')
    period_end = models.CharField(max_length=20, blank=True, default='')
    status = models.CharField(max_length=20, default='paid')
    payment = models.CharField(max_length=50, default='Whop')
    billing_reason = models.CharField(max_length=50, blank=True, default='')
    whop_membership_id = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.invoice_id} — {self.user.email}'


class BlogPost(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('published', 'Published')]

    title            = models.CharField(max_length=200)
    slug             = models.SlugField(max_length=220, unique=True)
    excerpt          = models.TextField(blank=True, default='')
    content          = models.TextField(default='')
    cover_image_url  = models.URLField(blank=True, default='')
    meta_description = models.TextField(blank=True, default='')
    tags             = models.CharField(max_length=300, blank=True, default='')
    status           = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    author           = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    ai_generated     = models.BooleanField(default=False)
    source_url       = models.URLField(blank=True, default='')
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)
    published_at     = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-published_at', '-created_at']

    def __str__(self):
        return self.title

    @property
    def reading_time(self):
        words = len(self.content.split())
        return max(1, round(words / 200))

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(',') if t.strip()]


class SupportMessage(models.Model):
    STATUS_CHOICES = [('open', 'Open'), ('replied', 'Replied'), ('closed', 'Closed')]
    PRIORITY_CHOICES = [('critical', 'Critical'), ('high', 'High'), ('normal', 'Normal'), ('low', 'Low')]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='support_messages')
    subject = models.CharField(max_length=200)
    body = models.TextField()
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='normal')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    reply_body = models.TextField(blank=True, default='')
    replied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.email} — {self.subject} ({self.status})'
