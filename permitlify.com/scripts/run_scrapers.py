#!/usr/bin/env python3
"""Daily cron entrypoint for the Accela scraper system.

Runs every enabled scraper once in series and exits when each thread
finishes. Wire this up as a Replit Scheduled Deployment to ingest
permits on a daily cadence:

    python3 scripts/run_scrapers.py

The ``scrapers_cron_enabled`` system_settings flag lets an admin pause
the schedule without un-scheduling the deployment.
"""

import os
import sys
import time
import logging

# Make the Django project importable when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'permitdaily.settings')
# When the cron is run locally (no DJANGO_SECRET_KEY in env) settings.py
# will refuse to import unless DEBUG is on. The production scheduled
# deployment will already have DJANGO_SECRET_KEY set, so this fallback
# is a no-op in production.
if not os.environ.get('DJANGO_SECRET_KEY'):
    os.environ.setdefault('DJANGO_DEBUG', '1')

import django  # noqa: E402

django.setup()

from core.db import (  # noqa: E402
    list_enabled_scrapers_all,
    get_scraper_run,
    get_system_setting,
    set_system_setting,
)
from core.scraper_accela import run_scraper_async  # noqa: E402


def _stamp_heartbeat(outcome: str, *, fired: bool = False) -> None:
    """Record that the cron entrypoint was invoked.

    The admin Cron page polls these keys to confirm the *external*
    trigger (DigitalOcean cron / systemd timer / GitHub Action) is
    actually hitting this script — without this signal the UI has no
    way to distinguish a paused schedule from a missing trigger.
    Writes are best-effort; we never let an instrumentation failure
    abort an actual cron pass.
    """
    from datetime import datetime as _dt
    now_iso = _dt.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    try:
        set_system_setting('scrapers_cron_last_heartbeat_at', now_iso)
        set_system_setting('scrapers_cron_last_heartbeat_outcome', outcome)
        if fired:
            set_system_setting('scrapers_cron_last_fired_at', now_iso)
    except Exception:
        log.exception('heartbeat write failed (non-fatal)')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [scrapers-cron] %(message)s',
)
log = logging.getLogger('cron')


def _wait_for_run(run_id: int, *, max_wait_seconds: int = 1800) -> dict | None:
    """Block until the given run row reports ``finished_at``.

    Caps the wait at ``max_wait_seconds`` so a stuck Firecrawl request
    cannot pin the cron job indefinitely — we let the daemon thread
    keep going and move on to the next scraper.
    """
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        row = get_scraper_run(run_id)
        if row and row.get('finished_at'):
            return row
        time.sleep(2.0)
    return get_scraper_run(run_id)


_DAY_INDEX = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def _gate_by_schedule() -> str | None:
    """Apply the configurable schedule from the admin Cron page.
    Returns ``None`` if the run should proceed, or a reason string if
    it should be skipped. Honours:
      * ``scrapers_cron_enabled``  – master kill switch.
      * ``scrapers_cron_days``     – csv of {mon..sun}; empty = all days.
      * ``scrapers_cron_at_utc``   – HH:MM UTC.
      * ``scrapers_cron_window_minutes`` – ± minutes around at_utc.
    """
    if not get_system_setting('scrapers_cron_enabled'):
        return 'scrapers_cron_enabled is false'

    from datetime import datetime as _dt
    now = _dt.utcnow()

    raw_days = (get_system_setting('scrapers_cron_days') or '').strip()
    days = {d for d in raw_days.split(',') if d in _DAY_INDEX}
    if days:
        today = _DAY_INDEX[now.weekday()]
        if today not in days:
            return (f'today ({today}) is not in scrapers_cron_days={sorted(days)}')

    at_utc = (get_system_setting('scrapers_cron_at_utc') or '').strip()
    if at_utc:
        try:
            hh, mm = at_utc.split(':')
            target_minutes = int(hh) * 60 + int(mm)
            now_minutes = now.hour * 60 + now.minute
            try:
                window = int(get_system_setting('scrapers_cron_window_minutes') or 30)
            except (TypeError, ValueError):
                window = 30
            window = max(1, min(window, 720))
            # Circular distance on the 1440-minute clock so a window that
            # straddles midnight (e.g. target 23:55 ± 30m matching 00:10)
            # works correctly.
            raw_delta = abs(now_minutes - target_minutes)
            delta = min(raw_delta, 1440 - raw_delta)
            if delta > window:
                return (f'now {now.strftime("%H:%M")} UTC is outside '
                        f'{at_utc} ± {window}m window')
        except Exception:
            # Bad config — ignore the gate rather than crash the cron.
            log.warning('invalid scrapers_cron_at_utc=%r — ignoring', at_utc)
    return None


def main() -> int:
    skip_reason = _gate_by_schedule()
    if skip_reason:
        log.info('skipping cron pass: %s', skip_reason)
        _stamp_heartbeat(f'skipped: {skip_reason}')
        return 0

    enabled = list_enabled_scrapers_all()
    if not enabled:
        log.info('no enabled scrapers, exiting')
        _stamp_heartbeat('skipped: no enabled scrapers')
        return 0

    _stamp_heartbeat('fired', fired=True)

    try:
        per_count = int(get_system_setting('scrapers_cron_count') or 20)
    except (TypeError, ValueError):
        per_count = 20
    per_count = max(1, min(per_count, 500))

    log.info('starting cron pass for %d enabled scraper(s) — count=%d',
             len(enabled), per_count)

    overall_ok = 0
    overall_fail = 0
    for s in enabled:
        sid = s['id']
        log.info('▶ scraper #%d "%s"', sid, s.get('name'))
        try:
            run_id = run_scraper_async(sid, mode='cron', kind='cron', count=per_count)
        except Exception:
            log.exception('failed to kick off scraper #%d', sid)
            overall_fail += 1
            continue

        finished = _wait_for_run(run_id, max_wait_seconds=1800)
        if not finished:
            log.warning('scraper #%d run %d did not complete in time', sid, run_id)
            overall_fail += 1
            continue
        status = finished.get('status') or 'unknown'
        log.info('   ↳ run %d finished — status=%s, ok=%d, fail=%d',
                 run_id, status,
                 int(finished.get('succeeded') or 0),
                 int(finished.get('failed') or 0))
        if status == 'success':
            overall_ok += 1
        else:
            overall_fail += 1

    log.info('done — %d succeeded, %d failed', overall_ok, overall_fail)
    return 0 if overall_fail == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
