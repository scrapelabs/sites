"""One-shot: scrape plainproxies.com/blog, rewrite each post with Claude, save to DB + JSON snapshot."""
import os, sys, re, json, time, django
from pathlib import Path
from urllib.parse import urlparse

JSON_SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "blog_posts.json"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "goldenproxies.settings")
django.setup()

import requests
from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify
from anthropic import Anthropic
from core.models import BlogPost

User = get_user_model()

POSTS = [
    "https://plainproxies.com/blog/integrations/ethically-sourced-residential-proxies",
    "https://plainproxies.com/blog/integrations/hidden-costs-of-bandwidth-unlimited-proxy-plans",
    "https://plainproxies.com/blog/integrations/ipv4-vs-ipv6-proxies-automation",
    "https://plainproxies.com/blog/integrations/ipv6-proxies-real-estate-listing-collection",
    "https://plainproxies.com/blog/integrations/isp-residential-proxies-account-management",
    "https://plainproxies.com/blog/integrations/residential-isp-proxies-market-research",
    "https://plainproxies.com/blog/integrations/residential-proxies-ad-verification-brand-protection",
    "https://plainproxies.com/blog/integrations/unlimited-residential-proxies-browser-automation",
    "https://plainproxies.com/blog/integrations/what-are-proxy-servers",
]

UA = {"User-Agent": "Mozilla/5.0 (compatible; GoldenProxiesBot/1.0)"}

client = Anthropic(
    base_url=os.environ["AI_INTEGRATIONS_ANTHROPIC_BASE_URL"],
    api_key=os.environ["AI_INTEGRATIONS_ANTHROPIC_API_KEY"],
)

SYS_PROMPT = """You are a senior content writer for GoldenProxies, a premium proxy services brand with a luxurious gold/white aesthetic. You will receive a competitor blog post (PlainProxies). Your job is to rewrite it as an original GoldenProxies article: same general topic and educational value, but completely new wording, structure, and voice.

Voice & style:
- Confident, helpful, professional. No fluff, no AI tells.
- Speak as GoldenProxies (use "we", "our team", "GoldenProxies customers" sparingly — never name competitors).
- Premium but accessible — like a great B2B SaaS blog (Cloudflare, Stripe).

Hard rules:
- Do NOT mention "PlainProxies", "Plain Proxies", or any competitor by name.
- Do NOT copy sentences. Reorder ideas, add fresh examples, vary sentence length.
- Output VALID JSON only, no markdown fences, no commentary outside JSON.
- HTML body must use semantic tags: <h2>, <h3>, <p>, <ul>, <li>, <strong>, <em>, <blockquote>. No <h1> (title is separate). No inline styles. No <script>.
- 800-1400 words in body.

Return JSON with exactly these keys:
{
  "title": "string, <=90 chars, compelling and SEO-friendly",
  "slug": "string, kebab-case, <=80 chars",
  "excerpt": "string, 140-200 chars, hook for blog list",
  "meta_description": "string, 140-160 chars, SEO meta",
  "tags": "string, 3-6 comma-separated lowercase tags",
  "content": "string, full HTML body"
}"""


def scrape(url: str) -> dict:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    title = (soup.find("h1") or soup.find("title")).get_text(strip=True)
    # Try common content containers
    article = (
        soup.find("article")
        or soup.find("div", class_=re.compile(r"(entry-content|post-content|article-content)"))
        or soup.find("main")
    )
    if article is None:
        article = soup.body
    # Strip nav/footer/script/style
    for tag in article.find_all(["script", "style", "nav", "footer", "header", "form", "aside"]):
        tag.decompose()
    text = article.get_text("\n", strip=True)
    # Cover image
    img = soup.find("meta", property="og:image")
    cover = img["content"] if img and img.get("content") else ""
    return {"title": title, "text": text[:18000], "cover": cover}


def rewrite(scraped: dict, source_url: str) -> dict:
    user_msg = (
        f"SOURCE URL: {source_url}\n"
        f"ORIGINAL TITLE: {scraped['title']}\n\n"
        f"ORIGINAL CONTENT (raw text):\n{scraped['text']}\n\n"
        "Rewrite this as an original GoldenProxies article. Return JSON only."
    )
    tool = {
        "name": "publish_post",
        "description": "Publish the rewritten GoldenProxies blog article.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "<=90 chars, SEO-friendly"},
                "slug": {"type": "string", "description": "kebab-case <=80 chars"},
                "excerpt": {"type": "string", "description": "140-200 chars hook"},
                "meta_description": {"type": "string", "description": "140-160 chars SEO"},
                "tags": {"type": "string", "description": "3-6 comma-separated lowercase tags"},
                "content": {"type": "string", "description": "Full HTML body, 800-1400 words. Use <h2>, <h3>, <p>, <ul>, <li>, <strong>, <em>, <blockquote>. No <h1>, no inline styles, no <script>."},
            },
            "required": ["title", "slug", "excerpt", "meta_description", "tags", "content"],
        },
    }
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=SYS_PROMPT,
        tools=[tool],
        tool_choice={"type": "tool", "name": "publish_post"},
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in msg.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "publish_post":
            return block.input
    raise RuntimeError("Claude did not return tool_use block")


def load_snapshot() -> dict:
    if JSON_SNAPSHOT.exists():
        return {p["source_url"]: p for p in json.loads(JSON_SNAPSHOT.read_text())}
    return {}


def save_snapshot(snapshot: dict):
    JSON_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    JSON_SNAPSHOT.write_text(json.dumps(list(snapshot.values()), indent=2, ensure_ascii=False))


def main():
    admin = User.objects.filter(is_superuser=True).first()
    if not admin:
        print("ERROR: no superuser found")
        return
    print(f"Author: {admin.email}")
    snapshot = load_snapshot()
    print(f"JSON snapshot has {len(snapshot)} posts")
    done_urls = set(BlogPost.objects.filter(source_url__in=POSTS).values_list("source_url", flat=True))
    print(f"DB already has: {len(done_urls)} / {len(POSTS)}")

    for i, url in enumerate(POSTS, 1):
        if url in done_urls and url in snapshot:
            print(f"[{i}/{len(POSTS)}] SKIP (already imported): {url}")
            continue
        print(f"\n[{i}/{len(POSTS)}] {url}")
        try:
            # If JSON has it but DB doesn't, just restore from snapshot
            if url in snapshot and url not in done_urls:
                d = snapshot[url]
                slug = d["slug"]
                if BlogPost.objects.filter(slug=slug).exists():
                    print(f"  slug {slug} already exists in DB, skipping")
                    continue
                post = BlogPost.objects.create(
                    title=d["title"], slug=slug, excerpt=d.get("excerpt", ""),
                    content=d["content"], cover_image_url=d.get("cover_image_url", ""),
                    meta_description=d.get("meta_description", ""), tags=d.get("tags", ""),
                    status="published", author=admin, ai_generated=True, source_url=url,
                    published_at=timezone.now(),
                )
                print(f"  ↻ restored from snapshot id={post.id} slug={post.slug}")
                continue

            s = scrape(url)
            print(f"  scraped: {s['title'][:70]} ({len(s['text'])} chars)")
            data = rewrite(s, url)
            slug = slugify(data["slug"])[:200]
            base_slug = slug
            n = 2
            while BlogPost.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{n}"
                n += 1
            post = BlogPost.objects.create(
                title=data["title"][:200],
                slug=slug,
                excerpt=data.get("excerpt", "")[:500],
                content=data["content"],
                cover_image_url=s["cover"][:200] if s["cover"] else "",
                meta_description=data.get("meta_description", "")[:300],
                tags=data.get("tags", "")[:300],
                status="published",
                author=admin,
                ai_generated=True,
                source_url=url,
                published_at=timezone.now(),
            )
            snapshot[url] = {
                "title": post.title, "slug": post.slug, "excerpt": post.excerpt,
                "content": post.content, "cover_image_url": post.cover_image_url,
                "meta_description": post.meta_description, "tags": post.tags,
                "source_url": url,
            }
            save_snapshot(snapshot)
            print(f"  ✓ saved id={post.id} slug={post.slug} (snapshot updated)")
            time.sleep(1)
        except Exception as e:
            print(f"  ✗ FAILED: {e!r}")

    print(f"\nDone. Total published posts: {BlogPost.objects.filter(status='published').count()}")
    print(f"Snapshot file: {JSON_SNAPSHOT} ({len(snapshot)} posts)")


if __name__ == "__main__":
    main()
