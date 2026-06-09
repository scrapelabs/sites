"""Load blog posts from data/blog_posts.json into the DB. Used by start.sh after migrate."""
import os, sys, json, django
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "goldenproxies.settings")
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone
from core.models import BlogPost

User = get_user_model()
SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "blog_posts.json"


def main():
    if not SNAPSHOT.exists():
        print(f"[load_blog] No snapshot at {SNAPSHOT}, skipping.")
        return
    posts = json.loads(SNAPSHOT.read_text())
    if not posts:
        print("[load_blog] Snapshot is empty, skipping.")
        return
    admin = User.objects.filter(is_superuser=True).first()
    if not admin:
        print("[load_blog] No superuser found, skipping.")
        return
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
    print(f"[load_blog] Restored {created} posts from JSON snapshot ({len(posts)} total in file).")


if __name__ == "__main__":
    main()
