from django.contrib import admin
from .models import UserProfile, Purchase, SupportMessage

admin.site.register(UserProfile)
admin.site.register(Purchase)
admin.site.register(SupportMessage)
