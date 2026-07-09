from django.contrib import admin
from .models import CheckoutConsent, ProxyCredential, Purchase, SupportMessage, SystemSetting, UserProfile

admin.site.register(UserProfile)
admin.site.register(SystemSetting)
admin.site.register(Purchase)
admin.site.register(SupportMessage)
admin.site.register(ProxyCredential)
admin.site.register(CheckoutConsent)
