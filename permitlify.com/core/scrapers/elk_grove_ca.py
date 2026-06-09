"""Elk Grove CA — standalone Accela permit-scraper bot.

Thin per-tenant config wrapper around the shared
:mod:`core.scrapers._accela_city` helper. Every line of behaviour
(HTTP/CSRF, pagination, grid harvesting, LLM extraction, CLI surface,
default budgets) lives in the helper — change a default there and
every city inherits it on the next run.

Run from the shell::

    DJANGO_DEBUG=1 python -m core.scrapers.elk_grove_ca \\
        --date-from 2026-05-01 --date-to 2026-05-03 --max 10
"""

from __future__ import annotations

import sys

from ._accela_city import CityConfig, make_run, make_cli

CFG = CityConfig(
    scraper_id = 77,
    city       = 'Elk Grove',
    state      = 'CA',
    url        = 'https://aca-prod.accela.com/elkgrove/Default.aspx',
)

run     = make_run(CFG)
run_cli = make_cli(CFG)


if __name__ == '__main__':
    sys.exit(run_cli())
