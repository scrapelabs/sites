"""Shared utilities for every per-source scraper engine.

Adding a new municipal source? Copy ``accela.py`` as a template and
import everything you need from this module — proxy-aware HTTP client,
ASP.NET state parser, HTML cleaner, GPT-OSS inference call. Keep the
source-specific helpers (search-form field names, pagination
convention, detail-URL shape) inside the source's own file.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from ..db import get_system_setting, record_claude_call

log = logging.getLogger(__name__)


# ── Local GPT-OSS inference ────────────────────────────────────────────

DO_BASE_URL_DEFAULT = os.environ.get('GPT_OSS_BASE_URL', 'http://127.0.0.1:8010/v1')

# Model catalogue. Prices are zero because Permitlify now points at the
# local llama.cpp GPT-OSS server instead of a paid hosted inference API.
OSS_MODELS = {
    'gpt-oss-20b-mxfp4': {'input_price': 0.0, 'output_price': 0.0},
    'openai-gpt-oss-20b': {'input_price': 0.0, 'output_price': 0.0},
}
DEFAULT_OSS_MODEL = os.environ.get('GPT_OSS_MODEL', 'gpt-oss-20b-mxfp4')

# Backwards-compat alias for callers that imported the literal constant
# before the base_url became admin-editable.
DO_BASE_URL = DO_BASE_URL_DEFAULT


class HttpScraperError(Exception):
    """Raised by helpers in this package — carries a user-readable msg."""


# ── HTML cleaning ─────────────────────────────────────────────────────
# Verbatim from the user's reference parser
# (attached_assets/accela_parser_test_*.py). Per user instruction:
# "use this in html … to clean it don't change anything".

def clean_html(html: str) -> str:
    """Remove script, style, meta, link, comment tags but keep HTML structure."""
    if not html:
        return ''
    # Remove <!-- comments -->
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    # Remove <script>...</script>
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove <style>...</style>
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove <meta ...> tags
    html = re.sub(r'<meta[^>]*/?>', '', html, flags=re.IGNORECASE)
    # Remove <link ...> tags
    html = re.sub(r'<link[^>]*/?>', '', html, flags=re.IGNORECASE)
    # Remove <noscript>...</noscript>
    html = re.sub(r'<noscript[^>]*>.*?</noscript>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Collapse multiple blank lines into one
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


# ── Visible-text extractor (HTML → just the text the user sees) ──────
#
# Source of truth: ``core.helpers.accela_parser`` — a verbatim copy of
# the user's reference parser at ``attached_assets/
# accela_parser_test_*.py`` (per project rule "USE HIS REFERENCE
# PARSER VERBATIM" + "don't reinvent — put helpers in helpers"). Any
# behaviour change MUST land in that helpers file, never here.
#
# We re-export the names below so existing call sites
# (``from .base import extract_permit_text``) keep working without an
# import-path churn across every per-source scraper.
from ..helpers.accela_parser import (
    _NOISE_LINES         as _ACCELA_NOISE_LINES,
    _VisibleTextExtractor as _RefVisibleTextExtractor,
    extract_permit_text  as _ref_extract_permit_text,
)


class _VisibleTextExtractor:
    """Tiny HTMLParser-backed visible-text extractor.

    Returns the page's text content in DOM order, one line per text
    node, with leading/trailing whitespace stripped and empty nodes
    dropped. Skips ``<script>`` / ``<style>`` / ``<noscript>`` /
    ``<head>`` and HTML comments. Decodes character refs.
    """

    _SKIP_TAGS = {'script', 'style', 'noscript', 'head', 'svg',
                  'iframe', 'template'}

    def __init__(self):
        from html.parser import HTMLParser
        self._lines: list[str] = []
        self._skip_depth = 0

        outer = self

        class _Parser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag.lower() in outer._SKIP_TAGS:
                    outer._skip_depth += 1

            def handle_endtag(self, tag):
                if tag.lower() in outer._SKIP_TAGS and outer._skip_depth:
                    outer._skip_depth -= 1

            def handle_startendtag(self, tag, attrs):
                pass  # void elements have no text

            def handle_data(self, data):
                if outer._skip_depth:
                    return
                txt = (data or '').strip()
                if txt:
                    outer._lines.append(txt)

            def handle_entityref(self, name):
                if outer._skip_depth:
                    return
                outer._lines.append(html_lib.unescape('&' + name + ';'))

            def handle_charref(self, name):
                if outer._skip_depth:
                    return
                outer._lines.append(html_lib.unescape('&#' + name + ';'))

        self._p = _Parser(convert_charrefs=False)

    def feed(self, html: str) -> None:
        self._p.feed(html or '')

    def close(self) -> str:
        try:
            self._p.close()
        except Exception:
            pass
        # Collapse runs of identical adjacent lines (Accela emits the
        # same address label twice in the visible + hidden cell pair).
        out: list[str] = []
        prev = None
        for line in self._lines:
            if line == prev:
                continue
            out.append(line)
            prev = line
        return '\n'.join(out).strip()


# NOTE: ``_ACCELA_NOISE_LINES`` is imported above from
# ``core.helpers.accela_parser`` (the verbatim reference). Do NOT
# redeclare it here — the helpers module is the single source of
# truth so any future tweak to the noise list lands in exactly one
# place. (Per user rule "don't reinvent — put helpers in helpers".)


def extract_div_by_id(html: str, div_id: str) -> str:
    """Return the inner HTML of the first ``<div id="{div_id}">…</div>``
    in the page, or '' if not found. Walks the tag stream and tracks
    nested ``<div>`` depth so it correctly returns the FULL subtree —
    a naïve regex would stop at the first ``</div>`` and lose all
    the children.

    Used to surgically pull the permit-data container out of an
    Accela CapDetail page (``<div id="divPermitDetailInfo">``) so
    the LLM only sees the card with Address / Owner / Applicant /
    Project Description / Job Value / Phone / Email — not the menu,
    walkme widgets, attachments panel, hidden ViewState forms, or
    the inspections / payments / fees panels.

    Generic on purpose so any other Accela / EnerGov / OpenGov
    container (``divApplicantInfo``, ``divLocationInfo``, …) can
    be pulled the same way without copy-pasting this loop.
    """
    if not html or not div_id:
        return ''
    # Locate the opening tag of the target div. Must match the id
    # attribute exactly (quoted, single OR double) and only inside a
    # <div ...> tag — `<input id="divX">` etc. must NOT match.
    open_pat = re.compile(
        r'<div\b[^>]*\bid\s*=\s*["\']' + re.escape(div_id) + r'["\'][^>]*>',
        re.IGNORECASE,
    )
    m = open_pat.search(html)
    if not m:
        return ''
    start = m.end()
    # Walk forward, tracking <div ...> open / </div> close depth so
    # we find the matching close, not the first one.
    pos = start
    depth = 1
    div_open  = re.compile(r'<div\b', re.IGNORECASE)
    div_close = re.compile(r'</div\s*>', re.IGNORECASE)
    while pos < len(html) and depth > 0:
        no = div_open.search(html, pos)
        nc = div_close.search(html, pos)
        if not nc:
            break  # malformed — bail
        if no and no.start() < nc.start():
            depth += 1
            pos = no.end()
        else:
            depth -= 1
            pos = nc.end()
            if depth == 0:
                # `nc.start()` is the start of the matching </div>
                return html[start:nc.start()]
    return ''


def extract_permit_text(html: str) -> str:
    """Thin delegator to the verbatim reference pipeline at
    ``core.helpers.accela_parser.extract_permit_text``.

    The body lives in the helpers module (a 1:1 copy of
    ``attached_assets/accela_parser_test_*.py``) so the production
    scrapers and the user's standalone reference cannot drift apart.
    Per the project rule "USE HIS REFERENCE PARSER VERBATIM" — and the
    related user rule "why always you change if this is working no
    need to change" — DO NOT inline a "tweaked" copy here. Edit the
    helpers file (and the standalone reference) instead.

    Pipeline (see helpers module for the actual code):
      1. Strip every tag via ``html.parser.HTMLParser`` (visible text
         only, in DOM order). NO div pre-slicing.
      2. Drop lines that exactly match the helpers' ``_NOISE_LINES``
         set (menu labels, panel headers, empty-state placeholders…).
      3. Drop blank / single-char lines.
      4. Join the survivors with ``\\n``.

    Empirically a 370 KB Accela CapDetail page collapses to ~300-700
    chars containing every field that matters (Address, Owner,
    Applicant, Project Description, Job Value, Phone, Email).

    Why no div slicing
    ------------------
    A previous version (PRs #231 / #233) sliced
    ``divWorkLocationInfo + divPermitDetailInfo`` first to "give the
    model a tighter window". That broke twice in production:

      * PR #231 dropped the work-location address entirely (the
        address div wasn't included).
      * Even after PR #233 added it back, certain Accela tenants
        (Wyandotte UG / Kansas City KS, May 2026) re-render those
        divs as empty data tables in response to the ``MoreDetail``
        UpdatePanel postback, so the slice came out empty and the
        LLM was fed 0–22 chars and returned ``page_unreadable`` for
        every permit.

    The reference parser sidesteps both failure modes by working on
    the full visible text and relying on the noise-line set to drop
    chrome — exactly what the user's standalone test does.
    """
    return _ref_extract_permit_text(html)


def extract_visible_text(html: str) -> str:
    """Return only the user-visible text of an HTML page, one line per
    text node, in DOM order. See :class:`_VisibleTextExtractor`.

    A 370 KB Accela CapDetail page empirically collapses to ~2-5 KB
    here, which trivially fits inside the LLM's prompt budget without
    truncation — preserving every field (Address, Owner, Applicant,
    Project Description, Job Value, contact info) that ``clean_html``
    would have pushed past the 32 KB cut-off.
    """
    if not html:
        return ''
    try:
        ex = _VisibleTextExtractor()
        ex.feed(html)
        return ex.close()
    except Exception:
        # Last-resort fallback: regex-strip all tags.
        log.exception('visible-text extraction failed; falling back to regex strip')
        stripped = re.sub(r'<[^>]+>', ' ', html)
        return re.sub(r'\s+', ' ', html_lib.unescape(stripped)).strip()


# ── ASP.NET WebForms helpers (used by every WebForms-backed source) ──

_RE_VIEWSTATE     = re.compile(
    r'<input[^>]*name="__VIEWSTATE"[^>]*value="([^"]*)"', re.IGNORECASE)
_RE_VIEWSTATE_GEN = re.compile(
    r'<input[^>]*name="__VIEWSTATEGENERATOR"[^>]*value="([^"]*)"', re.IGNORECASE)
_RE_EVENTVAL      = re.compile(
    r'<input[^>]*name="__EVENTVALIDATION"[^>]*value="([^"]*)"', re.IGNORECASE)


def parse_webforms_state(html: str) -> dict:
    """Pull ASP.NET hidden-field state out of any WebForms page.

    Returns whichever of ``__VIEWSTATE`` / ``__VIEWSTATEGENERATOR`` /
    ``__EVENTVALIDATION`` are present. Tenants vary — Wyandotte UG
    omits ``__EVENTVALIDATION``, others include all three. Forward
    whatever we find verbatim.

    NOTE: this returns only the three well-known fields. Most modern
    Accela tenants (Palmdale, Birmingham, San Diego, …) ALSO need
    ``ACA_CS_FIELD``, the calendar-extender ``*_ext_ClientState``
    fields, ``__VIEWSTATEENCRYPTED`` and a handful of page-specific
    hiddens — without them the ViewState MAC check rejects the
    postback with "Invalid viewstate". For postback re-submission
    use ``parse_all_hidden_inputs`` instead.
    """
    out = {}
    m = _RE_VIEWSTATE.search(html or '')
    if m:
        out['__VIEWSTATE'] = html_lib.unescape(m.group(1))
    m = _RE_VIEWSTATE_GEN.search(html or '')
    if m:
        out['__VIEWSTATEGENERATOR'] = html_lib.unescape(m.group(1))
    m = _RE_EVENTVAL.search(html or '')
    if m:
        out['__EVENTVALIDATION'] = html_lib.unescape(m.group(1))
    return out


_RE_HIDDEN_INPUT = re.compile(
    r'<input\b[^>]*type="hidden"[^>]*>', re.IGNORECASE)
_RE_INPUT_NAME   = re.compile(r'name="([^"]+)"',  re.IGNORECASE)
_RE_INPUT_VALUE  = re.compile(r'value="([^"]*)"', re.IGNORECASE)


def parse_all_hidden_inputs(html: str) -> dict:
    """Harvest every ``<input type="hidden">`` on a WebForms page.

    Modern Accela / ASP.NET tenants validate ViewState MAC against the
    *full* hidden-field set, not just ``__VIEWSTATE`` /
    ``__VIEWSTATEGENERATOR`` / ``__EVENTVALIDATION``. Specifically
    needed:

      * ``ACA_CS_FIELD`` — anti-CSRF nonce (rotates per request)
      * ``__VIEWSTATEENCRYPTED`` — empty-but-required marker
      * ``ctl00$...$txtGS*StartDate_ext_ClientState`` etc. — every
        calendar / watermark / mask AjaxControlToolkit extender
      * ``ctl00$HDExpressionParam`` and other page-level locals

    Returns ``{name: value}`` with HTML entities already decoded so the
    caller can plug them straight into ``http_post``. Last-write-wins
    on duplicate names (matches what a real browser would submit).
    """
    out = {}
    for tag in _RE_HIDDEN_INPUT.findall(html or ''):
        nm = _RE_INPUT_NAME.search(tag)
        if not nm:
            continue
        vm = _RE_INPUT_VALUE.search(tag)
        out[nm.group(1)] = html_lib.unescape(vm.group(1)) if vm else ''
    return out


_RE_FORM_ACTION = re.compile(
    r'<form\b[^>]*\bid="aspnetForm"[^>]*\baction="([^"]+)"', re.IGNORECASE)
_RE_FORM_ACTION_ALT = re.compile(
    r'<form\b[^>]*\baction="([^"]+)"[^>]*\bid="aspnetForm"', re.IGNORECASE)


def parse_form_action(html: str, fallback_url: str) -> str:
    """Absolute URL the WebForms ``<form id="aspnetForm">`` posts to.

    Accela frequently renders ``action="./CapHome.aspx?module=Building&amp;TabName=…"``
    — the relative ``./`` prefix and HTML-entity-encoded ampersands
    have to be normalised before we can hand the URL to ``http_post``,
    otherwise the MAC check on ``__VIEWSTATE`` fails (the page path
    is part of the MAC input).
    """
    m = _RE_FORM_ACTION.search(html or '') or _RE_FORM_ACTION_ALT.search(html or '')
    if not m:
        return fallback_url
    raw = html_lib.unescape(m.group(1)).strip()
    if not raw:
        return fallback_url
    return urllib.parse.urljoin(fallback_url, raw)


# ── Proxy support (PlainProxies / Bright Data / Smartproxy / ZenRows) ─

_RE_PROXY_FULL = re.compile(
    r'^(?P<scheme>https?://)?'
    r'(?:(?P<user>[^:@/\s]+):(?P<password>[^@/\s]+)@)?'
    r'(?P<host>[^:/@\s]+):(?P<port>\d+)/?$'
)


def parse_proxy_string(s: str) -> dict | None:
    """Parse an admin-supplied proxy connection string.

    Accepts every common shape:
      * ``user:pass@host:port``               — PlainProxies, Smartproxy
      * ``host:port``                         — open proxies
      * ``http://user:pass@host:port``        — explicit scheme
      * ``https://user:pass@host:port``

    Returns ``{'host', 'port', 'user', 'password', 'scheme', 'url'}``
    or ``None`` if the string is empty / unparseable. ``url`` is the
    fully-qualified ``http://user:pass@host:port`` form ready to pass
    to ``urllib.request.ProxyHandler``.
    """
    s = (s or '').strip()
    if not s:
        return None
    m = _RE_PROXY_FULL.match(s)
    if not m:
        return None
    parts = m.groupdict()
    scheme = (parts.get('scheme') or 'http://').rstrip('/').rstrip(':')
    if not scheme.endswith('://'):
        scheme = scheme + '://'
    host  = parts['host']
    port  = parts['port']
    user  = parts.get('user')  or ''
    pw    = parts.get('password') or ''
    if user and pw:
        url = f"{scheme}{urllib.parse.quote(user, safe='')}:" \
              f"{urllib.parse.quote(pw, safe='')}@{host}:{port}"
    else:
        url = f"{scheme}{host}:{port}"
    return {
        'host':     host,
        'port':     int(port),
        'user':     user,
        'password': pw,
        'scheme':   scheme.rstrip('://'),
        'url':      url,
    }


def _resolve_proxy_url() -> str | None:
    """Active scraper proxy URL or ``None``.

    Priority: system_setting ``scraper_proxy`` (admin-editable in
    Scraper Settings) → env var ``SCRAPER_PROXY``. Returns the
    full ``http://user:pass@host:port`` URL ready for ProxyHandler.
    """
    raw = (get_system_setting('scraper_proxy') or '').strip()
    if not raw:
        raw = (os.environ.get('SCRAPER_PROXY') or '').strip()
    parsed = parse_proxy_string(raw)
    return parsed['url'] if parsed else None


def build_proxy_opener(proxy_url: str | None = None
                       ) -> urllib.request.OpenerDirector:
    """Return an opener that routes both http + https through ``proxy_url``.

    If ``proxy_url`` is ``None`` we resolve it from settings/env. If
    nothing is configured we still return a standard opener so the
    rest of the code can call ``opener.open(...)`` unconditionally.
    """
    if proxy_url is None:
        proxy_url = _resolve_proxy_url()
    if not proxy_url:
        return urllib.request.build_opener()
    handler = urllib.request.ProxyHandler({
        'http':  proxy_url,
        'https': proxy_url,
    })
    return urllib.request.build_opener(handler)


# ── Proxied HTTP helpers (every WebForms scraper uses these) ──────────

# Browser-shaped User-Agent. Accela's edge fronting (and Cloudflare /
# Datadog WAF in front of aca-prod) silently drops requests whose UA
# string looks like a bot — and even when it doesn't, the rendered
# error page changes shape, breaking our scrapers. Pinning a real-
# browser UA keeps the responses identical to what a logged-out
# Citizen Access visitor would see.
_BROWSER_UA = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)


def _origin_of(url: str) -> str:
    """``https://host[:port]`` for an absolute URL. Used to satisfy the
    Origin header that Accela's WebForms anti-CSRF check requires on
    every postback (otherwise we get the canned 'Potential cross-site
    request forgery attacks. The Referer and Origin headers are
    missing' page back instead of the results grid)."""
    p = urllib.parse.urlsplit(url)
    if not p.scheme or not p.netloc:
        return ''
    return f'{p.scheme}://{p.netloc}'


def http_get(session_cookies: dict, url: str, *, timeout: int = 60,
             opener: urllib.request.OpenerDirector | None = None,
             referer: str | None = None):
    """GET → ``(html, session_cookies)``. Threads cookies so the
    ``ASP.NET_SessionId`` survives across requests. Routes through the
    admin-configured proxy when one is set. ``referer`` is sent when
    provided so subsequent navigations look like genuine click-throughs.
    """
    op = opener or build_proxy_opener()
    headers = {
        'User-Agent':      _BROWSER_UA,
        'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cookie':          '; '.join(f'{k}={v}' for k, v in session_cookies.items()),
    }
    if referer:
        headers['Referer'] = referer
    req = urllib.request.Request(url, method='GET', headers=headers)
    with op.open(req, timeout=timeout) as resp:
        body = resp.read().decode('utf-8', errors='replace')
        for sc in resp.headers.get_all('Set-Cookie') or []:
            kv = sc.split(';', 1)[0]
            if '=' in kv:
                k, v = kv.split('=', 1)
                session_cookies[k.strip()] = v.strip()
        return body, session_cookies


def http_post(session_cookies: dict, url: str, form: dict,
              *, timeout: int = 60,
              opener: urllib.request.OpenerDirector | None = None,
              referer: str | None = None):
    """POST a WebForms form-encoded body. Same cookie threading +
    optional proxy routing as ``http_get``. Sends ``Referer`` (defaults
    to the post URL itself, which is what a real browser would send
    for a self-targeted form submit) and ``Origin`` so Accela's anti-
    CSRF check accepts the postback."""
    op = opener or build_proxy_opener()
    data = urllib.parse.urlencode(form).encode('utf-8')
    headers = {
        'User-Agent':      _BROWSER_UA,
        'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Content-Type':    'application/x-www-form-urlencoded',
        'Cookie':          '; '.join(f'{k}={v}' for k, v in session_cookies.items()),
        'Referer':         referer or url,
        'Origin':          _origin_of(url),
    }
    req = urllib.request.Request(url, data=data, method='POST', headers=headers)
    with op.open(req, timeout=timeout) as resp:
        body = resp.read().decode('utf-8', errors='replace')
        for sc in resp.headers.get_all('Set-Cookie') or []:
            kv = sc.split(';', 1)[0]
            if '=' in kv:
                k, v = kv.split('=', 1)
                session_cookies[k.strip()] = v.strip()
        return body, session_cookies


# ── DO inference parameter resolution ─────────────────────────────────

def _resolve_do_base_url() -> str:
    """Active OpenAI-compatible parser base URL."""
    v = (get_system_setting('do_base_url') or '').strip()
    return v or DO_BASE_URL_DEFAULT


def _resolve_oss_model() -> str:
    """Active OSS parser model. Priority:
      1. ``accela_scraper_agent_model`` (the per-scraper parser card)
      2. ``accela_oss_model`` (legacy key, kept for backwards compat)
      3. ``DEFAULT_OSS_MODEL``
    """
    mdl = (get_system_setting('accela_scraper_agent_model') or '').strip()
    if mdl:
        return mdl
    mdl = (get_system_setting('accela_oss_model') or '').strip()
    if mdl:
        return mdl
    return DEFAULT_OSS_MODEL


def _resolve_float(setting: str, default: float, *,
                   lo: float = 0.0, hi: float = 1.0) -> float:
    """Read a numeric system_setting, clamp into ``[lo, hi]``, fall
    back to ``default`` on a missing or unparseable value."""
    raw = (get_system_setting(setting) or '').strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _resolve_int(setting: str, default: int, *,
                 lo: int = 1, hi: int = 32000) -> int:
    raw = (get_system_setting(setting) or '').strip()
    if not raw:
        return default
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _do_api_key() -> str:
    """Resolve an optional parser API key.

    Local GPT-OSS does not need a key. This intentionally does not read
    ``DO_API_KEY`` so a machine-level paid inference key can never leak into
    the free local parser path.
    """
    key = (get_system_setting('do_api_key') or '').strip()
    if not key:
        key = (os.environ.get('GPT_OSS_API_KEY') or '').strip()
    return key


# ── OpenAI-compatible local inference call ────────────────────────────

def oss_complete(prompt: str, *, model: str | None = None,
                 system: str | None = None,
                 max_tokens: int | None = None,
                 temperature: float | None = None,
                 top_p: float | None = None,
                 timeout: int = 90,
                 source: str = 'accela_oss',
                 scraper_run_id: int | None = None) -> dict:
    """One round-trip to the local OpenAI-compatible GPT-OSS server.

    All numeric knobs default to the admin-configured system_settings
    (``do_temperature``, ``do_max_tokens``, ``do_top_p``,
    ``do_base_url``) so a single Settings page controls parser
    behaviour platform-wide. Pass an explicit kwarg to override per
    call (useful for one-off ad-hoc scripts).

    Returns ``{text, input_tokens, output_tokens, elapsed_ms, model}``.
    Records a row in ``claude_calls`` with the chosen ``source`` so
    the existing AI Usage dashboard rolls OSS calls up alongside Claude.
    """
    mdl = (model or '').strip() or _resolve_oss_model()
    if not mdl:
        raise HttpScraperError('No OSS model specified.')
    key = _do_api_key()
    base = _resolve_do_base_url()

    mt = max_tokens   if max_tokens   is not None else _resolve_int('do_max_tokens', 1500, lo=64, hi=8000)
    tp = temperature  if temperature  is not None else _resolve_float('do_temperature', 0.0, lo=0.0, hi=2.0)
    tpp= top_p        if top_p        is not None else _resolve_float('do_top_p',       1.0, lo=0.0, hi=1.0)

    # Match the user's standalone reference parser exactly: when a
    # system prompt is provided, send it as a separate `system` role
    # message rather than concatenating it into the user content.
    # gpt-oss-20b is RLHF-trained against the system role and
    # produces measurably more reliable extractions when given
    # `[{system}, {user}]` instead of one big user message.
    msgs = []
    if system:
        msgs.append({'role': 'system', 'content': system})
    msgs.append({'role': 'user', 'content': prompt})

    body = json.dumps({
        'model':       mdl,
        'messages':    msgs,
        'temperature': tp,
        'max_tokens':  int(mt),
        'top_p':       tpp,
    }).encode('utf-8')

    headers = {
        'Content-Type': 'application/json',
        'Accept':       'application/json',
    }
    if key:
        headers['Authorization'] = f'Bearer {key}'
    req = urllib.request.Request(
        base.rstrip('/') + '/chat/completions',
        data=body, method='POST',
        headers=headers,
    )

    t0 = time.monotonic()
    status = None
    err = None
    in_tok = out_tok = 0
    text = ''
    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.getcode()
                payload = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                eb = json.loads(e.read().decode('utf-8'))
                em = ((eb.get('error') or {}).get('message')
                      or eb.get('message')
                      or str(eb))
            except Exception:
                em = f'HTTP {e.code}'
            err = f'GPT-OSS inference {e.code}: {em}'
            raise HttpScraperError(err) from e
        except urllib.error.URLError as e:
            err = f'OSS network error: {e.reason}'
            raise HttpScraperError(err) from e
        except Exception as e:
            err = f'OSS failed: {e}'
            raise HttpScraperError(err) from e

        choices = payload.get('choices') or []
        if choices:
            msg = (choices[0] or {}).get('message') or {}
            text = msg.get('content') or ''
        usage = payload.get('usage') or {}
        try:
            in_tok  = int(usage.get('prompt_tokens')     or 0)
            out_tok = int(usage.get('completion_tokens') or 0)
        except Exception:
            in_tok = out_tok = 0
    finally:
        try:
            record_claude_call(
                scraper_run_id=scraper_run_id,
                source=source,
                model=mdl,
                status_code=status,
                latency_ms=int((time.monotonic() - t0) * 1000),
                input_tokens=in_tok,
                output_tokens=out_tok,
                error=err,
            )
        except Exception:
            log.exception('OSS usage recording failed')

    return {
        'text':          text,
        'input_tokens':  in_tok,
        'output_tokens': out_tok,
        'elapsed_ms':    int((time.monotonic() - t0) * 1000),
        'model':         mdl,
    }


# ── Public extractor — drop-in alternative to claude_extract ──────────

def oss_extract(page_html: str, *, source_url: str = '',
                model: str | None = None,
                scraper_run_id: int | None = None) -> dict:
    """Parse a single permit detail page via GPT-OSS.

    Mirrors the shape and contract of the legacy ``claude_extract``
    so engine-agnostic worker code can call either one. Steps:

      * cleans the raw HTML (drops scripts/styles/meta to ~30 %)
      * prepends the admin-editable EXTRACT_SYSTEM_PROMPT
      * calls DO Serverless Inference
      * parses + normalises the JSON reply via the existing helpers

    The two normalisation helpers are imported lazily from the legacy
    Accela module so adding a new source doesn't require touching that
    file.
    """
    body = clean_html(page_html or '')
    if not body:
        raise HttpScraperError('No HTML to extract from — page was empty.')
    if len(body) > 32000:
        body = body[:32000] + '\n…(truncated)'

    from .. import scraper_accela
    parts = [scraper_accela.get_extraction_prompt()]
    if source_url:
        parts.append(f'Source URL: {source_url}')
    parts.append('Permit page HTML (cleaned):\n\n' + body)
    prompt = '\n\n'.join(parts)

    out = oss_complete(prompt, model=model,
                       scraper_run_id=scraper_run_id)
    raw = scraper_accela._extract_json(out['text'])
    return scraper_accela._normalise_permit(raw)


# ── Date helper (every WebForms search uses MM/DD/YYYY) ──────────────

def date_mmddyyyy(d) -> str:
    """MM/DD/YYYY string Accela's date inputs accept. Accepts a date,
    datetime, ISO ``YYYY-MM-DD`` string, or already-formatted string."""
    if d is None:
        return ''
    if hasattr(d, 'strftime'):
        return d.strftime('%m/%d/%Y')
    s = str(d).strip()
    try:
        return datetime.strptime(s, '%Y-%m-%d').strftime('%m/%d/%Y')
    except ValueError:
        return s
