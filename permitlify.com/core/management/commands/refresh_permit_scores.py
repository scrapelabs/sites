"""Recompute the materialized permit score (``permits.score_cache``).

The customer-facing /permits/ table sorts "best leads first" on the
12-factor derived score (``core.permit_score.derive_score``), which
Postgres can't compute. To keep page loads instant the score is
materialized into ``permits.score_cache`` and the table sorts on that
column. Several of the scoring factors (freshness, expiry, seasonal)
move with the calendar, so the column must be recomputed once a day.

The read path already self-heals via a debounced background thread, but
this command gives operators a deterministic hook for cron / a scheduled
job:

    python3 manage.py refresh_permit_scores            # stale rows (default)
    python3 manage.py refresh_permit_scores --all      # full re-score
    python3 manage.py refresh_permit_scores --only-null # just new rows
"""

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Recompute permits.score_cache from the 12-factor derived score.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--all', action='store_true',
            help='Re-score every permit, not just rows that are stale today.',
        )
        parser.add_argument(
            '--only-null', action='store_true',
            help='Score only rows that have never been scored.',
        )
        parser.add_argument(
            '--batch', type=int, default=4000,
            help='Rows per UPDATE batch (default 4000).',
        )

    def handle(self, *args, **options):
        from core.db import refresh_permit_scores

        only_null = bool(options.get('only_null'))
        do_all = bool(options.get('all'))
        batch = max(1, int(options.get('batch') or 4000))

        if only_null:
            mode = 'only-null'
            n = refresh_permit_scores(only_null=True, batch=batch)
        elif do_all:
            mode = 'all'
            n = refresh_permit_scores(only_stale=False, batch=batch)
        else:
            mode = 'stale'
            n = refresh_permit_scores(only_stale=True, batch=batch)

        self.stdout.write(self.style.SUCCESS(
            f'refresh_permit_scores ({mode}): updated {n} row(s).'))
