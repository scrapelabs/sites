"""Server-local scraper cron scheduler.

This runs inside the Windows Permitlify service process. It replaces the
old requirement for GitHub Actions or another external HTTP pinger by
periodically sending an internal cron signal to the same gate/spawn logic
used by ``/api/v1/scrapers/run-cron/``.
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

_STARTED = False
_LOCK = threading.Lock()


def _env_int(name: str, default: int, *, lo: int, hi: int) -> int:
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(lo, min(value, hi))


def start_server_cron_scheduler() -> bool:
    """Start the scheduler thread once for this process.

    Returns True when this call started the thread, False when it was
    disabled or already running.
    """
    global _STARTED
    if (os.environ.get('PERMITLIFY_SERVER_CRON_DISABLED') or '').strip().lower() in (
        '1', 'true', 'yes', 'on'
    ):
        log.info('server cron scheduler disabled by env')
        return False

    with _LOCK:
        if _STARTED:
            return False
        _STARTED = True

    poll_seconds = _env_int('PERMITLIFY_SERVER_CRON_POLL_SECONDS', 60, lo=30, hi=3600)
    first_delay = _env_int('PERMITLIFY_SERVER_CRON_FIRST_DELAY_SECONDS', 20, lo=0, hi=600)
    thread = threading.Thread(
        target=_scheduler_loop,
        args=(poll_seconds, first_delay),
        name='permitlify-server-cron',
        daemon=True,
    )
    thread.start()
    log.info('server cron scheduler started poll_seconds=%s first_delay=%s',
             poll_seconds, first_delay)
    return True


def _scheduler_loop(poll_seconds: int, first_delay: int) -> None:
    if first_delay:
        time.sleep(first_delay)
    while True:
        try:
            # Lazy import avoids pulling the heavy views module during Django
            # app loading. The helper handles schedule gating, active-batch
            # locking, heartbeat stamping, and subprocess spawn.
            from .views import _cron_signal_fire

            result = _cron_signal_fire(source='server')
            if result.get('fired'):
                log.info('server cron fired batch_id=%s slot=%s',
                         result.get('batch_id'), result.get('slot') or '')
            elif not result.get('ok', True):
                log.error('server cron signal failed: %s', result)
        except Exception:
            log.exception('server cron scheduler tick failed')
        time.sleep(poll_seconds)
