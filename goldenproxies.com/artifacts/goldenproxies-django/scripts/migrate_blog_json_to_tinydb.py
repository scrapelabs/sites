"""One-shot: take the legacy data/blog_posts.json snapshot, ensure those posts
exist in the live DB, then dump every persisted table into TinyDB."""
import os, sys, json, django
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "goldenproxies.settings")
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone
from core.models import BlogPost
from core import persistence
import core.signals  # noqa: F401

User = get_user_model()
LEGACY = Path(__file__).resolve().parent.parent / "data" / "blog_posts.json"


def main():
    # 1. Restore legacy blog_posts.json into the DB if it exists
    if LEGACY.exists():
        posts = json.loads(LEGACY.read_text())
        admin = User.objects.filter(is_superuser=True).first()
        if admin:
            created = 0
            for d in posts:
                if BlogPost.objects.filter(slug=d["slug"]).exists():
                    continue
                BlogPost.objects.create(
                    title=d["title"], slug=d["slug"], excerpt=d.get("excerpt", ""),
                    content=d["content"], cover_image_url=d.get("cover_image_url", ""),
                    meta_description=d.get("meta_description", ""), tags=d.get("tags", ""),
                    status="published", author=admin, ai_generated=True,
                    source_url=d.get("source_url", ""), published_at=timezone.now(),
                )
                created += 1
            print(f"[migrate] loaded {created} new blog posts from legacy JSON ({len(posts)} total)")
    else:
        print("[migrate] no legacy data/blog_posts.json present")

    # 2. Snapshot the entire DB into TinyDB
    counts = persistence.dump_all()
    total = sum(counts.values())
    print(f"[migrate] snapshotted {total} rows into TinyDB at {persistence.DB_PATH}:")
    for table, n in counts.items():
        print(f"  - {table}: {n}")


if __name__ == "__main__":
    main()
