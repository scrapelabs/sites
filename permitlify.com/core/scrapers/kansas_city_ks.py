"""Kansas City KS — standalone permit-scraper bot.

This was the original copy-paste reference implementation; the
boilerplate has now been extracted into
:mod:`core.scrapers._accela_city` so every Accela-backed city in
:mod:`core.scrapers` shares the same engine wiring, CLI surface and
default budgets. To extend to another Accela tenant, copy this file,
change the four :class:`CityConfig` fields, and you're done.

Live verified May 2026 against the Wyandotte County / Unified
Government (UG) Citizen Access tenant: 10 rows, all five grid columns
populated, addresses cleanly split into street/city/state/zip via
``usaddress``.
"""

from __future__ import annotations

import sys

from ._accela_city import CityConfig, make_run, make_cli

CFG = CityConfig(
    scraper_id = 37,
    city       = 'Kansas City',
    state      = 'KS',
    url        = 'https://aca-prod.accela.com/UG/Cap/CapHome.aspx?module=Building&TabName=Home',
)

run     = make_run(CFG)
run_cli = make_cli(CFG)


if __name__ == '__main__':
    sys.exit(run_cli())
