"""Run a Run-All / cron scraper batch in a standalone subprocess.

Spawned by ``admin_run_all_scrapers`` / ``admin_run_selected_scrapers`` /
``admin_run_cron_now`` / the HTTP cron trigger via ``subprocess.Popen``
so the batch coordinator survives Django dev-server auto-reloads,
workflow restarts and DO App Platform deploys — none of which can
reach this PID. Cooperative stop is via ``cron_batches.status =
'stopping'`` which the worker polls between scrapers.

Mirrors ``run_finder_batch`` so the two batch systems have one
shape — same PID-stamp contract, same subprocess.Popen handoff.
"""

import os
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run a scrapers batch coordinator in a standalone process.'

    def add_arguments(self, parser):
        parser.add_argument('batch_id', type=int)
        parser.add_argument(
            '--kind', type=str, default='run-all',
            choices=('run-all', 'cron'),
            help='run-all = concurrent Run All / Run Selected; '
                 'cron = serial cron-pass coordinator.',
        )
        parser.add_argument('--concurrency', type=int, default=5)
        parser.add_argument(
            '--scraper-ids', type=str, default='',
            help='Comma-separated scraper ids for "Run selected"; '
                 'empty = every enabled scraper.',
        )
        parser.add_argument(
            '--max-pages', type=int, default=None,
            help='Optional override for the per-scraper page cap.',
        )

    def handle(self, *args, **options):
        batch_id = options['batch_id']
        kind = options.get('kind') or 'run-all'

        # Stamp our PID into the batch row BEFORE doing any work so
        # `admin_active_batch`'s os.kill(pid, 0) liveness check can
        # see us the moment the parent finishes spawning.
        from core.db import update_cron_batch
        try:
            update_cron_batch(batch_id, coordinator_pid=os.getpid())
        except Exception:
            logger.exception('run_scrapers_batch: failed to stamp '
                             'coordinator_pid for batch=%s', batch_id)

        if kind == 'cron':
            from core.views import _run_cron_batch_worker
            _run_cron_batch_worker(batch_id)
            return

        # ── Run-All / Run-Selected path ──────────────────────────────
        raw_ids = (options.get('scraper_ids') or '').strip()
        scraper_ids = None
        if raw_ids:
            scraper_ids = [int(t) for t in raw_ids.split(',') if t.strip().isdigit()]
            if not scraper_ids:
                scraper_ids = None

        concurrency = max(1, min(int(options.get('concurrency') or 5), 10))
        max_pages = options.get('max_pages')

        from core.views import _run_all_batch_worker
        _run_all_batch_worker(
            batch_id,
            concurrency=concurrency,
            scraper_ids=scraper_ids,
            max_pages=max_pages,
        )
