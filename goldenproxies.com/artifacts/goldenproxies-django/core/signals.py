"""Django signal handlers. Auto-creates UserProfile and mirrors safe rows to TinyDB."""
from django.contrib.auth.models import User
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import (
    BlogPost,
    UserProfile,
)
from . import persistence

# Only mirror non-secret content. Do not persist users, password hashes, system
# settings/API keys, proxy credentials, checkout PII, invoices, or support data
# into data/persistent.json.
PERSISTED = [
    BlogPost,
]
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
