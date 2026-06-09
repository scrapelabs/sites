"""Django signal handlers. Auto-creates UserProfile and mirrors writes to TinyDB."""
from django.contrib.auth.models import User
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import (
    BlogPost,
    Invoice,
    Purchase,
    SupportMessage,
    SystemSetting,
    UserProfile,
)
from . import persistence

# Models whose writes/deletes we mirror to TinyDB.
# Ordering matters for restore (FK deps): User first, then everything else.
PERSISTED = [User, UserProfile, SystemSetting, BlogPost, Purchase, Invoice, SupportMessage]
for _m in PERSISTED:
    persistence.register(_m)


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


def _make_save_handler(model):
    def handler(sender, instance, **kwargs):
        try:
            persistence.upsert_row(instance)
        except Exception as exc:  # never break the request on a sync failure
            print(f"[persistence] upsert {model.__name__} failed: {exc!r}")
    return handler


def _make_delete_handler(model):
    def handler(sender, instance, **kwargs):
        try:
            persistence.delete_row(instance)
        except Exception as exc:
            print(f"[persistence] delete {model.__name__} failed: {exc!r}")
    return handler


for _m in PERSISTED:
    post_save.connect(_make_save_handler(_m), sender=_m, weak=False)
    post_delete.connect(_make_delete_handler(_m), sender=_m, weak=False)
