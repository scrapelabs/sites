"""Rendered-page fetching helpers backed by local Playwright.

Used by admin-only tooling that needs JavaScript-rendered article/page text
without sending content through a paid scraping API. Proxy routing is driven by
the ``datacenter_proxy`` system setting (or ``DATACENTER_PROXY`` env var).
"""

from __future__ import annotations

import logging
import os
import ipaddress
import re
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

from lxml import html as _lxml_html

from .db import get_system_setting
from .scrapers.base import build_proxy_opener, parse_proxy_string


log = logging.getLogger(__name__)

_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)


class BrowserFetchError(Exception):
    """Safe-to-display fetch failure."""


def _host_targets_private_network(host: str, port: int) -> bool:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise BrowserFetchError('URL host could not be resolved') from e
    for item in addresses:
        ip = ipaddress.ip_address(item[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return True
    return False


def _url_targets_private_network(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return True
    host = (parsed.hostname or '').strip().lower().rstrip('.')
    if not host or host == 'localhost' or host.endswith('.localhost'):
        return True
    return _host_targets_private_network(host, parsed.port or (443 if parsed.scheme == 'https' else 80))


def _validate_url(url: str) -> str:
    url = (url or '').strip()
    if not url:
        raise BrowserFetchError('URL is required')
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise BrowserFetchError('URL must start with http:// or https://')
    host = (parsed.hostname or '').strip().lower().rstrip('.')
    if not host:
        raise BrowserFetchError('URL host is required')
    if host == 'localhost' or host.endswith('.localhost'):
        raise BrowserFetchError('Localhost URLs are not allowed')
    if _host_targets_private_network(host, parsed.port or (443 if parsed.scheme == 'https' else 80)):
        raise BrowserFetchError('Private, local, link-local, and reserved network URLs are not allowed')
    return url


def datacenter_proxy_raw() -> str:
    return ((get_system_setting('datacenter_proxy') or '').strip()
            or (os.environ.get('DATACENTER_PROXY') or '').strip())


def datacenter_proxy_info() -> dict:
    raw = datacenter_proxy_raw()
    parsed = parse_proxy_string(raw) if raw else None
    return {
        'configured': bool(parsed),
        'label': f"{parsed['host']}:{parsed['port']}" if parsed else '',
        'valid': bool(parsed) or not raw,
    }


def _playwright_proxy(raw: str) -> dict | None:
    parsed = parse_proxy_string(raw)
    if not parsed:
        if raw:
            raise BrowserFetchError('Datacenter proxy format is invalid. Use user:pass@host:port or host:port.')
        return None
    proxy = {'server': f"{parsed['scheme']}://{parsed['host']}:{parsed['port']}"}
    if parsed.get('user'):
        proxy['username'] = parsed['user']
        proxy['password'] = parsed.get('password') or ''
    return proxy


def _chrome_executables() -> list[str | None]:
    candidates = [
        Path(os.environ.get('CHROME_PATH') or ''),
        Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
        Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
    ]
    out: list[str | None] = []
    for p in candidates:
        try:
            if str(p) and p.exists() and p.is_file():
                out.append(str(p))
        except OSError:
            continue
    out.append(None)  # fallback to Playwright-managed Chromium if installed
    return out


def _clean_text(text: str) -> str:
    text = (text or '').replace('\r', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def fetch_rendered_text(url: str, *, timeout: int = 60) -> dict:
    """Render ``url`` in Chrome/Edge and return readable text + metadata."""
    url = _validate_url(url)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # pragma: no cover - depends on deployment package
        raise BrowserFetchError('Playwright is not installed in the app environment.') from e

    timeout_ms = max(5000, int(timeout or 60) * 1000)
    proxy = _playwright_proxy(datacenter_proxy_raw())
    last_error = None
    started = time.monotonic()

    with sync_playwright() as p:
        for executable_path in _chrome_executables():
            browser = None
            try:
                launch_kwargs = {
                    'headless': True,
                    'args': ['--disable-gpu', '--disable-dev-shm-usage'],
                }
                if executable_path:
                    launch_kwargs['executable_path'] = executable_path
                if proxy:
                    launch_kwargs['proxy'] = proxy
                browser = p.chromium.launch(**launch_kwargs)
                context = browser.new_context(user_agent=_UA, viewport={'width': 1366, 'height': 900})
                def _guard_route(route):
                    try:
                        if _url_targets_private_network(route.request.url):
                            return route.abort()
                    except BrowserFetchError:
                        return route.abort()
                    return route.continue_()
                context.route('**/*', _guard_route)
                page = context.new_page()
                response = page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
                try:
                    page.wait_for_load_state('networkidle', timeout=5000)
                except Exception:
                    pass
                data = page.evaluate(
                    """() => {
                      const meta = (name) => {
                        const el = document.querySelector(`meta[name="${name}"]`) ||
                                   document.querySelector(`meta[property="${name}"]`);
                        return el ? (el.content || '') : '';
                      };
                      document.querySelectorAll('script,style,noscript,svg,canvas,nav,header,footer,aside,form,button,input,textarea,select,iframe,[aria-hidden="true"]').forEach(el => el.remove());
                      const roots = Array.from(document.querySelectorAll('article,main,[role="main"],.post-content,.entry-content,.article-content,.content'));
                      roots.sort((a,b) => ((b.innerText || b.textContent || '').length - (a.innerText || a.textContent || '').length));
                      const root = roots[0] || document.body;
                      return {
                        title: document.title || meta('og:title') || '',
                        description: meta('description') || meta('og:description') || '',
                        ogImage: meta('og:image') || '',
                        text: (root.innerText || root.textContent || '').trim(),
                        finalUrl: location.href,
                      };
                    }"""
                ) or {}
                text = _clean_text(data.get('text') or '')
                if not text:
                    raise BrowserFetchError('Playwright returned no readable text for that URL.')
                metadata = {
                    'title': data.get('title') or '',
                    'description': data.get('description') or '',
                    'ogImage': data.get('ogImage') or '',
                    'sourceURL': data.get('finalUrl') or url,
                    'statusCode': response.status if response else None,
                    'latencyMs': int((time.monotonic() - started) * 1000),
                    'proxy': datacenter_proxy_info().get('label') or '',
                }
                title = _clean_text(metadata['title'])
                markdown = f"# {title}\n\n{text}" if title and title not in text[:200] else text
                return {'markdown': markdown, 'metadata': metadata}
            except BrowserFetchError:
                raise
            except Exception as e:
                last_error = e
                log.debug('Playwright launch/fetch failed executable=%s error=%s', executable_path, e)
            finally:
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
    msg = str(last_error or 'unknown error')
    if 'Executable doesn' in msg or 'browser executable' in msg or 'playwright install' in msg.lower():
        raise BrowserFetchError('Playwright is installed, but no browser executable was found. Install Chrome/Edge, set CHROME_PATH, or run `playwright install chromium`.')
    raise BrowserFetchError(f'Playwright failed to load the URL: {last_error}')


def web_search(query: str, *, limit: int = 10, timeout: int = 20) -> list[dict]:
    """Small Bing HTML search fallback used by admin Accela finder tooling."""
    query = (query or '').strip()
    if not query:
        return []
    proxy_raw = datacenter_proxy_raw()
    parsed = parse_proxy_string(proxy_raw) if proxy_raw else None
    opener = build_proxy_opener(parsed['url'] if parsed else None)
    url = 'https://www.bing.com/search?q=' + urllib.parse.quote_plus(query)
    req = urllib.request.Request(url, headers={'User-Agent': _UA, 'Accept': 'text/html'})
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
        tree = _lxml_html.fromstring(body)
        rows = []
        for li in tree.xpath("//li[contains(concat(' ', normalize-space(@class), ' '), ' b_algo ')]"):
            hrefs = li.xpath('.//h2/a/@href')
            if not hrefs:
                continue
            title = _clean_text(' '.join(li.xpath('.//h2//text()')))
            desc = _clean_text(' '.join(li.xpath('.//p//text()')))
            rows.append({'url': hrefs[0], 'title': title, 'description': desc})
            if len(rows) >= limit:
                break
        return rows
    except Exception:
        log.exception('web_search failed for query=%s', query[:100])
        return []
