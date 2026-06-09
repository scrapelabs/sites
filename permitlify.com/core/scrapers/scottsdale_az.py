"""Scottsdale AZ — standalone Accela permit-scraper bot.

Thin per-tenant config wrapper around the shared
:mod:`core.scrapers._accela_city` helper. Every line of behaviour
(HTTP/CSRF, pagination, grid harvesting, LLM extraction, CLI surface,
default budgets) lives in the helper — change a default there and
every city inherits it on the next run.

Run from the shell::

    DJANGO_DEBUG=1 python -m core.scrapers.scottsdale_az \\
        --date-from 2026-05-01 --date-to 2026-05-03 --max 10
"""

from __future__ import annotations

import sys

from ._accela_city import CityConfig, make_run, make_cli

CFG = CityConfig(
    scraper_id = 42,
    city       = 'Scottsdale',
    state      = 'AZ',
    url        = 'https://aca-prod.accela.com/SCOTTSDALE/Cap/CapHome.aspx?module=Building',
)

run     = make_run(CFG)
run_cli = make_cli(CFG)


if __name__ == '__main__':
    sys.exit(run_cli())
