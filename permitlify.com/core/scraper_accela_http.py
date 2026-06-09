"""Backwards-compat shim — re-exports from the new ``core.scrapers`` package.

The Accela HTTP+OSS engine moved into ``core/scrapers/`` so each new
municipal source can live in its own file alongside it
(``core/scrapers/tyler_energov.py``, ``core/scrapers/opengov.py``, …)
following the same template. This shim keeps any older imports
(``from .scraper_accela_http import OSS_MODELS``) working unchanged.

New code should import directly from ``core.scrapers`` instead.
"""

from .scrapers import (  # noqa: F401  (re-export)
    HttpScraperError,
    OSS_MODELS,
    DEFAULT_OSS_MODEL,
    DO_BASE_URL,
    clean_html,
    parse_webforms_state,
    oss_complete,
    oss_extract,
    build_proxy_opener,
    parse_proxy_string,
    search_accela,
    paginate_next,
    fetch_detail,
)
