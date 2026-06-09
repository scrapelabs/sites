"""Per-source municipal-permit scraper engines.

Each public-records platform (Accela, Tyler EnerGov, OpenGov, CivicPlus,
Granicus, …) lives in its own module — same shape, copied from the
runbook in ``templates/core/admin_scraper_agent_plan.html``:

  * ``base.py``    — shared utilities every engine reuses: HTML cleaner,
                     ASP.NET WebForms helpers, proxied HTTP client,
                     DigitalOcean GPT-OSS inference call.
  * ``accela.py``  — Accela ASP.NET WebForms search → paginate → detail.
  * (planned)      ``tyler_energov.py``, ``opengov.py`` …

Every engine ends with one ``oss_extract(html, ...)`` call that hands the
cleaned page to GPT-OSS-20B and returns the same flat permit dict shape
as the legacy Claude pipeline, so downstream worker code is engine-agnostic.
"""

from .base import (
    HttpScraperError,
    OSS_MODELS,
    DEFAULT_OSS_MODEL,
    DO_BASE_URL,
    clean_html,
    parse_webforms_state,
    parse_all_hidden_inputs,
    parse_form_action,
    oss_complete,
    oss_extract,
    build_proxy_opener,
    parse_proxy_string,
)

from .accela import (
    search_accela,
    paginate_next,
    extract_grid_rows,
    split_us_address,
    fetch_detail,
)

# Per-city standalone bots. Each module is a self-contained, isolated
# wrapper around the generic Accela engine — see the docstring of
# ``kansas_city_ks`` for the template/extension guide.
from . import kansas_city_ks  # noqa: F401

__all__ = [
    'HttpScraperError',
    'OSS_MODELS', 'DEFAULT_OSS_MODEL', 'DO_BASE_URL',
    'clean_html', 'parse_webforms_state',
    'parse_all_hidden_inputs', 'parse_form_action',
    'oss_complete', 'oss_extract',
    'build_proxy_opener', 'parse_proxy_string',
    'search_accela', 'paginate_next', 'fetch_detail',
    'extract_grid_rows', 'split_us_address',
]
