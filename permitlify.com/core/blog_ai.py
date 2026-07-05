"""
AI clients for the admin blog editor.

Two thin urllib-based wrappers (no extra dependencies):

* ``firecrawl_scrape(url)`` — POST to firecrawl.dev/v1/scrape, returns the
  cleaned markdown + metadata (title, description, ogImage, ...).
* ``inference_rewrite(scraped, hint)`` — runs the scraped content through the
  local GPT-OSS endpoint (the same OpenAI-compatible endpoint the scrapers use, via
  ``core.scrapers.base.oss_complete``) and asks the model to return a single
  JSON object with all the fields the ``blog_posts`` table needs.

The rewrite model is read from the ``system_settings`` table at call time
(key ``blog_rewrite_model``) so the admin can switch models through the UI
without a redeploy.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.request

from . import db
from .db import get_system_setting

log = logging.getLogger(__name__)


FIRECRAWL_URL = 'https://api.firecrawl.dev/v1/scrape'
# Default local GPT-OSS model for blog rewriting. The admin can
# override this per-install via the ``blog_rewrite_model`` system_setting.
DEFAULT_MODEL = 'gpt-oss-20b-mxfp4'


class BlogAIError(Exception):
    """Raised on any upstream / config / parse failure. The string form is
    safe to surface directly to the admin UI."""


# ─────────────────────────── Firecrawl ────────────────────────────────

def firecrawl_scrape(url: str, timeout: int = 60) -> dict:
    """Scrape ``url`` via Firecrawl and return ``{markdown, metadata}``.

    Raises ``BlogAIError`` with a human-readable message on any failure.
    """
    url = (url or '').strip()
    if not url:
        raise BlogAIError('URL is required')
    if not (url.startswith('http://') or url.startswith('https://')):
        raise BlogAIError('URL must start with http:// or https://')

    api_key = (get_system_setting('firecrawl_api_key') or '').strip()
    if not api_key:
        raise BlogAIError('Firecrawl API key is not configured. Add it in Blog AI Settings.')

    body = json.dumps({
        'url': url,
        'formats': ['markdown'],
        'onlyMainContent': True,
    }).encode('utf-8')

    req = urllib.request.Request(
        FIRECRAWL_URL,
        data=body,
        method='POST',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
    )

    _t0 = time.monotonic()
    _status = None
    _bytes = 0
    _err = None
    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _status = resp.getcode()
                raw_body = resp.read()
                _bytes = len(raw_body)
                payload = json.loads(raw_body.decode('utf-8'))
        except urllib.error.HTTPError as e:
            _status = e.code
            try:
                err_body = json.loads(e.read().decode('utf-8'))
                err_msg = err_body.get('error') or err_body.get('message') or str(err_body)
            except Exception:
                err_msg = f'HTTP {e.code}'
            _err = f'Firecrawl error: {err_msg}'
            raise BlogAIError(_err) from e
        except urllib.error.URLError as e:
            _err = f'Firecrawl network error: {e.reason}'
            raise BlogAIError(_err) from e
        except Exception as e:
            _err = f'Firecrawl failed: {e}'
            raise BlogAIError(_err) from e

        if not payload.get('success', True):
            _err = f"Firecrawl reported failure: {payload.get('error', 'unknown')}"
            raise BlogAIError(_err)

        data = payload.get('data') or {}
        markdown = (data.get('markdown') or '').strip()
        metadata = data.get('metadata') or {}
        if not markdown:
            _err = 'Firecrawl returned no markdown content for that URL.'
            raise BlogAIError(_err)
    finally:
        try:
            db.record_firecrawl_call(
                source='blog', mode='blog', url=url,
                status_code=_status,
                latency_ms=int((time.monotonic() - _t0) * 1000),
                response_bytes=_bytes or None,
                error=_err,
            )
        except Exception:
            log.exception('blog firecrawl usage recording failed')

    return {'markdown': markdown, 'metadata': metadata}


# ──────────────────────── DO Serverless Inference ─────────────────────

REWRITE_SYSTEM_PROMPT = """You are the staff content editor for Permitlify, a SaaS that delivers daily building-permit leads to contractors, suppliers, and home-services businesses across the United States.

Your job: take the scraped article the user gives you and rewrite it as a publish-ready Permitlify blog post. The rewrite must:

* Be ORIGINAL prose — never copy sentences verbatim. Restructure, condense, and add Permitlify's editorial voice.
* Be aimed at contractors, builders, remodelers, roofers, plumbers, HVAC, solar, and similar trades who buy permit-data leads.
* Sound confident, practical, and slightly opinionated — never marketing fluff.
* Use <h2> and <h3> headings to break the body into scannable sections.
* Use short paragraphs (2-4 sentences) and <ul><li> bullet lists where useful.
* Be 800-1500 words of body content.
* Naturally mention permit-data / lead-generation themes when the source material is relevant; do NOT force-fit Permitlify into off-topic articles.
* End with one short call-to-action paragraph that invites the reader to try Permitlify.

CONTENT FORMAT — VERY IMPORTANT:
The "content" field MUST be valid HTML, NOT markdown. The body is rendered with `|safe` directly into the page, so any markdown literals (## headings, **bold**, - bullets) will display as raw text and look broken.

Allowed tags: <p>, <h2>, <h3>, <ul>, <ol>, <li>, <strong>, <em>, <a href="...">, <blockquote>, <hr>.
Do NOT use markdown syntax anywhere. Do NOT include the article title inside content. Do NOT wrap content in <html>/<body>/<article>.

Example of correct content shape:
<p>Opening lede paragraph that hooks the reader.</p>
<h2>First section heading</h2>
<p>Section body. Use <strong>bold</strong> for emphasis.</p>
<ul><li>Bullet one</li><li>Bullet two</li></ul>
<h2>Next section</h2>
<p>And so on.</p>

You MUST respond with a single JSON object and nothing else (no prose before or after, no markdown code fence). The JSON shape is:

{
  "title": "Catchy title, 50-70 chars",
  "slug": "lowercase-hyphenated-slug-no-stopwords",
  "tag": "Insights | Guides | Industry | Permits | Growth",
  "tag_color": "blue | green | purple | orange | red",
  "thumb": "single emoji that fits the topic",
  "thumb_bg": "linear-gradient(135deg,#hex1,#hex2) — pick colors that pair with the emoji mood",
  "excerpt": "1-2 sentence dek, max 180 chars, no period at end optional",
  "read_time": "X min read",
  "content": "Full body as HTML using only the tags listed above. NO markdown."
}

Quality bar: a Permitlify reader should learn something concrete in the first 30 seconds. Do not pad. Do not hedge. Write like an editor who has seen 1,000 contractor blogs."""


# Lightweight markdown → HTML converter. Acts as a safety net so the editor
# never publishes raw markdown literals if the model ignores the prompt.
# Idempotent on already-HTML content.
_MD_HEADING1_RE   = re.compile(r'^#\s+(.+)$')
_MD_HEADING2_RE   = re.compile(r'^##\s+(.+)$')
_MD_HEADING3_RE   = re.compile(r'^###\s+(.+)$')
_MD_HR_RE         = re.compile(r'^(---+|\*\*\*+|___+)\s*$')
_MD_ULITEM_RE     = re.compile(r'^\s*[-*]\s+(.+)$')
_MD_OLITEM_RE     = re.compile(r'^\s*\d+\.\s+(.+)$')
_MD_QUOTE_RE      = re.compile(r'^\s*>\s?(.*)$')
_MD_BOLD_RE       = re.compile(r'\*\*([^*\n]+)\*\*')
_MD_EM_AST_RE     = re.compile(r'(?<![\w*])\*([^*\n]+)\*(?![\w*])')
_MD_EM_UND_RE     = re.compile(r'(?<![\w_])_([^_\n]+)_(?![\w_])')
_MD_CODE_RE       = re.compile(r'`([^`\n]+)`')
_MD_LINK_RE       = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')
# Markdown signals — any of these in the body forces conversion. Includes
# the looser cases (single ``# `` heading, single ``*italic*``, ``[link](url)``)
# so a body that is mostly HTML but has a stray markdown fragment is still
# normalised rather than passed through.
_MD_SIGNAL_RE     = re.compile(
    r'(?m)^(#{1,3}\s|[-*]\s|\d+\.\s|>\s|---|\*\*\*|___)'
    r'|\*\*[^*\n]+\*\*'
    r'|(?<![\w*])\*[^*\n]+\*(?![\w*])'
    r'|(?<![\w_])_[^_\n]+_(?![\w_])'
    r'|\[[^\]]+\]\([^)\s]+\)'
)
_HTML_BLOCK_RE    = re.compile(r'(?i)<(p|h[1-6]|ul|ol|blockquote|hr|div|section|article)\b')

# Defensive HTML sanitizer (stdlib regex). The Claude output we trust is
# rendered via ``{{ article.content|safe }}``, so even though the source is
# our own AI we strip script/style blocks, JS event-handler attributes, and
# unsafe URL schemes before render. This is hygiene, not a hard XSS firewall.
_SANITIZE_BLOCK_RE   = re.compile(
    r'(?is)<(script|style|iframe|object|embed)\b[^>]*>.*?</\1\s*>'
)
_SANITIZE_VOID_RE    = re.compile(
    r'(?i)<(script|style|iframe|object|embed)\b[^>]*/?>'
)
_SANITIZE_HANDLER_RE = re.compile(
    r'''(?i)\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)'''
)
_SANITIZE_BAD_URL_RE = re.compile(
    r'''(?i)\b(href|src|formaction|action|xlink:href)\s*=\s*'''
    r'''(?:"\s*(?:javascript|vbscript|data):[^"]*"'''
    r'''|'\s*(?:javascript|vbscript|data):[^']*')'''
)


def _sanitize_html(text: str) -> str:
    """Strip script/style blocks, JS event-handler attributes, and unsafe
    URL schemes from an HTML fragment. Idempotent."""
    if not text:
        return ''
    text = _SANITIZE_BLOCK_RE.sub('', text)
    text = _SANITIZE_VOID_RE.sub('', text)
    text = _SANITIZE_HANDLER_RE.sub('', text)
    text = _SANITIZE_BAD_URL_RE.sub(r' \1="#"', text)
    return text


def _md_inline(s: str) -> str:
    """Apply inline markdown transforms (links, bold, italic, code)."""
    s = _MD_LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>',
        s,
    )
    s = _MD_BOLD_RE.sub(r'<strong>\1</strong>', s)
    s = _MD_EM_UND_RE.sub(r'<em>\1</em>', s)
    s = _MD_EM_AST_RE.sub(r'<em>\1</em>', s)
    s = _MD_CODE_RE.sub(r'<code>\1</code>', s)
    return s


def md_to_html(text: str) -> str:
    """Convert lightweight markdown (h1/h2/h3, bold/em, lists, blockquotes,
    horizontal rules, links) into the HTML subset the blog-post template
    expects, then run a defensive HTML sanitizer over the result.

    Pass-through behaviour: if the input already contains HTML block tags
    (<p>, <h2>, <ul>, ...) AND has no markdown signal, the body is sanitized
    and returned unchanged. Idempotent on already-clean HTML.

    Mixed content: lines that START with an HTML tag are emitted verbatim
    (after sanitizing) so the converter never wraps an existing ``<p>...</p>``
    inside another ``<p>``. Plain-prose lines are accumulated into ``<p>``
    blocks; markdown blocks (headings/lists/quotes/hr) are converted normally.
    """
    text = (text or '').strip()
    if not text:
        return ''

    has_md   = bool(_MD_SIGNAL_RE.search(text))
    has_html = bool(_HTML_BLOCK_RE.search(text))
    if has_html and not has_md:
        return _sanitize_html(text)

    # Walk lines; group consecutive same-kind lines into blocks.
    lines = text.split('\n')
    out: list[str] = []
    i = 0
    n = len(lines)

    def _flush_paragraph(buf: list[str]):
        if buf:
            joined = ' '.join(s.strip() for s in buf if s.strip())
            if joined:
                out.append(f'<p>{_md_inline(joined)}</p>')

    para_buf: list[str] = []

    while i < n:
        ln = lines[i]
        stripped = ln.strip()

        # Blank line → paragraph break
        if not stripped:
            _flush_paragraph(para_buf)
            para_buf = []
            i += 1
            continue

        # Line that starts with an HTML tag → emit verbatim. This prevents
        # nesting an existing <p>...</p> inside another <p> when a body
        # mixes HTML and markdown.
        if stripped.startswith('<'):
            _flush_paragraph(para_buf); para_buf = []
            out.append(ln)
            i += 1
            continue

        # Horizontal rule
        if _MD_HR_RE.match(stripped):
            _flush_paragraph(para_buf); para_buf = []
            out.append('<hr>')
            i += 1
            continue

        # H3
        m = _MD_HEADING3_RE.match(stripped)
        if m:
            _flush_paragraph(para_buf); para_buf = []
            out.append(f'<h3>{_md_inline(m.group(1).strip())}</h3>')
            i += 1
            continue

        # H2
        m = _MD_HEADING2_RE.match(stripped)
        if m:
            _flush_paragraph(para_buf); para_buf = []
            out.append(f'<h2>{_md_inline(m.group(1).strip())}</h2>')
            i += 1
            continue

        # H1 → also rendered as <h2> because the page already shows the
        # post title in an <h1>. We never want two H1s on a page.
        m = _MD_HEADING1_RE.match(stripped)
        if m:
            _flush_paragraph(para_buf); para_buf = []
            out.append(f'<h2>{_md_inline(m.group(1).strip())}</h2>')
            i += 1
            continue

        # Unordered list (consume contiguous bullet lines)
        if _MD_ULITEM_RE.match(ln):
            _flush_paragraph(para_buf); para_buf = []
            items = []
            while i < n and _MD_ULITEM_RE.match(lines[i]):
                items.append(_MD_ULITEM_RE.match(lines[i]).group(1).strip())
                i += 1
            out.append('<ul>' + ''.join(f'<li>{_md_inline(it)}</li>' for it in items) + '</ul>')
            continue

        # Ordered list
        if _MD_OLITEM_RE.match(ln):
            _flush_paragraph(para_buf); para_buf = []
            items = []
            while i < n and _MD_OLITEM_RE.match(lines[i]):
                items.append(_MD_OLITEM_RE.match(lines[i]).group(1).strip())
                i += 1
            out.append('<ol>' + ''.join(f'<li>{_md_inline(it)}</li>' for it in items) + '</ol>')
            continue

        # Blockquote (consume contiguous '> ' lines)
        if _MD_QUOTE_RE.match(ln):
            _flush_paragraph(para_buf); para_buf = []
            quoted = []
            while i < n and _MD_QUOTE_RE.match(lines[i]):
                quoted.append(_MD_QUOTE_RE.match(lines[i]).group(1).strip())
                i += 1
            body = ' '.join(q for q in quoted if q)
            out.append(f'<blockquote><p>{_md_inline(body)}</p></blockquote>')
            continue

        # Plain prose — accumulate into the paragraph buffer
        para_buf.append(ln)
        i += 1

    _flush_paragraph(para_buf)
    return _sanitize_html('\n'.join(out))


def _clean_str(v, default: str = '') -> str:
    return (str(v).strip() if v is not None else default) or default


def _slugify(text: str, fallback: str = 'untitled') -> str:
    """Lowercase, ascii-fold, hyphenated slug. Trimmed to 120 chars."""
    text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^a-zA-Z0-9]+', '-', text).strip('-').lower()
    return (text or fallback)[:120]


def _extract_json(raw: str) -> dict:
    """Pull the first ``{...}`` block out of the model's reply. Tolerates
    accidental prose / code-fence wrapping even though we ask it not to."""
    raw = (raw or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```\s*$', '', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find('{')
    end = raw.rfind('}')
    if start == -1 or end == -1 or end <= start:
        raise BlogAIError('The model did not return JSON. Try Rewrite again.')
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise BlogAIError(f'The model returned malformed JSON: {e}') from e


def inference_rewrite(scraped_markdown: str, source_url: str = '',
                      extra_hint: str = '', timeout: int = 120) -> dict:
    """Rewrite ``scraped_markdown`` via local GPT-OSS
    and return a normalised dict ready for ``upsert_blog_post()``.

    Uses the same inference endpoint as the scrapers
    (``core.scrapers.base.oss_complete``). The model is read from the
    ``blog_rewrite_model`` system_setting, falling back to ``DEFAULT_MODEL``.
    All shape + key normalisation happens here so the view layer just
    forwards the dict into the form.
    """
    if not (scraped_markdown or '').strip():
        raise BlogAIError('Nothing to rewrite — scrape a URL first.')

    # Imported lazily to avoid a circular import at module load and to
    # reuse the scrapers' OpenAI-compatible GPT-OSS client.
    from .scrapers.base import oss_complete, HttpScraperError

    model = (get_system_setting('blog_rewrite_model') or '').strip() or DEFAULT_MODEL

    user_prompt_parts = []
    if source_url:
        user_prompt_parts.append(f'Source URL: {source_url}')
    if extra_hint:
        user_prompt_parts.append(f'Editor note: {extra_hint}')
    user_prompt_parts.append('Scraped article (markdown):\n\n' + scraped_markdown)
    user_prompt = '\n\n'.join(user_prompt_parts)

    try:
        result = oss_complete(
            user_prompt,
            model=model,
            system=REWRITE_SYSTEM_PROMPT,
            max_tokens=8000,
            temperature=0.7,
            timeout=timeout,
            source='blog',
        )
    except HttpScraperError as e:
        raise BlogAIError(str(e)) from e

    text = (result.get('text') or '')
    if not text.strip():
        raise BlogAIError('The model returned an empty response.')

    raw = _extract_json(text)

    title = _clean_str(raw.get('title'), 'Untitled')
    slug = _slugify(_clean_str(raw.get('slug')) or title)
    # Defensive: convert any leaked markdown to HTML so the body always
    # renders cleanly through ``{{ article.content|safe }}`` even if the
    # model ignores the prompt's HTML-only requirement.
    body_html = md_to_html(_clean_str(raw.get('content'), ''))
    return {
        'title':     title,
        'slug':      slug,
        'tag':       _clean_str(raw.get('tag'), 'Insights')[:60],
        'tag_color': _clean_str(raw.get('tag_color'), 'blue')[:20],
        'thumb':     _clean_str(raw.get('thumb'), '📝')[:8],
        'thumb_bg':  _clean_str(raw.get('thumb_bg'),
                                'linear-gradient(135deg,#1d4ed8,#059669)'),
        'excerpt':   _clean_str(raw.get('excerpt'), '')[:500],
        'read_time': _clean_str(raw.get('read_time'), '5 min read')[:40],
        'content':   body_html,
    }


def slugify(text: str, fallback: str = 'untitled') -> str:
    """Public re-export — used by the view layer when the admin edits the
    title and we need to regenerate the slug."""
    return _slugify(text, fallback)
