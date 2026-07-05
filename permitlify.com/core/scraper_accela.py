"""Accela permit scraper — Firecrawl + Claude AI.

A "scraper" in this codebase is a saved Accela CapDetail URL plus
metadata. Each run fetches one or more URLs through Firecrawl (which
handles the .NET WebForms JS rendering), feeds the resulting markdown
to Claude with a strict extraction prompt, and pushes the structured
permit dict into the existing ``permits`` table via ``upsert_permit``.

Background runs are tracked in ``scraper_runs`` so the admin UI can
poll progress and render a progress bar without any external job
queue (we use the same fire-and-forget daemon-thread pattern the
email + auth-code subsystems use).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date

from . import db, pg
from .db import (
    add_supported_city,
    append_scraper_run_step,
    create_scraper_run,
    heartbeat_scraper_run,
    is_cancel_requested,
    get_scraper,
    get_scraper_run,
    get_supported_cities,
    get_system_setting,
    refresh_scraper_total_permits,
    update_scraper,
    update_scraper_run,
    upsert_permit,
)

log = logging.getLogger(__name__)

# ── Per-thread "current scraper run" tracker ─────────────────────────
# So claude_extract() / oss_complete() can stamp every API call
# with the run id without changing their public signature (and without
# threading run_id through every helper). The cron worker enters this
# context for the whole per-target unit of work.

_call_ctx = threading.local()

# In-process map of run_id -> live worker Thread. Populated by the
# daemon-thread entry-point (`_run_worker`) and consulted by the admin
# Force-stop endpoint so it can inject SystemExit into a wedged worker
# when cooperative cancel isn't enough. Only entries running on THIS
# gunicorn worker show up here — multi-worker setups fall back to the
# DB-stamped pid/tid printed for manual `kill -TERM <pid>` use.
_RUNNING_THREADS: dict[int, "threading.Thread"] = {}
_RUNNING_THREADS_LOCK = threading.Lock()


HEARTBEAT_INTERVAL_SECONDS = 15
HEARTBEAT_STALE_SECONDS = 60


def is_run_orphaned(run_id: int) -> bool:
    """A run is *orphaned* when the row says ``status='running'`` but
    no live worker exists to advance it. Three ways this happens:

      1. Server restart (gunicorn rolling-deploy, runserver autoreload,
         workflow restart). The original Python process is gone.
      2. The worker thread crashed without finalising.
      3. Container reboot where the new server process gets the same
         OS pid as the dead worker (pid 1, 7, 23 etc. get reused
         under Docker / DO App Platform), so a pid-equality check
         falsely says "still alive".

    Detection (in order of strength):
      • If the worker thread is in this process's ``_RUNNING_THREADS``
        map AND alive → NOT orphaned.
      • If ``heartbeat_at`` exists and is older than
        ``HEARTBEAT_STALE_SECONDS`` → orphaned. The heartbeat is the
        authoritative liveness signal — a real worker writes it every
        ~15s; a dead one can't.
      • If ``heartbeat_at`` is unset or fresh, fall back to pid
        equality: pid missing or != ours → orphaned.

    Used by the Stop / Force-stop endpoints + page renders to short-
    circuit straight to row finalisation when there's no worker left
    to honour the cooperative cancel flag.
    """
    import os as _os
    rid = int(run_id)
    with _RUNNING_THREADS_LOCK:
        t = _RUNNING_THREADS.get(rid)
    thread_alive_here = t is not None and t.is_alive()
    if thread_alive_here:
        return False
    try:
        row = get_scraper_run(rid)
    except Exception:
        return False
    if not row:
        return False
    if (row.get('status') or '').lower() != 'running':
        return False
    our_pid = _os.getpid()
    pid = row.get('worker_pid')
    # ── Strongest signal: worker_pid is OURS but we have no live
    # thread for this run in this process. That's only possible if
    # the worker thread died after writing the row (crashed mid-run,
    # SystemExit injection without finally completing the pop, etc.).
    # Even a "fresh" heartbeat is a ghost in this case — the
    # heartbeat thread is a *sibling* of the worker that just died,
    # so it almost certainly died too; if it didn't, it would be
    # bumping for a worker that no longer exists. Either way: orphan.
    # Without this short-circuit, the run shows "Running" for up to
    # HEARTBEAT_STALE_SECONDS (60s) AFTER the worker is provably
    # gone — which is exactly the bug admins kept reporting.
    if pid is not None and int(pid) == our_pid:
        return True
    # ── Heartbeat-based check: most reliable for the cross-process
    # case (different gunicorn worker, or any other process), and
    # immune to pid reuse across container restarts.
    hb = row.get('heartbeat_at')
    if hb is not None:
        try:
            from datetime import datetime as _dt, timezone as _tz
            now = _dt.now(_tz.utc)
            hb_dt = hb if hb.tzinfo else hb.replace(tzinfo=_tz.utc)
            age = (now - hb_dt).total_seconds()
            if age > HEARTBEAT_STALE_SECONDS:
                return True
            # Heartbeat is fresh — a live worker in another process
            # is updating it (we already ruled out our own pid above).
            return False
        except Exception:
            pass
    # No heartbeat ever recorded (legacy run started before this
    # column existed, or worker died before its first beat). Fall
    # back to pid equality.
    return pid is None or int(pid) != our_pid


def _heartbeat_loop(run_id: int, stop_event: "threading.Event") -> None:
    """Daemon-thread loop that bumps `heartbeat_at` every
    ``HEARTBEAT_INTERVAL_SECONDS`` until the worker signals stop.
    Runs alongside the scraper worker so the row's liveness is
    visible from any other process / page render."""
    rid = int(run_id)
    # Initial beat so a stop in the first 15s still leaves a fresh
    # timestamp the orphan check can see.
    heartbeat_scraper_run(rid)
    while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            heartbeat_scraper_run(rid)
        except Exception:
            log.exception('heartbeat_loop: write failed for run %s', rid)


def finalize_orphan_run(run_id: int, *, reason: str = 'orphaned') -> bool:
    """Mark a wedged ``status='running'`` row as ``cancelled`` when
    we know no worker will ever advance it (pid mismatch / restart).
    Also resets the parent scraper's ``last_run_status`` so the list
    page doesn't show a stuck 'running' pill.
    Idempotent — returns True if it actually flipped the row, False
    if there was nothing to do."""
    if not is_run_orphaned(run_id):
        return False
    try:
        row = get_scraper_run(int(run_id))
        update_scraper_run(
            int(run_id),
            status='cancelled',
            finished_at=datetime.utcnow(),
            current_step=f'cancelled — {reason}',
        )
        if row and row.get('scraper_id'):
            try:
                update_scraper(int(row['scraper_id']),
                               last_run_status='cancelled')
            except Exception:
                log.exception('finalize_orphan_run: scraper status '
                              'update failed for run %s', run_id)
        return True
    except Exception:
        log.exception('finalize_orphan_run failed for run_id=%s', run_id)
        return False


def reap_orphan_runs_live() -> int:
    """Liveness-aware reap. Walks every ``status='running'`` row and
    asks :func:`is_run_orphaned` whether a real worker is still
    advancing it. Each orphan is finalised via :func:`finalize_orphan_run`
    (status='cancelled', scraper's ``last_run_status`` updated, so the
    admin table's status pill + Run/Stop button state flip honestly).

    Difference vs :func:`sweep_orphan_runs`:
      • ``sweep_orphan_runs`` runs once at startup and uses pid mismatch
        as its primary signal.
      • This one runs on every admin-scrapers page load + AJAX refresh,
        and uses the heartbeat-based liveness check from
        :func:`is_run_orphaned` — so a worker thread that crashed
        silently within *this* process (still our pid, but heartbeat
        went stale > 60s ago, or the thread is no longer in
        ``_RUNNING_THREADS``) is also caught.

    Difference vs :func:`reap_stale_scraper_runs` (which runs on the
    classic 30-minute timer):
      • This one closes the window down to roughly the heartbeat-stale
        threshold (~60s of silence), so a dead scraper's button doesn't
        show "Running" for half an hour before the row finally flips.

    Returns the count of rows it flipped (for the caller's log line).
    Best-effort and idempotent — exceptions per row are swallowed so
    one bad row can't poison a whole sweep.
    """
    try:
        from .db import _ensure_scrapers_table
        _ensure_scrapers_table()
        with pg.conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT id FROM scraper_runs "
                "WHERE status = 'running' AND finished_at IS NULL"
            )
            ids = [int(r['id']) for r in cur.fetchall()]
    except Exception:
        log.exception('reap_orphan_runs_live: query failed')
        return 0
    flipped = 0
    for rid in ids:
        try:
            if is_run_orphaned(rid):
                if finalize_orphan_run(rid, reason='no live worker thread'):
                    flipped += 1
        except Exception:
            log.exception('reap_orphan_runs_live: per-row sweep failed for %s', rid)
    if flipped:
        log.info('reap_orphan_runs_live: finalised %d orphaned run row(s)', flipped)
    return flipped


def sweep_orphan_runs() -> int:
    """Startup helper: finalise every ``status='running'`` row whose
    ``worker_pid`` is not the current process. Called from the Django
    ``AppConfig.ready()`` hook so a workflow restart can never leave
    the admin staring at a permanently 'running' row.
    Returns the number of rows it flipped (for the startup log)."""
    import os as _os
    cur_pid = _os.getpid()
    try:
        from .db import _ensure_scrapers_table
        _ensure_scrapers_table()
        with pg.conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT id, worker_pid, scraper_id FROM scraper_runs "
                "WHERE status = 'running' AND finished_at IS NULL"
            )
            rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        log.exception('sweep_orphan_runs: query failed')
        return 0
    flipped = 0
    for r in rows:
        pid = (r or {}).get('worker_pid')
        # Belt-and-braces: even if the new process happens to be
        # assigned the same pid as the dead worker (pid reuse on
        # container restarts), an in-process worker thread for this
        # run cannot exist yet — startup runs before any new worker
        # is spawned. So same-pid + status='running' from the prior
        # boot is still orphan. Skip ONLY if our live worker map
        # actually holds the thread (impossible at startup, but
        # cheap correctness check for any future caller).
        with _RUNNING_THREADS_LOCK:
            if int(r['id']) in _RUNNING_THREADS:
                continue
        # If pid matches AND our live map is empty, we still treat it
        # as orphan rather than skipping — the old logic skipped here
        # and stranded rows on pid reuse.
        _ = pid; _ = cur_pid  # vars retained for log message below
        try:
            update_scraper_run(
                int(r['id']),
                status='cancelled',
                finished_at=datetime.utcnow(),
                current_step='cancelled — orphaned by server restart',
            )
            sid = r.get('scraper_id')
            if sid:
                try:
                    update_scraper(int(sid), last_run_status='cancelled')
                except Exception:
                    log.exception('sweep_orphan_runs: scraper status '
                                  'update failed for run %s', r.get('id'))
            flipped += 1
        except Exception:
            log.exception('sweep_orphan_runs: finalize failed for %s',
                          r.get('id'))
    if flipped:
        log.info('sweep_orphan_runs: finalised %d orphaned run row(s)',
                 flipped)
    return flipped


def force_kill_run_thread(run_id: int) -> dict:
    """Best-effort hard-stop. Injects ``SystemExit`` into the worker
    thread via the CPython private ``PyThreadState_SetAsyncExc`` API.

    Returns ``{'ok': bool, 'reason': str, 'pid': int|None, 'tid': int|None}``
    so the endpoint can give an honest reply (injected vs. wrong worker
    process vs. thread already gone vs. orphaned-and-finalised).

    Caveats:
      • Only works for threads in OUR process (in `_RUNNING_THREADS`).
        On DO App Platform we run a single gunicorn worker per dyno,
        so in practice it's fine.
      • Async-exc only fires when the target thread next executes a
        Python bytecode — a thread blocked in a C-level syscall
        (e.g. socket.recv) won't die until the call returns. The
        cooperative cancel flag remains the primary mechanism.
      • If the row is orphaned (worker_pid != our pid OR unset), we
        skip injection and report ``reason='orphan_finalised'`` so the
        caller can present an honest "yes, the run is now cancelled"
        toast instead of a confusing "skipped" warning.
    """
    import ctypes
    rid = int(run_id)
    with _RUNNING_THREADS_LOCK:
        t = _RUNNING_THREADS.get(rid)
    row = None
    try:
        row = get_scraper_run(rid)
    except Exception:
        row = None
    pid = (row or {}).get('worker_pid')
    tid = (row or {}).get('worker_tid')
    if t is None or not t.is_alive():
        # Nothing to inject into. If this is an orphan (pid mismatch),
        # finalise the row so the UI un-wedges instead of polling
        # forever.
        if finalize_orphan_run(rid, reason='orphaned worker (no live thread)'):
            return {'ok': True, 'reason': 'orphan_finalised',
                    'pid': pid, 'tid': tid}
        return {'ok': False, 'reason': 'thread_not_in_this_process',
                'pid': pid, 'tid': tid}
    target_tid = t.ident
    if not target_tid:
        return {'ok': False, 'reason': 'thread_has_no_ident',
                'pid': pid, 'tid': tid}
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(target_tid),
        ctypes.py_object(SystemExit),
    )
    if res == 0:
        return {'ok': False, 'reason': 'thread_id_invalid',
                'pid': pid, 'tid': tid}
    if res > 1:
        # CPython contract: if more than one thread was affected, undo.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(target_tid), None)
        return {'ok': False, 'reason': 'multiple_threads_affected',
                'pid': pid, 'tid': tid}
    return {'ok': True, 'reason': 'systemexit_injected',
            'pid': pid, 'tid': tid}


def kill_run_process(run_id: int) -> dict:
    """Nuclear option: send ``SIGTERM`` to the worker process hosting
    the scraper run. On Replit (``manage.py runserver``) this kills the
    Django dev server — the workflow manager auto-restarts it. On DO
    App Platform the gunicorn master replaces the dead worker.

    Sequence:
      1. Finalise the DB row as ``cancelled`` immediately — the dying
         process won't get a chance to write ``finished_at`` itself.
      2. Send ``SIGTERM`` to the target PID after a 1-second delay
         (via a daemon thread) so the HTTP response reaches the
         browser before the server dies.

    Returns ``{'ok': bool, 'reason': str, 'pid': int|None}``.
    """
    import os as _os, signal as _sig
    rid = int(run_id)
    try:
        row = get_scraper_run(rid)
    except Exception:
        return {'ok': False, 'reason': 'run_not_found', 'pid': None}
    if not row:
        return {'ok': False, 'reason': 'run_not_found', 'pid': None}
    st = (row.get('status') or '').lower()
    if st not in ('running', 'queued'):
        return {'ok': False, 'reason': f'already_{st}', 'pid': None}
    pid = row.get('worker_pid')
    if not pid:
        finalize_orphan_run(rid, reason='kill requested (no pid on record)')
        return {'ok': True, 'reason': 'no_pid_finalised', 'pid': None}
    pid = int(pid)
    try:
        update_scraper_run(
            rid,
            status='cancelled',
            finished_at=datetime.utcnow(),
            current_step='killed by admin (SIGTERM)',
        )
    except Exception:
        log.exception('kill_run_process: row finalise failed')
    with _RUNNING_THREADS_LOCK:
        _RUNNING_THREADS.pop(rid, None)
    def _deferred():
        import time as _t
        _t.sleep(1)
        try:
            _os.kill(pid, _sig.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            log.exception('kill_run_process: os.kill failed for pid=%s', pid)
    t = threading.Thread(target=_deferred, daemon=True)
    t.start()
    return {'ok': True, 'reason': 'sigterm_scheduled', 'pid': pid}


class _track_run:
    """Context manager: tags every Firecrawl/Claude call made on the
    current thread with the given scraper_run_id (and optional source
    label for the usage tables)."""

    def __init__(self, run_id, source: str = 'accela'):
        self.run_id = int(run_id) if run_id else None
        self.source = source
        self._prev = None

    def __enter__(self):
        self._prev = (
            getattr(_call_ctx, 'run_id', None),
            getattr(_call_ctx, 'source', None),
        )
        _call_ctx.run_id = self.run_id
        _call_ctx.source = self.source
        return self

    def __exit__(self, exc_type, exc, tb):
        prev_run, prev_src = self._prev
        _call_ctx.run_id = prev_run
        _call_ctx.source = prev_src
        return False


def _current_run_id():
    return getattr(_call_ctx, 'run_id', None)


def _current_source(default: str = 'accela'):
    return getattr(_call_ctx, 'source', None) or default


ANTHROPIC_URL    = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
DEFAULT_MODEL    = 'claude-3-5-sonnet-latest'


class RunCancelled(Exception):
    """Raised by worker hooks when the admin presses Stop. The outer
    branch catches it and finalises the run row with status='cancelled'
    so the UI shows an honest outcome instead of 'failed'."""


class ScraperError(Exception):
    """Raised by scraper helpers; carries a user-readable message."""


# Human-readable explanation for each save-time skip code that
# ``core.db.upsert_permit`` stamps onto the permit dict (``_skip_reason``)
# right before it returns ``None``. Lets the scraper report the REAL reason
# a row wasn't saved instead of the old catch-all "required identity fields
# not satisfied" message (which was wrong for contact/person-gate drops).
_UPSERT_SKIP_REASONS = {
    'missing_identity': 'missing required identity fields '
                        '(source / permit id / state / city)',
    'no_contact':       'no contractor email or phone — not an actionable lead',
    'person_no_name':   'contractor name looks like a private individual '
                        'missing a first or last name (homeowner, not a business)',
}


def _upsert_skip_text(permit: dict) -> str:
    """Map the ``_skip_reason`` an upsert stamped on ``permit`` to a
    user-readable phrase. Falls back to the catch-all if the upsert
    returned ``None`` without stamping a reason."""
    code = (permit or {}).get('_skip_reason') or ''
    return _UPSERT_SKIP_REASONS.get(
        code, 'skipped by a save-time filter (reason unavailable)')


# ─────────────────────────── URL helpers ──────────────────────────────

def parse_accela_url(url: str) -> dict:
    """Extract Accela CapDetail params (agencyCode, Module, capID1/2/3)
    from a URL. Returns an empty dict on a non-Accela URL."""
    try:
        parsed = urllib.parse.urlparse((url or '').strip())
    except Exception:
        return {}
    # Strict host allowlist — `endswith('accela.com')` would falsely
    # accept attacker-controlled hosts like `evilaccela.com`. Use the
    # parsed hostname (lower-cased, no port) and require it to BE
    # `accela.com` or a true subdomain (`*.accela.com`).
    host = (parsed.hostname or '').lower()
    if host != 'accela.com' and not host.endswith('.accela.com'):
        return {}
    qs = urllib.parse.parse_qs(parsed.query)
    out = {
        'agency_code': (qs.get('agencyCode') or [''])[0].upper(),
        'module':      (qs.get('Module')     or [''])[0],
        'cap_id_1':    (qs.get('capID1')     or [''])[0],
        'cap_id_2':    (qs.get('capID2')     or [''])[0],
        'cap_id_3':    (qs.get('capID3')     or [''])[0],
        'tab_name':    (qs.get('TabName')    or [''])[0],
    }
    return {k: v for k, v in out.items() if v} | {
        'agency_code': out['agency_code'],
        'module':      out['module'],
        'cap_id_1':    out['cap_id_1'],
        'cap_id_2':    out['cap_id_2'],
        'cap_id_3':    out['cap_id_3'],
    }


def build_accela_url(template_url: str, *, cap_id_3: int | str) -> str:
    """Rebuild an Accela CapDetail URL with a different ``capID3``."""
    parsed = urllib.parse.urlparse(template_url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    qs['capID3'] = [str(cap_id_3).zfill(5)]
    flat = [(k, v[0]) for k, v in qs.items()]
    new_q = urllib.parse.urlencode(flat, doseq=False)
    return urllib.parse.urlunparse(parsed._replace(query=new_q))


# ─────────────────────────── Firecrawl ────────────────────────────────

# JSON schema we send to Firecrawl's `jsonOptions`. The schema both
# (1) tells Firecrawl's LLM the exact shape we expect back, and
# (2) lets us pass the same shape into Claude as a fallback. Keep this
# in sync with `_normalise_permit` field names.
PERMIT_JSON_SCHEMA: dict = {
    'type': 'object',
    'properties': {
        'permit_number':    {'type': 'string',  'description': 'Visible record / CAP number for this permit.'},
        'detail_url':       {'type': ['string', 'null'], 'description': 'Absolute URL to the CapDetail.aspx page for this permit (the link wrapping the permit number in the list table). Null if the row has no link.'},
        'permit_type':      {'type': 'string',  'description': "Short category, e.g. 'Roofing', 'HVAC Replacement'."},
        'description':      {'type': 'string',  'description': 'Short ~2-line summary of the permit in plain words; do NOT repeat permit_type/status/address/dates/parties/valuation/trade.'},
        'status':           {'type': 'string',  'description': "e.g. 'Issued', 'Approved', 'Pending'."},
        'applied_date':     {'type': ['string', 'null'], 'description': 'YYYY-MM-DD or null.'},
        'issued_date':      {'type': ['string', 'null'], 'description': 'YYYY-MM-DD or null.'},
        'expires_date':     {'type': ['string', 'null'], 'description': 'YYYY-MM-DD or null.'},
        'address':          {'type': 'string',  'description': 'Full site address (street + number).'},
        'city':             {'type': 'string'},
        'state':            {'type': 'string',  'description': '2-letter US state code.'},
        'zip':              {'type': 'string',  'description': '5-digit ZIP.'},
        'latitude':         {'type': ['number', 'null']},
        'longitude':        {'type': ['number', 'null']},
        'owner_name':       {'type': 'string'},
        'contractor_name':  {'type': 'string'},
        'contractor_phone': {'type': 'string',  'description': 'Phone formatted (NNN) NNN-NNNN.'},
        'contractor_email': {'type': 'string',  'description': 'Lowercase, valid email.'},
        'valuation_cents':  {'type': ['integer', 'null'], 'description': 'Project value in cents (e.g. $45,000 → 4500000).'},
        'square_feet':      {'type': ['integer', 'null']},
        'trade':            {'type': 'string',  'description': 'One of: roofing, hvac, plumbing, electrical, solar, general, civil, other.'},
        'ai_score':         {'type': ['integer', 'null'], 'description': 'Composite lead-quality score 0-100, or null when the page had too little data to score.'},
        'ai_grade':         {'type': ['string', 'null'],  'description': 'Letter grade A through F, or null if ai_score is null.'},
        'ai_tier':          {'type': ['string', 'null'],  'description': 'One of: hot, warm, cool — or null.'},
        # Per-dimension breakdown. Each subscore is 0-100 OR null when
        # the underlying data was absent on the source page (golden rule
        # in the prompt — never punish missing data). The composite
        # ai_score is computed with weight renormalisation across the
        # non-null subscores.
        'ai_subscores': {
            'type': ['object', 'null'],
            'properties': {
                'lead_quality':         {'type': ['integer', 'null']},
                'urgency':              {'type': ['integer', 'null']},
                'project_value':        {'type': ['integer', 'null']},
                'contact_completeness': {'type': ['integer', 'null']},
                'intent_signal':        {'type': ['integer', 'null']},
                'trade_fit':            {'type': ['integer', 'null']},
                'geographic':           {'type': ['integer', 'null']},
                'status_actionability': {'type': ['integer', 'null']},
                'data_confidence':      {'type': ['integer', 'null']},
            },
        },
        # ai_subscore_reasons was removed from the schema (and the
        # prompt) in the token-cost trim — nothing in templates or
        # views read it, so spending ~200 output tokens per call to
        # generate 9 short sentences was pure waste. The cleaner
        # `_clean_subscore_reasons()` is kept defensively: if a
        # future prompt re-introduces it, the storage path still
        # works. Today it just returns None for every call.
    },
    'required': ['permit_number', 'permit_type', 'address'],
}


def get_extraction_prompt() -> str:
    """Active DETAIL-page extraction prompt — admin override
    (system_setting 'extraction_prompt') or the built-in default.
    Used for BOTH Firecrawl's `jsonOptions.prompt` AND Claude's
    system message when scraping a single CapDetail page."""
    override = (get_system_setting('extraction_prompt') or '').strip()
    return override or EXTRACT_SYSTEM_PROMPT


def _slice_scoring_section(text: str) -> str:
    """Return the per-dimension SCORING rubric from a prompt, dropping the
    server-side COMPOSITE/tier/grade tail. Marker search is
    case-insensitive so admin overrides with different casing/wording
    still slice cleanly. Returns '' when no scoring section is found."""
    if not text:
        return ''
    low = text.lower()
    start = low.find('scoring rubrics')          # admin override label
    if start == -1:
        start = low.find('scoring — "s" object')  # built-in default label
    if start == -1:
        start = low.find('scoring')               # bare fallback
    if start == -1:
        return ''
    rubric = text[start:]
    cidx = rubric.lower().find('composite')       # server-side only — drop
    if cidx != -1:
        rubric = rubric[:cidx].rstrip()
    return rubric


def get_scoring_only_prompt() -> str:
    """Slim system prompt for the parser fast-path.

    When the free regex parser already lifted both contractor email AND
    phone we skip the full ~50-field extraction. But we still want a
    real AI lead score, so we make a tiny SCORING-ONLY call: the model
    sees the (small) cleaned page text + the already-parsed fields and
    returns ONLY the ``"s"`` sub-score object (~30 output tokens vs
    ~2.5k for the full extraction). Downstream ``_normalise_permit``
    turns ``"s"`` into ai_score/grade/tier identically for both paths.

    The per-dimension rubric is sliced VERBATIM from the SCORING section
    of whatever extraction prompt is active (admin override or built-in
    default) so fast-path scores never drift from full-call scores —
    single source of truth. The COMPOSITE/tier/grade tail is dropped:
    the server recomputes the composite from the sub-scores, so asking
    the model for it would only waste tokens."""
    rubric = _slice_scoring_section(get_extraction_prompt())
    if len(rubric) < 200:
        # Active prompt (e.g. an unusually-worded admin override) didn't
        # yield a usable rubric — fall back to the built-in default's
        # scoring section so the model still gets dimension guidance.
        # The header below also self-defines the nine keys, so even an
        # empty rubric still produces a valid scoring contract.
        rubric = _slice_scoring_section(EXTRACT_SYSTEM_PROMPT)
    return (
        "You are PermitlifyScorer. You will receive the visible text of ONE "
        "permit detail page plus the structured fields a deterministic parser "
        "has already extracted from it. Every field is already captured — your "
        "ONLY job is to SCORE this permit, using the rubric below.\n\n"
        "Return STRICT JSON ONLY — a single object with exactly one key "
        "\"ai_subscores\", whose value holds these nine integer sub-scores "
        "(each 0-100, or null when the underlying data is genuinely absent):\n"
        "  lead_quality, urgency, project_value, contact_completeness,\n"
        "  intent_signal, trade_fit, geographic, status_actionability,\n"
        "  data_confidence\n"
        "Example: {\"ai_subscores\":{\"lead_quality\":80,\"urgency\":75,"
        "\"project_value\":75,\"contact_completeness\":80,\"intent_signal\":90,"
        "\"trade_fit\":100,\"geographic\":80,\"status_actionability\":100,"
        "\"data_confidence\":90}}\n"
        "Emit NOTHING else: no ai_score, no ai_grade, no ai_tier, no "
        "ai_reasoning, no ai_next_action, no prose, no markdown fences. The "
        "server computes the composite score from your sub-scores.\n\n"
        + rubric
    )


# ─────────── List-page (CapHome / search results) extraction ──────────

# Schema for the LIST page: an envelope object with one entry per
# visible row in the search results table.
PERMIT_LIST_JSON_SCHEMA: dict = {
    'type': 'object',
    'properties': {
        'permits': {
            'type':  'array',
            'items': PERMIT_JSON_SCHEMA,
        },
    },
    'required': ['permits'],
}

# Built-in (NOT user-editable) wrapper that turns the one unified
# extraction prompt into a list-page directive. The orchestration
# (date-range filter, follow each row to its detail page, paginate)
# now lives in `_run_worker` — the prompt only has to teach the LLM
# the per-row extraction shape PLUS how to surface a `detail_url`.
# NOTE: We use literal token strings (``__DATE_FILTER_RULE__`` /
# ``__PER_PERMIT_PROMPT__``) and ``str.replace`` rather than
# ``str.format`` because the wrapper text contains JSON examples
# with literal ``{…}`` braces — ``.format`` would try to interpret
# those as format placeholders and KeyError out.
LIST_PAGE_WRAPPER: str = """You are looking at an Accela CapHome / search-results page that
shows MANY permits in a single results table. Typical visible columns:

  Date | Permit Number | Permit Type | Status | Address | Action

═══════════════════════════════════════════════════════════════════════
              CRITICAL RULES — READ EVERY LINE
═══════════════════════════════════════════════════════════════════════
1. Return JSON of shape  { "permits": [ {…}, {…}, … ] }

2. EXHAUSTIVE EXTRACTION — return ONE entry for EVERY data row visible
   in the results table on THIS page.
   • Accela list pages typically show 10, 25, 50, or 100 rows.
   • If you can SEE 10 rows in the markdown, return 10 entries.
   • If you can SEE 25 rows, return 25 entries. No exceptions.
   • Returning only 2 or 3 rows when more are visible is a CRITICAL
     FAILURE — you are NOT being helpful by "summarising"; the
     downstream system NEEDS every row.
   • Do NOT sample. Do NOT pick the "best" rows. Do NOT skip rows
     because they look similar. Do NOT cap at any small number.

3. SELF-CHECK before returning: count how many <tr> data rows
   (i.e. table rows that are NOT the header) appear in the markdown
   for the results table, and confirm the length of your `permits`
   array equals that number. If they differ, you missed rows — go
   back and add them.

4. Do NOT invent rows you cannot see in the markdown.

5. Do NOT include the header row, pagination links, filter controls,
   "Records Found:" banners, or "Showing 1-10 of 100+" summary text
   as permit entries.

6. The page may say "Showing 1-10 of 100+" — only extract THIS page's
   visible rows. The system fetches subsequent pages itself in a
   separate call. Don't try to be clever about pagination.

7. For EACH row, fill `detail_url` with the FULL absolute URL that the
   Permit Number link points to (look for an <a href="…CapDetail.aspx
   …capID3=…"> wrapping the permit number text in the markdown). If
   you cannot find an href, set it to null. This URL is critical —
   the system uses it to follow into each permit's detail page.

8. Required fields per row are `permit_number`, `permit_type`, and
   `address`. If `address` is genuinely missing for a row use the
   empty string ""; do NOT skip the row.
__DATE_FILTER_RULE__
═══════════════════════════════════════════════════════════════════════

EXAMPLE — for a results table with 6 visible rows you would return:

  {
    "permits": [
      { "permit_number": "BLD22-00001", "permit_type": "Roofing",     "address": "12 Main St",  "applied_date": "2025-04-01", "detail_url": "https://…CapDetail.aspx?…capID3=001" },
      { "permit_number": "BLD22-00002", "permit_type": "HVAC",        "address": "44 Elm Ave",  "applied_date": "2025-04-02", "detail_url": "https://…CapDetail.aspx?…capID3=002" },
      { "permit_number": "BLD22-00003", "permit_type": "Plumbing",    "address": "9 Oak Rd",    "applied_date": "2025-04-03", "detail_url": "https://…CapDetail.aspx?…capID3=003" },
      { "permit_number": "BLD22-00004", "permit_type": "Electrical",  "address": "77 Pine Ln",  "applied_date": "2025-04-04", "detail_url": "https://…CapDetail.aspx?…capID3=004" },
      { "permit_number": "BLD22-00005", "permit_type": "Solar",       "address": "3 Cedar Ct",  "applied_date": "2025-04-05", "detail_url": "https://…CapDetail.aspx?…capID3=005" },
      { "permit_number": "BLD22-00006", "permit_type": "Re-roof",     "address": "21 Maple Dr", "applied_date": "2025-04-06", "detail_url": "https://…CapDetail.aspx?…capID3=006" }
    ]
  }

Note 6 input rows → 6 output entries. NEVER 2. NEVER 3.

For EACH visible row, extract using the per-permit schema below
(fields the LIST page does NOT show — contractor email/phone, owner,
valuation, dates other than `applied_date` — should be null/empty;
the system fetches the per-permit detail page after this list call
to fill them in via Claude):

__PER_PERMIT_PROMPT__
"""


def get_list_extraction_prompt(date_from=None, date_to=None) -> str:
    """Compose the LIST-page extraction prompt by wrapping the user's
    one unified per-permit prompt with list-page orchestration rules.
    The date-range filter is injected as an additional rule when the
    admin specified a window in the Run-now form."""
    rule = ''
    if date_from or date_to:
        rule = (
            '7. DATE FILTER: Only include rows whose Date column falls in\n'
            f'   [{date_from or "begin"} … {date_to or "today"}].\n'
            '   Skip out-of-range rows entirely (do NOT include them in\n'
            '   the output array — the system also re-checks dates on\n'
            '   its side as a safety net).'
        )
    per_permit = get_extraction_prompt()
    return (LIST_PAGE_WRAPPER
            .replace('__DATE_FILTER_RULE__', rule)
            .replace('__PER_PERMIT_PROMPT__', per_permit))


def is_accela_list_url(url: str) -> bool:
    """True if the URL points at an Accela CapHome / search-results
    page (many permits shown in a table) rather than a single
    CapDetail page (one permit per page).

    We treat anything that ISN'T explicitly CapDetail.aspx as a list
    page — Accela has several list-style pages (CapHome, AddressList,
    GeneralSearch, ContactList) and we want to fall through to the
    list extractor for all of them.
    """
    u = (url or '').lower()
    if 'capdetail.aspx' in u:
        return False
    return any(marker in u for marker in (
        'caphome.aspx', 'caplist.aspx', 'addresslist',
        'generalsearch', 'contactlist', '/cap/cap',
    )) or '?module=' in u and 'capid' not in u


def _http_fetch_page(url: str, *, timeout: int = 60, mode: str = 'detail',
                     date_from=None, date_to=None) -> dict:
    """Fetch an Accela page over plain HTTP and (for list mode) ask the
    DigitalOcean Serverless Inference model to pull a structured
    ``{permits: [...]}`` envelope out of the visible page text.

    Drop-in replacement for the old Firecrawl-backed scraper helper:
    same return shape (``markdown`` / ``html`` / ``json`` / ``metadata``)
    so the surrounding upsert pipeline is unchanged.

    Notes:
      * Goes through the admin-configured scraper proxy if one is set
        (same pool as the per-source scrapers in ``core.scrapers.base``).
      * Sends a browser-shaped User-Agent — Accela's edge silently
        drops requests with a Python UA.
      * ``mode='detail'`` returns only markdown; the caller already
        runs Claude/OSS extraction on it.
      * ``mode='list'`` calls the OSS chat endpoint with the active
        list-extraction prompt so the returned ``json`` matches what
        the previous Firecrawl ``jsonOptions`` produced.
    """
    url = (url or '').strip()
    if not url:
        raise ScraperError('URL is required')
    if not (url.startswith('http://') or url.startswith('https://')):
        raise ScraperError('URL must start with http:// or https://')
    if mode not in ('detail', 'list'):
        raise ScraperError(f'_http_fetch_page: bad mode {mode!r}')

    from .scrapers.base import (
        build_proxy_opener, extract_visible_text, HttpScraperError,
        oss_complete,
    )

    req = urllib.request.Request(
        url,
        method='GET',
        headers={
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    )
    opener = build_proxy_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ScraperError(f'HTTP {e.code} fetching {url}') from e
    except urllib.error.URLError as e:
        raise ScraperError(f'Network error fetching {url}: {e.reason}') from e
    except Exception as e:
        raise ScraperError(f'Fetch failed for {url}: {e}') from e

    html = ''
    for enc in ('utf-8', 'cp1252', 'latin-1'):
        try:
            html = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not html:
        html = raw.decode('utf-8', errors='replace')

    visible = extract_visible_text(html) or ''
    if not visible.strip() and not html.strip():
        raise ScraperError(f'Page at {url} returned no content.')

    js: dict = {}
    if mode == 'list':
        prompt = get_list_extraction_prompt(date_from, date_to)
        body = visible[:32000]
        if len(visible) > 32000:
            body = body + '\n…(truncated)'

        # Extract anchors pointing at CapDetail.aspx so the LLM can
        # populate per-row ``detail_url`` — visible-text extraction
        # drops href attributes, so we surface them explicitly here.
        # Resolved to absolute URLs against the page URL.
        from urllib.parse import urljoin
        links: list[str] = []
        seen_hrefs: set[str] = set()
        for m in re.finditer(
            r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            html, flags=re.I | re.S,
        ):
            href = (m.group(1) or '').strip()
            if not href or href.startswith(('#', 'javascript:', 'mailto:')):
                continue
            if 'capdetail' not in href.lower():
                continue
            abs_href = urljoin(url, href)
            if abs_href in seen_hrefs:
                continue
            seen_hrefs.add(abs_href)
            # Strip inner tags from the anchor text.
            text = re.sub(r'<[^>]+>', '', m.group(2) or '').strip()
            links.append(f'- "{text[:80]}" → {abs_href}')
            if len(links) >= 200:
                break
        link_block = ''
        if links:
            link_block = (
                '\n\nCapDetail links found on this page '
                '(use these as `detail_url` when matching a row by '
                'permit number / address):\n' + '\n'.join(links)
            )

        full_prompt = (
            prompt
            + '\n\nReturn ONLY a JSON object of the form '
              '{"permits": [...]} matching the schema rules above. '
              'No prose, no markdown fences.'
            + link_block
            + '\n\nList page visible text:\n\n' + body
        )
        try:
            out = oss_complete(full_prompt, scraper_run_id=_current_run_id())
        except HttpScraperError as e:
            raise ScraperError(f'List extraction failed: {e}') from e
        try:
            js = _extract_json(out.get('text') or '')
            if not isinstance(js, dict):
                js = {}
        except ScraperError:
            # Empty / malformed JSON → return no rows; downstream code
            # treats this as a 0-row page (the legacy Firecrawl path
            # behaved the same way when its hosted model returned junk).
            js = {}

    return {
        'markdown': visible,
        'html':     html,
        'json':     js,
        'metadata': {'fetched_via': 'http', 'mode': mode},
    }


# ─────────────────────────── Anthropic / Claude ────────────────────────

from core.helpers.accela_parser import SYSTEM_PROMPT as EXTRACT_SYSTEM_PROMPT  # verbatim reference parser — single source of truth


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of Claude's response."""
    text = (text or '').strip()
    if not text:
        raise ScraperError('Claude returned an empty response.')
    # Direct parse first — strict prompt usually gives us bare JSON.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back: find the first {...} balanced block.
    start = text.find('{')
    if start < 0:
        raise ScraperError('Claude response had no JSON object.')
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                blob = text[start:i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError as e:
                    raise ScraperError(f'Claude JSON parse failed: {e}') from e
    raise ScraperError('Claude JSON object was not closed.')


def _extract_with_fallback(markdown: str, *, source_url: str = '',
                           run_id: int | None = None,
                           ctx: str = '') -> tuple[dict, bool, str]:
    """Parse-first / Claude-fallback extractor for Accela detail pages.

    Tries the free regex parser (``parse_accela_detail``) first. Only
    falls back to Claude when the two leadgen-critical fields
    (contractor_email AND contractor_phone) are both empty — typical
    Accela pages with a populated Applicant/Contractor block parse
    cleanly without any inference call, saving ~60% of LLM tokens.

    Returns ``(permit_dict, inference_used, parse_method)`` where
    ``parse_method`` is one of ``parser`` | ``parser+claude`` |
    ``claude-only``. Emits dual-channel diagnostics so admins can audit
    the savings in both the CLI stdout and the per-run log tab:
      * ``log.info`` lines tagged ``accela-parse`` (CLI / journald)
      * ``append_scraper_run_step`` entries (the admin's live log panel)
    """
    from .helpers.accela_parser import parse_accela_detail

    def _emit(level: str, msg: str) -> None:
        # Always log to stdout/stderr so `tail -f` of the worker shows
        # the parse-vs-claude decisions in real time.
        if level == 'err':
            log.error('accela-parse %s', msg)
        elif level == 'warn':
            log.warning('accela-parse %s', msg)
        else:
            log.info('accela-parse %s', msg)
        # Mirror into the per-run UI log so the admin sees the same
        # information in the "Logs" tab on the scraper detail page.
        if run_id is not None:
            try:
                append_scraper_run_step(run_id, msg, level)
            except Exception:
                # Logging the log is a luxury — never crash the worker.
                pass

    md_len = len(markdown or '')
    try:
        parsed = parse_accela_detail(markdown, source_url=source_url)
    except Exception as exc:
        log.exception('parse_accela_detail raised on %s', source_url[:120])
        parsed = {}

    pe = (parsed.get('contractor_email') or '').strip()
    pp = (parsed.get('contractor_phone') or '').strip()
    pname = (parsed.get('contractor_name') or '').strip()

    if pe and pp:
        # FAST PATH — both contact fields present, skip the LLM entirely.
        _emit('ok',
              f'{ctx}🟢 PARSED w/o inference · '
              f'contractor={pname[:30] or "—"} '
              f'email={pe[:35]} phone={pp} '
              f'(md={md_len}B, Claude skipped → token save)')
        return parsed, False, 'parser'

    # SLOW PATH — at least one critical field missing, fall back to LLM.
    missing = []
    if not pe:
        missing.append('email')
    if not pp:
        missing.append('phone')
    _emit('warn',
          f'{ctx}🟡 parser missing {"/".join(missing)} '
          f'(name={pname[:30] or "—"}, md={md_len}B) — '
          f'falling back to Claude inference')

    try:
        claude_data = claude_extract(markdown, source_url=source_url)
    except Exception as exc:
        # Claude failed AND parser was incomplete — return whatever the
        # parser did manage to get rather than dropping the row.
        _emit('err',
              f'{ctx}🔴 Claude failed: {str(exc)[:140]} — '
              f'falling back to partial parser result')
        return parsed, True, 'parser-only-claude-failed'

    # Merge: Claude wins for any non-empty field; parser fills holes
    # Claude left blank (e.g., parsed permit_number sticks if Claude
    # returned ``""``).
    merged = dict(parsed)
    for k, v in (claude_data or {}).items():
        if v not in (None, '', [], {}):
            merged[k] = v

    ce = (merged.get('contractor_email') or '').strip()
    cp = (merged.get('contractor_phone') or '').strip()
    cn = (merged.get('contractor_name')  or '').strip()
    _emit('ok',
          f'{ctx}🔵 CLAUDE extracted · '
          f'contractor={cn[:30] or "—"} '
          f'email={ce[:35] or "—"} phone={cp or "—"}')
    return merged, True, 'parser+claude'


def claude_extract(markdown: str, *, source_url: str = '',
                   api_key: str | None = None,
                   model: str | None = None,
                   timeout: int = 90) -> dict:
    """Send the scraped markdown to Claude and return a parsed permit
    dict matching the columns of the ``permits`` table."""
    md = (markdown or '').strip()
    if not md:
        raise ScraperError('No markdown to extract from — Firecrawl returned empty.')

    key = (api_key or get_system_setting('claude_api_key') or '').strip()
    if not key:
        raise ScraperError('Claude API key is not configured. '
                           'Add it in Scraper Settings.')
    mdl = (model or get_system_setting('claude_model') or '').strip() or DEFAULT_MODEL

    user_parts = []
    if source_url:
        user_parts.append(f'Source URL: {source_url}')
    # Hard cap the markdown we send so a runaway page doesn't blow the
    # token budget. 32 KB is plenty for a single CapDetail screen.
    if len(md) > 32000:
        md = md[:32000] + '\n…(truncated)'
    user_parts.append('Permit page markdown:\n\n' + md)
    user_prompt = '\n\n'.join(user_parts)

    body = json.dumps({
        'model':      mdl,
        'max_tokens': 1500,
        'system':     get_extraction_prompt(),
        'messages':   [{'role': 'user', 'content': user_prompt}],
    }).encode('utf-8')

    req = urllib.request.Request(
        ANTHROPIC_URL, data=body, method='POST',
        headers={
            'x-api-key':         key,
            'anthropic-version': ANTHROPIC_VERSION,
            'Content-Type':      'application/json',
            'Accept':            'application/json',
        },
    )
    _t0 = time.monotonic()
    _status = None
    _err = None
    _in_tok = 0
    _out_tok = 0
    payload = None
    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _status = resp.getcode()
                payload = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            _status = e.code
            try:
                err_body = json.loads(e.read().decode('utf-8'))
                err_msg = (err_body.get('error') or {}).get('message') or str(err_body)
            except Exception:
                err_msg = f'HTTP {e.code}'
            _err = f'Claude error: {err_msg}'
            raise ScraperError(_err) from e
        except urllib.error.URLError as e:
            _err = f'Claude network error: {e.reason}'
            raise ScraperError(_err) from e
        except Exception as e:
            _err = f'Claude failed: {e}'
            raise ScraperError(_err) from e

        usage = (payload or {}).get('usage') or {}
        try:
            _in_tok  = int(usage.get('input_tokens')  or 0)
            _out_tok = int(usage.get('output_tokens') or 0)
        except Exception:
            _in_tok = _out_tok = 0
    finally:
        try:
            db.record_claude_call(
                scraper_run_id=_current_run_id(),
                source=_current_source('accela'),
                model=mdl,
                status_code=_status,
                latency_ms=int((time.monotonic() - _t0) * 1000),
                input_tokens=_in_tok,
                output_tokens=_out_tok,
                error=_err,
            )
        except Exception:
            log.exception('claude usage recording failed')

    blocks = payload.get('content') or []
    text = ''
    for b in blocks:
        if isinstance(b, dict) and b.get('type') == 'text':
            text += b.get('text') or ''
    raw = _extract_json(text)
    return _normalise_permit(raw)


# ─────────────────────────── Accela link finder ──────────────────────────
#
# Helpers for the Scrapers → "Accela Permit Search" admin page. Given a
# city + state, we hand the lookup to Firecrawl's autonomous Agent
# (``app.agent(prompt=…, schema=…, model=…, max_credits=…)``) which
# performs its own browsing/search and returns a structured pick that
# matches our Pydantic schema. The agent call is recorded to
# ``firecrawl_calls`` with ``mode='agent'`` and ``source='accela_finder'``
# so the Firecrawl Usage dashboard keeps a single canonical view of
# every external API hit.

ACCELA_FINDER_SOURCE = 'accela_finder'

# ── Claude-backed finder ─────────────────────────────────────────────
# The finder used to call Firecrawl's autonomous Agent (one HTTP call
# per city, browses the open web, returns a structured pick). It has
# been switched to Claude per admin request: Claude has been verified
# to return the right URL from its training-data knowledge of public
# Accela deployments, doesn't require a per-call browse credit, and
# keeps all AI usage in one ledger (`claude_calls`).
#
# The Firecrawl helper below (`firecrawl_agent_pick`) is kept in the
# file as dead code for revertability — nothing currently calls it.
# The Claude finder (`claude_finder_pick`) is also kept below for
# revertability but replaced by `oss_finder_pick` which uses the same
# DigitalOcean Serverless Inference the scrapers already run on.
ACCELA_FINDER_DEFAULT_MODEL       = 'openai-gpt-oss-20b'
ACCELA_FINDER_DEFAULT_MAX_TOKENS  = 1500
ACCELA_FINDER_MAX_TOKENS_CAP      = 8000
ACCELA_FINDER_DEFAULT_MAX_CREDITS = ACCELA_FINDER_DEFAULT_MAX_TOKENS
ACCELA_FINDER_SOURCE              = 'accela_finder'

ACCELA_FINDER_DEFAULT_PROMPT = (
    "Find the public 'Citizen Access' building-permit search page "
    "for {city}, {state} hosted by Accela."
)

FIRECRAWL_SEARCH_URL = 'https://api.firecrawl.dev/v1/search'

ACCELA_FINDER_SYSTEM_PROMPT = (
    "You are a research assistant. You are given Google search results "
    "for a US city's Accela 'Citizen Access' building-permit page. "
    "Your job is to pick the BEST result that is the actual permit "
    "search page.\n\n"
    "STRICT RULES:\n"
    " 1. The URL host MUST be accela.com or a subdomain of accela.com "
    "(e.g. aca-prod.accela.com, aca.accela.com). Reject *.gov / *.org "
    "portals even when they link to Accela.\n"
    " 2. PREFER pages whose path contains 'CapHome.aspx', "
    "'GeneralSearch', or '/Cap/' — these are the actual permit search "
    "forms. Reject login pages (Default.aspx without /Cap/), "
    "account-creation pages, and home/welcome landing pages.\n"
    " 3. The URL must let a citizen SEARCH building permits, not a "
    "single permit-detail page (CapDetail) and not an unrelated module "
    "like business licences or planning.\n"
    " 4. Confidence is 'high' if a search result clearly matches the "
    "city and has CapHome/Cap in the path, 'medium' if the result is "
    "likely but ambiguous (e.g. county-level portal), 'low' if no "
    "accela.com result was found.\n"
    " 5. If NONE of the search results contain a suitable accela.com "
    "URL, return url=null with confidence='low'.\n\n"
    "Reply with EXACTLY one JSON object and nothing else — no prose, "
    "no markdown fences:\n"
    '  {"url": "https://…accela.com/…" or null,\n'
    '   "city": "<city>",\n'
    '   "state": "<state>",\n'
    '   "confidence": "high"|"medium"|"low",\n'
    '   "reason": "one short sentence"}'
)

ACCELA_FINDER_OUTPUT_BUDGET_NEEDLES = (
    'max_tokens',
    'output_tokens',
    'output tokens per minute',
    'output_tokens_per_minute',
)
ACCELA_FINDER_INPUT_RATE_LIMIT_NEEDLES = (
    'input tokens per minute',
    'input_tokens_per_minute',
    'rate limit',
    'overloaded',
    'capacity',
)
ACCELA_FINDER_BUDGET_HINT_NEEDLES = (
    *ACCELA_FINDER_INPUT_RATE_LIMIT_NEEDLES,
    *ACCELA_FINDER_OUTPUT_BUDGET_NEEDLES,
)


def _firecrawl_search(query: str, *, limit: int = 10,
                      timeout: int = 20) -> list[dict]:
    """Google search via Firecrawl ``/v1/search``.

    Returns a list of ``{url, title, description}`` dicts (may be
    empty on error or zero results). Never raises — the caller treats
    an empty list as "no search results" and tells the model accordingly.
    """
    key = (get_system_setting('firecrawl_api_key') or '').strip()
    if not key:
        log.warning('firecrawl_api_key not set — skipping search')
        return []
    body = json.dumps({'query': query, 'limit': limit}).encode('utf-8')
    req = urllib.request.Request(
        FIRECRAWL_SEARCH_URL, data=body, method='POST',
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type':  'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data.get('data') or []
    except Exception as e:
        log.exception('Firecrawl search failed for query=%s', query[:80])
        return []


FIRECRAWL_SCRAPE_URL = 'https://api.firecrawl.dev/v1/scrape'

ACCELA_VERIFY_SYSTEM_PROMPT = (
    "You are a verification assistant. You are given the rendered page "
    "content (markdown) of a URL on accela.com. Your job is to confirm "
    "the page is a WORKING Accela Citizen Access permit-search page.\n\n"
    "PASS the page if ALL of these are true:\n"
    " 1. It is an Accela Citizen Access page (not a generic accela.com "
    "marketing page).\n"
    " 2. It has permit-related functionality — search forms, record "
    "search, permit type selectors, 'Building', 'Planning', 'Apply', "
    "'Search Records', module tabs, etc.\n"
    " 3. It is NOT an error page, 404, maintenance page, server error, "
    "or completely blank/empty page.\n\n"
    "IMPORTANT:\n"
    " - Many Accela portals serve an entire county or region. The city "
    "name may NOT appear on the page — that is OK. Do NOT reject a "
    "working permit search page just because the city name is missing.\n"
    " - Login pages that also show a search form or module tabs = PASS.\n"
    " - ONLY reject if the page is clearly from a DIFFERENT US STATE "
    "than expected (e.g. expected California but page says 'Welcome to "
    "New York').\n\n"
    "Reply with EXACTLY one JSON object and nothing else:\n"
    '  {"verified": true or false,\n'
    '   "reason": "one short sentence explaining why"}'
)


def _firecrawl_fetch_page(url: str, *, timeout: int = 30) -> str | None:
    """Fetch a URL via Firecrawl /v1/scrape and return markdown content.

    Lightweight call — only requests markdown, no JSON extraction.
    Returns the markdown string or None on any error. Never raises.
    """
    key = (get_system_setting('firecrawl_api_key') or '').strip()
    if not key:
        log.warning('firecrawl_api_key not set — skipping page fetch')
        return None
    body = json.dumps({
        'url': url,
        'formats': ['markdown'],
        'onlyMainContent': True,
        'waitFor': 3000,
        'timeout': 20000,
        'actions': [
            {'type': 'wait', 'milliseconds': 2000},
        ],
    }).encode('utf-8')
    req = urllib.request.Request(
        FIRECRAWL_SCRAPE_URL, data=body, method='POST',
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type':  'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if not data.get('success'):
            return None
        return (data.get('data') or {}).get('markdown') or None
    except Exception as e:
        log.exception('Firecrawl page fetch failed for url=%s', url[:120])
        return None


def _format_search_results(results: list[dict]) -> str:
    """Format Firecrawl search results as numbered text for the model."""
    if not results:
        return '(No search results found.)'
    lines = []
    for i, r in enumerate(results, 1):
        url   = r.get('url', '')
        title = r.get('title', '')
        desc  = (r.get('description') or '')[:200]
        lines.append(f'{i}. {title}\n   URL: {url}\n   {desc}')
    return '\n\n'.join(lines)


def oss_finder_pick(city: str, state_name: str,
                    *, prompt_template: str | None = None,
                    model: str | None = None,
                    max_tokens: int | None = None,
                    timeout: int = 90) -> dict:
    """Find the best Accela permit-search URL for one city.

    Two-step flow:
      1. **Firecrawl search** — Google for the city's Accela page
      2. **DO Inference** — analyse those results and pick the best URL

    Every step is logged to a ``step_log`` text field and the full
    search results are stored as JSONB so the admin can debug exactly
    what happened on every call.
    """
    from .scrapers.base import oss_complete, HttpScraperError

    steps = []
    def _step(msg):
        steps.append(f'[{time.strftime("%H:%M:%S")}] {msg}')

    tmpl = (prompt_template or ACCELA_FINDER_DEFAULT_PROMPT).strip()
    if not tmpl:
        tmpl = ACCELA_FINDER_DEFAULT_PROMPT
    if len(tmpl) > 4000:
        tmpl = tmpl[:4000]

    mdl = (model or ACCELA_FINDER_DEFAULT_MODEL).strip()
    if not mdl:
        mdl = ACCELA_FINDER_DEFAULT_MODEL

    try:
        mt = int(max_tokens) if max_tokens is not None else ACCELA_FINDER_DEFAULT_MAX_TOKENS
    except (TypeError, ValueError):
        mt = ACCELA_FINDER_DEFAULT_MAX_TOKENS
    mt = max(1, min(mt, ACCELA_FINDER_MAX_TOKENS_CAP))

    safe_city  = (city  or '').strip()
    safe_state = (state_name or '').strip()

    _step(f'START finder for {safe_city}, {safe_state}')
    _step(f'Model: {mdl}, max_tokens: {mt}')

    _t0    = time.monotonic()
    _err   = None
    text   = ''
    parsed = None
    in_tok = out_tok = 0
    search_count = 0
    search_ms = 0
    inference_ms = 0
    search_query_used = ''

    search_query = (
        f'{safe_city} {safe_state} Accela Citizen Access '
        f'building permit search site:accela.com'
    )
    search_query_used = search_query
    _step(f'SEARCH query: {search_query}')
    _ts = time.monotonic()
    search_results = _firecrawl_search(search_query, limit=10)
    search_ms = int((time.monotonic() - _ts) * 1000)
    search_count = len(search_results)
    _step(f'SEARCH returned {search_count} results in {search_ms}ms')

    if search_results:
        for i, sr in enumerate(search_results):
            _step(f'  result[{i+1}]: {sr.get("url","?")} — {sr.get("title","?")}')
    else:
        _step('SEARCH fallback: retrying without site:accela.com')
        fallback_q = f'{safe_city} {safe_state} Accela building permit'
        search_query_used = fallback_q
        _ts2 = time.monotonic()
        search_results = _firecrawl_search(fallback_q, limit=10)
        fb_ms = int((time.monotonic() - _ts2) * 1000)
        search_ms += fb_ms
        search_count = len(search_results)
        _step(f'SEARCH fallback returned {search_count} results in {fb_ms}ms')
        for i, sr in enumerate(search_results):
            _step(f'  result[{i+1}]: {sr.get("url","?")} — {sr.get("title","?")}')

    if not search_results:
        _step('WARNING: no search results at all — model will guess from memory')

    results_text = _format_search_results(search_results)

    try:
        user_prompt = tmpl.format(city=safe_city, state=safe_state)
    except (IndexError, KeyError):
        user_prompt = (tmpl
                       .replace('{city}',  safe_city)
                       .replace('{state}', safe_state))

    prompt = (
        f'{user_prompt}\n\n'
        f'--- GOOGLE SEARCH RESULTS ---\n\n'
        f'{results_text}'
    )

    system_prompt = ACCELA_FINDER_SYSTEM_PROMPT
    _step(f'INFERENCE calling {mdl} with {len(prompt)} char prompt')

    _ti = time.monotonic()
    try:
        out = oss_complete(
            prompt,
            system=system_prompt,
            model=mdl,
            max_tokens=mt,
            temperature=0.0,
            timeout=timeout,
            source=ACCELA_FINDER_SOURCE,
        )
        text    = out.get('text', '')
        in_tok  = out.get('input_tokens', 0)
        out_tok = out.get('output_tokens', 0)
        inference_ms = int((time.monotonic() - _ti) * 1000)
        _step(f'INFERENCE ok in {inference_ms}ms — {in_tok} in / {out_tok} out tokens')
        _step(f'INFERENCE raw response: {text[:500]}')
    except HttpScraperError as e:
        inference_ms = int((time.monotonic() - _ti) * 1000)
        _err = str(e)
        _step(f'INFERENCE ERROR in {inference_ms}ms: {_err}')
    except Exception as e:
        inference_ms = int((time.monotonic() - _ti) * 1000)
        _err = f'DO Inference failed: {e}'
        _step(f'INFERENCE EXCEPTION in {inference_ms}ms: {_err}')

    if text and not _err:
        try:
            parsed = _extract_json(text)
            _step(f'PARSE ok: {json.dumps(parsed)[:300]}')
        except ScraperError as e:
            _err = f'Model returned non-JSON output: {e}'
            _step(f'PARSE ERROR: {_err}')
    elif not _err and not text:
        _err = 'Model returned an empty response.'
        _step(f'PARSE ERROR: {_err}')

    chosen_url = None
    confidence = 'low'
    reason     = ''
    if isinstance(parsed, dict):
        raw_url = parsed.get('url')
        if isinstance(raw_url, str):
            chosen_url = raw_url.strip() or None
        confidence = (parsed.get('confidence') or 'low')
        if not isinstance(confidence, str):
            confidence = 'low'
        confidence = confidence.strip().lower()
        reason = (parsed.get('reason') or '')
        if not isinstance(reason, str):
            reason = ''
        reason = reason.strip()[:300]
    if confidence not in ('high', 'medium', 'low'):
        confidence = 'low'

    if chosen_url:
        try:
            host = (urllib.parse.urlparse(chosen_url).hostname or '').lower()
        except Exception:
            host = ''
        if host != 'accela.com' and not host.endswith('.accela.com'):
            _step(f'HOST REJECT: {host} is not accela.com')
            reason     = f'Model picked non-accela host: {host or "unknown"}'
            chosen_url = None
            confidence = 'low'
        else:
            _step(f'URL PICKED: {chosen_url} — starting verification')
            _tv = time.monotonic()
            page_md = _firecrawl_fetch_page(chosen_url, timeout=35)
            verify_fetch_ms = int((time.monotonic() - _tv) * 1000)
            if page_md:
                _step(f'VERIFY FETCH ok in {verify_fetch_ms}ms — {len(page_md)} chars')
                verify_prompt = (
                    f'URL: {chosen_url}\n'
                    f'Expected city: {safe_city}\n'
                    f'Expected state: {safe_state}\n\n'
                    f'--- PAGE CONTENT ---\n\n'
                    f'{page_md[:6000]}'
                )
                _step(f'VERIFY calling {mdl} to check page content')
                _tv2 = time.monotonic()
                try:
                    v_out = oss_complete(
                        verify_prompt,
                        system=ACCELA_VERIFY_SYSTEM_PROMPT,
                        model=mdl,
                        max_tokens=500,
                        temperature=0.0,
                        timeout=60,
                        source=ACCELA_FINDER_SOURCE,
                    )
                    v_text = v_out.get('text', '')
                    v_in = v_out.get('input_tokens', 0)
                    v_ot = v_out.get('output_tokens', 0)
                    in_tok += v_in
                    out_tok += v_ot
                    verify_inf_ms = int((time.monotonic() - _tv2) * 1000)
                    _step(f'VERIFY INFERENCE ok in {verify_inf_ms}ms — {v_in} in / {v_ot} out')
                    _step(f'VERIFY raw response: {v_text[:300]}')
                    try:
                        v_parsed = _extract_json(v_text)
                        verified = v_parsed.get('verified', False) if isinstance(v_parsed, dict) else False
                        v_reason = (v_parsed.get('reason', '') if isinstance(v_parsed, dict) else '')[:200]
                        if verified:
                            _step(f'VERIFY PASSED: {v_reason}')
                            reason = v_reason or reason
                        else:
                            _step(f'VERIFY FAILED: {v_reason} — rejecting URL')
                            reason = f'Page verification failed: {v_reason}'
                            chosen_url = None
                            confidence = 'low'
                    except ScraperError:
                        _step(f'VERIFY PARSE ERROR — keeping URL as-is')
                except Exception as ve:
                    _step(f'VERIFY INFERENCE ERROR: {ve} — keeping URL as-is')
            else:
                _step(f'VERIFY FETCH FAILED in {verify_fetch_ms}ms — page is dead/unreachable')
                reason = f'Page could not be loaded (Firecrawl fetch failed)'
                chosen_url = None
                confidence = 'low'
    else:
        _step(f'NO URL returned by model')

    latency = int((time.monotonic() - _t0) * 1000)
    _step(f'DONE total={latency}ms search={search_ms}ms inference={inference_ms}ms '
          f'url={"found" if chosen_url else "null"} conf={confidence}')

    step_log = '\n'.join(steps)

    try:
        db.record_finder_request(
            city=safe_city,
            state=safe_state,
            model=mdl,
            search_query=search_query_used,
            search_results=search_results,
            search_count=search_count,
            prompt=prompt,
            system_prompt=system_prompt,
            raw_response=text if text else None,
            parsed_json=parsed,
            url_found=chosen_url,
            confidence=confidence,
            reason=reason,
            error=_err,
            latency_ms=latency,
            search_ms=search_ms,
            inference_ms=inference_ms,
            input_tokens=in_tok,
            output_tokens=out_tok,
            source=ACCELA_FINDER_SOURCE,
            step_log=step_log,
        )
    except Exception:
        log.exception('finder request logging failed')

    return {
        'ok':         _err is None,
        'url':        chosen_url,
        'confidence': confidence,
        'reason':     reason or (_err or '')[:300],
        'error':      _err,
        'log': {
            'status':         200 if not _err else None,
            'agent_id':       None,
            'model':          mdl,
            'credits_used':   (in_tok + out_tok) or None,
            'credits_budget': mt,
            'web_searches':   search_count,
            'retries':        0,
            'prompt':         prompt,
            'data':           parsed if parsed is not None else (text[:2000] or None),
            'error':          _err,
            'latency_ms':     latency,
        },
    }


def claude_finder_pick(city: str, state_name: str,
                       *, prompt_template: str | None = None,
                       model: str | None = None,
                       max_tokens: int | None = None,
                       api_key: str | None = None,
                       timeout: int = 120) -> dict:
    """Ask Claude to pick the best Accela permit-search URL for one city.

    Replaces the previous Firecrawl-Agent finder. Returns the SAME
    envelope shape so the JS / push wiring stays unchanged::

        {
          'ok':         bool,
          'url':        str | None,
          'confidence': 'high'|'medium'|'low',
          'reason':     str,
          'error':      str | None,
          'log': {
              'status':         int | None,    # HTTP status
              'agent_id':       str | None,    # Claude message id
              'model':          str,
              'credits_used':   int | None,    # input+output tokens
              'credits_budget': int,           # max_tokens we sent
              'prompt':         str,
              'data':           Any,           # parsed JSON or None
              'error':          str | None,
              'latency_ms':     int,
          },
        }

    Records exactly one row in ``claude_calls`` with
    ``source='accela_finder'`` so the existing Claude Usage dashboard
    counts it. Never raises on a Claude failure — surfaces the error
    in the returned envelope so the table can render it inline next
    to successful rows.
    """
    key = (api_key or get_system_setting('claude_api_key') or '').strip()
    if not key:
        raise ScraperError('Claude API key is not configured. '
                           'Add it in Scraper Settings.')

    # ── Sanitise the optional knobs ────────────────────────────────
    tmpl = (prompt_template or ACCELA_FINDER_DEFAULT_PROMPT).strip()
    if not tmpl:
        tmpl = ACCELA_FINDER_DEFAULT_PROMPT
    if len(tmpl) > 4000:
        tmpl = tmpl[:4000]

    mdl = (model or ACCELA_FINDER_DEFAULT_MODEL).strip()
    if mdl not in ACCELA_FINDER_VALID_MODELS:
        mdl = ACCELA_FINDER_DEFAULT_MODEL

    try:
        mt = int(max_tokens) if max_tokens is not None else ACCELA_FINDER_DEFAULT_MAX_TOKENS
    except (TypeError, ValueError):
        mt = ACCELA_FINDER_DEFAULT_MAX_TOKENS
    mt = max(1, min(mt, ACCELA_FINDER_MAX_TOKENS_CAP))

    # Substitute {city} / {state} placeholders. Forgive missing braces
    # on a custom prompt — str.format would raise KeyError otherwise.
    safe_city  = (city  or '').strip()
    safe_state = (state_name or '').strip()
    try:
        prompt = tmpl.format(city=safe_city, state=safe_state)
    except (IndexError, KeyError):
        prompt = (tmpl
                  .replace('{city}',  safe_city)
                  .replace('{state}', safe_state))

    # The system prompt carries the mandatory rules (use web-search,
    # accela.com host rule, JSON output schema). It is intentionally
    # NOT user-editable — see ACCELA_FINDER_SYSTEM_PROMPT for why.
    system_prompt = ACCELA_FINDER_SYSTEM_PROMPT

    body = json.dumps({
        'model':      mdl,
        'max_tokens': mt,
        'system':     system_prompt,
        # Server-side web search: Anthropic runs the searches itself
        # and returns the final text in the same response — no
        # tool-result loop on our side. See ACCELA_FINDER_WEB_SEARCH_TOOL
        # for the rationale (claude.ai parity).
        'tools':      [ACCELA_FINDER_WEB_SEARCH_TOOL],
        'messages':   [{'role': 'user', 'content': prompt}],
    }).encode('utf-8')

    req = urllib.request.Request(
        ANTHROPIC_URL, data=body, method='POST',
        headers={
            'x-api-key':         key,
            'anthropic-version': ANTHROPIC_VERSION,
            'Content-Type':      'application/json',
            'Accept':            'application/json',
        },
    )

    _t0       = time.monotonic()
    _status   = None
    _err      = None
    _msg_id   = None
    _in_tok   = 0
    _out_tok  = 0
    _retries  = 0
    payload   = None
    try:
        # Anthropic enforces a per-org input-tokens-per-minute budget.
        # With web_search enabled each call now consumes ~25-50k input
        # tokens (search results land in context), so two cities back-
        # to-back can blow the default 50k/min limit and surface as a
        # 429. Anthropic returns ``retry-after`` (seconds) on those —
        # honour it up to four times before giving up. Bounded to ~3
        # min total extra wait per city: heavy admin sweeps (~10 cities
        # of 30-50k tokens each) need that headroom because even after
        # one 30s wait the next call can still overlap the sliding
        # 60-second window. The JS queue spaces calls ~8s apart so the
        # retry path should rarely fire more than once.
        for _attempt in range(5):  # initial + 4 retries
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    _status = resp.getcode()
                    payload = json.loads(resp.read().decode('utf-8'))
                    break  # success
            except urllib.error.HTTPError as e:
                _status = e.code
                try:
                    err_body = json.loads(e.read().decode('utf-8'))
                    err_msg  = (err_body.get('error') or {}).get('message') or str(err_body)
                except Exception:
                    err_msg  = f'HTTP {e.code}'
                if e.code == 429 and _attempt < 4:
                    # ``Retry-After`` is RFC-7231 — typically an integer
                    # number of seconds. Clamp to [1, 45] so a buggy /
                    # absurdly large value can't wedge the request.
                    try:
                        wait = int(e.headers.get('retry-after') or 30)
                    except (TypeError, ValueError):
                        wait = 30
                    wait = max(1, min(wait, 45))
                    _retries += 1
                    time.sleep(wait)
                    # Rebuild the request — urllib.Request is single-use
                    # once .urlopen has consumed it on the failed attempt.
                    req = urllib.request.Request(
                        ANTHROPIC_URL, data=body, method='POST',
                        headers={
                            'x-api-key':         key,
                            'anthropic-version': ANTHROPIC_VERSION,
                            'Content-Type':      'application/json',
                            'Accept':            'application/json',
                        },
                    )
                    continue
                _err = f'Claude error: {err_msg}'
                break
            except urllib.error.URLError as e:
                _err = f'Claude network error: {e.reason}'
                break
            except Exception as e:
                _err = f'Claude failed: {e}'
                break

        if payload is not None:
            _msg_id = payload.get('id')
            usage   = payload.get('usage') or {}
            try:
                _in_tok  = int(usage.get('input_tokens')  or 0)
                _out_tok = int(usage.get('output_tokens') or 0)
            except Exception:
                _in_tok = _out_tok = 0
            # web_search ran server-side: Anthropic reports the request
            # count under usage.server_tool_use.web_search_requests so
            # the admin can tell at a glance whether Claude actually
            # browsed (=> high-quality answer) vs replied from memory
            # (=> the prompt or tool wiring may need adjustment).
            try:
                _web_searches = int(((usage.get('server_tool_use') or {})
                                     .get('web_search_requests')) or 0)
            except Exception:
                _web_searches = 0
        else:
            _web_searches = 0
    finally:
        try:
            db.record_claude_call(
                scraper_run_id=_current_run_id(),
                source=_current_source(ACCELA_FINDER_SOURCE),
                model=mdl,
                status_code=_status,
                latency_ms=int((time.monotonic() - _t0) * 1000),
                input_tokens=_in_tok,
                output_tokens=_out_tok,
                error=_err,
            )
        except Exception:
            log.exception('claude finder usage recording failed')

    # ── Rewrite the friendly hint for known transient errors ───────
    # Two distinct failure modes get conflated by Anthropic's generic
    # "rate limit" wording. Check OUTPUT-budget needles FIRST because
    # they're strictly more specific (max_tokens / output_tokens /
    # output tokens per minute) than the generic 'rate limit' that
    # lives in INPUT — without this ordering, an output-TPM 429 would
    # be misclassified as input-TPM and the admin would be told to
    # wait when the actual fix is to raise the cap. The reverse error
    # (the more common one in practice) is also handled correctly:
    # input-TPM 429 messages don't contain max_tokens / output_tokens,
    # so they fall through to the INPUT branch as expected.
    if _err:
        _err_lc = _err.lower()
        if any(n in _err_lc for n in ACCELA_FINDER_OUTPUT_BUDGET_NEEDLES):
            _err = (
                f"Claude wanted more output than budgeted "
                f"({mt} tokens). Raise 'Max output tokens' in the "
                f"finder settings. (Original error: {_err})"
            )
        elif any(n in _err_lc for n in ACCELA_FINDER_INPUT_RATE_LIMIT_NEEDLES):
            _err = (
                f"Anthropic per-minute input-token budget exhausted "
                f"after {_retries} retries. Each web-search call uses "
                f"~30-50k input tokens; the org's default cap is 50k/min. "
                f"Wait ~60 s for the budget to refresh and re-run just "
                f"the failed cities, or raise the org's input-token "
                f"limit at https://console.anthropic.com/settings/limits. "
                f"(Original error: {_err})"
            )

    # ── Parse Claude's reply into our envelope ─────────────────────
    text = ''
    parsed = None
    if payload is not None:
        for b in payload.get('content') or []:
            if isinstance(b, dict) and b.get('type') == 'text':
                text += b.get('text') or ''
        if text:
            try:
                parsed = _extract_json(text)
            except ScraperError as e:
                if not _err:
                    _err = f'Claude returned non-JSON output: {e}'
        else:
            # No text block at all in a 200 response is a real failure
            # mode when web_search is enabled: if max_tokens runs out
            # mid tool-use Claude can return only server_tool_use /
            # web_search_tool_result blocks with no final answer. Without
            # this guard we'd silently report ok=true,url=null which
            # looks identical to a legitimate "no URL found" answer.
            if not _err:
                stop = payload.get('stop_reason') or 'unknown'
                _err = (f'Claude returned no text answer (stop_reason='
                        f'{stop}). Try raising "Max output tokens" — web '
                        f'search may have consumed the budget.')

    chosen_url = None
    confidence = 'low'
    reason     = ''
    if isinstance(parsed, dict):
        raw_url = parsed.get('url')
        if isinstance(raw_url, str):
            chosen_url = raw_url.strip() or None
        confidence = (parsed.get('confidence') or 'low')
        if not isinstance(confidence, str):
            confidence = 'low'
        confidence = confidence.strip().lower()
        reason = (parsed.get('reason') or '')
        if not isinstance(reason, str):
            reason = ''
        reason = reason.strip()[:300]
    if confidence not in ('high', 'medium', 'low'):
        confidence = 'low'

    # ── Defence-in-depth: enforce the host rule the prompt sets ────
    if chosen_url:
        try:
            host = (urllib.parse.urlparse(chosen_url).hostname or '').lower()
        except Exception:
            host = ''
        if host != 'accela.com' and not host.endswith('.accela.com'):
            reason     = f'Claude picked non-accela host: {host or "unknown"}'
            chosen_url = None
            confidence = 'low'

    return {
        'ok':         _err is None,
        'url':        chosen_url,
        'confidence': confidence,
        'reason':     reason or (_err or '')[:300],
        'error':      _err,
        'log': {
            'status':         _status,
            'agent_id':       _msg_id,
            'model':          mdl,
            'credits_used':   (_in_tok + _out_tok) or None,
            'credits_budget': mt,
            'web_searches':   _web_searches,
            'retries':        _retries,
            'prompt':         prompt[:2000],
            'data':           parsed if parsed is not None else (text[:1000] or None),
            'error':          _err,
            'latency_ms':     int((time.monotonic() - _t0) * 1000),
        },
    }


def firecrawl_agent_pick(city: str, state_name: str,
                         *, prompt_template: str | None = None,
                         model: str | None = None,
                         max_credits: int | None = None,
                         api_key: str | None = None,
                         timeout: int = 180) -> dict:
    """Use Firecrawl's autonomous Agent to find the best Accela
    permit-search URL for one city.

    Returns a dict::

        {
          'ok':           bool,
          'url':          str | None,
          'confidence':   'high' | 'medium' | 'low',
          'reason':       str,
          'error':        str | None,
          'log': {
              'status':       'completed' | 'failed' | 'processing' | None,
              'agent_id':     str | None,
              'model':        str | None,
              'credits_used': int | None,
              'prompt':       str,         # what we actually sent
              'data':         Any,         # raw .data from the agent (may be None)
              'error':        str | None,
              'latency_ms':   int,
          },
        }

    Records exactly one row in ``firecrawl_calls`` with
    ``mode='agent'`` and ``source='accela_finder'`` so the existing
    Firecrawl Usage dashboard counts it. Never raises on a Firecrawl
    failure — surfaces the error in the returned envelope so the
    table can render it inline alongside successful rows.
    """
    # Lazy imports — keep the SDK out of the import path of every
    # request that doesn't use the finder, and make the optional
    # dependency easy to spot.
    try:
        from firecrawl import Firecrawl
        from pydantic import BaseModel, Field
    except Exception as e:                               # pragma: no cover
        raise ScraperError(
            'Firecrawl SDK is not installed. Run '
            '"pip install firecrawl-py pydantic".'
        ) from e

    key = (api_key or get_system_setting('firecrawl_api_key') or '').strip()
    if not key:
        raise ScraperError('Firecrawl API key is not configured. '
                           'Add it in Scraper Settings.')

    # ── Sanitise the optional knobs ────────────────────────────────
    tmpl = (prompt_template or ACCELA_FINDER_DEFAULT_PROMPT).strip()
    if not tmpl:
        tmpl = ACCELA_FINDER_DEFAULT_PROMPT
    if len(tmpl) > 4000:
        tmpl = tmpl[:4000]

    mdl = (model or ACCELA_FINDER_DEFAULT_MODEL).strip()
    if mdl not in ACCELA_FINDER_VALID_MODELS:
        mdl = ACCELA_FINDER_DEFAULT_MODEL

    try:
        mc = int(max_credits) if max_credits is not None else ACCELA_FINDER_DEFAULT_MAX_CREDITS
    except (TypeError, ValueError):
        mc = ACCELA_FINDER_DEFAULT_MAX_CREDITS
    mc = max(1, mc)

    # Substitute {city} / {state} placeholders (forgive missing braces
    # on a custom prompt — str.format would raise KeyError).
    safe_city  = (city  or '').strip()
    safe_state = (state_name or '').strip()
    try:
        prompt = tmpl.format(city=safe_city, state=safe_state)
    except (IndexError, KeyError):
        prompt = (tmpl
                  .replace('{city}',  safe_city)
                  .replace('{state}', safe_state))

    # ── Schema the agent must conform its answer to ────────────────
    class AccelaSuggestion(BaseModel):
        url:        str | None = Field(
            None,
            description=(
                'Public Citizen Access permit-search URL on accela.com or '
                'a subdomain of accela.com. Null if no suitable URL exists.'
            ),
        )
        confidence: str = Field(
            'low',
            description='One of: high, medium, low.',
        )
        reason:     str = Field(
            '',
            description='One short sentence explaining the choice.',
        )

    fc = Firecrawl(api_key=key)

    _t0 = time.monotonic()
    _err = None
    _status = None
    _agent_id = None
    _credits = None
    _data = None
    response_obj = None
    try:
        try:
            response_obj = fc.agent(
                prompt=prompt,
                schema=AccelaSuggestion,
                model=mdl,                # 'spark-1-mini' | 'spark-1-pro'
                max_credits=mc,
                timeout=timeout,
            )
        except Exception as e:
            _err = f'Firecrawl agent error: {e}'

        if response_obj is not None:
            _status   = getattr(response_obj, 'status', None)
            _agent_id = getattr(response_obj, 'id', None)
            _credits  = getattr(response_obj, 'credits_used', None)
            _data     = getattr(response_obj, 'data', None)
            resp_err  = getattr(response_obj, 'error', None)
            if not _err and resp_err:
                _err = f'Firecrawl agent reported: {resp_err}'

        # If the agent ran out of *our* per-call budget (not the
        # Firecrawl account balance), rewrite the error so the admin
        # knows the fix is to raise "Max credits / city" — not to top
        # up their Firecrawl account. Also covers the agent's own
        # "Refusal: Error: Agent reached max credits" wording.
        if _err:
            _err_lc = _err.lower()
            if any(n in _err_lc for n in ACCELA_FINDER_BUDGET_HINT_NEEDLES):
                _err = (
                    f"Agent ran out of the per-city credit budget "
                    f"(used {mc} credits before finishing). "
                    f"Increase 'Max credits / city' in the Firecrawl "
                    f"agent settings above and retry. "
                    f"(Original error: {_err})"
                )
    finally:
        try:
            db.record_firecrawl_call(
                scraper_run_id=_current_run_id(),
                source=_current_source(ACCELA_FINDER_SOURCE),
                mode='agent',
                url=(safe_city + ', ' + safe_state)[:2048],
                status_code=200 if _status == 'completed' else (500 if _err else None),
                latency_ms=int((time.monotonic() - _t0) * 1000),
                response_bytes=None,
                error=_err,
                city=safe_city or None,
                state=safe_state or None,
            )
        except Exception:
            log.exception('firecrawl agent usage recording failed')

    # Normalise the structured pick into our standard envelope ──
    chosen_url = None
    confidence = 'low'
    reason     = ''
    if isinstance(_data, dict):
        chosen_url = (_data.get('url') or '').strip() or None
        confidence = (_data.get('confidence') or 'low').strip().lower()
        reason     = (_data.get('reason') or '').strip()[:300]
    elif _data is not None:
        # Pydantic model instance (or namespace-like)
        try:
            chosen_url = (getattr(_data, 'url', None) or '').strip() or None
            confidence = (getattr(_data, 'confidence', None) or 'low').strip().lower()
            reason     = (getattr(_data, 'reason', None) or '').strip()[:300]
        except Exception:
            pass
    if confidence not in ('high', 'medium', 'low'):
        confidence = 'low'

    # ── Defence-in-depth: enforce the host rule the prompt sets ──
    if chosen_url:
        try:
            host = (urllib.parse.urlparse(chosen_url).hostname or '').lower()
        except Exception:
            host = ''
        if host != 'accela.com' and not host.endswith('.accela.com'):
            reason     = f'Agent picked non-accela host: {host or "unknown"}'
            chosen_url = None
            confidence = 'low'

    # ── Log payload (browser-renderable) ──────────────────────────
    if isinstance(_data, dict):
        log_data = _data
    elif _data is None:
        log_data = None
    else:
        try:
            log_data = _data.model_dump()
        except Exception:
            try:
                log_data = dict(_data)
            except Exception:
                log_data = {'_repr': repr(_data)[:500]}

    return {
        'ok':         _err is None,
        'url':        chosen_url,
        'confidence': confidence,
        'reason':     reason or (_err or '')[:300],
        'error':      _err,
        'log': {
            'status':         _status,
            'agent_id':       _agent_id,
            'model':          mdl,
            'credits_used':   _credits,
            'credits_budget': mc,
            'prompt':         prompt[:2000],
            'data':           log_data,
            'error':          _err,
            'latency_ms':     int((time.monotonic() - _t0) * 1000),
        },
    }



# ───────────────────────── Per-scraper Agent ──────────────────────────
#
# The per-scraper page (/admin-panel/scrapers/<sid>/) used to drive a
# two-stage Firecrawl-scrape + Claude-extract pipeline. We now hand the
# entire job (browse the search page, paginate, open each detail page,
# pull every interesting field) to Firecrawl's autonomous Agent — same
# SDK the finder uses, just with a permits-list schema. One prompt =
# one HTTP call = N structured permit dicts back, ready for upsert.

ACCELA_SCRAPER_AGENT_SOURCE = 'accela_scraper_agent'

ACCELA_SCRAPER_AGENT_DEFAULT_MODEL       = 'openai-gpt-oss-20b'
# Free-text model id from the DigitalOcean Serverless Inference catalogue.
# Old allowlist (``spark-1-mini`` / ``spark-1-pro``) is gone — DO ships
# new models continuously and admins type the id straight into the
# Scrapers settings card. The save endpoint enforces a 200-char cap +
# ``[A-Za-z0-9._-/]`` syntax so a typo can't blow out the row.
ACCELA_SCRAPER_AGENT_DEFAULT_MAX_CREDITS = 1500
ACCELA_SCRAPER_AGENT_MAX_CREDITS_CAP     = 50000


# ─────────────────────────── Normalisation ────────────────────────────

_PHONE_RE = re.compile(r'\d')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

_ALLOWED_TRADES = {
    'roofing', 'hvac', 'plumbing', 'electrical', 'solar',
    'general', 'civil', 'other',
}
_ALLOWED_TIERS = {'hot', 'warm', 'cool'}

# Exact dimensions allowed in ai_subscores / ai_subscore_reasons. Kept
# as a closed set so a misbehaving model can't smuggle arbitrary keys
# (or arbitrarily-large blobs) into the permits.raw JSONB column.
_AI_SUBSCORE_KEYS = (
    'lead_quality', 'urgency', 'project_value', 'contact_completeness',
    'intent_signal', 'trade_fit', 'geographic', 'status_actionability',
    'data_confidence',
)


def _clean_subscores(v):
    """Normalise the model's `ai_subscores` dict to {key: int 0-100 | None}.
    Unknown keys dropped; non-numeric values become None (== "missing"
    per the prompt's golden rule). Returns None if the model omitted
    the field entirely, so we can tell "model didn't run" from
    "model ran and every subscore was null".
    """
    if not isinstance(v, dict):
        return None
    out = {}
    for k in _AI_SUBSCORE_KEYS:
        raw = v.get(k)
        if raw is None or raw == '':
            out[k] = None
            continue
        try:
            n = int(float(raw))
        except (TypeError, ValueError):
            out[k] = None
            continue
        out[k] = max(0, min(100, n))
    return out


def _clean_subscore_reasons(v):
    """Normalise `ai_subscore_reasons` to {key: short str | None}.
    Caps each reason at 240 chars so a runaway model can't bloat the
    permits.raw JSONB. Unknown keys dropped.
    """
    if not isinstance(v, dict):
        return None
    out = {}
    for k in _AI_SUBSCORE_KEYS:
        raw = v.get(k)
        if raw is None:
            out[k] = None
            continue
        s = str(raw).strip()
        out[k] = (s[:240] or None)
    return out


def _clean(v, default=''):
    if v is None:
        return default
    return str(v).strip()


def _to_int(v, default=None):
    if v is None or v == '':
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_iso_date(v):
    if not v:
        return None
    if isinstance(v, (date, datetime)):
        return v.strftime('%Y-%m-%d')
    s = str(v).strip()
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m-%d-%Y', '%d/%m/%Y',
                '%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def _norm_phone(v):
    if not v:
        return ''
    digits = ''.join(_PHONE_RE.findall(str(v)))
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) != 10:
        return ''
    return f'({digits[0:3]}) {digits[3:6]}-{digits[6:]}'


def _norm_email(v):
    if not v:
        return ''
    s = str(v).strip().lower()
    return s if _EMAIL_RE.match(s) else ''


def _norm_state(v):
    s = _clean(v).upper()
    if len(s) == 2:
        return s
    # Common full-name → abbr fallback for the most likely Accela states.
    full = {
        'WASHINGTON': 'WA', 'TEXAS': 'TX', 'CALIFORNIA': 'CA',
        'FLORIDA': 'FL', 'GEORGIA': 'GA', 'NEW YORK': 'NY',
        'ARIZONA': 'AZ', 'COLORADO': 'CO', 'OREGON': 'OR',
        'NORTH CAROLINA': 'NC', 'SOUTH CAROLINA': 'SC',
        'TENNESSEE': 'TN', 'NEVADA': 'NV', 'UTAH': 'UT',
    }
    return full.get(s, s[:2])


def _normalise_permit(raw: dict) -> dict:
    """Coerce the model's JSON into the exact shape ``upsert_permit`` accepts.

    Scoring shape change (May 2026): the model now returns ONLY the 9 raw
    sub-scores under either ``"s":{"lq":…}`` (new, short-key) or the
    legacy ``"ai_subscores":{"lead_quality":…}`` (old, long-key). The
    composite ai_score, tier, grade and reasoning are now computed
    deterministically server-side from those numbers via
    ``core.ai_phrases``, cutting output tokens ~75% and eliminating
    LLM drift on the rubric. Legacy long-key responses are still
    accepted so a stale cached row or a model that hasn't picked up
    the new prompt yet still scores correctly.
    """
    from . import ai_phrases
    trade = _clean(raw.get('trade'), 'other').lower()
    if trade not in _ALLOWED_TRADES:
        trade = 'other'

    # Accept BOTH the new compact "s" object AND the legacy
    # "ai_subscores" long-key object. ``normalise_short_keys`` returns
    # long-key form regardless, then ``_clean_subscores`` enforces the
    # closed key set + int|None invariant we've always had.
    raw_subs = raw.get('s') if isinstance(raw.get('s'), dict) else raw.get('ai_subscores')
    subs = _clean_subscores(ai_phrases.normalise_short_keys(raw_subs))

    # Composite + tier + grade are now ALWAYS computed from sub-scores.
    # If the model also emitted ai_score (legacy), we ignore it — sub-
    # scores are the source of truth so one number can't disagree with
    # the breakdown beneath it.
    score = ai_phrases.composite_score(subs)
    tier  = ai_phrases.tier_for(score)
    grade = ai_phrases.grade_for(score)

    # Per-factor explanations are generated locally from the phrase
    # library — the model no longer writes English at all. Seed the
    # variant picker with the permit number so the same permit
    # consistently renders the same phrase on every re-score (no UI
    # flicker) while different permits in the same bucket naturally
    # rotate through the 3 variants per phrase.
    seed = _clean(raw.get('permit_number'))
    subscore_reasons = ai_phrases.compose_subscore_reasons(subs, seed=seed)
    return {
        'permit_number':    _clean(raw.get('permit_number')),
        'permit_type':      _clean(raw.get('permit_type'), 'Permit'),
        'description':      _clean(raw.get('description')),
        'status':           _clean(raw.get('status'), 'unknown').lower(),
        'applied_date':     _to_iso_date(raw.get('applied_date')),
        'issued_date':      _to_iso_date(raw.get('issued_date')),
        'expires_date':     _to_iso_date(raw.get('expires_date')),
        'address':          _clean(raw.get('address')),
        'city':             _clean(raw.get('city')).title(),
        'state':            _norm_state(raw.get('state')),
        'zip':              _clean(raw.get('zip')),
        'latitude':         raw.get('latitude') if isinstance(raw.get('latitude'), (int, float)) else None,
        'longitude':        raw.get('longitude') if isinstance(raw.get('longitude'), (int, float)) else None,
        # Parties — deterministic safety net enforcing the prompt rule
        # that (contractor_name, contractor_phone, contractor_email)
        # MUST describe the same real person, so the client calls/emails
        # the right contact. Two known LLM drift modes need fixing:
        #
        #   (A) Model files the Applicant's name under owner_name while
        #       leaving the Applicant's phone/email in contractor_*.
        #       Detected by: contractor_name empty + owner_name filled
        #       + (phone OR email) present. Fix: copy owner_name into
        #       contractor_name so the unit is whole.
        #
        #   (B) Model correctly fills contractor_name=owner but then
        #       wipes owner_name to '' (legacy swap behaviour). Fix:
        #       keep owner_name populated — the property owner is the
        #       same person, and downstream code expects owner_name
        #       to be present whenever an Owner block exists.
        #
        # Net rule applied here: if contractor info is incomplete but
        # the owner has the contact, copy owner_name → contractor_name
        # AND keep owner_name intact. The result is one aligned
        # contact triple plus an accurate owner_name.
        **(lambda: (lambda owner, contractor, phone, email: (
            (lambda final_contractor: {
                # owner_name is preserved verbatim — it is the property
                # owner regardless of whether they are also the contact.
                'owner_name':       owner,
                'contractor_name':  final_contractor,
                'contractor_phone': phone,
                'contractor_email': email,
                # Unified single-contact convenience fields used by
                # CRM push / notification templates downstream.
                'contact_name':     final_contractor or owner,
                'contact_type':     ('contractor' if final_contractor
                                     else ('owner' if owner else '')),
            })(
                # Whoever the phone/email belongs to gets named here.
                # If contractor_name is blank but a phone or email
                # was extracted, the only sane source is the owner —
                # copy the owner name in so the contact triple is
                # complete and aligned.
                contractor or ((owner if (phone or email) else '')),
            )
        ))(
            _clean(raw.get('owner_name')),
            _clean(raw.get('contractor_name')),
            _norm_phone(raw.get('contractor_phone')),
            _norm_email(raw.get('contractor_email')),
        ))(),
        'valuation_cents':  _to_int(raw.get('valuation_cents'), 0),
        'square_feet':      _to_int(raw.get('square_feet')),
        'trade':            trade,
        'ai_score':         score,
        'ai_grade':         grade,
        'ai_tier':          tier,
        'ai_model_version': (get_system_setting('claude_model') or DEFAULT_MODEL),
        'ai_scored_at':     datetime.utcnow(),
        # Sub-scores stored verbatim; per-factor reasons composed from
        # the local phrase library so the existing detail-modal UI
        # (which reads ai_subscore_reasons) renders unchanged.
        'ai_subscores':         subs,
        'ai_subscore_reasons':  subscore_reasons,
    }


# ─────────────────────────── City auto-register ───────────────────────

def _validate_and_register_city(city: str, state: str) -> bool:
    """If the (city, state) pair isn't in the supported_cities list,
    add it. Returns True if we just added a new entry."""
    c = _clean(city).title()
    s = _clean(state).upper()
    if not c or not s:
        return False
    existing = get_supported_cities()
    if any(x['city'].lower() == c.lower() and x['state'] == s for x in existing):
        return False
    try:
        added = add_supported_city(c, s)
        if added:
            log.info('scraper auto-registered new supported city: %s, %s', c, s)
        return added
    except Exception:
        log.exception('auto-register city failed for %s, %s', c, s)
        return False


# ─────────────────────────── Orchestration ────────────────────────────

def scrape_one(scraper: dict, url: str | None = None,
               *, run_id: int | None = None) -> dict:
    """Run the full pipeline for one Accela URL.

    Steps:
      1. Firecrawl scrape (markdown + html + Firecrawl-LLM json)
      2. If Firecrawl's `json` is non-empty → use it directly
         (skip the second Claude call — saves time and tokens).
      3. Otherwise → Claude extracts from the markdown.
      4. Normalise → register city → upsert_permit.
    Returns the upserted permit dict on success.
    """
    # If a run_id is provided and the thread isn't already inside a
    # _track_run scope, tag the API calls with it for the duration of
    # this scrape. (The cron worker sets this once for the whole run;
    # ad-hoc sync calls from views go through this branch.)
    if run_id is not None and _current_run_id() is None:
        with _track_run(run_id, source='accela'):
            return _scrape_one_inner(scraper, url, run_id=run_id)
    return _scrape_one_inner(scraper, url, run_id=run_id)


def _scrape_one_inner(scraper: dict, url: str | None = None,
                      *, run_id: int | None = None) -> dict:
    target_url = (url or scraper['url']).strip()
    fc = _http_fetch_page(target_url)
    fc_json = fc.get('json') or {}
    # The pure-HTTP fetcher only returns structured JSON for LIST
    # pages, so detail pages always fall through to Claude/OSS
    # extraction on the visible page text.
    has_min_fields = bool(
        (fc_json.get('permit_number') or '').strip()
        or (fc_json.get('address') or '').strip()
    )
    _scrape_one_inference_used = False
    _scrape_one_parse_method   = 'list-json'
    if has_min_fields:
        permit = _normalise_permit(fc_json)
    else:
        # Try the free parser first — only call Claude when contractor
        # email/phone can't be lifted out of the markdown.
        raw_permit, _scrape_one_inference_used, _scrape_one_parse_method = (
            _extract_with_fallback(
                fc['markdown'],
                source_url=target_url,
                run_id=run_id,
                ctx='scrape_one: ',
            )
        )
        permit = _normalise_permit(raw_permit)
    if not permit.get('permit_number'):
        # Fall back to the URL's capID composite so we don't lose a row
        # — better to upsert with a synthetic id than drop the result.
        parsed = parse_accela_url(target_url)
        composite = '-'.join(filter(None, [
            parsed.get('agency_code', ''),
            parsed.get('cap_id_1', ''),
            parsed.get('cap_id_2', ''),
            parsed.get('cap_id_3', ''),
        ]))
        permit['permit_number'] = composite or f'unknown-{int(time.time())}'

    # Source-id stable across reruns: the agency + cap composite.
    parsed = parse_accela_url(target_url)
    composite = '-'.join(filter(None, [
        parsed.get('agency_code', ''),
        parsed.get('cap_id_1', ''),
        parsed.get('cap_id_2', ''),
        parsed.get('cap_id_3', ''),
    ])) or permit['permit_number']

    permit['source']           = db._scraper_source_tag(scraper['id'])
    permit['source_permit_id'] = composite
    permit['jurisdiction']     = scraper.get('agency_code') or scraper.get('city') or ''
    # Stash a generous chunk of Firecrawl's raw markdown so the
    # admin can hit "View source" later and audit what the LLM saw.
    permit['raw'] = {
        'scraped_url':        target_url,
        'scraper_id':         scraper['id'],
        'scraper_name':       scraper.get('name'),
        'mode':               'detail',
        'fetched_at':         datetime.utcnow().isoformat() + 'Z',
        'markdown':           (fc.get('markdown') or '')[:80000],
        'firecrawl_json':     fc_json,
        'firecrawl_metadata': fc.get('metadata') or {},
        # Inference-savings audit — see _extract_with_fallback().
        'inference_used':     _scrape_one_inference_used,
        'parse_method':       _scrape_one_parse_method,
    }
    _validate_and_register_city(permit.get('city', ''), permit.get('state', ''))
    # Lineage: tag the permit with the run that created it (if any).
    # Sync paths sometimes call us without a run_id — that's fine,
    # the column is nullable.
    if run_id is not None:
        permit['scraper_run_id'] = int(run_id)
    upsert_permit(permit)
    return permit


def scrape_list(scraper: dict, url: str | None = None,
                *, run_id: int | None = None) -> list[dict]:
    """Scrape an Accela CapHome / search-results LIST URL.

    Fires ONE Firecrawl request that returns the rendered markdown
    PLUS a structured ``{permits: […]}`` JSON extraction. We then
    iterate the rows, upsert each, and return the list of upserted
    permit dicts so the caller (the worker thread) can tick a
    real progress bar.
    """
    if run_id is not None and _current_run_id() is None:
        with _track_run(run_id, source='accela'):
            return _scrape_list_inner(scraper, url, run_id=run_id)
    return _scrape_list_inner(scraper, url, run_id=run_id)


def _scrape_list_inner(scraper: dict, url: str | None = None,
                       *, run_id: int | None = None) -> list[dict]:
    target_url = (url or scraper['url']).strip()
    fc = _http_fetch_page(target_url, mode='list')
    fc_json = fc.get('json') or {}
    rows = fc_json.get('permits') if isinstance(fc_json, dict) else None
    if not isinstance(rows, list):
        rows = []
    md = (fc.get('markdown') or '')[:80000]

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        permit = _normalise_permit(row)
        # The permit_number is the only stable identifier we get from
        # a list row — without it the upsert key collides on
        # empty-string and the row would be dropped silently.
        pnum = (permit.get('permit_number') or '').strip()
        if not pnum:
            continue
        # Required identity tuple — `upsert_permit` keys on
        # (source, source_permit_id) and ALSO requires non-empty
        # state + city; missing any of these and the upsert returns
        # None without writing anything.
        permit['source']           = db._scraper_source_tag(scraper['id'])
        permit['source_permit_id'] = pnum
        permit['jurisdiction']     = scraper.get('agency_code') or scraper.get('city') or ''
        if not (permit.get('state') or '').strip():
            permit['state'] = (scraper.get('state') or '').strip()
        if not (permit.get('city') or '').strip():
            permit['city'] = (scraper.get('city') or '').strip()
        if not (permit.get('state') or '').strip() or not (permit.get('city') or '').strip():
            # We can't satisfy upsert_permit's required fields — skip
            # rather than pretend it succeeded.
            continue
        permit['raw'] = {
            'scraped_url':        target_url,
            'scraper_id':         scraper['id'],
            'scraper_name':       scraper.get('name'),
            'mode':               'list',
            'fetched_at':         datetime.utcnow().isoformat() + 'Z',
            # Save the page markdown ONCE per permit so View-source
            # works on every row of this run. Truncated to 80KB.
            'markdown':           md,
            'list_row':           row,
            'firecrawl_metadata': fc.get('metadata') or {},
        }
        _validate_and_register_city(permit.get('city', ''), permit.get('state', ''))
        if run_id is not None:
            permit['scraper_run_id'] = int(run_id)
        result = upsert_permit(permit)
        if result is None:
            # upsert_permit returns None when required keys are missing
            # — treat as a hard skip, don't add to the success bucket.
            continue
        out.append(permit)
    return out


# ─────────────────────────── Background runner ────────────────────────

# Cap how far the unified list-scrape walks Accela pagination. Even
# when URL increments DO work (rare), we don't want a runaway
# many-page run on the admin's first click — they can re-run with
# a tighter date filter for more pages.
_MAX_LIST_PAGES: int = 5


def _coerce_iso_date(v) -> str:
    """Best-effort ISO-date coercion. Accepts ``date``/``datetime``,
    a YYYY-MM-DD string, or a falsy value (returns '')."""
    if not v:
        return ''
    if isinstance(v, (date, datetime)):
        return v.strftime('%Y-%m-%d')
    s = str(v).strip()
    if not s:
        return ''
    iso = _to_iso_date(s)
    return iso or s[:10]


def _row_in_date_range(row: dict, df: str, dt: str) -> bool:
    """Post-extraction safety net for the date filter. The unified
    prompt asks the LLM to drop out-of-range rows itself, but LLMs
    occasionally include them anyway — so we re-check here on the
    Python side. A row with no parseable applied_date is kept (we
    don't want to silently drop rows just because the LLM missed a
    date column)."""
    if not df and not dt:
        return True
    raw = row.get('applied_date') or row.get('issued_date')
    iso = _to_iso_date(raw) if raw else None
    if not iso:
        return True
    if df and iso < df:
        return False
    if dt and iso > dt:
        return False
    return True


def _accela_paged_url(url: str, page_num: int) -> str:
    """Best-effort URL-based pagination for Accela list pages.

    Real Accela CapHome paging uses ASP.NET ``__doPostBack`` which we
    can't trigger without a real browser. As a fallback we set the
    most common URL parameter Accela honours when paging IS exposed
    in the query string (``PageNumber``). Most production Accela
    instances will simply ignore it and return page 1 again — the
    worker detects that via the dedupe set and stops.
    """
    if page_num <= 1:
        return url
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    qs['PageNumber'] = [str(page_num)]
    flat = [(k, v[0]) for k, v in qs.items()]
    new_q = urllib.parse.urlencode(flat, doseq=False)
    return urllib.parse.urlunparse(parsed._replace(query=new_q))


def _run_agent_branch(*, scraper: dict, run_id: int,
                      date_from=None, date_to=None,
                      max_pages: int | None = None) -> None:
    """Branch 0: hand the whole list+detail+extract job to Firecrawl
    Agent in a single SDK call, then upsert each returned permit.

    Reads the admin-saved agent settings (prompt template / model /
    max credits) from system_settings, falling back to the constants
    above. Streams progress to scraper_runs.step_log so the admin
    terminal panel still feels live (the Agent itself is a single
    long-poll call so we can't stream per-row progress *during* the
    fetch — but we always log start, the per-row upsert phase, and a
    final summary).
    """
    df = _coerce_iso_date(date_from)
    dt = _coerce_iso_date(date_to)

    # Pure-HTTP + DigitalOcean OSS path — no Firecrawl involved.
    saved_model = (get_system_setting('accela_scraper_agent_model') or '').strip()
    model = saved_model or ACCELA_SCRAPER_AGENT_DEFAULT_MODEL

    date_blurb = ''
    if df or dt:
        date_blurb = f' (date filter: {df or "begin"} → {dt or "today"})'
    append_scraper_run_step(
        run_id,
        f'▶ Starting agent scrape{date_blurb} (model={model})',
        'info',
    )
    update_scraper_run(
        run_id,
        status='running',
        current_step=f'agent browsing {scraper.get("url") or "the search page"}…',
    )

    # ── Pure-HTTP + DigitalOcean Serverless Inference path.
    # No third-party agent service, no Firecrawl API key required.
    # Searches via raw HTTP (proxy-aware), paginates through results,
    # opens each CapDetail page, and parses with the admin-chosen
    # DO Inference model (free-text from the DO Inference settings card).
    # Same envelope contract used previously so the surrounding
    # logging / upsert / failure handling is unchanged.
    from .scrapers.accela import oss_agent_scrape_permits

    # ── Per-permit immediate-upsert closures ──────────────────────────
    # Two callbacks plumbed into the scraper so each permit is durably
    # written the instant its LLM extraction finishes, AND so re-runs
    # skip permits we already have without burning a single LLM token
    # on them.
    #
    # Why closures instead of moving the logic into the scraper module:
    # the upsert phase needs DB access + the scraper's source/jurisdiction
    # context + live `update_scraper_run` writes, all of which already
    # live here. The scraper module stays a pure search+extract layer.
    source_tag_early = db._scraper_source_tag(scraper['id'])
    base_url_early   = scraper.get('url') or ''

    # State protected by upsert_lock (the scraper calls back from up
    # to 4 worker threads in parallel).
    upsert_lock     = threading.Lock()
    upsert_stats: dict = {
        'inserted':  0,
        'updated':   0,
        'cross_dup': 0,
        'failed':    0,
        'errors':    [],
        'seen':      set(),   # in-batch dedup (source|permit_number)
        'processed': 0,
    }

    def _is_already_inserted(permit_number: str) -> bool:
        """Cheap pre-extraction skip: drop permits already in DB AND
        permits we previously proved junk (no contractor email/phone),
        so a re-run only pays Firecrawl + LLM tokens for genuinely-new
        rows. Pre-junk-table this loop re-fetched + re-extracted every
        junk row on every re-scrape — burnt $230 of inference in one
        day before the user caught it.
        """
        try:
            row = pg.query_one(
                "SELECT 1 FROM permits "
                "WHERE source = %s AND source_permit_id = %s LIMIT 1",
                (source_tag_early, permit_number),
            )
            if row is not None:
                return True
        except Exception:
            log.exception('is_already_inserted check failed for %s',
                          permit_number)
            return False
        # Junk-table check — same (source, permit_number) lineage key
        # the gate writes from ``upsert_permit``. Plus a triple-key
        # fallback in case the same municipal permit was ingested under
        # a sibling scraper id and proved junk there too.
        try:
            if db.is_junk_permit(source_tag_early, permit_number):
                return True
            _st = (scraper.get('state') or '').strip()
            _ct = (scraper.get('city')  or '').strip()
            if _st and _ct and db.is_junk_permit_by_number(
                    permit_number, _st, _ct):
                return True
        except Exception:
            log.exception('is_junk_permit check failed for %s',
                          permit_number)
        return False

    def _on_permit_extracted(raw: dict, grid_row: dict) -> None:
        """Upsert one permit IMMEDIATELY after its LLM extraction
        completes — record-by-record durability so a Stop / crash
        later in the run never loses already-extracted data."""
        if not isinstance(raw, dict):
            return
        try:
            permit = _normalise_permit(raw)
            pnum = (permit.get('permit_number') or '').strip()
            if not pnum:
                raise ScraperError('missing permit_number')

            # Identity / provenance backfill (mirrors the old
            # post-loop logic, kept verbatim so behaviour is
            # unchanged from the admin's POV).
            permit['source']           = source_tag_early
            permit['source_permit_id'] = pnum
            permit['jurisdiction']     = (scraper.get('agency_code')
                                          or scraper.get('city') or '')
            if not (permit.get('state') or '').strip():
                permit['state'] = (scraper.get('state') or '').strip()
            if not (permit.get('city') or '').strip():
                permit['city'] = (scraper.get('city') or '').strip()
            if not (permit.get('state') or '').strip():
                raise ScraperError(f'{pnum}: missing state '
                                   '(agent + scraper both empty)')
            if not (permit.get('city') or '').strip():
                raise ScraperError(f'{pnum}: missing city '
                                   '(agent + scraper both empty)')

            local_key = f'{source_tag_early}|{pnum}'
            with upsert_lock:
                if local_key in upsert_stats['seen']:
                    # In-batch dup (same row on two pagination
                    # pages) — skip silently, no double-count.
                    return
                upsert_stats['seen'].add(local_key)

            # Pull the LLM debug payload off the parsed dict before we
            # serialise it as `agent_extracted` — we want it in the
            # row's `raw` envelope under its own key (so the admin
            # View modal can show "input we sent / output we got"),
            # NOT mixed back into the model's structured JSON output.
            llm_debug = raw.pop('__llm_debug', None) if isinstance(raw, dict) else None
            _ = permit.pop('__llm_debug', None)

            permit['raw'] = {
                'agent_extracted': raw,
                'list_url':        base_url_early,
                'detail_url':      raw.get('detail_url') or '',
                'scraper_id':      scraper['id'],
                'scraper_name':    scraper.get('name'),
                'mode':            'agent',
                'fetched_at':      datetime.utcnow().isoformat() + 'Z',
                'llm_debug':       llm_debug or {},
            }
            _validate_and_register_city(permit.get('city', ''),
                                        permit.get('state', ''))
            permit['scraper_run_id'] = int(run_id)

            result = upsert_permit(permit)
            if result is None:
                raise ScraperError(
                    f'{pnum}: not saved — {_upsert_skip_text(permit)}')
            action, _pid = result

            # Pull the row we just touched so we can tell the admin
            # whether this insert "won" the row (action=inserted) or
            # whether it got cross-source-deduped onto an existing
            # row owned by a different scraper — the latter is the #1
            # reason "parsed 5 / table shows 0 new" happens, and
            # before this log line that outcome was completely
            # invisible (the run log only said "parsed", the table
            # filtered by source showed nothing new, and the admin
            # was left guessing).
            cross_hit = False
            existing_src = ''
            existing_pid = ''
            if action == 'updated':
                existing = pg.query_one(
                    'SELECT source, source_permit_id FROM permits WHERE id = %s',
                    (int(_pid),),
                )
                if existing:
                    existing_src = (existing.get('source') or '')
                    existing_pid = (existing.get('source_permit_id') or '')
                    if existing_src and existing_src != source_tag_early:
                        cross_hit = True

            with upsert_lock:
                if action == 'inserted':
                    upsert_stats['inserted'] += 1
                else:
                    upsert_stats['updated'] += 1
                if cross_hit:
                    upsert_stats['cross_dup'] += 1
                upsert_stats['processed'] += 1
                snap = (upsert_stats['inserted']
                        + upsert_stats['updated'],
                        upsert_stats['failed'])

            # Per-permit DB-outcome log so the admin can see what
            # actually happened to every parsed row. Three outcomes:
            #   • inserted        — brand new row owned by this scraper
            #   • updated         — refreshed an existing row from THIS scraper
            #   • cross-src dup   — merged into an existing row owned by
            #                       a DIFFERENT scraper (won't show up
            #                       under this scraper's filter)
            if action == 'inserted':
                outcome = '＋ inserted'
                outcome_lvl = 'ok'
            elif cross_hit:
                outcome = (f'⇆ cross-src dup → merged into '
                           f'{existing_src}:{existing_pid} '
                           f'(row id {_pid}) — won\'t appear under '
                           f'this scraper\'s filter')
                outcome_lvl = 'warn'
            else:
                outcome = f'↻ updated (row id {_pid})'
                outcome_lvl = 'ok'
            try:
                append_scraper_run_step(
                    run_id,
                    f'    └─ {pnum}: {outcome}',
                    outcome_lvl,
                )
            except Exception:
                pass
            # Tick the run row OUTSIDE the lock so DB latency on the
            # status update never serialises the worker threads.
            try:
                update_scraper_run(
                    run_id,
                    succeeded=snap[0],
                    failed=snap[1],
                )
            except Exception:
                pass
        except Exception as e:
            log.exception('per-permit immediate upsert failed')
            with upsert_lock:
                upsert_stats['failed'] += 1
                upsert_stats['errors'].append({
                    'url':   (raw.get('detail_url') or base_url_early),
                    'error': str(e)[:300],
                    'when':  datetime.utcnow().isoformat() + 'Z',
                })
                if len(upsert_stats['errors']) > 50:
                    upsert_stats['errors'] = upsert_stats['errors'][-50:]
                snap = (upsert_stats['inserted']
                        + upsert_stats['updated'],
                        upsert_stats['failed'])
            try:
                append_scraper_run_step(
                    run_id,
                    f'  ✗ {(raw.get("permit_number") or "?")}: '
                    f'upsert failed: {str(e)[:140]}',
                    'err',
                )
            except Exception:
                pass
            try:
                update_scraper_run(
                    run_id,
                    succeeded=snap[0],
                    failed=snap[1],
                )
            except Exception:
                pass

    # Page-cap: either the explicit per-run override from the admin UI
    # ("# of pages" input next to Run now) or the module default (50).
    from .scrapers.accela import ACCELA_MAX_PAGES_DEFAULT as _MP_DEFAULT
    effective_max_pages = int(max_pages) if max_pages else _MP_DEFAULT

    def _on_permit_junk(permit: dict, grid_row: dict) -> None:
        """The scraper's in-flight 'no email AND no phone' gate just
        dropped this row. Record the verdict in `junk_permits` so the
        NEXT run's pre-detail skip loop drops this permit_number
        without re-paying fetch_detail + LLM tokens — the actual
        $230/day fix. The earlier `upsert_permit` gate never sees
        these rows (the scraper-side gate `continue`s before invoking
        the upsert callback), so this is the only place to write the
        junk record.
        """
        try:
            permit = permit if isinstance(permit, dict) else {}
            grid_row = grid_row if isinstance(grid_row, dict) else {}
            pnum = ((permit.get('permit_number') or
                     grid_row.get('permit_number') or '').strip())
            if not pnum:
                return
            durl = ((grid_row.get('detail_url') or
                     permit.get('detail_url') or '').strip())
            st = ((permit.get('state') or scraper.get('state') or '').strip())
            ct = ((permit.get('city')  or scraper.get('city')  or '').strip())
            db.mark_junk_permit(
                source_tag_early, pnum,
                permit_number=pnum, state=st, city=ct,
                detail_url=durl, reason='no_contact',
            )
        except Exception:
            log.exception('mark_junk_permit failed for %s',
                          (permit or {}).get('permit_number'))

    envelope = oss_agent_scrape_permits(
        scraper=scraper,
        date_from=df,
        date_to=dt,
        model=model,
        max_pages=effective_max_pages,
        scraper_run_id=run_id,
        is_already_inserted=_is_already_inserted,
        on_permit_extracted=_on_permit_extracted,
        on_permit_junk=_on_permit_junk,
    )
    permits_raw = envelope.get('permits') or []
    agent_log   = envelope.get('log') or {}
    credits_used = agent_log.get('credits_used')
    latency_ms   = agent_log.get('latency_ms')
    err          = envelope.get('error')

    cu_blurb = ''
    if credits_used is not None:
        cu_blurb = f' · {credits_used} credits used'
    if isinstance(latency_ms, int):
        cu_blurb += f' · {latency_ms / 1000:.1f}s'

    if err:
        append_scraper_run_step(
            run_id,
            f'⚠ Agent reported: {err[:300]}{cu_blurb}',
            'warn',
        )
    if not permits_raw:
        no_row_level = 'warn' if err else 'info'
        append_scraper_run_step(
            run_id,
            f'ℹ Agent returned 0 permit rows{cu_blurb}.',
            no_row_level,
        )
        no_row_status = 'failed' if err else 'success'
        update_scraper_run(
            run_id,
            total_targets=0,
            processed=0,
            succeeded=0,
            failed=0,
            current_step='done — 0 rows',
            error=([{'url':   scraper.get('url') or '',
                     'error': err[:300],
                     'when':  datetime.utcnow().isoformat() + 'Z'}]
                   if err else None),
        )
        refresh_scraper_total_permits(scraper['id'])
        update_scraper_run(run_id, status=no_row_status,
                           finished_at=datetime.utcnow(),
                           current_step='done — 0 rows')
        update_scraper(scraper['id'], last_run_at=datetime.utcnow(),
                       last_run_status=no_row_status)
        return

    n = len(permits_raw)

    # ── Per-permit upsert is already DONE ─────────────────────────────
    # Each row in `permits_raw` was upserted by `_on_permit_extracted`
    # the instant its LLM extraction returned (see closures above), so
    # the historical "upserting N permits…" post-loop is unnecessary
    # here — it would only re-do idempotent work and double-count
    # against the live counters. We just read the final stats off the
    # closure's `upsert_stats` dict and write the run summary.
    with upsert_lock:
        inserted  = upsert_stats['inserted']
        updated   = upsert_stats['updated']
        cross_dup = upsert_stats['cross_dup']
        failed    = upsert_stats['failed']
        errors    = list(upsert_stats['errors'])
    succeeded = inserted + updated

    if errors:
        update_scraper_run(run_id, error=errors)

    append_scraper_run_step(
        run_id,
        f'📊 Agent returned {n} permit row(s){cu_blurb} — '
        f'{succeeded} already saved record-by-record during extraction.',
        'ok',
    )

    summary = (
        f'🏁 Done — {succeeded} saved '
        f'({inserted} new, {updated} updated, {cross_dup} cross-src dup), '
        f'{failed} failed{cu_blurb}'
    )
    append_scraper_run_step(
        run_id, summary,
        'ok' if failed == 0 and succeeded > 0 else
        ('warn' if succeeded else 'err'),
    )

    refresh_scraper_total_permits(scraper['id'])
    cancelled = is_cancel_requested(run_id)
    if cancelled:
        final_status = 'cancelled'
    else:
        final_status = (
            'success' if failed == 0 and succeeded > 0 else
            'failed'  if succeeded == 0 else
            'partial'
        )
    update_scraper_run(
        run_id,
        status=final_status,
        finished_at=datetime.utcnow(),
        succeeded=succeeded,
        failed=failed,
        current_step=(f'cancelled — {succeeded} ok, {failed} failed'
                      if cancelled
                      else f'done — {succeeded} ok, {failed} failed'),
    )
    update_scraper(
        scraper['id'],
        last_run_at=datetime.utcnow(),
        last_run_status=final_status,
    )


def _run_worker(scraper_id: int, run_id: int, *, mode: str,
                count: int | None = None,
                date_from=None, date_to=None,
                max_pages: int | None = None) -> None:
    """The body of the daemon thread. Catches everything so a per-URL
    failure can never bring the process down."""
    # Stamp pid + thread ident on the run row + register the live thread
    # in the in-process map so the admin Force-stop endpoint can target
    # us. Best-effort — never block the run on bookkeeping I/O.
    import os as _os
    cur_thread = threading.current_thread()
    tid = cur_thread.ident or 0
    pid = _os.getpid()
    try:
        update_scraper_run(run_id, worker_pid=pid, worker_tid=int(tid))
    except Exception:
        pass
    with _RUNNING_THREADS_LOCK:
        _RUNNING_THREADS[int(run_id)] = cur_thread
    # Heartbeat: a tiny daemon thread bumps `heartbeat_at` every 15s
    # so any other process (or this one after a restart) can tell the
    # difference between a live worker and a dead row whose pid was
    # reused by the OS. Stopped via Event so we never linger past the
    # main worker.
    hb_stop = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop, args=(int(run_id), hb_stop),
        name=f'scraper-run-{run_id}-heartbeat', daemon=True,
    )
    hb_thread.start()
    try:
        with _track_run(run_id, source='accela'):
            return _run_worker_body(
                scraper_id, run_id, mode=mode, count=count,
                date_from=date_from, date_to=date_to,
                max_pages=max_pages,
            )
    finally:
        hb_stop.set()
        with _RUNNING_THREADS_LOCK:
            _RUNNING_THREADS.pop(int(run_id), None)
        # ── Guaranteed reconcile of the scraper row ───────────────
        # Every branch inside `_run_worker_body` is *supposed* to
        # call `_finalize()` on its way out, but exception paths
        # (agent crash, network blow-up, OOM, KeyboardInterrupt)
        # have historically leaked: the run row gets marked failed,
        # but the parent `scrapers.last_run_status` stays 'running'
        # and `total_permits` is never recomputed — so the
        # `/admin-panel/scrapers/` grid keeps showing 0 forever
        # even though hundreds of permits were upserted.
        # This outer `finally` is the last-chance reconcile: it
        # always recounts permits for this scraper and clears any
        # stuck 'running' status to match the actual run row.
        try:
            refresh_scraper_total_permits(scraper_id)
        except Exception:
            log.exception('post-run total_permits refresh failed for scraper_id=%s', scraper_id)
        try:
            run_row = db.get_scraper_run(int(run_id)) or {}
            run_status = (run_row.get('status') or '').strip().lower()
            sc_row = get_scraper(scraper_id) or {}
            sc_status = (sc_row.get('last_run_status') or '').strip().lower()
            if sc_status == 'running':
                # Mirror whatever the run row ended up with; default
                # to 'failed' if the run row is also stuck (worker
                # died before finalising).
                final = run_status if run_status in (
                    'success', 'partial', 'failed', 'cancelled'
                ) else 'failed'
                update_scraper(scraper_id,
                               last_run_at=datetime.utcnow(),
                               last_run_status=final)
        except Exception:
            log.exception('post-run last_run_status reconcile failed for scraper_id=%s', scraper_id)


def _run_worker_body(scraper_id: int, run_id: int, *, mode: str,
                     count: int | None = None,
                     date_from=None, date_to=None,
                     max_pages: int | None = None) -> None:
    scraper = get_scraper(scraper_id)
    if not scraper:
        update_scraper_run(run_id, status='failed',
                           current_step='scraper not found',
                           finished_at=datetime.utcnow())
        return

    update_scraper_run(run_id, status='running',
                       current_step='preparing targets')
    update_scraper(scraper_id, last_run_status='running')

    succeeded = 0
    failed = 0
    errors: list[dict] = []
    # Per-run counters for the parse-first / Claude-fallback extractor.
    # Mutated by the inner detail loop via closure (see the call to
    # ``_extract_with_fallback`` below); emitted as a summary line in
    # ``_finalize`` so the admin sees the inference savings for the run.
    _ext_stats = {'parsed': 0, 'claude': 0}

    def _finalize() -> None:
        refresh_scraper_total_permits(scraper_id)
        # Inference-savings summary — one line per finished run, in both
        # the CLI log and the per-run log tab.
        total_ext = _ext_stats['parsed'] + _ext_stats['claude']
        if total_ext:
            pct = (_ext_stats['parsed'] * 100) // total_ext
            summary = (
                f'💸 Inference savings · '
                f'{_ext_stats["parsed"]}/{total_ext} permits parsed '
                f'without Claude ({pct}% token-save) · '
                f'{_ext_stats["claude"]} required LLM fallback'
            )
            log.info('accela-parse %s', summary)
            try:
                append_scraper_run_step(run_id, summary, 'ok')
            except Exception:
                pass
        final_status = (
            'success' if failed == 0 and succeeded > 0 else
            'failed'  if succeeded == 0 else
            'partial'
        )
        update_scraper_run(
            run_id,
            status=final_status,
            finished_at=datetime.utcnow(),
            current_step=f'done — {succeeded} ok, {failed} failed',
        )
        update_scraper(
            scraper_id,
            last_run_at=datetime.utcnow(),
            last_run_status=final_status,
        )

    # ─── Branch 0: single-mode via the Firecrawl Agent ─────────────
    # New default for the per-scraper "Run now" button. ONE agent
    # call replaces the old list-fetch + per-row Claude pipeline:
    # the agent autonomously paginates, opens each detail page and
    # returns a structured permits[] array we can upsert directly.
    # Set system_setting `accela_scraper_use_agent` = 'off' to fall
    # through to the legacy code path below.
    _agent_pref = (get_system_setting('accela_scraper_use_agent') or 'on').strip().lower()
    if mode in ('single', 'cron') and _agent_pref != 'off':
        try:
            _run_agent_branch(
                scraper=scraper, run_id=run_id,
                date_from=date_from, date_to=date_to,
                max_pages=max_pages,
            )
        except Exception as exc:
            # Surface the real exception class + message + traceback
            # into the run row so the admin can diagnose without
            # tailing the DO App Platform server log (which rolls
            # over fast). Without this, an upsert / glue-code failure
            # after a successful 100-permit parse looks identical to
            # a network timeout and we lose all the parsed payload.
            import traceback
            tb_text = traceback.format_exc()
            exc_class = type(exc).__name__
            exc_msg   = str(exc) or '(no message)'
            log.exception('scraper run %s — agent branch crashed', run_id)
            append_scraper_run_step(
                run_id,
                f'✗ Fatal: agent branch crashed — {exc_class}: '
                f'{exc_msg[:240]} — finalising the run',
                'err',
            )
            # Stash the last ~3 KB of the traceback in the run row so
            # the Run Details modal can render it. Capped to keep the
            # JSONB column small.
            update_scraper_run(
                run_id,
                error=[{'url':       scraper.get('url') or '',
                        'error':     f'{exc_class}: {exc_msg[:300]}',
                        'traceback': tb_text[-3000:],
                        'when':      datetime.utcnow().isoformat() + 'Z'}],
                current_step=f'crashed — {exc_class}',
            )
            update_scraper(scraper_id, last_run_at=datetime.utcnow(),
                           last_run_status='failed')
            update_scraper_run(run_id, status='failed',
                               finished_at=datetime.utcnow())
            return
        # _run_agent_branch handles its own _finalize-equivalent.
        return

    # ─── Branch 1: single-mode against a LIST URL (CapHome etc.) ───
    # Unified flow: paginate the list (best-effort URL increment) →
    # date-filter rows → per-row open the CapDetail page and have
    # Claude do a full extraction → upsert. Every meaningful step
    # gets appended to step_log so the admin terminal panel renders
    # a CLI-style transcript in real time.
    if mode in ('single', 'cron') and is_accela_list_url(scraper['url']):
        list_url = scraper['url']
        df = _coerce_iso_date(date_from)
        dt = _coerce_iso_date(date_to)

        date_blurb = ''
        if df or dt:
            date_blurb = f' (date filter: {df or "begin"} → {dt or "today"})'
        append_scraper_run_step(
            run_id,
            f'▶ Starting unified list scrape{date_blurb}', 'info',
        )

        try:
            page_targets, seen_pnums, all_rows = [], set(), []
            for page_num in range(1, _MAX_LIST_PAGES + 1):
                page_url = _accela_paged_url(list_url, page_num) if page_num > 1 else list_url
                append_scraper_run_step(
                    run_id,
                    f'📋 Fetching list page {page_num} via HTTP…',
                    'info',
                )
                update_scraper_run(
                    run_id,
                    current_step=f'fetching list page {page_num}…',
                )
                try:
                    fc_list = _http_fetch_page(
                        page_url, mode='list',
                        date_from=df, date_to=dt,
                    )
                except ScraperError as e:
                    append_scraper_run_step(
                        run_id,
                        f'⚠ Page {page_num} fetch failed: {str(e)[:160]}',
                        'warn',
                    )
                    if page_num == 1:
                        raise
                    break

                fc_json = fc_list.get('json') or {}
                page_rows = fc_json.get('permits') if isinstance(fc_json, dict) else None
                if not isinstance(page_rows, list):
                    page_rows = []
                # Filter: dedupe across pages + drop out-of-date rows.
                # IMPORTANT: pagination decisions MUST be based on raw
                # row counts (true exhaustion vs URL-paginator-ignored),
                # NOT on the post-date-filter count — page 1 might be
                # entirely out-of-range while page 2 contains keepers.
                kept_this_page = []
                dropped_dup, dropped_date = 0, 0
                raw_row_count = 0  # rows we actually saw in the markdown
                new_row_count = 0  # rows whose permit# we hadn't seen
                for row in page_rows:
                    if not isinstance(row, dict):
                        continue
                    raw_row_count += 1
                    pnum = (row.get('permit_number') or '').strip()
                    if pnum and pnum in seen_pnums:
                        dropped_dup += 1
                        continue
                    new_row_count += 1
                    if not _row_in_date_range(row, df, dt):
                        dropped_date += 1
                        # Still mark it seen so we don't re-process the
                        # same row if the URL paginator returns it again.
                        if pnum:
                            seen_pnums.add(pnum)
                        continue
                    if pnum:
                        seen_pnums.add(pnum)
                    kept_this_page.append(row)
                    page_targets.append((page_num, page_url, row, fc_list))
                msg = (f'✓ Page {page_num}: {raw_row_count} row(s), '
                       f'kept {len(kept_this_page)}')
                if dropped_dup:
                    msg += f', {dropped_dup} dup'
                if dropped_date:
                    msg += f', {dropped_date} out-of-range'
                append_scraper_run_step(
                    run_id, msg,
                    'ok' if kept_this_page else 'warn',
                )
                all_rows.extend(kept_this_page)

                # Pagination stop conditions (date filter intentionally
                # NOT consulted here — see comment above):
                #   (a) page returned 0 raw rows → list exhausted
                #   (b) page returned rows but every one was a dup of
                #       a previous page → the URL ?PageNumber=N param
                #       isn't honoured by this Accela instance (some
                #       CapHome screens use POST __doPostBack which we
                #       can't follow without a real browser); no point
                #       fetching another identical page.
                if raw_row_count == 0:
                    append_scraper_run_step(
                        run_id,
                        ('ℹ Page 1 returned 0 rows — stopping.'
                         if page_num == 1
                         else f'ℹ Page {page_num} returned 0 rows — list exhausted.'),
                        'info' if page_num > 1 else 'warn',
                    )
                    break
                if page_num > 1 and new_row_count == 0:
                    append_scraper_run_step(
                        run_id,
                        f'ℹ Page {page_num} repeated page 1 — Accela '
                        'is using JS-only paging which we cannot '
                        'follow. Stopping.',
                        'info',
                    )
                    break

            n = len(page_targets)
            update_scraper_run(
                run_id,
                total_targets=max(n, 1),
                current_step=f'parsing {n} permit(s) across pages…',
            )
            append_scraper_run_step(
                run_id,
                f'📊 Total to process: {n} permit(s) across '
                f'{min(page_num, _MAX_LIST_PAGES)} page(s)',
                'info',
            )

            if n == 0:
                errors.append({
                    'url':   list_url,
                    'error': 'List page parsed but produced 0 rows after '
                             'date filtering. Widen the date range, edit '
                             'the extraction prompt, or open the URL in a '
                             'browser to verify it has visible permits.',
                    'when':  datetime.utcnow().isoformat() + 'Z',
                })
                update_scraper_run(run_id, error=errors)

            for j, (page_num, page_url, row, fc_list) in enumerate(page_targets, 1):
                pnum_log = (row.get('permit_number') or f'row-{j}').strip()
                update_scraper_run(
                    run_id,
                    processed=j - 1,
                    current_step=f'permit {j}/{n}: {pnum_log}',
                )
                try:
                    permit = _normalise_permit(row)
                    pnum = (permit.get('permit_number') or '').strip()
                    if not pnum:
                        raise ScraperError(f'row {j} missing permit_number')

                    # ── Per-row detail follow-up via Firecrawl + Claude ──
                    # If the list extractor surfaced a detail_url, fetch
                    # that page so we can fill in contractor info, dates,
                    # owner, valuation — fields the LIST table never
                    # shows. If anything goes wrong with the detail call
                    # we keep the partial list-row data rather than
                    # failing the whole permit.
                    detail_url = (row.get('detail_url') or '').strip()
                    detail_md = ''
                    detail_used = False
                    inference_used = False
                    parse_method = 'list-only'
                    if detail_url and detail_url.lower().startswith(('http://', 'https://')):
                        append_scraper_run_step(
                            run_id,
                            f'  → ({j}/{n}) {pnum}: opening detail page…',
                            'info',
                        )
                        try:
                            fc_detail = _http_fetch_page(detail_url, mode='detail')
                            detail_md = (fc_detail.get('markdown') or '')[:80000]
                            if detail_md:
                                # Parse-first / Claude-fallback. Logs the
                                # decision (parser-hit vs claude) to both
                                # stdout and the per-run log tab so the
                                # admin can audit the token-savings.
                                extracted, inference_used, parse_method = (
                                    _extract_with_fallback(
                                        detail_md,
                                        source_url=detail_url,
                                        run_id=run_id,
                                        ctx=f'  ({j}/{n}) {pnum}: ',
                                    )
                                )
                                # Track per-run savings counters so we can
                                # emit a summary at the end (parsed vs
                                # inferred). Defined in the outer
                                # _scrape_list_inner scope (see init below).
                                if inference_used:
                                    _ext_stats['claude'] += 1
                                else:
                                    _ext_stats['parsed'] += 1
                                # Merge: detail-extracted data wins for
                                # any field it returned non-empty; list
                                # row stays as fallback.
                                merged = dict(permit)
                                for k, v in extracted.items():
                                    if v not in (None, '', [], {}):
                                        merged[k] = v
                                permit = merged
                                detail_used = True
                        except ScraperError as e:
                            append_scraper_run_step(
                                run_id,
                                f'  ⚠ {pnum}: detail fetch failed '
                                f'({str(e)[:100]}) — using list-row data',
                                'warn',
                            )

                    # ── Required identity for upsert_permit ───────────
                    # Without (source, source_permit_id, state, city) the
                    # upsert silently drops the row. Backfill from the
                    # scraper config when the extractor left them blank.
                    permit['source']           = db._scraper_source_tag(scraper['id'])
                    permit['source_permit_id'] = pnum
                    permit['jurisdiction']     = scraper.get('agency_code') or scraper.get('city') or ''
                    if not (permit.get('state') or '').strip():
                        permit['state'] = (scraper.get('state') or '').strip()
                    if not (permit.get('city') or '').strip():
                        permit['city'] = (scraper.get('city') or '').strip()
                    if not (permit.get('state') or '').strip():
                        raise ScraperError(f'row {j} missing state '
                                           '(extractor + scraper both empty)')
                    if not (permit.get('city') or '').strip():
                        raise ScraperError(f'row {j} missing city '
                                           '(extractor + scraper both empty)')
                    permit['raw'] = {
                        'scraped_url':        detail_url or page_url,
                        'list_url':           list_url,
                        'list_page':          page_num,
                        'scraper_id':         scraper['id'],
                        'scraper_name':       scraper.get('name'),
                        'mode':               'detail' if detail_used else 'list',
                        'detail_used':        detail_used,
                        'fetched_at':         datetime.utcnow().isoformat() + 'Z',
                        'markdown':           detail_md or (fc_list.get('markdown') or '')[:80000],
                        'list_row':           row,
                        'firecrawl_metadata': fc_list.get('metadata') or {},
                        # Inference-savings audit — set per-row by
                        # _extract_with_fallback (or stays at defaults
                        # when the row had no detail_url).
                        'inference_used':     inference_used,
                        'parse_method':       parse_method,
                    }
                    _validate_and_register_city(
                        permit.get('city', ''), permit.get('state', ''),
                    )
                    # Lineage: stamp the run id on every permit this run
                    # creates / updates so the admin can later list or
                    # bulk-delete just this run's output.
                    permit['scraper_run_id'] = int(run_id)
                    result = upsert_permit(permit)
                    if result is None:
                        raise ScraperError(
                            f'row {j} ({pnum}) not saved — '
                            f'{_upsert_skip_text(permit)}'
                        )
                    succeeded += 1
                    grade = permit.get('ai_grade') or '?'
                    score = permit.get('ai_score') or 0
                    src   = 'detail+claude' if detail_used else 'list-only'
                    append_scraper_run_step(
                        run_id,
                        f'  ✓ ({j}/{n}) {pnum} saved · {grade} ({score}) · {src}',
                        'ok',
                    )
                except Exception as e:
                    failed += 1
                    log.exception('list-row %s upsert failed', j)
                    errors.append({
                        'url':   (row.get('detail_url') or page_url) + f'#row-{j}',
                        'error': str(e)[:300],
                        'when':  datetime.utcnow().isoformat() + 'Z',
                    })
                    if len(errors) > 50:
                        errors = errors[-50:]
                    append_scraper_run_step(
                        run_id,
                        f'  ✗ ({j}/{n}) {pnum_log} failed: {str(e)[:140]}',
                        'err',
                    )
                update_scraper_run(run_id,
                                   processed=j,
                                   succeeded=succeeded,
                                   failed=failed,
                                   error=errors)

            append_scraper_run_step(
                run_id,
                f'🏁 Done — {succeeded} saved, {failed} failed',
                'ok' if failed == 0 else ('warn' if succeeded else 'err'),
            )
        except Exception as e:
            failed += 1
            log.exception('scraper run %s — list-page fetch failed', run_id)
            errors.append({
                'url':   scraper['url'],
                'error': str(e)[:300],
                'when':  datetime.utcnow().isoformat() + 'Z',
            })
            append_scraper_run_step(
                run_id,
                f'✗ Fatal: list fetch failed — {str(e)[:200]}',
                'err',
            )
            update_scraper_run(run_id,
                               processed=0,
                               failed=failed,
                               error=errors,
                               current_step=f'list fetch failed: {str(e)[:80]}')
        _finalize()
        return

    # ─── Branch 2: detail URLs (single + backfill + cron) ──────────
    targets: list[str] = []
    if mode == 'single':
        targets = [scraper['url']]
    else:
        # backfill or cron — enumerate cap_id_3 backwards from the
        # template's stored cap_id_3 by `count` steps. Accela CAP IDs
        # are roughly time-ordered so the most recent N IDs are a
        # decent proxy for "the last N permits".
        if is_accela_list_url(scraper['url']):
            failed += 1
            errors.append({
                'url':   scraper['url'],
                'error': 'Backfill walks the cap_id_3 sequence backwards — '
                         'that only works on CapDetail.aspx URLs. Configure '
                         'this scraper with a single-permit URL, or use '
                         '"Run now" to scrape the list page in place.',
                'when':  datetime.utcnow().isoformat() + 'Z',
            })
            update_scraper_run(
                run_id,
                processed=0,
                total_targets=0,
                failed=failed,
                error=errors,
                current_step='backfill needs a CapDetail.aspx URL, not a list URL',
            )
            # Route through _finalize so scraper.last_run_at /
            # last_run_status get refreshed and admin metadata stays
            # consistent with the run row.
            _finalize()
            return
        n = max(1, min(int(count or 20), 500))
        parsed = parse_accela_url(scraper['url'])
        try:
            base = int((parsed.get('cap_id_3') or '0').lstrip('0') or '0')
        except (TypeError, ValueError):
            base = 0
        for offset in range(n):
            cid = base - offset
            if cid <= 0:
                break
            targets.append(build_accela_url(scraper['url'], cap_id_3=cid))

    update_scraper_run(run_id,
                       total_targets=len(targets),
                       current_step=f'fetching {len(targets)} target(s)')

    for i, url in enumerate(targets, 1):
        update_scraper_run(run_id,
                           processed=i - 1,
                           current_step=f'scraping {i}/{len(targets)}…')
        try:
            scrape_one(scraper, url)
            succeeded += 1
        except Exception as e:
            failed += 1
            log.exception('scraper run %s failed for url %s', run_id, url)
            errors.append({
                'url':   url,
                'error': str(e)[:300],
                'when':  datetime.utcnow().isoformat() + 'Z',
            })
            if len(errors) > 50:
                errors = errors[-50:]
        update_scraper_run(run_id,
                           processed=i,
                           succeeded=succeeded,
                           failed=failed,
                           error=errors)
    _finalize()


def run_scraper_async(scraper_id: int, *, mode: str = 'single',
                      kind: str = 'manual',
                      count: int | None = None,
                      date_from=None, date_to=None,
                      max_pages: int | None = None) -> int:
    """Kick off a scraper run in a daemon thread and return the run_id
    immediately so the UI can begin polling the progress endpoint.

    `max_pages` (optional) caps how many list pages the agent branch
    will walk. Default behaviour (None) defers to
    `ACCELA_MAX_PAGES_DEFAULT` (currently 50). The admin "Run now" UI
    exposes this as a numeric override per run."""
    if mode not in ('single', 'backfill', 'cron'):
        raise ScraperError(f'unknown mode: {mode}')
    targets_estimate = 1 if mode == 'single' else max(1, int(count or 20))
    run_id = create_scraper_run(
        scraper_id,
        kind=kind,
        mode=mode,
        total_targets=targets_estimate,
        date_from=date_from,
        date_to=date_to,
    )
    t = threading.Thread(
        target=_run_worker,
        kwargs={
            'scraper_id': scraper_id,
            'run_id':     run_id,
            'mode':       mode,
            'count':      count,
            'date_from':  date_from,
            'date_to':    date_to,
            'max_pages':  max_pages,
        },
        daemon=True,
        name=f'scraper-run-{run_id}',
    )
    t.start()
    return run_id


def run_scraper_now(scraper_id: int, *,
                    max_pages: int | None = None,
                    kind: str = 'manual') -> int:
    """Synchronous foreground variant of `run_scraper_async` for the
    "single + agent branch" path. Creates the run row, runs the worker
    body INLINE (no daemon thread) and returns the run_id once done.

    Exists so the admin "Script command" panel can advertise a CLI
    invocation that actually completes when copy/pasted into
    ``manage.py shell -c "..."`` — the async variant returns
    immediately, which would let the shell process exit before the
    daemon worker thread had a chance to do anything.
    """
    run_id = create_scraper_run(scraper_id, kind=kind, mode='single',
                                total_targets=1)
    _run_worker(scraper_id=scraper_id, run_id=run_id, mode='single',
                max_pages=max_pages)
    return run_id


def run_scraper_sync(scraper_id: int, *, kind: str = 'manual') -> dict:
    """Synchronous single-URL run — used by the 'Run now' admin button
    when the admin wants the result immediately rather than polling.
    Returns {run_id, status, succeeded, failed}."""
    scraper = get_scraper(scraper_id)
    if not scraper:
        raise ScraperError('scraper not found')
    run_id = create_scraper_run(scraper_id, kind=kind, mode='single',
                                total_targets=1)
    update_scraper_run(run_id, status='running',
                       current_step='scraping single URL')
    try:
        scrape_one(scraper, run_id=run_id)
        update_scraper_run(run_id, status='success',
                           processed=1, succeeded=1,
                           finished_at=datetime.utcnow(),
                           current_step='done — 1 ok, 0 failed')
        update_scraper(scraper_id,
                       last_run_at=datetime.utcnow(),
                       last_run_status='success')
        refresh_scraper_total_permits(scraper_id)
        return {'run_id': run_id, 'status': 'success',
                'succeeded': 1, 'failed': 0}
    except Exception as e:
        update_scraper_run(
            run_id, status='failed',
            processed=1, failed=1,
            finished_at=datetime.utcnow(),
            current_step=f'error: {e}'[:200],
            error=[{'url': scraper['url'], 'error': str(e)[:300],
                    'when': datetime.utcnow().isoformat() + 'Z'}],
        )
        update_scraper(scraper_id,
                       last_run_at=datetime.utcnow(),
                       last_run_status='failed')
        raise
