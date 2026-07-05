"""Accela ASP.NET WebForms scraper — pure HTTP, GPT-OSS parser.

Wire-level architecture (verified live against Wyandotte County KS
``aca-prod.accela.com/UG``, May 2026):

  1. SEARCH    POST ``/UG/Cap/CapHome.aspx?module=Building&TabName=Building``
               with ``__EVENTTARGET = ctl00$PlaceHolderMain$btnNewSearch``,
               carried-over ``__VIEWSTATE`` + ``__VIEWSTATEGENERATOR``,
               and ``txtGSStartDate`` / ``txtGSEndDate`` in MM/DD/YYYY.

  2. PAGINATE  POST again with
               ``__EVENTTARGET = ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctlNN``.
               The ctlNN suffix is NOT a static page-index formula —
               Accela renders 10 numbered links at a time and the
               control ids reset when the pager window rolls forward
               (page 10 → 11). ``paginate_next`` therefore PARSES the
               live pager HTML for the target page-number link, and
               falls back to the ``Next >`` link when the target lies
               beyond the current 10-page window. ViewState rotates
               on every postback — re-parse from the previous response.

  3. DETAIL    GET ``/UG/Cap/CapDetail.aspx?Module=Building&capID1=&capID2=&capID3=&agencyCode=UG``.
               Caveat: contractor / owner / valuation hide behind a
               ``Show More`` postback whose target id contains the
               literal ``MoreDetail`` — we POST once to expand it.

  4. PARSE     Hand the cleaned HTML to ``scrapers.base.oss_extract``.

This file is the canonical template — copy it as ``tyler_energov.py``
or ``opengov.py`` when adding a new source. The shared helpers in
``scrapers.base`` (proxied HTTP, ASP.NET state parser, OSS client)
mean each new source file should be ~150 LOC of source-specific logic.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from .base import (
    HttpScraperError,
    build_proxy_opener,
    clean_html,
    extract_div_by_id,
    extract_visible_text,
    extract_permit_text,
    date_mmddyyyy,
    http_get,
    http_post,
    oss_complete,
    parse_all_hidden_inputs,
    parse_form_action,
    parse_webforms_state,
)
from ..helpers.accela_parser import parse_accela_detail
log = logging.getLogger(__name__)


# ── Search-page auto-discovery ───────────────────────────────────────
#
# Many configured Accela URLs aren't the search page itself — they're
# the tenant's landing page (``Default.aspx``, ``Welcome.aspx``,
# ``CommunityView``, or a bare ``/{TENANT}/``). The actual permit-search
# page is always one click away: ``Cap/CapHome.aspx?module=Building``
# (or ``Permits``, ``Planning``, etc.). Probing all 71 zero-permit
# scrapers on 2026-05-05 showed:
#   • ~30 land on a non-search page (Default / Welcome / CommunityView).
#   • ~8  redirect to ``Error.aspx`` because the configured URL has a
#         stale module name — the page-not-found body still LISTS the
#         working CapHome URLs in its left nav.
#   • ~8  redirect to ``Login.aspx`` (tenant requires authentication —
#         we cannot fix that from a scraper config change).
# Asking the user to hand-edit each URL is hostile when the search
# page is reachable from the configured URL via a single hop.
#
# ``_resolve_search_url`` does that hop automatically: if the configured
# URL doesn't render the search form, it harvests every
# ``Cap/CapHome.aspx?module=...`` link from the page (and from the
# tenant's ``/{TENANT}/Default.aspx``) and tries them in a sensible
# preference order until one renders ``txtGSStartDate``.

# Modules ranked by how likely they are to expose the date-range
# search we use: Building/Permits/Construction tend to be the busiest
# and most-likely-to-have-a-date-search modules; Licenses/Enforcement/
# ConsumerAffairs etc. usually use a different search form. Within
# the same module, prefer the most-specific TabName.
_MODULE_PRIORITY = [
    'building', 'permits', 'permit',  'construction',
    'planning', 'engineering', 'dsd', 'community',
    'code', 'enforcement', 'licenses', 'license',
    'rental', 'inspection', 'inspections', 'business',
]


def _score_caphome_link(href: str) -> tuple:
    """Lower tuple = better candidate. Used to pick between e.g.
    ``module=Building`` (good) vs ``module=ConsumerAffairs`` (bad)
    when the landing page exposes both."""
    h = href.lower()
    qs = urllib.parse.urlparse(h).query
    params = dict(urllib.parse.parse_qsl(qs))
    module = (params.get('module') or '').lower()
    tab    = (params.get('tabname') or '').lower()
    try:
        m_rank = _MODULE_PRIORITY.index(module)
    except ValueError:
        m_rank = len(_MODULE_PRIORITY) + (0 if module else 1)
    # Prefer URLs whose TabName matches the module (cleaner deep link),
    # and strongly de-prefer "?BB=1" / module-less URLs.
    tab_match = 0 if (tab and module and tab.startswith(module)) else 1
    no_module = 0 if module else 1
    return (no_module, m_rank, tab_match, len(h))


_RE_CAPHOME_HREF = re.compile(
    r'''href=["']([^"']*?Cap/CapHome\.aspx\?[^"']*?)["']''',
    re.IGNORECASE,
)


def _harvest_caphome_links(html: str, base_url: str) -> list[str]:
    """Return every absolute ``Cap/CapHome.aspx?module=...`` URL on
    the page, de-duplicated, sorted best-first."""
    raws = set(m.group(1).replace('&amp;', '&')
               for m in _RE_CAPHOME_HREF.finditer(html))
    abs_urls = {urllib.parse.urljoin(base_url, r) for r in raws}
    return sorted(abs_urls, key=_score_caphome_link)


def _resolve_search_url(initial_url: str, *, timeout: int,
                        opener) -> tuple[str, str, dict]:
    """Return ``(search_url, page_html, cookies)`` for an Accela tenant.

    If ``initial_url`` already renders the date-range search form
    (``txtGSStartDate``), it's returned as-is. Otherwise we follow at
    most one hop: harvest every ``Cap/CapHome.aspx?module=…`` link from
    the configured page (and from the tenant root) and probe them in
    module-priority order until one renders the search form.

    Raises ``HttpScraperError`` with a descriptive message when:
      • the tenant redirects to ``Login.aspx`` (auth-walled — needs
        per-tenant credentials we don't have);
      • no ``Cap/CapHome.aspx`` link on the configured page or the
        tenant root renders the search form (the URL really is wrong
        and the admin needs to fix it).
    """
    cookies: dict = {}
    page_html, cookies = http_get(cookies, initial_url,
                                  timeout=timeout, opener=opener)
    # Detect auth-walled tenants up front — the configured URL "works"
    # (200 OK, has __VIEWSTATE) but it's the login form, not the
    # search form. Failing here with a clear message is much kinder
    # than later raising "no __VIEWSTATE" or returning 0 rows.
    low = page_html.lower()
    is_login = (
        'login.aspx' in low
        and ('txtuserid' in low or 'txtpassword' in low
             or 'name="ctl00$placeholdermain$accountlogin' in low)
    )
    if is_login:
        raise HttpScraperError(
            'Tenant requires authentication (configured URL redirects '
            'to Login.aspx). Per-tenant credentials are not yet '
            'supported by this scraper.')
    if 'txtGSStartDate' in page_html:
        return initial_url, page_html, cookies
    # A handful of Accela tenants (Fort Lauderdale FTL/Permits, …)
    # render a perfectly valid search page that simply has NO date
    # range — the form is keyed on permit number / address / parcel /
    # license instead. We must accept it, otherwise the resolver
    # tries 10 candidates that don't exist and raises. Detection:
    # the page hosts the tenant's general search form AND a
    # ``btnNewSearch`` submit button (same EVENTTARGET we POST).
    # ``search_accela`` skips the date inputs in that case (see
    # below) so the POST still validates against ViewState MAC.
    if ('generalSearchForm' in page_html
            and 'btnNewSearch' in page_html):
        return initial_url, page_html, cookies

    # Build candidate list: links on this page, plus links on the
    # tenant's Default.aspx (covers the case where the configured URL
    # IS the Default page but its left-nav uses JS / hidden menus we
    # didn't pick up, or where it's Error.aspx whose nav links are
    # the SAME nav the working pages have).
    parsed = urllib.parse.urlparse(initial_url)
    # Tenant root = first path segment. /COSA/Cap/CapHome.aspx → /COSA/
    parts = [p for p in parsed.path.split('/') if p]
    tenant_root = (
        f'{parsed.scheme}://{parsed.netloc}/{parts[0]}/Default.aspx'
        if parts else None
    )
    candidates: list[str] = _harvest_caphome_links(page_html, initial_url)
    tried_root = False
    if tenant_root and tenant_root.lower() != initial_url.lower():
        try:
            root_html, _ = http_get({}, tenant_root,
                                    timeout=timeout, opener=opener)
            tried_root = True
            extra = _harvest_caphome_links(root_html, tenant_root)
            # Keep first-seen order across both lists, but re-sort
            # the union by priority so the cleanest module wins.
            seen = set(candidates)
            for c in extra:
                if c not in seen:
                    candidates.append(c); seen.add(c)
            candidates.sort(key=_score_caphome_link)
        except Exception:  # noqa: BLE001 — best-effort fallback
            pass

    # Brute-force fallback: many tenants render their top nav via
    # JS / Telerik widgets, so the Cap/CapHome.aspx URLs aren't in
    # the raw HTML we just downloaded. The URL convention is
    # universal across every Accela tenant we've audited:
    #   /{TENANT}/Cap/CapHome.aspx?module=<Module>&TabName=<Module>
    # so when link-harvesting comes up empty (or only finds modules
    # that don't expose the date search), generate a candidate list
    # from `_MODULE_PRIORITY` against the tenant code we already
    # parsed. Verified live against Naperville (DUPAGE/Building),
    # Lawton (OKIE/Building), Kettering (kettering/Building),
    # Chesapeake (CHESAPEAKE/Building), Bullhead City (MOHAVE/Building)
    # — all five resolve to a working search page this way.
    if parts:
        tenant_code = parts[0]
        guess_modules = ['Building', 'Permits', 'Construction',
                         'Planning', 'Engineering', 'Code',
                         'Enforcement', 'Licenses', 'Inspections']
        guesses = [
            (f'{parsed.scheme}://{parsed.netloc}/{tenant_code}'
             f'/Cap/CapHome.aspx?module={m}&TabName={m}')
            for m in guess_modules
        ]
        seen = {c.lower() for c in candidates}
        for g in guesses:
            if g.lower() not in seen:
                candidates.append(g); seen.add(g.lower())
        candidates.sort(key=_score_caphome_link)

    if not candidates:
        raise HttpScraperError(
            f'Configured URL is not an Accela permit-search page and '
            f'no Cap/CapHome.aspx links were found on it'
            + (' or on the tenant root.' if tried_root else '.')
            + ' Update the scraper URL to a CapHome.aspx?module=...'
              ' search page.')

    # Probe candidates in priority order. Stop at the first one that
    # actually renders the date-range search form. Cap at 10 probes —
    # any tenant beyond that with no matches is misconfigured. (Was
    # 6; bumped to 10 to accommodate the brute-force module fallback,
    # which adds up to 9 candidates on top of any harvested links.)
    # IMPORTANT: carry the session cookies from the landing-page GET
    # forward into every candidate probe. Several Accela tenants
    # (San Diego DSD, South Portland COSP, Boulder BOCO, Asheville
    # BUNCOMBECONC, Plymouth MN MDH, …) only render the
    # ``txtGSStartDate`` search panel when the request comes in with
    # an established ``ASP.NET_SessionId`` cookie — the very first
    # request gets a chrome-only landing page even on the right URL.
    # The earlier "fresh cookies per probe" approach was the reason
    # those tenants wouldn't auto-resolve.
    MAX_PROBES = 12
    last_err: Exception | None = None
    for cand in candidates[:MAX_PROBES]:
        try:
            probe_cookies = dict(cookies)  # copy: don't pollute on miss
            cand_html, probe_cookies = http_get(probe_cookies, cand,
                                                timeout=timeout,
                                                opener=opener)
        except Exception as e:  # noqa: BLE001
            last_err = e; continue
        if 'login.aspx' in cand_html.lower() and 'txtuserid' in cand_html.lower():
            continue  # this module needs login — try the next one
        if 'txtGSStartDate' in cand_html:
            return cand, cand_html, probe_cookies
        # Same date-less search page acceptance as the initial-URL
        # branch above. Keeps probe behaviour symmetric so candidates
        # discovered through link-harvesting / module-guessing also
        # resolve on tenants like FTL/Permits.
        if ('generalSearchForm' in cand_html
                and 'btnNewSearch' in cand_html):
            return cand, cand_html, probe_cookies
    raise HttpScraperError(
        f'Tried {min(len(candidates), MAX_PROBES)} Cap/CapHome.aspx '
        f'candidate(s) reachable from the configured URL — none '
        f'rendered the date-range search form (txtGSStartDate). '
        f'Update the scraper URL or check whether the tenant requires '
        f'login.'
        + (f' Last probe error: {last_err}' if last_err else ''))


def search_accela(search_url: str, *, date_from, date_to,
                  timeout: int = 60,
                  permit_type: str | None = None,
                  extra_form: dict | None = None) -> dict:
    """Run the date-range search on a CapHome.aspx page.

    Returns ``{'html': str, 'cookies': dict, 'state': dict, 'action': str}``
    — the raw results-grid HTML, the WebForms state needed to paginate,
    and the resolved form-action URL (some Accela tenants render
    ``action="./CapHome.aspx?…"`` rather than the page URL itself, and
    posting back to the *exact* action URL is required for the
    ViewState MAC check to pass).

    Routes through the admin-configured scraper proxy when one is set.
    """
    opener = build_proxy_opener()
    # Auto-discover the real search page when ``search_url`` is the
    # tenant landing page / a stale module URL that redirects to
    # Error.aspx. Returns the configured URL unchanged when it already
    # has the search form, so the happy path (Wyandotte UG, COSA, …)
    # is unaffected.
    resolved_url, page_html, cookies = _resolve_search_url(
        search_url, timeout=timeout, opener=opener)
    state = parse_webforms_state(page_html)
    if '__VIEWSTATE' not in state:
        raise HttpScraperError(
            'Search page did not include __VIEWSTATE — is the URL '
            'really an Accela CapHome.aspx page?')
    # From this point on the resolved URL is the source of truth for
    # the form-action / referer / pagination URL. The caller's original
    # ``search_url`` is no longer used.
    search_url = resolved_url

    # Carry EVERY hidden input forward, not just the 3 well-known
    # ViewState fields. Modern Accela tenants (Palmdale, Birmingham,
    # San Diego, …) include ACA_CS_FIELD (anti-CSRF), the calendar /
    # watermark / mask AjaxControlToolkit *_ext_ClientState fields,
    # __VIEWSTATEENCRYPTED and other page-level hiddens — all of which
    # are part of the MAC inputs for ViewState validation. Drop any
    # one and the postback comes back as "Invalid viewstate".
    form = parse_all_hidden_inputs(page_html)
    form['__EVENTTARGET']   = 'ctl00$PlaceHolderMain$btnNewSearch'
    form['__EVENTARGUMENT'] = ''
    # Some Accela tenants (FTL/Permits, …) render a search form with
    # NO date-range inputs. Posting the date fields anyway is mostly
    # harmless (ASP.NET WebForms drops unknown fields) but on a few
    # tenants the extra fields fail server-side validation. Only set
    # them when the page actually has them — every tenant that uses
    # date filtering today keeps the exact same behaviour, and the
    # in-memory ``_in_range`` row filter still narrows results to
    # the requested window so the cron's "last N days" semantics
    # survive on date-less tenants too.
    if 'txtGSStartDate' in page_html:
        form['ctl00$PlaceHolderMain$generalSearchForm$txtGSStartDate'] = date_mmddyyyy(date_from)
        form['ctl00$PlaceHolderMain$generalSearchForm$txtGSEndDate']   = date_mmddyyyy(date_to)
    # Optional Permit Type dropdown filter (Accela field
    # ``ddlGSPermitType``). Some tenants — Boulder County (BOCO),
    # San Diego, Scottsdale, … — render a non-empty Permit Type
    # ``<select>`` and return ZERO rows for an "any" submit; the
    # admin must pick a specific type (e.g. "Building Permit
    # Request" → value ``Building/Application Request/Building
    # Permit/NA``) before any rows come back. We expose the chosen
    # value via the per-scraper ``config['permit_type']`` field; the
    # caller (``oss_agent_scrape_permits``) reads it and threads it
    # through. When unset (the common case), we leave the dropdown
    # alone — every Accela tenant we tested handles the empty
    # default as "all permit types".
    if permit_type:
        form['ctl00$PlaceHolderMain$generalSearchForm$ddlGSPermitType'] = permit_type
    # Generic per-tenant extra form fields (e.g. {ddlGSStreetSuffix: 'AVE'}
    # for a Street-Suffix loop pass on Fort Lauderdale). Same intent as
    # ``permit_type`` above but for arbitrary <select>/<input> names that
    # aren't part of the well-known set. Caller is responsible for
    # sending the FULL form-field name (``ctl00$…$ddlGSStreetSuffix``).
    if extra_form:
        form.update({k: v for k, v in extra_form.items() if v is not None})

    action_url = parse_form_action(page_html, search_url)
    results_html, cookies = http_post(cookies, action_url, form,
                                      timeout=timeout, opener=opener,
                                      referer=search_url)
    return {
        'html':    results_html,
        'cookies': cookies,
        'state':   parse_webforms_state(results_html),
        'action':  action_url,
        # Surface the URL we actually posted against so the caller can
        # use it for paginate_next's referer + log it for diagnostics
        # when auto-discovery resolved a different URL than configured.
        'resolved_url': search_url,
    }


# Anchor element + its inner text. Inner text may contain nested HTML
# entities (``Next &gt;``) or even a wrapping ``<span>`` on some Accela
# skins — strip tags after capture rather than baking that into the
# pattern, which would make the regex fragile.
_RE_ANCHOR = re.compile(
    r"<a\b([^>]*?)>(.*?)</a>",
    flags=re.IGNORECASE | re.DOTALL)
# ``__doPostBack`` arg extractor — tolerant of: HTML-entity encoded
# apostrophes (``&#39;``, ``&apos;``) AND literal single / double
# quotes, plus arbitrary whitespace around the comma. Accela's stock
# skin emits the entity form, but custom skins (and some upstream
# WebForms versions) emit literal quotes — we accept both.
_RE_DOPOSTBACK = re.compile(
    r"__doPostBack\(\s*"
    r"""(?:&#39;|&apos;|['\"])"""
    r"([^'\"&]+?)"
    r"""(?:&#39;|&apos;|['\"])"""
    r"\s*,\s*"
    r"""(?:&#39;|&apos;|['\"])"""
    r"([^'\"&]*?)"
    r"""(?:&#39;|&apos;|['\"])"""
    r"\s*\)",
    flags=re.IGNORECASE)
_RE_HREF       = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""",
                            flags=re.IGNORECASE)
_RE_TAG_STRIP  = re.compile(r"<[^>]+>")


_RE_PAGER_BLOCK = re.compile(
    r'<table\b[^>]*\bclass="[^"]*\baca_pagination\b[^"]*"[^>]*>(.*?)</table>',
    re.DOTALL | re.IGNORECASE)


def _parse_pager_links(html: str) -> dict[str, str]:
    """Return ``{visible_label: postback_target}`` for every page-link
    rendered inside the ``gdvPermitList`` pager row.

    Anchor + ``__doPostBack`` are parsed in two stages so the matcher
    tolerates:
      • single- vs double-quoted ``href`` attributes,
      • entity-encoded (``&#39;`` / ``&apos;``) vs literal quotes
        inside the ``__doPostBack(...)`` call,
      • arbitrary whitespace inside the call,
      • nested ``<span>`` / ``<b>`` markup around the label.

    Labels are HTML-unescaped and tag-stripped; the forward/backward
    ellipsis collapse to ``'...'``, ``Next &gt;`` to ``Next``, ``&lt;
    Prev`` / ``< Prev`` to ``Prev`` (case-insensitive). Comparisons
    against numeric pages are exact strings (``'11'`` etc.) — that's
    intentional so the caller's `str(page_index) in pager` test stays
    O(1) and unambiguous.

    The CURRENT page is intentionally absent (Accela renders it as a
    ``<span>`` not an ``<a>``), which is what lets the caller test for
    "is target page present in current pager window?" cheaply.

    Scope: anchored on the ``<table class="aca_pagination">`` block
    Accela renders around the pager row. We can't filter on the
    ``$ctl{NN}$`` suffix of the pager container (older tenants emit
    ``$ctl13$``, Milwaukee emits ``$ctl23$``, others vary) — but
    every Accela tenant verified so far wraps the pager links inside
    this dedicated table, so narrowing to that block keeps us safe
    from grid-row action buttons (Pay Fees, Export, …) that share the
    same grid id without needing a brittle suffix filter.
    """
    import html as _htmllib
    out: dict[str, str] = {}
    pager_html = html or ''
    m_block = _RE_PAGER_BLOCK.search(pager_html)
    if m_block:
        pager_html = m_block.group(1)
    for attrs, inner in _RE_ANCHOR.findall(pager_html):
        href_m = _RE_HREF.search(attrs or '')
        if not href_m:
            continue
        href = href_m.group(1) or href_m.group(2) or ''
        if '__doPostBack' not in href:
            continue
        m = _RE_DOPOSTBACK.search(href)
        if not m:
            continue
        target = _htmllib.unescape(m.group(1)).strip()
        # The pager block we've narrowed to lives inside the
        # gdvPermitList grid on every tenant — drop anything that
        # somehow slipped in pointing elsewhere.
        if '$dgvPermitList$gdvPermitList$' not in target:
            continue
        # Strip nested markup from the visible text before we use it
        # as a label, then HTML-unescape.
        txt = _RE_TAG_STRIP.sub('', inner or '')
        txt = _htmllib.unescape(txt).strip()
        if not txt:
            continue
        low = txt.lower()
        if txt == '...' or low in ('…',):
            # Two ellipses can appear (back-window + fwd-window) on
            # mid-window pages. Last-write-wins gives us the FORWARD
            # one, which is the only one we need to jump windows.
            out['...'] = target
        elif low.startswith('next'):
            out['Next'] = target
        elif low.startswith('prev') or txt in ('< Prev',):
            out['Prev'] = target
        else:
            out[txt] = target
    return out


def paginate_next(prev_html: str, search_url: str, cookies: dict,
                  *, page_index: int, timeout: int = 60,
                  action_url: str | None = None,
                  permit_type: str | None = None,
                  extra_form: dict | None = None) -> dict:
    """Fetch the next results page. ``page_index`` is the 1-based
    target page number.

    Resolves the postback target by PARSING the live pager links in
    ``prev_html`` rather than computing a static ``ctl{page+1}`` id.
    The static formula only works inside the first 10-page window —
    once Accela rolls the pager forward (page 10 → 11) the control
    suffixes reset and the old formula targets the wrong page (or
    silently re-fetches page 1, which the caller's dedupe set treats
    as exhaustion).

    Resolution order:
      1. If ``str(page_index)`` is a visible numeric link → use it.
      2. Else use the ``Next >`` link (advances exactly one page,
         which is always what the caller wants because pages are
         walked monotonically).
      3. Else use the forward ``...`` ellipsis (skips to the next
         10-page window — only reached when ``Next`` is somehow
         absent, e.g. malformed pager HTML).
      4. Otherwise raise — there is genuinely no next page to fetch.

    ``action_url`` is the form-action URL returned by ``search_accela``
    — re-using it (instead of falling back to ``search_url``) is what
    keeps the ViewState MAC check happy on tenants that render an
    explicit ``<form action="./CapHome.aspx?…">``.

    Returns the same envelope as ``search_accela``.
    """
    if page_index < 2:
        raise HttpScraperError('paginate_next: page_index must be ≥ 2')
    state = parse_webforms_state(prev_html)
    if '__VIEWSTATE' not in state:
        raise HttpScraperError('Pagination POST: no __VIEWSTATE on prev page.')
    pager = _parse_pager_links(prev_html)
    label = str(page_index)
    if label in pager:
        target = pager[label]
    elif 'Next' in pager:
        target = pager['Next']
    elif '...' in pager:
        target = pager['...']
    else:
        raise HttpScraperError(
            f'paginate_next: no pager link for page {page_index} '
            f'(pager labels seen: {sorted(pager)!r})')
    # Same all-hiddens-forward rule as search_accela — drop any field
    # and modern Accela rejects the postback with "Invalid viewstate".
    form = parse_all_hidden_inputs(prev_html)
    form['__EVENTTARGET']   = target
    form['__EVENTARGUMENT'] = ''
    # Carry the per-tenant Permit Type filter forward on every page —
    # ``parse_all_hidden_inputs`` only catches ``<input type="hidden">``
    # elements, NOT ``<select>`` values, so without this the second
    # POST would silently revert to "all types" and (on tenants that
    # require a type to be picked) return 0 rows on page 2+.
    if permit_type:
        form['ctl00$PlaceHolderMain$generalSearchForm$ddlGSPermitType'] = permit_type
    # Carry per-tenant extra form fields (e.g. the street-suffix loop
    # value) forward on every page — same hidden-input gap that bites
    # ``permit_type``: <select> values aren't in the hidden-input set
    # ``parse_all_hidden_inputs`` returns, so without this the page-2
    # POST silently reverts to "all" and pagination drifts away from
    # what page 1 returned.
    if extra_form:
        form.update({k: v for k, v in extra_form.items() if v is not None})
    opener = build_proxy_opener()
    post_url = action_url or parse_form_action(prev_html, search_url)
    next_html, cookies = http_post(cookies, post_url, form,
                                   timeout=timeout, opener=opener,
                                   referer=search_url)
    return {
        'html':    next_html,
        'cookies': cookies,
        'state':   parse_webforms_state(next_html),
        'action':  post_url,
    }


_RE_SELECT_BLOCK = re.compile(
    r'<select\b[^>]*\bname="([^"]+)"[^>]*>(.*?)</select>',
    re.DOTALL | re.IGNORECASE)
_RE_OPTION = re.compile(
    r'<option\b[^>]*\bvalue="([^"]*)"[^>]*>(.*?)</option>',
    re.DOTALL | re.IGNORECASE)


def _extract_select_options(html: str, field_name: str) -> list[tuple[str, str]]:
    """Return ``[(value, text), …]`` for every non-empty ``<option>``
    in the ``<select name="…">`` whose name matches ``field_name``.

    Empty values (``value=""`` placeholders like ``--Select--``) are
    dropped so callers can iterate "real" choices directly. Option
    text is stripped of HTML tags + collapsed whitespace.
    """
    if not (html and field_name):
        return []
    out: list[tuple[str, str]] = []
    for sel in _RE_SELECT_BLOCK.finditer(html):
        if sel.group(1) != field_name:
            continue
        for opt in _RE_OPTION.finditer(sel.group(2)):
            val = opt.group(1)
            if not val:
                continue
            txt = re.sub(r'<[^>]+>', '', opt.group(2) or '')
            txt = re.sub(r'\s+', ' ', txt).strip()
            out.append((val, txt))
        break
    return out


_RE_MOREDETAIL_NAME = re.compile(r'name="(ctl00\$[^"]*MoreDetail[^"]*)"')
_RE_MOREDETAIL_ID   = re.compile(r'id="(ctl00_[^"]*MoreDetail[^"]*)"')


def _find_all_moredetail_targets(body: str) -> list[str]:
    """Return EVERY distinct ``MoreDetail`` postback target on the
    page, in DOM order, normalised to the ``ctl00$…$…`` event-target
    form Accela's __doPostBack expects.

    Accela CapDetail.aspx exposes several independent collapsible
    sections — each with its own ``Show More`` postback whose target
    id contains the word ``MoreDetail`` (e.g. Application Info,
    Contacts, Parcel Info, License Info). The previous one-shot
    expansion pattern only opened the FIRST section, so contact /
    contractor blocks that live further down stayed collapsed and
    the contractor email / phone were literally absent from the HTML
    the LLM ever saw — root cause of "the model isn't extracting
    email or phone" even on permits the user knows have them.
    """
    out: list[str] = []
    seen: set[str] = set()
    for rx in (_RE_MOREDETAIL_NAME, _RE_MOREDETAIL_ID):
        for raw in rx.findall(body or ''):
            tgt = (raw.replace('_', '$')
                   if '_' in raw and '$' not in raw
                   else raw)
            if tgt not in seen:
                seen.add(tgt)
                out.append(tgt)
    return out


def _detail_payload_size(body: str) -> int:
    """Return the visible-text size of the two divs that actually
    carry the data we care about (work-location address + permit
    detail / applicant / contact / project description).

    Used by ``fetch_detail`` as a guardrail: if a ``MoreDetail``
    postback comes back with FEWER visible chars in these divs than
    we already had, the postback is destructive (Accela's UpdatePanel
    re-renders an empty data table when triggered without the right
    grid context — observed live on Wyandotte UG / Kansas City KS,
    May 2026: pre-postback `divPermitDetailInfo` had 269 visible
    chars including phone + email + project description; post-postback
    it dropped to 49 chars containing only the literal "More Details
    Parcel Information as of Case Date" — wiping out everything the
    LLM needed). Measuring just the two relevant divs (rather than
    the whole document) avoids being fooled by chrome shrinkage.
    """
    total = 0
    for did in ('divWorkLocationInfo', 'divPermitDetailInfo'):
        snip = extract_div_by_id(body, did)
        if snip:
            total += len(extract_visible_text(snip))
    return total


def fetch_detail(detail_url: str, *, expand_more: bool = True,
                 timeout: int = 60,
                 max_expansions: int = 8) -> str:
    """Pull a single CapDetail page. When ``expand_more`` is true,
    we iteratively issue a postback for EVERY ``MoreDetail`` Show-More
    section the page exposes, so contractor / owner / valuation /
    contact email / phone / parcel / licence info are all materialised
    in the returned HTML before the LLM ever sees it.

    Each expansion can reveal further nested ``MoreDetail`` sections
    (Accela renders them lazily), so we re-scan the post-postback HTML
    and keep going until either no NEW targets appear or we hit
    ``max_expansions`` (safety cap to bound runaway pages).

    Destructive-postback guard
    --------------------------
    Some Accela instances (confirmed live on Wyandotte UG /
    Kansas City KS, May 2026) respond to the ``lnkMoreDetail``
    postback with a re-rendered UpdatePanel whose data tables are
    EMPTY — clobbering the populated rows from the initial GET.
    Symptom in the LLM debug payload: ``cleaned_html`` is 0–22 chars
    even though the raw HTML is 225 KB and the divs are present, so
    the model returns ``page_unreadable`` warnings and every
    contractor / value / description field comes back blank.

    Defence: after every postback, compare the visible-text size of
    ``divWorkLocationInfo + divPermitDetailInfo`` to the pre-postback
    snapshot. If it shrank, the postback was destructive — revert
    to the prior body and stop expanding (further expansions on the
    re-rendered body would only cascade the damage).
    """
    opener = build_proxy_opener()
    cookies: dict = {}
    body, cookies = http_get(cookies, detail_url,
                             timeout=timeout, opener=opener)
    if not expand_more:
        return body

    expanded_targets: set[str] = set()
    for _ in range(max_expansions):
        targets = [t for t in _find_all_moredetail_targets(body)
                   if t not in expanded_targets]
        if not targets:
            break
        target = targets[0]
        expanded_targets.add(target)
        form = parse_all_hidden_inputs(body)
        form['__EVENTTARGET']   = target
        form['__EVENTARGUMENT'] = ''
        action_url = parse_form_action(body, detail_url)
        prev_body = body
        prev_size = _detail_payload_size(body)
        try:
            body, _ = http_post(cookies, action_url, form,
                                timeout=timeout, opener=opener,
                                referer=detail_url)
        except HttpScraperError:
            # One section failing to expand should never sink the
            # whole detail fetch — the LLM still gets what we have.
            body = prev_body
            break
        new_size = _detail_payload_size(body)
        if new_size < prev_size:
            # Destructive postback (see docstring) — keep the rich
            # pre-postback body and abort further expansions.
            body = prev_body
            break
    return body


# ── Row-link extraction (results grid → list[CapDetail urls]) ─────────

_RE_DETAIL_HREF = re.compile(
    r'href="([^"]*CapDetail\.aspx[^"]*)"', flags=re.IGNORECASE)


def extract_detail_links(results_html: str, *, search_url: str) -> list[str]:
    """Pull every distinct CapDetail.aspx absolute URL out of a search-
    results-grid page. Order-preserving dedup so the first appearance
    wins (Accela sometimes renders the same row twice in the DOM).
    Relative hrefs are absolutised against ``search_url`` so the
    fetcher can call them straight."""
    seen: dict[str, None] = {}
    base = search_url
    for raw in _RE_DETAIL_HREF.findall(results_html or ''):
        href = raw.strip().replace('&amp;', '&')
        if not href:
            continue
        absurl = urllib.parse.urljoin(base, href)
        if absurl not in seen:
            seen[absurl] = None
    return list(seen.keys())


# ── Grid-row extraction (results table → list[dict] of pre-LLM facts) ──
#
# Modern Accela CapHome.aspx grids render every row with stable element
# ids of the form ``ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList_
# ctl{NN}_lbl{Field}1?``. The values are plain text — no LLM needed —
# so we harvest them up front as a guaranteed fallback for the LLM
# extraction (which on small models like ``openai-gpt-oss-20b``
# occasionally drops permit_number when the prompt is long).
#
# Discovered live on Wyandotte UG (Kansas City KS) May 2026:
#   lblUpdatedTime  → date column (printed as the page header "Date")
#   lblPermitNumber1→ the human-readable permit number, e.g. "NSF26-0141"
#   lblType         → permit type, e.g. "New Single Family"
#   lblStatus       → status, e.g. "In Review", "Issued"
#   lblAddress      → "<street>, <city> <ST> <zip>" (one cell)
#   <input id=RecordId value="26CAP-00000-00A2W"> — internal Accela id
#
_RE_ROW_BLOCK = re.compile(
    r'<tr[^>]*ACA_TabRow_(?:Odd|Even)[^>]*>(.*?)</tr>',
    flags=re.IGNORECASE | re.DOTALL)
_RE_GRID_ID_PREFIX = re.compile(
    r'_dgvPermitList_gdvPermitList_(ctl\d+)_', flags=re.IGNORECASE)
_RE_LBL = {
    'date':    re.compile(r'_lblUpdatedTime[^>]*>([^<]*)<',   re.IGNORECASE),
    'number':  re.compile(r'_lblPermitNumber1[^>]*>([^<]*)<', re.IGNORECASE),
    'type':    re.compile(r'_lblType[^>]*>([^<]*)<',          re.IGNORECASE),
    'status':  re.compile(r'_lblStatus[^>]*>([^<]*)<',        re.IGNORECASE),
    'address': re.compile(r'_lblAddress[^>]*>([^<]*)<',       re.IGNORECASE),
}
_RE_ROW_HREF = re.compile(
    r'_hlPermitNumber"\s+href="([^"]+)"', re.IGNORECASE)
_RE_RECORD_ID = re.compile(
    r'<input[^>]*ID="RecordId"[^>]*value="([^"]+)"', re.IGNORECASE)


def _txt(html: str) -> str:
    """Decode the small set of HTML entities Accela emits and trim."""
    if not html:
        return ''
    return (html.replace('&amp;', '&')
                 .replace('&nbsp;', ' ')
                 .replace('&#39;', "'")
                 .replace('&quot;', '"')
                 .strip())


def _iso_date(s: str) -> str | None:
    """Parse Accela's MM/DD/YYYY (and a couple of common variants)
    into ISO ``YYYY-MM-DD``. Returns ``None`` on anything we can't
    confidently convert."""
    s = (s or '').strip()
    if not s:
        return None
    from datetime import datetime as _dt
    for fmt in ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d', '%m-%d-%Y'):
        try:
            return _dt.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def split_us_address(addr: str) -> dict:
    """Split a one-line US street address into ``{address, city, state,
    zip}`` using the ``usaddress`` library. Falls back to a simple
    regex if usaddress can't tag it cleanly. Returns empty strings for
    any field we couldn't extract — never raises.

    Examples (live Accela):
      '2510 HIAWATHA ST, KANSAS CITY KS 66104'
        → {'address': '2510 HIAWATHA ST',
           'city':    'KANSAS CITY',
           'state':   'KS',
           'zip':     '66104'}
    """
    out = {'address': '', 'city': '', 'state': '', 'zip': ''}
    raw = (addr or '').strip()
    if not raw:
        return out
    try:
        import usaddress  # imported lazily — only when we have an address
        tagged, _ = usaddress.tag(raw)
        street_parts = []
        for k in ('AddressNumber',
                  'StreetNamePreDirectional', 'StreetNamePreType',
                  'StreetName', 'StreetNamePostType',
                  'StreetNamePostDirectional', 'OccupancyType',
                  'OccupancyIdentifier'):
            v = tagged.get(k)
            if v:
                street_parts.append(v)
        out['address'] = ' '.join(street_parts).strip()
        out['city']    = (tagged.get('PlaceName') or '').strip()
        out['state']   = (tagged.get('StateName') or '').strip().upper()
        zc = (tagged.get('ZipCode') or '').strip()
        out['zip']     = zc[:5] if zc else ''
    except Exception:
        # Fallback: assume the trailing token is a 5-digit zip, the
        # one before is a 2-letter state, the cell before the comma
        # is the city, everything before the comma is the street.
        m = re.match(
            r'^(.*?),\s*(.*?)\s+([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$',
            raw)
        if m:
            out['address'] = m.group(1).strip()
            out['city']    = m.group(2).strip()
            out['state']   = m.group(3).strip().upper()
            out['zip']     = m.group(4).strip()
        else:
            out['address'] = raw  # last resort — raw line in address
    return out


def extract_grid_rows(results_html: str, *,
                      search_url: str) -> list[dict]:
    """Harvest every permit row out of the search-results grid.

    Returns a list of dicts, one per data row::

        {
          'detail_url':    str,    # absolute CapDetail.aspx URL
          'permit_number': str,    # e.g. 'NSF26-0141'
          'permit_type':   str,    # e.g. 'New Single Family'
          'status':        str,    # e.g. 'In Review'
          'address_raw':   str,    # full one-line address as displayed
          'address':       str,    # street portion, via usaddress
          'city':          str,
          'state':         str,
          'zip':           str,
          'grid_date':     str|None,  # ISO YYYY-MM-DD if parseable
          'grid_date_raw': str,    # original MM/DD/YYYY text
          'record_id':     str,    # internal Accela RecordId, e.g. '26CAP-00000-00A2W'
        }

    Order-preserving dedup by ``detail_url`` (Accela occasionally
    renders the same row twice in the DOM). Relative hrefs are
    absolutised against ``search_url`` so the row is ready to drive
    the per-detail fetch loop directly.
    """
    rows: list[dict] = []
    seen_urls: dict[str, None] = {}
    for block in _RE_ROW_BLOCK.findall(results_html or ''):
        href_m = _RE_ROW_HREF.search(block)
        if not href_m:
            continue
        href = href_m.group(1).strip().replace('&amp;', '&')
        if not href:
            continue
        absurl = urllib.parse.urljoin(search_url, href)
        if absurl in seen_urls:
            continue
        seen_urls[absurl] = None

        addr_raw = _txt((_RE_LBL['address'].search(block) or [None, ''])
                        and (_RE_LBL['address'].search(block).group(1)
                             if _RE_LBL['address'].search(block) else ''))
        # Cleaner re-do of the above (the inline conditional was for
        # an empty-block guard — but the search has already been
        # validated; just call once below for clarity):
        def _grab(key: str) -> str:
            m = _RE_LBL[key].search(block)
            return _txt(m.group(1)) if m else ''

        addr_raw = _grab('address')
        date_raw = _grab('date')
        rid_m = _RE_RECORD_ID.search(block)
        split = split_us_address(addr_raw)

        rows.append({
            'detail_url':    absurl,
            'permit_number': _grab('number'),
            'permit_type':   _grab('type'),
            'status':        _grab('status'),
            'address_raw':   addr_raw,
            'address':       split['address'],
            'city':          split['city'],
            'state':         split['state'],
            'zip':           split['zip'],
            'grid_date':     _iso_date(date_raw),
            'grid_date_raw': date_raw,
            'record_id':     rid_m.group(1).strip() if rid_m else '',
        })
    return rows


# ── Full pure-HTTP + DO Inference run (drop-in for old Firecrawl path) ─

ACCELA_MAX_PAGES_DEFAULT   = 50    # Default page-cap for any caller that
                                   # does not pass `max_pages` explicitly.
                                   # The admin "Run now" UI exposes this
                                   # as an editable "# of pages" input so
                                   # backfills can override (1-1000).
ACCELA_TOKENS_PER_PERMIT   = 1500  # only used for credit budgeting, never
                                   # as a per-run permit cap — the user wants
                                   # the full date range, however large.

ACCELA_PER_PAGE_TIMEOUT    = 60
ACCELA_PER_DETAIL_TIMEOUT  = 60


def _detail_llm_enabled() -> bool:
    """Whether to enrich Accela detail parses with GPT-OSS.

    Deterministic parsing is the default because visible Accela facts
    are already extractable from HTML, while local gpt-oss-20b often
    spends the whole request budget reasoning and times out on full
    permit prompts. Set system_settings.accela_detail_llm_enrichment or
    ACCELA_DETAIL_LLM_ENRICHMENT to on/true/1 to opt back in.
    """
    raw = os.environ.get('ACCELA_DETAIL_LLM_ENRICHMENT', '')
    try:
        from ..db import get_system_setting
        raw = raw or (get_system_setting('accela_detail_llm_enrichment') or '')
    except Exception:
        pass
    return str(raw).strip().lower() in ('1', 'true', 'on', 'yes')


def oss_agent_scrape_permits(
    scraper: dict,
    *,
    date_from: str | None = None,
    date_to:   str | None = None,
    prompt_template: str | None = None,
    model:           str | None = None,
    max_credits:     int | None = None,
    max_pages:       int = ACCELA_MAX_PAGES_DEFAULT,
    scraper_run_id:  int | None = None,
    timeout:         int = 600,
    # Optional caller hooks (added 2026-05) for incremental durability:
    #   is_already_inserted(permit_number_from_grid: str) -> bool
    #     If provided, called BEFORE the LLM extraction phase. Grid
    #     rows whose permit_number already exists in the DB are
    #     dropped from the work-list, so a re-run only processes
    #     genuinely new permits and we never burn LLM tokens on rows
    #     we already have.
    #   on_permit_extracted(permit_dict, grid_row) -> None
    #     If provided, called the instant a per-detail extraction
    #     succeeds (inside the worker thread, so the callback MUST
    #     be thread-safe). The caller uses this to upsert each
    #     permit immediately — guaranteeing zero data loss if the
    #     run is stopped mid-flight or the worker crashes during
    #     a later extraction. The returned envelope's ``permits``
    #     list is still populated for back-compat, but callers
    #     using the callback should rely on its own stats.
    is_already_inserted=None,
    on_permit_extracted=None,
    #   on_permit_junk(permit_dict, grid_row) -> None
    #     If provided, called the instant the in-scraper "no email AND
    #     no phone" gate drops a row (BEFORE on_permit_extracted, which
    #     never fires for junk). The caller uses this to record the
    #     junk verdict in `junk_permits` so a re-run's pre-detail skip
    #     loop short-circuits this same permit number without re-paying
    #     fetch_detail + LLM inference. Without this, the scraper-side
    #     gate drops the row silently, upsert_permit never sees it, and
    #     the next run re-burns the same tokens — exactly the $230/day
    #     bug that motivated junk_permits in the first place.
    on_permit_junk=None,
) -> dict:
    """Pure-HTTP + DigitalOcean Serverless Inference replacement for
    the legacy Firecrawl-Agent run. Same envelope contract::

        {
          'ok':      bool,
          'permits': [ {raw extracted dict}, ... ],
          'error':   str | None,
          'log': {
              'status':         'completed' | 'failed',
              'agent_id':       None,             # no remote agent
              'model':          str,
              'credits_used':   int,              # total tokens used
              'credits_budget': int,              # caller's budget
              'prompt':         str,              # truncated prompt
              'latency_ms':     int,
              'error':          str | None,
          },
        }

    Pipeline:
      1. ``search_accela`` POSTs the date-range search (proxied if a
         scraper proxy is configured in Settings).
      2. ``paginate_next`` walks subsequent pages up to ``max_pages``.
      3. ``extract_detail_links`` yields the CapDetail URLs.
      4. ``fetch_detail`` pulls each detail page (with the Show-More
         expand postback when present).
      5. Each detail page goes through ``oss_complete`` against the
         local GPT-OSS parser model. The response is parsed into
         the same shape the caller's ``_normalise_permit`` expects.

    NEVER raises on a per-permit failure — partial results are still
    returned in the envelope; the caller decides what to do.
    """
    from .. import scraper_accela as _sa  # lazy: avoid circular import
    safe_url   = (scraper.get('url') or '').strip()
    safe_city  = (scraper.get('city') or '').strip()
    safe_state = (scraper.get('state') or '').strip()
    if not safe_url:
        return {
            'ok': False, 'permits': [],
            'error': 'Scraper has no URL configured.',
            'log': {'status': 'failed', 'agent_id': None,
                    'model': model or '', 'credits_used': 0,
                    'credits_budget': max_credits or 0, 'prompt': '',
                    'latency_ms': 0, 'error': 'no url'},
        }

    # No per-run permit cap — pagination is bounded by `max_pages`,
    # the date-range filter, and the cooperative cancel flag. The
    # previous cap silently truncated legitimate large date ranges
    # (50,000 credits / 1,500 per permit = exactly 33 permits, which
    # matches the symptom users were hitting).
    chosen_model = (model or '').strip()  # passed straight to oss_complete

    t0 = time.monotonic()
    total_in_tokens  = 0
    total_out_tokens = 0
    permits_out: list[dict] = []
    first_err: str | None = None
    prompt_preview = ''

    # Lazy-import the run-log streamer so this module stays importable
    # in unit-test contexts that don't have the worker stack loaded.
    def _log(msg: str, level: str = 'info') -> None:
        if scraper_run_id is None:
            return
        try:
            from ..db import append_scraper_run_step, update_scraper_run
            append_scraper_run_step(scraper_run_id, msg, level)
            update_scraper_run(scraper_run_id, current_step=msg)
        except Exception:
            log.exception('progress log emit failed')

    def _progress(*, total: int | None = None, processed: int | None = None,
                  succeeded: int | None = None, failed: int | None = None,
                  ) -> None:
        """Push live counters onto ``scraper_runs`` so the admin
        terminal panel's ``N / M · ✓X · ✗Y`` header animates in real
        time during the long parse phase. Without this, the header
        sits at ``0 / 0 · ✓0 · ✗0`` for the entire LLM-extraction
        run and only flips to its final value when the upsert phase
        starts (~minutes later)."""
        if scraper_run_id is None:
            return
        kwargs: dict = {}
        if total     is not None: kwargs['total_targets'] = total
        if processed is not None: kwargs['processed']     = processed
        if succeeded is not None: kwargs['succeeded']     = succeeded
        if failed    is not None: kwargs['failed']        = failed
        if not kwargs:
            return
        try:
            from ..db import update_scraper_run
            update_scraper_run(scraper_run_id, **kwargs)
        except Exception:
            log.exception('progress counter update failed')

    # ─── 1+2. Search + paginate ──────────────────────────────────────
    # Parse the optional date window into real ``date`` objects so we
    # can short-circuit pagination as soon as the grid scrolls past the
    # earliest date the user asked about. The grid already exposes
    # ``grid_date`` per row (parsed from Accela's ``lblUpdatedTime``
    # column), so date filtering is pure Python — no LLM round-trip
    # required just to learn whether row N is in range.
    def _parse_iso(s: str | None):
        if not s:
            return None
        try:
            return datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return None
    df_dt = _parse_iso(date_from)
    dt_dt = _parse_iso(date_to)

    # ── Default date window when caller didn't supply one ────────────
    # Most Accela tenants (Wyandotte UG, City of San Antonio COSA,
    # San Diego, …) treat blank ``txtGSStartDate`` / ``txtGSEndDate``
    # as "no filter applied" and respond with an empty results grid +
    # a "Please enter a search criteria" warning, NOT with all recent
    # permits the way a human would expect. Confirmed live against
    # COSA on 2026-05-05: posting the search form with both date fields
    # blank returned 0 grid rows even though the same form with
    # date_from=today / date_to=today returned a real row.
    #
    # Per product decision (May 2026): when the caller passes None for
    # either bound (cron heartbeat, quick "Run now" without filling in
    # the date pickers, retry of an old run record whose date_from/
    # date_to columns are NULL), default to a very wide 10-year window
    # ending today. This satisfies Accela's "non-blank dates required"
    # constraint while making the date filter effectively a no-op — the
    # only real ceiling on how much we ingest becomes ``max_pages``
    # (50 by default, see ``ACCELA_MAX_PAGES_DEFAULT``). The pagination
    # early-stop on "page entirely older than date_from" also becomes a
    # no-op for the default window, since no active tenant has 50 pages
    # of permits older than 10 years.
    user_pinned_dates = not (df_dt is None and dt_dt is None)
    if not user_pinned_dates:
        from datetime import date as _date, timedelta as _td
        dt_dt = _date.today()
        df_dt = dt_dt - _td(days=3650)        # ~10 years back
        date_from = df_dt.isoformat()
        date_to   = dt_dt.isoformat()
        _log(f'ℹ No date window supplied — using wide default window so '
             f'pagination (capped at max_pages={max_pages}) is the only '
             f'real ceiling.', 'info')

    def _in_range(row: dict) -> bool:
        """True iff the row's grid_date falls inside [df_dt, dt_dt].
        Rows with an unparseable grid date are kept (we cannot prove
        they are out of range — better to defer to the LLM than to
        silently drop them). When the caller didn't pin a date window,
        keep everything — the wide default is just there to satisfy
        Accela's form-validation, not to filter anything out."""
        if not user_pinned_dates:
            return True
        gd = _parse_iso(row.get('grid_date'))
        if gd is None:
            return True
        if df_dt and gd < df_dt:
            return False
        if dt_dt and gd > dt_dt:
            return False
        return True

    def _all_older_than_window(rows: list[dict]) -> bool:
        """True iff every row on the page has a parseable grid_date
        strictly older than ``df_dt``. Used to stop paginating once
        Accela has scrolled past the user's window — the grid is
        sorted newest-first by default, so a whole page of stale
        dates means everything beyond is also stale. Disabled when
        the caller didn't pin a date window so ``max_pages`` is the
        only ceiling."""
        if not user_pinned_dates or not df_dt or not rows:
            return False
        for r in rows:
            gd = _parse_iso(r.get('grid_date'))
            if gd is None or gd >= df_dt:
                return False
        return True

    # Cooperative cancel: cheap DB poll. Returning True from this lambda
    # signals the worker should stop pagination (and skip pending
    # detail extractions) so the admin's Stop button takes effect
    # within ~1 page rather than after the full 50-page cap.
    def _cancel_requested() -> bool:
        if scraper_run_id is None:
            return False
        try:
            from ..db import is_cancel_requested as _icr
            return bool(_icr(scraper_run_id))
        except Exception:
            return False

    grid_rows: list[dict] = []
    # Per-tenant Permit Type dropdown filter (BOCO, San Diego, …).
    # Stored in scrapers.config['permit_type'] as the literal
    # ``value=""`` attribute of the desired ``<option>`` (e.g.
    # ``Building/Application Request/Building Permit/NA``).
    cfg = scraper.get('config') or {}
    permit_type = (cfg.get('permit_type') or '').strip() or None
    if permit_type:
        _log(f'🔎 Permit Type filter active: {permit_type}', 'info')

    # ── Optional dropdown-loop ────────────────────────────────────────
    # Some Accela tenants (Fort Lauderdale, …) effectively require a
    # ``<select>`` filter to be set before the search returns useful
    # rows — leaving "Street Suffix" blank may cap the result set or
    # silently skip permits. Configure ``config['loop_field']`` with
    # the FULL form-field name (e.g.
    # ``ctl00$PlaceHolderMain$generalSearchForm$ddlGSStreetSuffix``)
    # and the scraper will enumerate every non-empty ``<option>`` on
    # the search page, run one search + pagination cycle per value,
    # and merge results — deduping by ``detail_url`` across passes
    # so a permit that matches multiple loop values is only scraped
    # once. Optional ``config['loop_values']`` (csv or list) limits
    # the loop to a chosen subset; empty means "every option".
    loop_field  = (cfg.get('loop_field') or '').strip()
    loop_filter_raw = cfg.get('loop_values')

    # ── Optional Permit Number / General-Search text-box value ───────
    # Some Accela tenants (e.g. Roseville CA — scraper #160) return
    # the most useful permit set when the General Search "Permit
    # Number" text box is populated with a partial value (e.g. "bd"
    # matches every BD-prefixed record). When ``config['search_input']``
    # is set we type it into the standard permit-number text-box field
    # (``ctl00$PlaceHolderMain$generalSearchForm$txtGSPermitNumber`` —
    # the canonical Accela name; override with ``search_input_field``
    # for non-standard tenants). The value is merged into ``extra_form``
    # so the same loop / single-pass plumbing carries it through every
    # pagination POST without further changes.
    search_input = (cfg.get('search_input') or '').strip()
    search_input_field = ((cfg.get('search_input_field') or '').strip()
                          or 'ctl00$PlaceHolderMain$generalSearchForm$txtGSPermitNumber')
    if search_input:
        _log(f'🔎 Search input filter active: "{search_input}" → '
             f'{search_input_field.rsplit("$", 1)[-1]}', 'info')

    def _with_search_input(extra: dict | None) -> dict | None:
        """Augment an ``extra_form`` dict with the configured search-input
        value so every pass (loop or single) sends the same typed text."""
        if not search_input:
            return extra
        merged = dict(extra or {})
        merged[search_input_field] = search_input
        return merged
    if isinstance(loop_filter_raw, list):
        loop_filter = {str(v).strip() for v in loop_filter_raw if str(v).strip()}
    else:
        loop_filter = {v.strip() for v in str(loop_filter_raw or '').split(',')
                       if v.strip()}

    def _scrape_pass(extra_form: dict | None, label: str | None) -> None:
        """Run one search + pagination cycle, optionally with extra
        form fields (the loop value). Appends in-range rows to the
        outer ``grid_rows`` list, deduping by detail_url against rows
        collected on previous passes."""
        nonlocal safe_url
        if label:
            _log(f'🔎 Pass: {label}', 'info')
        _log(f'🔎 POSTing search form: {safe_url}')
        extra_form = _with_search_input(extra_form)
        page = search_accela(safe_url, date_from=date_from,
                             date_to=date_to,
                             permit_type=permit_type,
                             extra_form=extra_form,
                             timeout=ACCELA_PER_PAGE_TIMEOUT)
        resolved = page.get('resolved_url')
        if resolved and resolved != safe_url:
            _log(f'↪ Auto-discovered search page: {resolved}', 'info')
            safe_url = resolved
        action_url = page.get('action') or safe_url
        seen_pre   = {r['detail_url'] for r in grid_rows}
        first_all  = extract_grid_rows(page['html'], search_url=safe_url)
        first_new  = [r for r in first_all if r['detail_url'] not in seen_pre]
        first_rows = [r for r in first_new  if _in_range(r)]
        first_drop = len(first_new) - len(first_rows)
        grid_rows.extend(first_rows)
        drop_blurb = f' (dropped {first_drop} out-of-range)' if first_drop else ''
        _log(f'📄 Page 1: {len(first_rows)} permit row(s) in range'
             f'{drop_blurb}')
        if not first_all:
            _log('ℹ Page 1 returned 0 grid rows — nothing to paginate. '
                 '(Common causes: blank required date filter, search '
                 'form rejected, or the URL really has no permits in '
                 'the requested window.)', 'warn')
            return
        if _all_older_than_window(first_all):
            _log('⏹ Stopping pagination — first page is already entirely '
                 'older than date_from.', 'info')
            return
        for pi in range(2, max_pages + 1):
            if _cancel_requested():
                _log('⏹ Stopping pagination — admin requested cancel.',
                     'warn')
                return
            try:
                page = paginate_next(page['html'], safe_url,
                                     page['cookies'],
                                     page_index=pi,
                                     action_url=action_url,
                                     permit_type=permit_type,
                                     extra_form=extra_form,
                                     timeout=ACCELA_PER_PAGE_TIMEOUT)
            except HttpScraperError:
                _log(f'⏹ Stopping pagination — Accela returned no '
                     f'page {pi}.', 'info')
                return
            seen     = {r['detail_url'] for r in grid_rows}
            page_all = extract_grid_rows(page['html'], search_url=safe_url)
            page_new = [r for r in page_all if r['detail_url'] not in seen]
            if not page_new:
                _log(f'⏹ Stopping pagination — page {pi} had no new '
                     f'rows (Accela looped back to start).', 'info')
                return
            page_in   = [r for r in page_new if _in_range(r)]
            page_drop = len(page_new) - len(page_in)
            grid_rows.extend(page_in)
            drop_blurb = (f' (dropped {page_drop} out-of-range)'
                          if page_drop else '')
            _log(f'📄 Page {pi}: +{len(page_in)} permit row(s) '
                 f'in range{drop_blurb} (total {len(grid_rows)})')
            if _all_older_than_window(page_all):
                _log(f'⏹ Stopping pagination — page {pi} entirely '
                     f'older than {date_from}.', 'info')
                return

    try:
        if loop_field:
            # Resolve the search page once just to enumerate the loop
            # dropdown's options. We don't need the result HTML — only
            # the search FORM HTML — so this is cheap. Each pass below
            # will re-resolve internally (a few extra GETs per loop is
            # acceptable; cookies + proxy reuse kick in via the opener).
            _opener = build_proxy_opener()
            try:
                _resolved, _form_html, _ = _resolve_search_url(
                    safe_url, timeout=ACCELA_PER_PAGE_TIMEOUT,
                    opener=_opener)
            except HttpScraperError as e:
                raise
            options = _extract_select_options(_form_html, loop_field)
            if not options:
                _log(f'⚠ loop_field {loop_field!r} not found on the '
                     f'search page — falling back to a single search '
                     f'pass with no loop.', 'warn')
                _scrape_pass(None, None)
            else:
                if loop_filter:
                    options = [(v, t) for (v, t) in options
                               if v in loop_filter]
                _log(f'🔁 Looping {len(options)} option(s) of '
                     f'{loop_field.rsplit("$", 1)[-1]} '
                     f'(dedup by permit URL across passes).', 'info')
                for idx, (val, txt) in enumerate(options, 1):
                    if _cancel_requested():
                        _log('⏹ Stopping loop — admin requested cancel.',
                             'warn')
                        break
                    label = (f'{idx}/{len(options)} '
                             f'{loop_field.rsplit("$", 1)[-1]}={txt or val} '
                             f'(running total: {len(grid_rows)} row(s))')
                    _scrape_pass({loop_field: val}, label)
        else:
            _scrape_pass(None, None)
    except HttpScraperError as e:
        first_err = f'Accela HTTP search failed: {e}'
        _log(f'❌ {first_err}', 'err')
    except Exception as e:
        first_err = f'Accela search crashed: {e}'
        log.exception('search/paginate crash')
        _log(f'❌ {first_err}', 'err')

    n_targets = len(grid_rows)
    if n_targets == 0:
        _log('⚠ Search succeeded but zero permits matched the date range.',
             'warn')

    # ─── 2b. Re-run skip: drop grid rows we've already ingested ──────
    # The cheapest correct dedup we can do: ask the caller (which has
    # DB access + the source_tag) which permit_numbers from this grid
    # batch are already in `permits`. This avoids burning a $0.0001
    # LLM call per already-known permit on every re-run, AND keeps
    # the live progress total honest (N is "new permits" not "search
    # hits"). Skipped rows are logged once with a count so the admin
    # can see the work being elided.
    skipped_existing = 0
    if grid_rows and is_already_inserted is not None:
        kept: list[dict] = []
        for r in grid_rows:
            pn = (r.get('permit_number') or '').strip()
            if not pn:
                # No permit_number yet — let the LLM phase try; the
                # later upsert step will reject if still empty.
                kept.append(r)
                continue
            try:
                already = bool(is_already_inserted(pn))
            except Exception:
                # Callback should be defensive; if it raises we err
                # on the side of re-processing rather than dropping.
                log.exception('is_already_inserted callback failed for %s', pn)
                already = False
            if already:
                skipped_existing += 1
            else:
                kept.append(r)
        if skipped_existing:
            _log(f'⏭ Skipping {skipped_existing}/{n_targets} permit(s) '
                 f'already in DB — re-processing only the {len(kept)} '
                 f'new one(s).', 'info')
        grid_rows = kept
        n_targets = len(grid_rows)

    use_detail_llm = _detail_llm_enabled()
    if n_targets > 0:
        if use_detail_llm:
            _log(f'🚀 Parsing {n_targets} permit detail page(s) via '
                 f'deterministic parser + GPT-OSS enrichment '
                 f'(4 in parallel)…')
        else:
            _log(f'🚀 Parsing {n_targets} permit detail page(s) via '
                 f'deterministic parser (4 parallel fetches; GPT-OSS '
                 f'detail enrichment disabled)…')
    # Seed the run header so the admin terminal shows ``0 / N`` while
    # the LLM extractions are still in flight, instead of staying at
    # ``0 / 0`` until the upsert phase begins.
    _progress(total=n_targets, processed=0, succeeded=0, failed=0)

    # ─── 3. Per-detail extraction via DO Serverless Inference ────────
    extract_sys_prompt = ''
    try:
        extract_sys_prompt = _sa.get_extraction_prompt() or ''
    except Exception:
        extract_sys_prompt = ''

    # Fields we always backfill from the grid row when the LLM left
    # them blank/null. Grid values are scraped from plain HTML labels,
    # so they're 100 % reliable — never let the LLM "lose" them.
    _GRID_FALLBACK_FIELDS = (
        'permit_number', 'permit_type', 'status',
        'address', 'city', 'state', 'zip',
    )

    def _merge_grid_into_llm(llm_dict: dict, row: dict) -> dict:
        """Fill any missing/blank fields in the LLM extraction with
        the grid-row facts. LLM wins on conflict — we only fill blanks.
        Always sets ``detail_url`` and ``record_number`` from the grid.
        """
        merged = dict(llm_dict or {})
        # Drop AI fields the model emits despite the prompt telling it not
        # to. ``ai_reasoning`` and ``ai_next_action`` are unused anywhere
        # downstream — they only clutter the run output preview and the
        # persisted JSONB. Strip here, the single chokepoint every output
        # path (parse-ok, parse-fail, fetch-fail, crash) funnels through,
        # so no scraper ever surfaces or stores them.
        for _dead in ('ai_reasoning', 'ai_next_action'):
            merged.pop(_dead, None)
        # Hard-set provenance fields the grid is always authoritative for.
        merged['detail_url'] = row['detail_url']
        if row.get('record_id') and not merged.get('record_number'):
            merged['record_number'] = row['record_id']
        # Backfill grid → LLM blanks for the user-visible fields.
        for f in _GRID_FALLBACK_FIELDS:
            cur = merged.get(f)
            if cur in (None, '', 0) and row.get(f):
                merged[f] = row[f]
        # issued_date: the grid "Date" column (lblUpdatedTime). LLM
        # may have provided issued_date OR applied_date; only fill
        # issued_date if BOTH LLM dates are missing.
        if (not merged.get('issued_date')
                and not merged.get('applied_date')
                and row.get('grid_date')):
            merged['issued_date'] = row['grid_date']
        return merged

    def _backfill_llm_from_parser(llm_dict: dict, parsed: dict) -> dict:
        """Fill blank GPT fields from the deterministic Accela parser."""
        def _blank(value) -> bool:
            text = str(value or '').strip().lower()
            return text in ('', 'none', 'null', 'n/a', 'na', '-')

        merged = dict(llm_dict or {})
        for field, value in (parsed or {}).items():
            if field.startswith('__'):
                continue
            if _blank(value) or value == 0:
                continue
            if _blank(merged.get(field)) or merged.get(field) == 0:
                merged[field] = value
        return merged

    def _process_one(idx_row: tuple[int, dict]) -> dict:
        idx, row = idx_row
        durl = row['detail_url']
        out_rec: dict = {'idx': idx, 'row': row, 'durl': durl,
                          'permit': None, 'in': 0, 'out': 0,
                          'err': None, 'prompt_preview': '',
                          'prompt_head_tail': '', 'cleaned_head_tail': '',
                          'prompt_chars': 0, 'cleaned_chars': 0,
                          'llm_text': '', 'llm_model': '',
                          'method': 'deterministic parser'}
        parsed_detail: dict = {}

        def _attach_debug(rec: dict) -> None:
            """Glue the per-permit LLM debug payload onto the permit
            dict so the upsert callback can persist it into the row's
            JSONB ``raw`` envelope. Surfaced in the admin "View"
            modal as the answer to "what input did you give the
            model, and what did it actually return?"."""
            permit = rec.get('permit')
            if not isinstance(permit, dict):
                return
            permit['__llm_debug'] = {
                'model':         rec.get('llm_model') or '',
                'detail_url':    rec.get('durl') or '',
                'prompt':        rec.get('prompt_head_tail') or '',
                'prompt_chars':  rec.get('prompt_chars') or 0,
                'cleaned_html':  rec.get('cleaned_head_tail') or '',
                'cleaned_chars': rec.get('cleaned_chars') or 0,
                'raw_response':  rec.get('llm_text') or '',
                'input_tokens':  rec.get('in') or 0,
                'output_tokens': rec.get('out') or 0,
                'parse_error':   rec.get('err') or '',
            }
        try:
            html = fetch_detail(durl, expand_more=True,
                                timeout=ACCELA_PER_DETAIL_TIMEOUT)
            # Use extract_permit_text() — the verbatim pipeline from the
            # user's reference parser (attached_assets/
            # accela_parser_test_*.py): strip every HTML tag, drop the
            # ~50-string noise list (menu labels, empty-state text,
            # file-upload chrome, …), drop blank/single-char lines.
            #
            # Empirical sizes for a typical Accela CapDetail page:
            #   raw HTML            : ~370 KB
            #   clean_html (tags)   : ~110 KB  (28k tokens)
            #   extract_visible_text: ~3 KB
            #   extract_permit_text : ~500-700 chars  ← what we use
            #
            # The two earlier attempts (clean_html with structure, then
            # extract_visible_text without the noise filter) both left
            # the model with too much chrome around the actual permit
            # fields and contractor / phone / email / valuation came
            # back blank. This pipeline matches the user's working
            # standalone test exactly.
            cleaned = extract_permit_text(html)
            parsed_detail = parse_accela_detail(cleaned, source_url=durl)
            if not use_detail_llm:
                out_rec['permit'] = _merge_grid_into_llm(parsed_detail, row)
                out_rec['llm_model'] = 'deterministic-parser'
                _attach_debug(out_rec)
                return out_rec
            # ── GPT-OSS-only extraction ──
            # Every permit's details come straight from DO Serverless
            # Inference (GPT-OSS). The system prompt is sent as a separate
            # `system` role message (NOT concatenated into the user
            # content), and the user content is just the URL + cleaned
            # text in the shape the reference uses:
            #   user_content = f"PAGE URL: {detail_url}\n\n{permit_text}"
            user_content = (
                f'PAGE URL: {durl}\n\n{cleaned}'
                if durl else cleaned
            )
            # `full_prompt` is kept for the debug payload only — what we
            # actually send to the model is system + user_content via
            # oss_complete(system=..., prompt=user_content).
            full_prompt = (
                (extract_sys_prompt + '\n\n' if extract_sys_prompt else '')
                + user_content
            )
            if idx == 1:
                out_rec['prompt_preview'] = full_prompt[:2000]
            # Stash a per-permit copy of EXACTLY what we sent the model
            # (head + tail of the prompt) so the admin "View" modal can
            # show "here's the input, here's the raw output" — answers
            # the recurring "I'm not sure what input you're giving the
            # model" question without bloating Postgres JSONB. 12 KB
            # head + 4 KB tail keeps the row small while preserving
            # both the system instructions at the top and the closing
            # context at the bottom of the cleaned HTML.
            def _trim(s: str, head: int = 12000, tail: int = 4000) -> str:
                if not s or len(s) <= head + tail + 64:
                    return s or ''
                return (s[:head]
                        + f'\n…[{len(s) - head - tail} chars trimmed]…\n'
                        + s[-tail:])
            out_rec['prompt_head_tail'] = _trim(full_prompt)
            out_rec['cleaned_head_tail'] = _trim(cleaned)
            out_rec['prompt_chars'] = len(full_prompt)
            out_rec['cleaned_chars'] = len(cleaned)
            llm = oss_complete(
                user_content,
                system=extract_sys_prompt or None,
                model=chosen_model or None,
                source='accela_scraper_agent',
                scraper_run_id=scraper_run_id,
                timeout=min(timeout, 120),
                # Per-detail JSON the model has to emit covers ~50
                # fields: identification + dates + location + parties
                # (incl. contractor_phone / contractor_email) + work
                # characterisation + 9 sub-scores + 12 signals + tags
                # + reasoning + next-action + outreach + warnings.
                # Empirically that's 2.0-2.8k output tokens. The
                # module-wide default of 1500 was truncating the
                # response mid-JSON — _extract_json then raised, the
                # row fell back to the grid-only merge, and the
                # admin saw blank phone/email/owner/contractor on
                # permits that DO render those fields. Pinning a
                # generous per-call cap fixes that.
                max_tokens=4000,
            )
            out_rec['in']  = llm.get('input_tokens')  or 0
            out_rec['out'] = llm.get('output_tokens') or 0
            llm_text = llm.get('text') or ''
            out_rec['llm_text'] = llm_text
            out_rec['llm_model'] = llm.get('model') or chosen_model or ''
            try:
                raw = _sa._extract_json(llm_text)
            except Exception as e:
                # LLM failed to return JSON — still emit a row built
                # from the grid alone so we never silently drop a
                # permit we already know exists.
                out_rec['err'] = f'JSON parse failed: {e}'
                out_rec['permit'] = _merge_grid_into_llm(parsed_detail, row)
                _attach_debug(out_rec)
                return out_rec
            if isinstance(raw, dict):
                # GPT-OSS is the sole extractor; only the search-results
                # grid backfills permit #/address/etc. that the model
                # left blank. The deterministic parser also fills blanks
                # so visible contact fields are not lost when local GPT-OSS
                # omits them.
                raw = _backfill_llm_from_parser(raw, parsed_detail)
                out_rec['permit'] = _merge_grid_into_llm(raw, row)
                out_rec['method'] = 'GPT-OSS inference'
                _attach_debug(out_rec)
            else:
                # Model returned valid JSON that wasn't an object (a list
                # or scalar) — treat it as a parse failure so the run loop
                # never counts a permit-less row as a success. Keep the
                # grid row so we don't silently drop a permit we know
                # exists (mirrors the JSON-exception path above).
                out_rec['err'] = (
                    f'JSON parse failed: expected object, got '
                    f'{type(raw).__name__}'
                )
                out_rec['permit'] = _merge_grid_into_llm(parsed_detail, row)
                _attach_debug(out_rec)
        except HttpScraperError as e:
            out_rec['err'] = f'fetch failed: {e}'
            out_rec['permit'] = _merge_grid_into_llm(parsed_detail, row)
        except Exception as e:
            log.exception('per-permit failure')
            out_rec['err'] = f'crashed: {e}'
            out_rec['permit'] = _merge_grid_into_llm(parsed_detail, row)
        return out_rec

    # Run up to 4 detail extractions concurrently. DO Inference is the
    # bottleneck (~5-10s per call), and Accela tolerates 4 parallel
    # in-flight requests fine in our live tests.
    if grid_rows:
        completed = 0
        parsed_ok = 0
        parsed_err = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_process_one, (i, r)): i
                       for i, r in enumerate(grid_rows, 1)}
            cancelled_mid = False
            for fut in as_completed(futures):
                # Pre-check cancel BEFORE waiting on the next result so
                # the Stop button takes effect within ~one in-flight LLM
                # call (≤ ACCELA_PER_DETAIL_TIMEOUT) rather than after
                # the entire queued backlog drains. We cancel every
                # not-yet-running future (ThreadPoolExecutor honours
                # cancel() only for tasks still in the work-queue) and
                # then break out — the 1-3 already-running threads will
                # finish and the pool's __exit__ joins them.
                if not cancelled_mid and _cancel_requested():
                    cancelled_mid = True
                    pending = sum(1 for f in futures if f.cancel())
                    in_flight = sum(1 for f in futures
                                    if not f.done() and not f.cancelled())
                    _log(f'⏹ Cancel requested — skipped {pending} queued '
                         f'extraction(s); waiting for {in_flight} in-flight '
                         f'to finish.', 'warn')
                if fut.cancelled():
                    continue
                try:
                    rec = fut.result()
                except Exception as e:
                    log.exception('per-permit future crashed')
                    parsed_err += 1
                    completed += 1
                    if first_err is None:
                        first_err = f'permit future crashed: {e}'
                    _progress(processed=completed,
                              succeeded=parsed_ok,
                              failed=parsed_err)
                    continue
                completed += 1
                idx = rec['idx']
                total_in_tokens  += rec['in']
                total_out_tokens += rec['out']
                if rec['prompt_preview'] and not prompt_preview:
                    prompt_preview = rec['prompt_preview']
                if rec['permit'] is not None:
                    # Drop "junk" rows the model parsed but that have
                    # NEITHER a contact email NOR a contact phone —
                    # those records are unactionable for outreach (the
                    # whole point of the platform) and just clutter the
                    # admin grid. We check contractor_* / applicant_* /
                    # owner_* in turn (whichever family the LLM
                    # populated), and treat whitespace / "None" / "N/A"
                    # strings as empty.
                    def _blank(v) -> bool:
                        s = str(v or '').strip().lower()
                        return s in ('', 'none', 'null', 'n/a', 'na', '-')
                    p = rec['permit']
                    no_email = (_blank(p.get('contractor_email'))
                                and _blank(p.get('applicant_email'))
                                and _blank(p.get('owner_email')))
                    no_phone = (_blank(p.get('contractor_phone'))
                                and _blank(p.get('applicant_phone'))
                                and _blank(p.get('owner_phone')))
                    if no_email and no_phone:
                        pn_skip = (p.get('permit_number')
                                   or rec['row'].get('permit_number') or '')
                        suffix_skip = f" — {pn_skip}" if pn_skip else ''
                        _log(f"  ⊘ permit {completed}/{n_targets} "
                             f"skipped (no email AND no phone — "
                             f"unactionable for outreach)"
                             f"{suffix_skip}", 'warn')
                        # Record the junk verdict so the next run's
                        # pre-detail skip loop drops this permit_number
                        # without re-paying fetch_detail + LLM tokens.
                        # Callback is best-effort: a write failure
                        # MUST NOT break the run.
                        if on_permit_junk is not None:
                            try:
                                on_permit_junk(p, rec['row'])
                            except Exception:
                                log.exception(
                                    'on_permit_junk callback failed')
                        if on_permit_extracted is None:
                            _progress(processed=completed,
                                      succeeded=parsed_ok,
                                      failed=parsed_err)
                        else:
                            _progress(processed=completed)
                        continue
                    permits_out.append(rec['permit'])
                    # Per-permit immediate durability: hand the freshly
                    # extracted permit to the caller's upsert callback
                    # the instant it's ready, so a Stop / Force-stop /
                    # crash later in the run can never lose this one.
                    # Errors are logged and surfaced via rec['err'] so
                    # the live counters reflect the DB-write outcome,
                    # not just the parse outcome.
                    if on_permit_extracted is not None:
                        try:
                            on_permit_extracted(rec['permit'], rec['row'])
                        except Exception as cb_err:
                            log.exception('on_permit_extracted callback failed')
                            rec['err'] = (
                                (rec['err'] + ' | ' if rec['err'] else '')
                                + f'upsert callback: {cb_err}'
                            )
                pn = ((rec['permit'] or {}).get('permit_number')
                      or rec['row'].get('permit_number') or '')
                suffix = f" — {pn}" if pn else ''
                # Surface contact-extraction outcome in the run log so
                # the admin can see at a glance, per permit, whether
                # the LLM pulled an email + phone out of the detail
                # page or not (and which value it picked). This is the
                # #1 reason a permit gets reported as "blank" — the
                # row parses fine but contractor_email/phone come back
                # empty either because the page genuinely doesn't list
                # them or because the prompt missed them.
                p = rec.get('permit') or {}
                def _pick(*keys):
                    for k in keys:
                        v = str(p.get(k) or '').strip()
                        if v and v.lower() not in ('none','null','n/a','na','-'):
                            return v
                    return ''
                em = _pick('contractor_email','applicant_email','owner_email')
                ph = _pick('contractor_phone','applicant_phone','owner_phone')
                em_part = f"✉ {em}" if em else "✉ ∅"
                ph_part = f"☎ {ph}" if ph else "☎ ∅"
                contact = f" · {em_part} · {ph_part}"
                if rec['err']:
                    parsed_err += 1
                    if first_err is None:
                        first_err = f"permit {idx} {rec['err']}"
                    _log(f"  ⚠ permit {completed}/{n_targets} "
                         f"enrichment {rec['err'][:120]} — kept parsed row"
                         f"{suffix}{contact}", 'warn')
                else:
                    parsed_ok += 1
                    _log(f"  ✓ permit {completed}/{n_targets} "
                         f"parsed via {rec.get('method') or 'deterministic parser'}"
                         f"{suffix}{contact}", 'ok')
                # Live header counters so ``N / M · ✓X · ✗Y`` ticks
                # forward in real time during the parse phase. The
                # upsert phase will overwrite these at the end with
                # the authoritative DB-write outcome (insert / update
                # / cross-src-dup vs. parse-OK), so a parse here
                # counted as ✓ is a "candidate" — the final summary
                # may downgrade it if the row fails identity checks.
                if on_permit_extracted is None:
                    _progress(processed=completed,
                              succeeded=parsed_ok,
                              failed=parsed_err)
                else:
                    _progress(processed=completed)

    used_tokens = total_in_tokens + total_out_tokens
    elapsed_ms  = int((time.monotonic() - t0) * 1000)

    final_model = (chosen_model
                   or (permits_out and 'auto')
                   or 'auto')
    return {
        # ``ok`` = the run completed without a fatal error AND it
        # produced something downstream can act on. Either we
        # extracted at least one permit, OR we found grid rows that
        # we attempted to parse (so the search itself succeeded
        # even if the LLM came back empty for every row).
        'ok':      first_err is None and bool(permits_out or grid_rows),
        'permits': permits_out,
        'error':   first_err,
        'log': {
            'status':         'completed' if first_err is None else 'failed',
            'agent_id':       None,
            'model':          chosen_model or 'auto',
            'credits_used':   used_tokens,
            'credits_budget': max_credits or 0,
            'prompt':         prompt_preview[:2000],
            'latency_ms':     elapsed_ms,
            'error':          first_err,
        },
    }
