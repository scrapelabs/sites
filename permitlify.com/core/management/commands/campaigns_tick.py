"""Dispatch any due rows for active email campaigns. Designed to be
called from cron every ~15 minutes (or as often as you like — sends are
capped by ``email_campaigns.daily_cap`` per campaign per 24h window).

Pacing rule of thumb: setting daily_cap=200 and running this command
every 15 minutes spreads ~13 sends per tick across the day, which is
gentle on Gmail/Outlook reputation for a warming-up domain.
"""
from __future__ import annotations

import logging
import time
from django.core.management.base import BaseCommand

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Dispatch due rows for active email campaigns."

    def add_arguments(self, parser):
        parser.add_argument('--per-tick', type=int, default=20,
                            help='Hard cap per campaign per tick (default 20). '
                                 'Combine with cron frequency to pace daily volume.')
        parser.add_argument('--campaign', type=int, default=0,
                            help='If set, run only this campaign id.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from core import db
        from core.views import _campaign_tick_once

        per_tick = max(1, int(opts.get('per_tick') or 20))
        only     = int(opts.get('campaign') or 0)
        dry      = bool(opts.get('dry_run'))

        t0 = time.time()
        camps = db.campaigns_list(limit=200)
        active = [c for c in camps if c.get('status') == 'sending'
                  and (not only or c['id'] == only)]
        if not active:
            self.stdout.write("campaigns_tick: no active campaigns.")
            return

        totals = {'sent': 0, 'skipped': 0, 'failed': 0}
        for c in active:
            res = _campaign_tick_once(c['id'], per_tick=per_tick, dry=dry)
            for k in totals:
                totals[k] += res.get(k, 0)
            self.stdout.write(
                f"  campaign #{c['id']} {c['name']!r}: "
                f"{res.get('sent',0)} sent · {res.get('skipped',0)} skipped · "
                f"{res.get('failed',0)} failed (remaining quota: {res.get('quota_left','?')})"
            )

        self.stdout.write(
            f"campaigns_tick: {totals['sent']} sent · "
            f"{totals['skipped']} skipped · {totals['failed']} failed · "
            f"in {time.time()-t0:.2f}s ({'DRY' if dry else 'LIVE'})"
        )
