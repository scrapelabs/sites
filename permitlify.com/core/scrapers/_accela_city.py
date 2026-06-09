"""Shared boilerplate for single-Accela-tenant scraper modules.

Every Accela-backed city scraper in :mod:`core.scrapers` is a thin
config wrapper around :func:`core.scrapers.accela.oss_agent_scrape_permits`.
The KS reference bot (:mod:`core.scrapers.kansas_city_ks`) used to
hand-roll its own ``run`` + ``run_cli`` + ``_scraper_payload`` block,
which meant copy-pasting ~150 lines of identical code into every new
city file. That violates the user's standing instruction
(\"don't reinvent functions if we have same function used in many
place put it in a helper\").

This module is that helper. A per-city scraper now needs only a
:class:`CityConfig` instance + two factory calls::

    from ._accela_city import CityConfig, make_run, make_cli

    CFG = CityConfig(
        scraper_id = 36,
        city       = 'Wichita',
        state      = 'KS',
        url        = 'https://aca-prod.accela.com/WICHITA/Default.aspx',
    )

    run     = make_run(CFG)
    run_cli = make_cli(CFG)

    if __name__ == '__main__':
        import sys
        sys.exit(run_cli())

Nothing about the engine, CLI surface, default budget, or envelope
shape is duplicated per-city — change a default here and every city
inherits it on the next run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from .accela import oss_agent_scrape_permits

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Operational defaults — apply to every Accela tenant unless the
# admin UI / CLI flags override them. Keeping them here (instead of
# in each per-city file) means a one-liner change tunes every city
# at once.
# ─────────────────────────────────────────────────────────────────────
DEFAULT_MAX_PERMITS   = 25
# Very wide default window — Accela rejects blank date fields, so we
# need *some* value, but in practice pagination (``max_pages=50`` in
# ``core/scrapers/accela.py``) + ``max_permits`` cap the run far
# before this window matters. Matches accela.py's default behaviour.
DEFAULT_LOOKBACK_DAYS = 3650
DEFAULT_MODEL         = 'openai-gpt-oss-20b'
DEFAULT_TIMEOUT_SEC   = 600
# 1500 tokens × 25 permits ≈ 37.5 k tokens; well below DO Inference
# per-call limits and leaves headroom for the cleaned HTML payload.
DEFAULT_MAX_CREDITS   = 50_000


@dataclass(frozen=True)
class CityConfig:
    """The four pieces of state that vary city-to-city.

    Attributes
    ----------
    scraper_id
        ``scrapers.id`` row this bot corresponds to. The orchestrator
        uses it to thread run-log entries through the admin UI.
    city, state
        Display fields only — every per-permit address is recovered
        from the grid row via ``usaddress``.
    url
        The Accela CapHome.aspx (or Default.aspx) search-form URL for
        this tenant. Found from the public Citizen Access portal.
    source
        Engine module key. Defaults to ``'accela'`` — every city in
        this helper is Accela-backed by definition.
    """
    scraper_id: int
    city:       str
    state:      str
    url:        str
    source:     str = 'accela'

    @property
    def display_name(self) -> str:
        return f'{self.city} {self.state} Accela Permits'

    def to_payload(self) -> dict:
        """Build the dict shape ``oss_agent_scrape_permits`` expects.

        Mirrors the columns ``core.db.list_scrapers`` returns so this
        bot can be invoked with or without a real ``scraper_run_id``.
        """
        return {
            'id':     self.scraper_id,
            'name':   self.display_name,
            'source': self.source,
            'url':    self.url,
            'city':   self.city,
            'state':  self.state,
        }


def make_run(cfg: CityConfig) -> Callable:
    """Return a ``run(...)`` callable bound to ``cfg``.

    The returned function has the same signature + envelope every other
    Accela path returns, so :mod:`core.scraper_accela` can call it
    unchanged.
    """
    def run(*,
            date_from:      str | None = None,
            date_to:        str | None = None,
            max_permits:    int | None = None,
            model:          str | None = None,
            scraper_run_id: int | None = None,
            timeout:        int = DEFAULT_TIMEOUT_SEC) -> dict:
        # Apply default date window if caller didn't pin one. Accela
        # tenants reject blank ``txtGSStartDate`` / ``txtGSEndDate``
        # fields with "Please enter a search criteria", so we cannot
        # literally leave them empty — instead we use a very wide
        # ~10-year window that lets the form post but doesn't filter
        # anything in practice. The real ceiling becomes pagination
        # (``ACCELA_MAX_PAGES_DEFAULT`` = 50) plus the per-CLI
        # ``max_permits`` cap. Matches the same change in
        # ``core/scrapers/accela.py`` so the cron + agent + per-city
        # paths all behave identically when no dates are supplied.
        if not date_from and not date_to:
            today = date.today()
            date_from = (today - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat()
            date_to   = today.isoformat()

        n = int(max_permits or DEFAULT_MAX_PERMITS)
        # Same accounting the engine uses: max_credits / 1500 tokens-per-permit.
        max_credits = max(DEFAULT_MAX_CREDITS, n * 1500)

        log.info('▶ %s %s scrape: %s → %s, max %d permits, model=%s',
                 cfg.city, cfg.state, date_from, date_to, n,
                 model or DEFAULT_MODEL)

        return oss_agent_scrape_permits(
            scraper        = cfg.to_payload(),
            date_from      = date_from,
            date_to        = date_to,
            model          = model or DEFAULT_MODEL,
            max_credits    = max_credits,
            scraper_run_id = scraper_run_id,
            timeout        = timeout,
        )
    run.__doc__ = (
        f'Scrape {cfg.city} {cfg.state} once and return the standard '
        f'Accela envelope. See `core.scrapers._accela_city.make_run` '
        f'for parameter docs.'
    )
    return run


def make_cli(cfg: CityConfig) -> Callable[[list[str] | None], int]:
    """Return a ``run_cli(argv=None)`` callable bound to ``cfg``.

    Exits non-zero on error so cron / shell pipelines can detect
    failures. Importable + invocable as
    ``python -m core.scrapers.<slug>``.
    """
    run = make_run(cfg)
    prog = f'python -m core.scrapers.{cfg.city.lower().replace(" ", "_")}_{cfg.state.lower()}'

    def run_cli(argv: list[str] | None = None) -> int:
        p = argparse.ArgumentParser(
            prog        = prog,
            description = (f'Standalone {cfg.city} {cfg.state} permit '
                           f'scraper ({cfg.source}, '
                           f'scraper_id={cfg.scraper_id}).'),
        )
        p.add_argument('--date-from', dest='date_from', default=None,
                       help='ISO YYYY-MM-DD lower bound (default: 7 days ago)')
        p.add_argument('--date-to', dest='date_to', default=None,
                       help='ISO YYYY-MM-DD upper bound (default: today)')
        p.add_argument('--max', dest='max_permits', type=int,
                       default=DEFAULT_MAX_PERMITS,
                       help=f'Max permits to extract (default: '
                            f'{DEFAULT_MAX_PERMITS})')
        p.add_argument('--model', default=DEFAULT_MODEL,
                       help=f'DO Inference model (default: {DEFAULT_MODEL})')
        p.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SEC,
                       help=f'Whole-run timeout seconds '
                            f'(default: {DEFAULT_TIMEOUT_SEC})')
        p.add_argument('--json', action='store_true',
                       help='Print the full envelope as JSON '
                            '(default: human summary)')
        args = p.parse_args(argv)

        # Bootstrap Django — the engine reaches into ``core.db`` for
        # proxy + DO Inference key lookups. Idempotent if already up.
        import os
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'permitdaily.settings')
        import django
        try:
            django.setup()
        except Exception:
            pass  # already set up by the caller

        logging.basicConfig(
            level  = logging.INFO,
            format = '%(asctime)s %(levelname)-5s %(name)s: %(message)s',
            stream = sys.stderr,
        )

        env = run(
            date_from   = args.date_from,
            date_to     = args.date_to,
            max_permits = args.max_permits,
            model       = args.model,
            timeout     = args.timeout,
        )

        if args.json:
            print(json.dumps(env, indent=2, default=str))
        else:
            permits = env.get('permits') or []
            rlog    = env.get('log') or {}
            print(f'\n=== {cfg.city} {cfg.state} — {len(permits)} permit(s) ===')
            print(f'  status         : {rlog.get("status")}')
            print(f'  credits used   : {rlog.get("credits_used")}')
            print(f'  latency        : {(rlog.get("latency_ms") or 0)/1000:.1f}s')
            if env.get('error'):
                print(f'  ⚠ error        : {env["error"]}')
            for i, prm in enumerate(permits, 1):
                pn = prm.get('permit_number') or '(no number)'
                pt = prm.get('permit_type') or ''
                ad = prm.get('address') or ''
                ci = prm.get('city') or ''
                st = prm.get('state') or ''
                zp = prm.get('zip') or ''
                ds = (prm.get('issued_date') or prm.get('applied_date') or '')
                print(f'  {i:2d}. {pn:14s} {ds:10s} {pt:25.25s} '
                      f'{ad}, {ci} {st} {zp}')

        return 0 if env.get('ok') and not env.get('error') else 1

    return run_cli
