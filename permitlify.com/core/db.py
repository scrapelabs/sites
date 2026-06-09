"""
Postgres-backed data layer (Supabase).

Mirrors the public API previously implemented on top of TinyDB. Every record
is stored as a JSONB `data` column with materialized columns where queries
need them, and the row's SERIAL `id` becomes the `'id'` field every caller
expects.
"""
import hashlib
import json
import logging
import secrets
import threading
import time
from datetime import datetime, date, timedelta, timezone
from psycopg import errors as psycopg_errors
from psycopg.types.json import Json

from core import pg

log = logging.getLogger(__name__)


# ── helpers ─────────────────────────────────────────────────────────────

def _row_to_doc(row: dict | None, id_field: str = 'id') -> dict | None:
    """Merge a row of {id, ..., data: {...}} into a flat dict like the old TinyDB shape."""
    if row is None:
        return None
    data = row.get('data') or {}
    if isinstance(data, str):
        data = json.loads(data)
    out = dict(data)
    out['id'] = row[id_field]
    return out


def hash_password(password: str) -> str:
    salt = 'permitdaily_salt_v1'
    return hashlib.sha256(f'{salt}{password}'.encode()).hexdigest()


# ── lightweight in-process TTL cache for hot read-only admin queries ──
#
# Admin pages (`admin_dashboard`, `admin_users_view`, `admin_revenue_view`,
# `admin_cities_view`, every `_admin_base_ctx` call) hammer the same
# read-only queries on every navigation. Across coast-to-coast latency
# (DigitalOcean NYC1 ↔ Supabase us-west-1 ≈ 90 ms RTT) those round-trips
# dominate page load time even though the underlying queries are cheap.
#
# A short in-process TTL serves repeat reads from RAM. Every write path
# explicitly invalidates the relevant keys so admins never see stale
# data after they make a change. Per-worker cache is fine — the TTL is
# short enough that drift between workers is negligible.

_TTL_CACHE: dict[str, tuple[float, object]] = {}
_TTL_CACHE_LOCK = threading.Lock()
_TTL_DEFAULT_SECONDS = 30.0


def _ttl_get(key: str):
    with _TTL_CACHE_LOCK:
        entry = _TTL_CACHE.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() >= expires_at:
            _TTL_CACHE.pop(key, None)
            return None
        return value


def _ttl_set(key: str, value, ttl: float = _TTL_DEFAULT_SECONDS):
    with _TTL_CACHE_LOCK:
        _TTL_CACHE[key] = (time.time() + ttl, value)


def _ttl_invalidate_prefix(*prefixes: str):
    with _TTL_CACHE_LOCK:
        for k in list(_TTL_CACHE.keys()):
            if any(k == p or k.startswith(p + ':') for p in prefixes):
                _TTL_CACHE.pop(k, None)


def _invalidate_users_cache():
    """Wipe every cached aggregate that depends on the users table."""
    _ttl_invalidate_prefix(
        'all_users', 'plan_counts', 'city_counts',
        'joined_counts', 'total_users',
    )


def _invalidate_banned_cache():
    _ttl_invalidate_prefix('all_banned')


def _invalidate_permits_cache():
    """Drop every cached `get_recent_permits_for_cities` slice. Called by
    upsert_permit so a fresh scrape never serves rows older than the
    incoming batch on the /permits/ page."""
    _ttl_invalidate_prefix('permits_cities')


# ── users ───────────────────────────────────────────────────────────────

def create_user(email: str, password: str, name: str = '', plan: str = 'starter') -> dict | None:
    email = email.lower().strip()
    existing = pg.query_one("SELECT id FROM users WHERE email = %s", (email,))
    if existing:
        return None
    user = {
        'email': email,
        'password': hash_password(password),
        'name': name or email.split('@')[0].title(),
        'plan': plan,
        'cities': [],
        'avatar_initials': ''.join(w[0].upper() for w in (name or email).split()[:2]),
        'onboarding_complete': False,
        'subscription_active': False,
        'terms_accepted_at': None,
        # Stamped at signup so the admin "Joined" column / "new this month"
        # counter aren't blank for self-served users (seed users get this
        # explicitly; without it real signups showed "—" indefinitely).
        'joined': date.today().isoformat(),
        # Per-user Whop billing mode. New signups always default to 'prod' so
        # they hit live $29/$99/$249 plan IDs. Admins can flip individual
        # accounts to 'dev' (or bulk-flip many) from /admin-panel/users/ to
        # bill them against the $1 test plans instead. See core/whop.py.
        'whop_mode': 'prod',
    }
    row = pg.execute_returning(
        """INSERT INTO users (email, reset_token, data)
           VALUES (%s, NULL, %s)
           RETURNING id""",
        (email, Json(user)),
    )
    user['id'] = row['id']
    _invalidate_users_cache()
    return user


def authenticate_user(email: str, password: str):
    email = email.lower().strip()
    row = pg.query_one(
        "SELECT id, data FROM users WHERE email = %s AND data->>'password' = %s",
        (email, hash_password(password)),
    )
    return _row_to_doc(row)


def get_user_by_id(user_id: int):
    row = pg.query_one("SELECT id, data FROM users WHERE id = %s", (int(user_id),))
    return _row_to_doc(row)


def update_user(user_id: int, **fields) -> bool:
    """
    Atomic shallow-merge of `fields` into the user's JSONB doc, then re-sync
    the materialized email/reset_token columns from the resulting doc. The
    merge happens in a single SQL statement so concurrent updates to
    *different* keys do not clobber each other.

    Postgres caveat we got bitten by: in a plain
    ``UPDATE ... SET data = data || patch, reset_token = data->>'reset_token'``
    every SET expression sees the **OLD** row, so the materialized column
    ends up stamped with the *pre-merge* value — silently desyncing the
    lookup column from the JSONB. (Real-world fallout: forgot-password
    links coming back as "invalid" because the column the lookup query
    hits never received the freshly-minted token, and email changes that
    didn't propagate to the materialized email column.) Fix: compute the
    merged document in a sub-SELECT first and derive every materialized
    column from that single new value, all in one statement.
    """
    if not fields:
        return True
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    patch = dict(fields)
    if 'email' in patch and patch['email']:
        patch['email'] = str(patch['email']).lower().strip()
    row = pg.execute_returning(
        """UPDATE users u
              SET data        = sub.newdata,
                  email       = COALESCE(lower(NULLIF(sub.newdata->>'email','')), u.email),
                  reset_token = sub.newdata->>'reset_token'
             FROM (SELECT id, data || %s::jsonb AS newdata
                     FROM users
                    WHERE id = %s) sub
            WHERE u.id = sub.id
            RETURNING u.id""",
        (Json(patch), uid),
    )
    if row is not None:
        _invalidate_users_cache()
    return row is not None


def _backfill_missing_joined_dates(default_date: str = '2026-04-20') -> int:
    """One-shot, idempotent boot-time backfill: any user JSONB doc that
    is missing the ``joined`` key (or has it stored as NULL / empty
    string) gets stamped with ``default_date`` so the admin Users panel
    "Joined" column stops rendering as "—".

    Pre-PR-#110 signups (both email and Google) didn't set ``joined``,
    leaving them blank in /admin-panel/users/. We can't know the real
    join date for those rows, so we default to 2026-04-20 — a sensible
    "we shipped this fix on April 28th, assume they joined ~a week ago"
    placeholder. New signups now stamp ``joined: today`` at
    create_user / create_user_from_google time, so this is a true
    one-shot — once it's run on prod, every subsequent boot is a no-op.

    Returns the number of rows patched (mostly for tests / logs).
    """
    try:
        n = pg.execute(
            """UPDATE users
                  SET data = jsonb_set(data, '{joined}', to_jsonb(%s::text), true)
                WHERE COALESCE(data->>'joined', '') = ''""",
            (default_date,),
        )
        if n:
            _invalidate_users_cache()
        return int(n or 0)
    except Exception:
        log.exception("joined-date backfill failed (non-fatal)")
        return 0


def _repair_materialized_user_columns() -> int:
    """One-shot backfill: re-sync the materialized ``email`` and
    ``reset_token`` columns from JSONB. Repairs rows desynced by the
    pre-fix ``update_user`` (which read OLD-row values when computing
    those columns). Idempotent — once everything's in sync this is a
    no-op. Safe to run on every boot.

    Returns the number of rows repaired (mostly for tests / logs).
    """
    try:
        n = pg.execute(
            """UPDATE users
                  SET email       = COALESCE(lower(NULLIF(data->>'email','')), email),
                      reset_token = data->>'reset_token'
                WHERE COALESCE(lower(NULLIF(data->>'email','')), '')
                          IS DISTINCT FROM COALESCE(email, '')
                   OR COALESCE(data->>'reset_token', '')
                          IS DISTINCT FROM COALESCE(reset_token, '')"""
        )
        if n:
            _invalidate_users_cache()
        return int(n or 0)
    except Exception:
        log.exception("user-column repair failed (non-fatal)")
        return 0


def increment_user_field(user_id: int, field: str, amount: int = 1) -> int:
    """Atomic increment of an integer field stored inside the JSONB doc."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return 0
    row = pg.execute_returning(
        """UPDATE users
              SET data = jsonb_set(
                  data,
                  ARRAY[%s],
                  to_jsonb(COALESCE((data->>%s)::int, 0) + %s),
                  true)
            WHERE id = %s
            RETURNING (data->>%s)::int AS v""",
        (field, field, int(amount), uid, field),
    )
    if row is not None:
        _invalidate_users_cache()
    return int(row['v']) if row else 0


def get_all_users():
    """Full user list. Cached in-process for `_TTL_DEFAULT_SECONDS` so
    rapid admin navigation (overview → users → revenue → cities) reuses
    the same fetch instead of paying ~90 ms RTT to Supabase on every
    page. Every user write path invalidates this entry."""
    cached = _ttl_get('all_users')
    if cached is not None:
        return cached
    rows = pg.query("SELECT id, data FROM users ORDER BY id")
    docs = [_row_to_doc(r) for r in rows]
    _ttl_set('all_users', docs)
    return docs


def count_users_by_plan(admin_emails: tuple = ()) -> dict:
    """SQL-side `GROUP BY plan` count. Used by admin pages that only
    need plan totals (dashboard, revenue) so they can skip the full
    user table fetch entirely. 30 s TTL cached.

    Users whose ``subscription_active`` flag is anything other than
    ``true`` are bucketed under the synthetic ``no_plan`` key —
    regardless of what the (default-on-signup) ``plan`` field says —
    so the admin Users tab and MRR math stop double-counting brand-
    new signups as paying Starter customers. The ``no_plan`` key is
    always present in the returned dict (zero if all users have
    paid), keeping callers KeyError-free.

    ``admin_emails`` is the allow-list of accounts (typically
    ``core.decorators.ADMIN_EMAILS``) whose access does NOT go through
    Whop. Callers MUST pass this tuple so admin accounts are counted
    by their stored ``plan`` field instead of being dumped into
    ``no_plan`` whenever their ``subscription_active`` happens to be
    false. Keeping the SQL helper and the in-memory ``_admin_plan_counts``
    helper agreed on the exemption rule is what guarantees the dashboard
    KPI cards and the Users tab KPI cards show identical numbers.
    """
    # Cache key includes the allow-list so a caller that exempts a
    # different (or empty) admin set sees its own bucketing — prevents
    # cross-contamination if this helper ever grows a second caller.
    cache_key = 'plan_counts:' + ','.join(sorted(admin_emails))
    cached = _ttl_get(cache_key)
    if cached is not None:
        return dict(cached)
    sql = """SELECT
                CASE
                  WHEN {admin_clause}
                       COALESCE((data->>'subscription_active')::boolean, false)
                       THEN COALESCE(LOWER(NULLIF(data->>'plan','')), 'starter')
                  ELSE 'no_plan'
                END AS plan,
                COUNT(*) AS n
              FROM users
             GROUP BY 1"""
    if admin_emails:
        # Bind the admin allow-list as a Postgres array via `= ANY(%s)`.
        # The earlier `IN %s` form assumed psycopg would expand a
        # Python tuple into `(a, b, c)` syntax, but psycopg3 (used here
        # via the Supabase transaction pooler with prepare_threshold=None)
        # passes the parameter through positional binding instead, which
        # produced `IN $1` and crashed every admin page with a Postgres
        # `syntax error at or near "$1"`. `= ANY(%s)` with a list is the
        # canonical pattern already used elsewhere in this file (see
        # `id = ANY(%s)`, `LOWER(city) = ANY(%s)`, etc.) and works
        # identically for set membership. Lowercased for parity with
        # ADMIN_EMAILS, which is already lowercase by convention.
        sql = sql.format(admin_clause="LOWER(data->>'email') = ANY(%s) OR")
        rows = pg.query(sql, ([e.lower() for e in admin_emails],))
    else:
        sql = sql.format(admin_clause="")
        rows = pg.query(sql)
    counts = {'starter': 0, 'pro': 0, 'agency': 0, 'no_plan': 0}
    for r in rows:
        plan = r['plan'] or 'no_plan'
        counts[plan] = counts.get(plan, 0) + int(r['n'])
    _ttl_set(cache_key, counts)
    return dict(counts)


def count_users_joined_in_month(month_prefix: str) -> int:
    """COUNT(*) WHERE data->>'joined' LIKE 'YYYY-MM%'. Cached 30 s."""
    key = f'joined_counts:{month_prefix}'
    cached = _ttl_get(key)
    if cached is not None:
        return cached
    row = pg.query_one(
        "SELECT COUNT(*) AS n FROM users WHERE data->>'joined' LIKE %s",
        (month_prefix + '%',),
    )
    n = int(row['n']) if row else 0
    _ttl_set(key, n)
    return n


def aggregate_user_cities() -> list[tuple[str, int]]:
    """SQL-side per-city user count derived from `data->'cities'`.
    Returns descending [(city, n), ...]. Cached 30 s."""
    cached = _ttl_get('city_counts')
    if cached is not None:
        return list(cached)
    rows = pg.query(
        """SELECT city, COUNT(*) AS n
             FROM users,
                  jsonb_array_elements_text(
                      COALESCE(data->'cities', '[]'::jsonb)
                  ) AS city
            GROUP BY city
            ORDER BY n DESC"""
    )
    out = [(r['city'], int(r['n'])) for r in rows]
    _ttl_set('city_counts', out)
    return list(out)


def total_user_count() -> int:
    """Cached COUNT(*) for the users table. 30 s TTL."""
    cached = _ttl_get('total_users')
    if cached is not None:
        return cached
    row = pg.query_one("SELECT COUNT(*) AS n FROM users")
    n = int(row['n']) if row else 0
    _ttl_set('total_users', n)
    return n


def get_users_by_ids(ids: list[int]) -> list[dict]:
    """Bulk fetch a small set of users by id. Returns flat docs, order
    follows the input list. Skips ids that don't resolve."""
    clean: list[int] = []
    for x in ids or []:
        try:
            clean.append(int(x))
        except (TypeError, ValueError):
            continue
    if not clean:
        return []
    rows = pg.query(
        "SELECT id, data FROM users WHERE id = ANY(%s)",
        (clean,),
    )
    by_id = {r['id']: _row_to_doc(r) for r in rows}
    return [by_id[i] for i in clean if i in by_id]


def delete_user(user_id: int) -> bool:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    pg.execute("DELETE FROM users WHERE id = %s", (uid,))
    _invalidate_users_cache()
    return True


def bulk_delete_users(user_ids: list[int]) -> int:
    """Delete many users in a single statement. Returns rows removed."""
    ids: list[int] = []
    for x in user_ids or []:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not ids:
        return 0
    n = pg.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))
    if n:
        _invalidate_users_cache()
    return n or 0


def get_user_by_email(email: str):
    row = pg.query_one(
        "SELECT id, data FROM users WHERE email = %s",
        (email.lower().strip(),),
    )
    return _row_to_doc(row)


def get_user_by_api_key(key: str) -> dict | None:
    """Find the user that owns a given API key by JSONB containment.

    Backed by the GIN index on ``users.data->'api_keys'`` (created by
    ``_ensure_perf_indexes`` once on the first referral/affiliate path
    and on every fresh deploy via ``scripts/migrate_to_supabase.py``),
    so this is O(log n) instead of the O(n) ``get_all_users()`` scan
    it replaces in ``_api_auth``. We deliberately don't trigger DDL
    from the API hot path.

    Returns ``None`` if no user owns the key. The caller is responsible
    for verifying the matching key entry is still ``active`` and that
    the user's plan permits API access.
    """
    if not key:
        return None
    needle = json.dumps([{'key': str(key)}])
    row = pg.query_one(
        "SELECT id, data FROM users WHERE data->'api_keys' @> %s::jsonb LIMIT 1",
        (needle,),
    )
    return _row_to_doc(row)


def set_reset_token(email: str, token: str, expiry_iso: str) -> bool:
    user = get_user_by_email(email)
    if user is None:
        return False
    return update_user(user['id'], reset_token=token, reset_expiry=expiry_iso)


def get_user_by_reset_token(token: str):
    row = pg.query_one(
        "SELECT id, data FROM users WHERE reset_token = %s",
        (token,),
    )
    return _row_to_doc(row)


def clear_reset_token(user_id: int) -> bool:
    return update_user(user_id, reset_token=None, reset_expiry=None)


# ── Google OAuth (sign-in with Google) ─────────────────────────

def get_user_by_google_sub(google_sub: str) -> dict | None:
    """Find a user previously linked to a Google account by Google's stable
    'sub' (subject) identifier. Returns None if no user is linked."""
    if not google_sub:
        return None
    row = pg.query_one(
        "SELECT * FROM users WHERE data->>'google_sub' = %s LIMIT 1",
        (str(google_sub),),
    )
    return _row_to_doc(row)


def link_google_to_user(user_id: int, google_sub: str, google_email: str) -> bool:
    """Attach Google identity fields to an existing user without touching their
    password. Used when a user with a matching email signs in with Google for
    the first time."""
    return update_user(
        user_id,
        google_sub=str(google_sub),
        google_linked_email=(google_email or '').lower(),
    )


def create_user_from_google(email: str, name: str, google_sub: str) -> dict | None:
    """Create a new user keyed on a verified Google identity.

    The local password is set to a random unguessable token; the user is
    expected to use Google sign-in or the password-reset flow to access the
    account with credentials.

    Race-safe: if a concurrent request inserts the same email between our
    pre-check and INSERT, Postgres' UNIQUE constraint on ``users.email`` will
    fire — we catch that and return ``None`` so the caller can fall back to
    fetching by email/sub instead of raising a 500.
    """
    email = (email or '').lower().strip()
    if not email or not google_sub:
        return None
    existing = pg.query_one("SELECT id FROM users WHERE email = %s", (email,))
    if existing:
        return None
    initials = ''.join(w[0].upper() for w in (name or email).split()[:2]) or email[:2].upper()
    user = {
        'email': email,
        'password': hash_password(secrets.token_urlsafe(32)),
        'name': name or email.split('@')[0].title(),
        'plan': 'starter',
        'cities': [],
        'avatar_initials': initials,
        'onboarding_complete': False,
        'subscription_active': False,
        'terms_accepted_at': None,
        # Same rationale as create_user() — stamp the join date so the admin
        # panel "Joined" column isn't blank for Google-signup users either.
        'joined': date.today().isoformat(),
        'auth_provider': 'google',
        # Per-user Whop billing mode — see create_user() above.
        'whop_mode': 'prod',
        'google_sub': str(google_sub),
        'google_linked_email': email,
    }
    try:
        row = pg.execute_returning(
            """INSERT INTO users (email, reset_token, data)
               VALUES (%s, NULL, %s)
               RETURNING id""",
            (email, Json(user)),
        )
    except Exception:
        # Most likely the unique-email index fired because a concurrent request
        # inserted this email first. Let the caller decide what to do.
        return None
    user['id'] = row['id']
    _invalidate_users_cache()
    return user


# ── TOTP / 2FA ─────────────────────────────────────────────────

def set_totp_secret(user_id: int, secret: str) -> bool:
    return update_user(user_id, totp_secret=secret, totp_enabled=False)


def enable_totp(user_id: int) -> bool:
    return update_user(user_id, totp_enabled=True)


def disable_totp(user_id: int) -> bool:
    return update_user(user_id, totp_secret=None, totp_enabled=False)


# ── Invoices ───────────────────────────────────────────────────

def get_user_invoices(user_id: int) -> list:
    rows = pg.query(
        """SELECT id, data FROM invoices
            WHERE user_id = %s
            ORDER BY period_start_ts DESC NULLS LAST""",
        (int(user_id),),
    )
    return [_row_to_doc(r) for r in rows]


def upsert_invoice(invoice: dict) -> str:
    inv_id = invoice.get('invoice_id', '')
    if not inv_id:
        return ''
    user_id = int(invoice.get('user_id', 0) or 0)
    period_ts = invoice.get('period_start_ts')
    try:
        period_ts = int(period_ts) if period_ts is not None else None
    except (TypeError, ValueError):
        period_ts = None
    pg.execute(
        """INSERT INTO invoices (invoice_id, user_id, period_start_ts, data)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (invoice_id) DO UPDATE
             SET user_id         = EXCLUDED.user_id,
                 period_start_ts = EXCLUDED.period_start_ts,
                 data            = EXCLUDED.data""",
        (inv_id, user_id, period_ts, Json(invoice)),
    )
    return inv_id


def get_invoice_by_id(invoice_id: str) -> dict:
    row = pg.query_one(
        "SELECT id, data FROM invoices WHERE invoice_id = %s",
        (invoice_id,),
    )
    return _row_to_doc(row) or {}


# ── Banned emails ──────────────────────────────────────────────

def is_email_banned(email: str) -> bool:
    row = pg.query_one(
        "SELECT 1 FROM banned_emails WHERE email = %s",
        (email.lower().strip(),),
    )
    return row is not None


def ban_email(email: str, name: str = '', banned_by: str = '') -> bool:
    from datetime import date
    email = email.lower().strip()
    if is_email_banned(email):
        return False
    rec = {
        'email':     email,
        'name':      name,
        'banned_on': date.today().isoformat(),
        'banned_by': banned_by,
    }
    pg.execute(
        """INSERT INTO banned_emails (email, data) VALUES (%s, %s)
           ON CONFLICT (email) DO NOTHING""",
        (email, Json(rec)),
    )
    _invalidate_banned_cache()
    return True


def unban_email(email: str) -> bool:
    pg.execute("DELETE FROM banned_emails WHERE email = %s", (email.lower().strip(),))
    _invalidate_banned_cache()
    return True


def get_all_banned() -> list:
    """Banned email list. Cached for `_TTL_DEFAULT_SECONDS` because every
    admin page renders the sidebar via `_admin_base_ctx` which calls this
    helper once per request — without the cache that's an extra cross-region
    round-trip on every admin click."""
    cached = _ttl_get('all_banned')
    if cached is not None:
        return list(cached)
    rows = pg.query(
        """SELECT id, data FROM banned_emails
            ORDER BY data->>'banned_on' DESC NULLS LAST"""
    )
    docs = [_row_to_doc(r) for r in rows]
    _ttl_set('all_banned', docs)
    return list(docs)


# ── Login History ──────────────────────────────────────────────

def record_login_event(user_id: int, status: str, device: str = '',
                       ip: str = '', ua: str = '') -> dict:
    from datetime import datetime
    rec = {
        'user_id':  int(user_id),
        'status':   status,
        'device':   device or 'Unknown Device',
        'ip':       ip or '',
        'ua':       ua or '',
        'ts':       datetime.now().isoformat(),
    }
    row = pg.execute_returning(
        """INSERT INTO login_history (user_id, data)
           VALUES (%s, %s) RETURNING id""",
        (int(user_id), Json(rec)),
    )
    rec['id'] = row['id']
    return rec


def clear_login_history_for_user(user_id: int) -> int:
    n = pg.execute("DELETE FROM login_history WHERE user_id = %s", (int(user_id),))
    return n or 0


def get_login_history_for_user(user_id: int, limit: int = 20) -> list:
    rows = pg.query(
        """SELECT id, data FROM login_history
            WHERE user_id = %s
            ORDER BY data->>'ts' DESC NULLS LAST
            LIMIT %s""",
        (int(user_id), int(limit)),
    )
    return [_row_to_doc(r) for r in rows]


def is_new_device_for_user(user_id: int, device: str, ip: str) -> bool:
    """Return True iff this (device, ip) combination has never been
    successfully used to sign in to the given account.

    Used by the login-alert email gate so users only get a "new sign-in
    detected" email the first time a fresh device or location appears
    on their account — same UX pattern Google / GitHub use. Same-device
    same-IP logins are silent.

    The check matches BOTH device fingerprint AND IP simultaneously to
    behave well for two common cases:

      * Frequent traveler on the same laptop hopping between coffee
        shop / home / office IPs — alerts fire on each new IP, which
        is the cautious thing to do (a stolen session cookie reused
        from another network looks identical to a "new IP" event,
        and a curious user can ignore the email).
      * Same office IP but a brand-new browser/device — alerts fire
        on the new device fingerprint.

    Only ``status='success'`` rows count, so a single failed attempt
    from a leaked credential doesn't pre-warm the device list and
    silence the legitimate alert when the attacker actually gets in.

    Returns ``True`` if no matching row exists (treat as new), ``False``
    otherwise. Defensive: any DB error returns ``False`` (no email)
    because we'd rather miss an alert than spam a user during a Postgres
    incident.
    """
    try:
        row = pg.query_one(
            """SELECT 1
                 FROM login_history
                WHERE user_id  = %s
                  AND data->>'status' = 'success'
                  AND data->>'device' = %s
                  AND data->>'ip'     = %s
                LIMIT 1""",
            (int(user_id), (device or 'Unknown Device'), (ip or '')),
        )
    except Exception:
        return False
    return row is None


# ── Sessions ───────────────────────────────────────────────────

def create_session(user_id: int, session_key: str,
                   device: str = '', ip: str = '', ua: str = '') -> dict:
    from datetime import datetime
    now = datetime.now().isoformat()
    rec = {
        'user_id':     int(user_id),
        'session_key': session_key,
        'device':      device or 'Unknown Device',
        'ip':          ip or '',
        'ua':          ua or '',
        'created_at':  now,
        'last_seen':   now,
    }
    row = pg.execute_returning(
        """INSERT INTO sessions (user_id, session_key, data)
           VALUES (%s, %s, %s) RETURNING id""",
        (int(user_id), session_key, Json(rec)),
    )
    rec['id'] = row['id']
    return rec


def touch_session(session_key: str) -> None:
    from datetime import datetime
    now = datetime.now().isoformat()
    pg.execute(
        """UPDATE sessions
              SET data = jsonb_set(data, '{last_seen}', to_jsonb(%s::text), true)
            WHERE session_key = %s""",
        (now, session_key),
    )


def get_sessions_for_user(user_id: int) -> list:
    rows = pg.query(
        "SELECT id, data FROM sessions WHERE user_id = %s ORDER BY id",
        (int(user_id),),
    )
    return [_row_to_doc(r) for r in rows]


def is_session_valid(user_id: int, session_key: str) -> bool:
    row = pg.query_one(
        "SELECT 1 FROM sessions WHERE user_id = %s AND session_key = %s",
        (int(user_id), session_key),
    )
    return row is not None


def delete_session_by_id(session_doc_id: int) -> bool:
    pg.execute("DELETE FROM sessions WHERE id = %s", (int(session_doc_id),))
    return True


def delete_sessions_for_user(user_id: int, except_key: str = None) -> int:
    if except_key:
        n = pg.execute(
            "DELETE FROM sessions WHERE user_id = %s AND session_key <> %s",
            (int(user_id), except_key),
        )
    else:
        n = pg.execute("DELETE FROM sessions WHERE user_id = %s", (int(user_id),))
    return n or 0


# ── Demo seed ──────────────────────────────────────────────────

def _seed_demo_user_now():
    """Internal: insert the built-in demo admin (mk@permitdaily.com).

    Caller is responsible for the one-shot guard. See
    ``seed_demo_user()`` for the public entry-point with the flag check.
    """
    rec = {
        'email': 'mk@permitdaily.com',
        'password': hash_password('demo1234'),
        'name': 'Marcus Kim',
        'plan': 'agency',
        'cities': ['Fort Worth', 'Arlington', 'Dallas', 'Austin'],
        'avatar_initials': 'MK',
        'joined': '2026-01-05',
        'status': 'active',
        'is_admin': True,
        'alerts_sent': 1840,
        'api_calls': 14200,
    }
    # ON CONFLICT (email) DO NOTHING makes this safe under multi-worker
    # gunicorn startup races (two workers both pass the empty-DB check and
    # try to insert simultaneously — the second one no-ops instead of
    # raising a UNIQUE violation).
    pg.execute(
        "INSERT INTO users (email, data) VALUES (%s, %s) "
        "ON CONFLICT (email) DO NOTHING",
        ('mk@permitdaily.com', Json(rec)),
    )


def seed_demo_user():
    """One-shot seeder for the built-in demo admin (mk@permitdaily.com).

    Runs at most once per database. After the first run the
    ``demo_user_seeded`` flag is recorded in ``system_settings`` and we
    never auto-insert again — so if an admin deletes the demo user from
    /admin-panel/users/ they stay deleted across server restarts.

    Brownfield-safe: if the ``users`` table already has rows the *first*
    time we see this DB, we treat it as an existing install and just set
    the flag without inserting (so currently-deleted demo users do not
    come back on the deploy that ships this change).

    NOTE: When called as part of the bootstrap pair (``seed_demo_user``
    then ``seed_sample_users``) the demo insert would otherwise make the
    sample seeder think the DB was brownfield. To avoid that, both
    seeders share a single brownfield-decision snapshot taken *before*
    either of them inserts anything — see ``_seed_initial_data()``.
    """
    if get_system_setting('demo_user_seeded', False):
        return

    has_any_users = pg.query_one("SELECT 1 FROM users LIMIT 1")
    if has_any_users:
        set_system_setting('demo_user_seeded', True)
        return

    _seed_demo_user_now()
    set_system_setting('demo_user_seeded', True)


def seed_sample_users():
    """One-shot seeder for the sample customer accounts.

    Same semantics as ``seed_demo_user``: runs once on a truly fresh DB,
    then the ``sample_users_seeded`` flag prevents any future re-seeding.
    Deleted sample users stay deleted across restarts. See the note on
    ``seed_demo_user`` re: ordering with the demo seeder.
    """
    if get_system_setting('sample_users_seeded', False):
        return

    has_any_users = pg.query_one("SELECT 1 FROM users LIMIT 1")
    if has_any_users:
        # Brownfield install — don't recreate any sample users that the
        # admin has already removed. Just mark as done.
        set_system_setting('sample_users_seeded', True)
        return

    _seed_sample_users_now()
    set_system_setting('sample_users_seeded', True)


def _seed_sample_users_now():
    """Internal: insert the sample customer accounts.

    Caller is responsible for the one-shot guard. See
    ``seed_sample_users()`` for the public entry-point.
    """
    sample = [
        {'name': 'Sarah Mitchell',  'email': 'sarah@roofpro.com',           'plan': 'pro',     'cities': ['Dallas', 'Fort Worth'],                        'joined': '2026-01-15'},
        {'name': 'James Thornton',  'email': 'james@hvacking.com',           'plan': 'agency',  'cities': ['Austin', 'San Antonio', 'Houston'],             'joined': '2026-01-22'},
        {'name': 'Diana Cruz',      'email': 'd.cruz@plumbfast.com',         'plan': 'starter', 'cities': ['Arlington'],                                    'joined': '2026-02-03'},
        {'name': 'Ryan Okafor',     'email': 'ryan@voltelectrical.com',      'plan': 'pro',     'cities': ['Dallas'],                                       'joined': '2026-02-19'},
        {'name': 'Carla Nguyen',    'email': 'carla@ngroofing.com',          'plan': 'agency',  'cities': ['Fort Worth', 'Arlington', 'Dallas'],             'joined': '2026-02-28'},
        {'name': 'Tom Reyes',       'email': 't.reyes@acrepair.net',         'plan': 'pro',     'cities': ['San Antonio'],                                  'joined': '2026-03-08'},
        {'name': 'Brittany Hall',   'email': 'bhall@handyworks.io',          'plan': 'starter', 'cities': ['Austin'],                                       'joined': '2026-03-15'},
        {'name': 'Derek Simmons',   'email': 'derek@simmons-roofing.com',    'plan': 'pro',     'cities': ['Houston', 'Galveston'],                         'joined': '2026-03-27'},
        {'name': 'Priya Shah',      'email': 'priya@shahcontracting.com',    'plan': 'agency',  'cities': ['Dallas', 'Fort Worth', 'Plano', 'Irving'],      'joined': '2026-04-02'},
        {'name': 'Luis Moreno',     'email': 'luis@morenoelec.com',          'plan': 'starter', 'cities': ['El Paso'],                                      'joined': '2026-04-10'},
        {'name': 'Amanda Foster',   'email': 'amanda@fosterroofing.biz',     'plan': 'pro',     'cities': ['Fort Worth', 'Denton'],                         'joined': '2026-04-14'},
        {'name': 'Kenji Watanabe',  'email': 'kenji@watanabeplumbing.com',   'plan': 'starter', 'cities': ['Plano'],                                        'joined': '2026-04-17'},
    ]
    for su in sample:
        email = su['email'].lower().strip()
        existing = pg.query_one("SELECT 1 FROM users WHERE email = %s", (email,))
        if existing:
            continue
        rec = {
            'email': email,
            'password': hash_password('sample123'),
            'name': su['name'],
            'plan': su['plan'],
            'cities': su['cities'],
            'avatar_initials': ''.join(w[0].upper() for w in su['name'].split()[:2]),
            'joined': su['joined'],
            'status': 'active',
            'is_admin': False,
        }
        # ON CONFLICT (email) DO NOTHING for the same multi-worker
        # safety reason as the demo-user insert above.
        pg.execute(
            "INSERT INTO users (email, data) VALUES (%s, %s) "
            "ON CONFLICT (email) DO NOTHING",
            (email, Json(rec)),
        )


def seed_initial_data():
    """Bootstrap the demo + sample users on a fresh DB.

    Single entry-point to call at app import-time. Both seeders are
    one-shot (gated by ``demo_user_seeded`` / ``sample_users_seeded``
    flags in ``system_settings``). This wrapper takes a *single*
    "is the DB empty?" snapshot before either seeder inserts anything,
    so on a truly fresh DB both sets of users get created — without
    the demo insert tricking the sample seeder into thinking we're
    on a brownfield install.

    On every restart after the first, both flag checks short-circuit
    and this is essentially free.

    Recovery note: if a seed insert fails after the demo user has been
    written (and committed) but before the sample users finish, the
    next restart will see ``db_was_empty=False`` and mark both flags
    as done without inserting the missing sample users. To recover,
    clear the flags manually:
        DELETE FROM system_settings
        WHERE key IN ('demo_user_seeded','sample_users_seeded');
    then restart. ``ON CONFLICT (email) DO NOTHING`` on the inserts
    means existing users won't conflict.
    """
    demo_done   = get_system_setting('demo_user_seeded',    False)
    sample_done = get_system_setting('sample_users_seeded', False)
    if demo_done and sample_done:
        return

    # Take the brownfield snapshot ONCE, before any insert.
    db_was_empty = pg.query_one("SELECT 1 FROM users LIMIT 1") is None

    if not demo_done:
        if db_was_empty:
            _seed_demo_user_now()
        set_system_setting('demo_user_seeded', True)

    if not sample_done:
        if db_was_empty:
            _seed_sample_users_now()
        set_system_setting('sample_users_seeded', True)

    # ── one-shot repair ─────────────────────────────────────────
    # Backfill the materialized ``email`` / ``reset_token`` columns
    # from JSONB for any rows the previous (buggy) update_user
    # desynced. Idempotent — safe to run on every boot.
    try:
        n = _repair_materialized_user_columns()
        if n:
            log.info("user-column repair: re-synced %d row(s)", n)
    except Exception:
        log.exception("user-column repair: skipped (non-fatal)")

    # ── one-shot joined-date backfill ───────────────────────────
    # Stamp ``joined: 2026-04-20`` on every user that doesn't have
    # one yet so the admin "Joined" column isn't blank. Idempotent.
    try:
        n = _backfill_missing_joined_dates()
        if n:
            log.info("joined-date backfill: stamped %d user(s)", n)
    except Exception:
        log.exception("joined-date backfill: skipped (non-fatal)")


# ── Affiliate / Referral program ──────────────────────────────
#
# All affiliate state lives in two places:
#
#   1. Per-user JSONB fields on ``users.data``:
#        * referral_code                 — 8-char unique code (lazy-assigned)
#        * referred_by_user_id           — int, set at signup if ?ref= was used
#        * referred_by_code              — code string (audit trail; survives
#                                          deletion of the referrer account)
#        * referral_first_payment_credited — bool, set once we have credited
#                                            the referrer for this user
#
#   2. The ``referral_events`` table — one row per signup or commission
#      event. ``commission`` rows carry an ``amount_cents`` so we can total
#      earnings in plain SQL.
#
# Commission rate is read from ``system_settings.affiliate_commission_pct``
# (default 20). It is applied to the first paid plan price the referee
# pays — see ``credit_referral_first_payment`` and the call site in
# ``ls_webhook``.

_REFERRAL_TABLE_READY = False
_PERF_INDEXES_READY = False
_BLOG_TABLE_READY = False
_BLOG_SEED_DONE = False


# ─── Blog posts ────────────────────────────────────────────────
# Migrated from the static ``BLOG_ARTICLES`` dict in ``core.blog_articles``
# to a real Postgres table so we can paginate, full-text search, and edit
# posts at runtime without a deploy. The schema is intentionally simple —
# one row per post, slug as the natural key, JSONB ``related`` column for
# the inter-article link list, and a real ``published_at`` timestamp so
# we can ORDER BY date instead of relying on dict insertion order.

def _ensure_blog_table():
    """Create the ``blog_posts`` table on first use (idempotent)."""
    global _BLOG_TABLE_READY
    if _BLOG_TABLE_READY:
        return
    pg.execute(
        """CREATE TABLE IF NOT EXISTS blog_posts (
              id              SERIAL PRIMARY KEY,
              slug            VARCHAR(140) NOT NULL UNIQUE,
              title           TEXT NOT NULL,
              author          VARCHAR(120) NOT NULL DEFAULT 'Permitlify Team',
              author_initials VARCHAR(8)   NOT NULL DEFAULT 'PL',
              date_label      VARCHAR(60)  NOT NULL,
              published_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
              read_time       VARCHAR(40)  NOT NULL DEFAULT '5 min read',
              tag             VARCHAR(60)  NOT NULL DEFAULT 'Insights',
              tag_color       VARCHAR(20)  NOT NULL DEFAULT 'blue',
              thumb           TEXT         NOT NULL DEFAULT '📝',
              thumb_bg        TEXT         NOT NULL,
              excerpt         TEXT         NOT NULL,
              content         TEXT         NOT NULL,
              related         JSONB        NOT NULL DEFAULT '[]'::jsonb,
              is_featured     BOOLEAN      NOT NULL DEFAULT FALSE,
              created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
              updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
           )"""
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS blog_posts_pub_idx "
        "ON blog_posts(published_at DESC)"
    )
    # Trigram index speeds up the case-insensitive ILIKE searches the
    # blog list view runs against title/excerpt/content. pg_trgm ships
    # with Supabase by default; skipping silently if the extension can't
    # be enabled (the search will still work, just sequential-scan).
    try:
        pg.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        pg.execute(
            "CREATE INDEX IF NOT EXISTS blog_posts_title_trgm_idx "
            "ON blog_posts USING gin (title gin_trgm_ops)"
        )
        pg.execute(
            "CREATE INDEX IF NOT EXISTS blog_posts_excerpt_trgm_idx "
            "ON blog_posts USING gin (excerpt gin_trgm_ops)"
        )
    except Exception:
        pass
    _BLOG_TABLE_READY = True
    # First-deploy seed: if the table is empty, populate from the static
    # ``BLOG_ARTICLES`` dict so production has content immediately after
    # the migration ships. Subsequent edits become DB-only. Tracked by a
    # *separate* flag so a transient seed failure (e.g. concurrent worker
    # racing the first request) doesn't permanently leave the blog empty
    # — we'll retry on the next request that finds the table empty.
    global _BLOG_SEED_DONE
    if _BLOG_SEED_DONE:
        return
    try:
        row = pg.query_one("SELECT COUNT(*) AS n FROM blog_posts")
        if int((row or {}).get('n') or 0) == 0:
            _seed_blog_posts_from_static()
        _BLOG_SEED_DONE = True
    except Exception:
        # Leave _BLOG_SEED_DONE = False so the next request retries.
        pass


def _seed_blog_posts_from_static():
    """Populate blog_posts from the static ``BLOG_ARTICLES`` dict.

    Used as the first-deploy seed and re-runnable as a refresh — every row
    is upserted by slug. Date strings like ``'April 19, 2026'`` are parsed
    into a real timestamp so ORDER BY published_at works correctly.
    """
    import datetime as _dt
    from .blog_articles import BLOG_ARTICLES
    FEATURED_SLUG = 'building-permits-best-lead-source'
    def _parse(label: str):
        for fmt in ('%B %d, %Y', '%b %d, %Y'):
            try:
                return _dt.datetime.strptime(label or '', fmt)
            except ValueError:
                continue
        return _dt.datetime.utcnow()
    for slug, post in BLOG_ARTICLES.items():
        record = dict(post)
        record['published_at'] = _parse(post.get('date', ''))
        record['is_featured']  = (slug == FEATURED_SLUG)
        upsert_blog_post(record)


def list_blog_posts(query: str = '', page: int = 1, per_page: int = 10):
    """Return a (rows, total_count, total_pages) tuple for the blog list view.

    ``query`` is matched case-insensitively against title, excerpt and
    body. Results are ordered newest first by ``published_at``. Pagination
    is 1-indexed; out-of-range pages clamp to the last available page.
    """
    _ensure_blog_table()
    per_page = max(1, min(int(per_page or 10), 50))
    page     = max(1, int(page or 1))

    where = ''
    params: tuple = ()
    q = (query or '').strip()
    if q:
        like = f'%{q}%'
        where = ("WHERE title ILIKE %s OR excerpt ILIKE %s "
                 "OR content ILIKE %s OR tag ILIKE %s")
        params = (like, like, like, like)

    total_row = pg.query_one(
        f"SELECT COUNT(*) AS n FROM blog_posts {where}", params
    )
    total = int((total_row or {}).get('n') or 0)
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    rows = pg.query(
        f"""SELECT slug, title, author, author_initials, date_label AS date,
                   read_time, tag, tag_color, thumb, thumb_bg, excerpt
              FROM blog_posts
              {where}
             ORDER BY published_at DESC, id DESC
             LIMIT %s OFFSET %s""",
        params + (per_page, offset),
    )
    return rows, total, total_pages, page


def get_blog_post(slug: str):
    """Fetch a single blog post by slug, or ``None`` if not found."""
    _ensure_blog_table()
    if not slug:
        return None
    return pg.query_one(
        """SELECT slug, title, author, author_initials, date_label AS date,
                  read_time, tag, tag_color, thumb, thumb_bg, excerpt,
                  content, related, is_featured, published_at
             FROM blog_posts WHERE slug = %s""",
        (slug,),
    )


def delete_blog_post(slug: str) -> bool:
    """Delete a blog post by slug. Returns True if a row was removed."""
    _ensure_blog_table()
    if not slug:
        return False
    cur = pg.execute("DELETE FROM blog_posts WHERE slug = %s", (slug,))
    # psycopg returns the cursor; rowcount tells us if anything matched.
    try:
        return (cur.rowcount or 0) > 0
    except Exception:
        return True


def slug_exists(slug: str) -> bool:
    """True if a row with this slug already exists. Used by the editor to
    suggest a unique slug when the admin tweaks the title."""
    _ensure_blog_table()
    if not slug:
        return False
    row = pg.query_one(
        "SELECT 1 AS x FROM blog_posts WHERE slug = %s LIMIT 1", (slug,)
    )
    return bool(row)


def get_featured_blog_post():
    """Return the post flagged ``is_featured=TRUE`` (newest if multiple), or
    fall back to the most recent post."""
    _ensure_blog_table()
    row = pg.query_one(
        """SELECT slug, title, excerpt, thumb, thumb_bg, tag
             FROM blog_posts
            WHERE is_featured = TRUE
            ORDER BY published_at DESC LIMIT 1"""
    )
    if row:
        return row
    return pg.query_one(
        """SELECT slug, title, excerpt, thumb, thumb_bg, tag
             FROM blog_posts
            ORDER BY published_at DESC, id DESC LIMIT 1"""
    )


def get_related_blog_posts(slugs):
    """Resolve a list of related-article slugs into the rows the sidebar
    needs. Preserves the order the slugs were given in and silently drops
    any slug that no longer exists."""
    _ensure_blog_table()
    slugs = [s for s in (slugs or []) if s]
    if not slugs:
        return []
    rows = pg.query(
        """SELECT slug, title, date_label AS date, thumb, thumb_bg
             FROM blog_posts WHERE slug = ANY(%s)""",
        (slugs,),
    )
    by_slug = {r['slug']: r for r in rows}
    return [by_slug[s] for s in slugs if s in by_slug]


def upsert_blog_post(post: dict) -> None:
    """Insert or update a single blog post by slug. Used by the seed/
    migration script that imports the static ``BLOG_ARTICLES`` dict into
    the table on first deploy."""
    _ensure_blog_table()
    pg.execute(
        """INSERT INTO blog_posts (
              slug, title, author, author_initials, date_label, published_at,
              read_time, tag, tag_color, thumb, thumb_bg, excerpt, content,
              related, is_featured, updated_at
           ) VALUES (
              %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s,
              %s::jsonb, %s, NOW()
           )
           ON CONFLICT (slug) DO UPDATE SET
              title           = EXCLUDED.title,
              author          = EXCLUDED.author,
              author_initials = EXCLUDED.author_initials,
              date_label      = EXCLUDED.date_label,
              published_at    = EXCLUDED.published_at,
              read_time       = EXCLUDED.read_time,
              tag             = EXCLUDED.tag,
              tag_color       = EXCLUDED.tag_color,
              thumb           = EXCLUDED.thumb,
              thumb_bg        = EXCLUDED.thumb_bg,
              excerpt         = EXCLUDED.excerpt,
              content         = EXCLUDED.content,
              related         = EXCLUDED.related,
              is_featured     = EXCLUDED.is_featured,
              updated_at      = NOW()""",
        (
            post['slug'],
            post['title'],
            post.get('author', 'Permitlify Team'),
            post.get('author_initials', 'PL'),
            post.get('date', ''),
            post.get('published_at'),
            post.get('read_time', '5 min read'),
            post.get('tag', 'Insights'),
            post.get('tag_color', 'blue'),
            post.get('thumb', '📝'),
            post.get('thumb_bg', 'linear-gradient(135deg,#1e3a8a 0%,#1d4ed8 100%)'),
            post.get('excerpt', ''),
            post.get('content', ''),
            json.dumps(post.get('related') or []),
            bool(post.get('is_featured', False)),
        ),
    )


def _ensure_perf_indexes():
    """Create JSONB lookup indexes that back hot single-row queries.

    These cover paths that previously required a full ``users`` scan or
    a full ``referral_events`` scan:

      * ``users (data->>'google_sub')`` — Google OAuth callback
      * ``users (data->>'referral_code')`` — referral landing & uniqueness check
      * ``users USING gin ((data->'api_keys') jsonb_path_ops)`` — API auth
      * ``referral_events (event_type)`` — admin Affiliates aggregations

    All statements are ``IF NOT EXISTS`` and an in-process flag prevents
    repeat round-trips after the first call per worker. Safe at import time
    or first-use; we use first-use to avoid blocking app startup if the DB
    is briefly unreachable.
    """
    global _PERF_INDEXES_READY
    if _PERF_INDEXES_READY:
        return
    try:
        pg.execute(
            """CREATE INDEX IF NOT EXISTS users_google_sub_idx
                  ON users ((data->>'google_sub'))
                  WHERE data->>'google_sub' IS NOT NULL"""
        )
        pg.execute(
            """CREATE INDEX IF NOT EXISTS users_referral_code_idx
                  ON users ((data->>'referral_code'))
                  WHERE data->>'referral_code' IS NOT NULL"""
        )
        pg.execute(
            """CREATE INDEX IF NOT EXISTS users_api_keys_gin_idx
                  ON users USING gin ((data->'api_keys') jsonb_path_ops)"""
        )
        pg.execute(
            """CREATE INDEX IF NOT EXISTS referral_events_event_type_idx
                  ON referral_events(event_type)"""
        )
    except Exception:
        # Don't crash the request if DDL temporarily fails (e.g. permissions
        # in a sandbox). Log it so the failure is visible — otherwise we'd
        # silently fall back to sequential scans forever. Subsequent calls
        # will retry, but only on the referral/affiliate path (we never
        # call this from the API hot path).
        log.exception("ensure_perf_indexes: failed to create JSONB indexes")
        return
    _PERF_INDEXES_READY = True


def _ensure_referral_table():
    """Create ``referral_events`` if it does not exist yet.

    Safe to call repeatedly — ``CREATE TABLE IF NOT EXISTS`` is a no-op
    after the first run, and an in-process flag short-circuits any DB
    round-trip on subsequent calls within the same worker.
    """
    global _REFERRAL_TABLE_READY
    if _REFERRAL_TABLE_READY:
        return
    pg.execute(
        """CREATE TABLE IF NOT EXISTS referral_events (
              id               SERIAL PRIMARY KEY,
              referrer_user_id INT  NOT NULL,
              referee_user_id  INT  NOT NULL,
              event_type       TEXT NOT NULL,
              amount_cents     INT  NOT NULL DEFAULT 0,
              data             JSONB,
              created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
           )"""
    )
    pg.execute(
        """CREATE INDEX IF NOT EXISTS referral_events_referrer_idx
              ON referral_events(referrer_user_id)"""
    )
    pg.execute(
        """CREATE INDEX IF NOT EXISTS referral_events_referee_idx
              ON referral_events(referee_user_id)"""
    )
    _REFERRAL_TABLE_READY = True
    # Co-create the JSONB perf indexes once the table is guaranteed to exist
    # (the referral_events(event_type) index in particular needs the table).
    _ensure_perf_indexes()


def _generate_referral_code() -> str:
    """8-char URL-safe code. Avoids easily confused chars (0/O, 1/I/l)."""
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(8))


def ensure_referral_code(user_id: int) -> str:
    """Return the user's referral code, generating one if they don't have it.

    Handles the (extremely unlikely) collision case by retrying. The code is
    written back to the user's JSONB doc via the atomic shallow-merge so two
    concurrent calls for the same user end up with the same code (last writer
    wins, but they're both writing the same thing on retry).
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return ''
    user = get_user_by_id(uid) or {}
    existing = (user.get('referral_code') or '').strip()
    if existing:
        return existing
    for _ in range(8):
        code = _generate_referral_code()
        clash = pg.query_one(
            "SELECT 1 FROM users WHERE data->>'referral_code' = %s LIMIT 1",
            (code,),
        )
        if not clash:
            update_user(uid, referral_code=code)
            return code
    # Pathological: 8 collisions in a row. Fall back to a timestamp suffix.
    code = _generate_referral_code() + str(int(datetime.now().timestamp()))[-2:]
    update_user(uid, referral_code=code)
    return code


def get_user_by_referral_code(code: str) -> dict | None:
    if not code:
        return None
    row = pg.query_one(
        "SELECT id, data FROM users WHERE data->>'referral_code' = %s LIMIT 1",
        (str(code).strip().upper(),),
    )
    return _row_to_doc(row)


def record_referral_event(referrer_user_id: int, referee_user_id: int,
                          event_type: str, amount_cents: int = 0,
                          data: dict | None = None) -> dict | None:
    """Append-only ledger row. Returns the inserted row dict."""
    _ensure_referral_table()
    try:
        rid = int(referrer_user_id)
        eid = int(referee_user_id)
    except (TypeError, ValueError):
        return None
    if rid == eid:
        return None  # never self-credit
    row = pg.execute_returning(
        """INSERT INTO referral_events
              (referrer_user_id, referee_user_id, event_type, amount_cents, data)
           VALUES (%s, %s, %s, %s, %s)
           RETURNING id, created_at""",
        (rid, eid, event_type, int(amount_cents or 0), Json(data or {})),
    )
    return row


def get_referral_stats_for_user(user_id: int) -> dict:
    """Aggregate stats for one referrer: counts and total earnings (cents)."""
    _ensure_referral_table()
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return {'signups': 0, 'paid': 0, 'earnings_cents': 0}
    row = pg.query_one(
        """SELECT
              COUNT(*) FILTER (WHERE event_type = 'signup')                  AS signups,
              COUNT(*) FILTER (WHERE event_type = 'commission')              AS paid,
              COALESCE(SUM(amount_cents) FILTER (WHERE event_type = 'commission'), 0) AS earnings_cents
            FROM referral_events
           WHERE referrer_user_id = %s""",
        (uid,),
    )
    return {
        'signups':        int((row or {}).get('signups')  or 0),
        'paid':           int((row or {}).get('paid')     or 0),
        'earnings_cents': int((row or {}).get('earnings_cents') or 0),
    }


def get_referees_for_user(user_id: int) -> list[dict]:
    """List of users who signed up via this referrer, with payment + earnings."""
    _ensure_referral_table()
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return []
    rows = pg.query(
        """SELECT
              u.id    AS referee_id,
              u.email AS referee_email,
              u.data  AS referee_data,
              MIN(e.created_at) FILTER (WHERE e.event_type = 'signup')     AS signed_up_at,
              MAX(e.created_at) FILTER (WHERE e.event_type = 'commission') AS first_paid_at,
              COALESCE(SUM(e.amount_cents) FILTER (WHERE e.event_type = 'commission'), 0) AS earnings_cents
            FROM referral_events e
            JOIN users u ON u.id = e.referee_user_id
           WHERE e.referrer_user_id = %s
        GROUP BY u.id, u.email, u.data
        ORDER BY MIN(e.created_at) DESC NULLS LAST""",
        (uid,),
    )
    out = []
    for r in rows:
        rd = r.get('referee_data') or {}
        if isinstance(rd, str):
            rd = json.loads(rd)
        out.append({
            'id':             r['referee_id'],
            'email':          r['referee_email'],
            'name':           rd.get('name', ''),
            'plan':           (rd.get('plan') or 'starter').lower(),
            'subscription_active': bool(rd.get('subscription_active')),
            'signed_up_at':   r.get('signed_up_at'),
            'first_paid_at':  r.get('first_paid_at'),
            'earnings_cents': int(r.get('earnings_cents') or 0),
        })
    return out


def get_all_referrers_with_stats() -> list[dict]:
    """For the admin Affiliates page — every user who has at least one
    referral event, with totals. Sorted by earnings desc, then signups desc.
    """
    _ensure_referral_table()
    rows = pg.query(
        """SELECT
              u.id, u.email, u.data,
              COUNT(*) FILTER (WHERE e.event_type = 'signup')     AS signups,
              COUNT(*) FILTER (WHERE e.event_type = 'commission') AS paid,
              COALESCE(SUM(e.amount_cents) FILTER (WHERE e.event_type = 'commission'), 0) AS earnings_cents,
              MAX(e.created_at) AS last_event_at
            FROM referral_events e
            JOIN users u ON u.id = e.referrer_user_id
        GROUP BY u.id, u.email, u.data
        ORDER BY earnings_cents DESC, signups DESC, u.id"""
    )
    out = []
    for r in rows:
        d = r.get('data') or {}
        if isinstance(d, str):
            d = json.loads(d)
        out.append({
            'id':             r['id'],
            'email':          r['email'],
            'name':           d.get('name', ''),
            'plan':           (d.get('plan') or 'starter').lower(),
            'referral_code':  d.get('referral_code', ''),
            'avatar_initials': d.get('avatar_initials', ''),
            'signups':        int(r.get('signups')        or 0),
            'paid':           int(r.get('paid')           or 0),
            'earnings_cents': int(r.get('earnings_cents') or 0),
            'last_event_at':  r.get('last_event_at'),
        })
    return out


def get_total_affiliate_stats() -> dict:
    """Platform-wide totals for the admin KPI strip."""
    _ensure_referral_table()
    row = pg.query_one(
        """SELECT
              COUNT(DISTINCT referrer_user_id) FILTER (WHERE event_type = 'signup') AS active_affiliates,
              COUNT(*)        FILTER (WHERE event_type = 'signup')     AS total_signups,
              COUNT(*)        FILTER (WHERE event_type = 'commission') AS total_paid,
              COALESCE(SUM(amount_cents) FILTER (WHERE event_type = 'commission'), 0) AS total_earnings_cents
            FROM referral_events"""
    )
    row = row or {}
    return {
        'active_affiliates':    int(row.get('active_affiliates')    or 0),
        'total_signups':        int(row.get('total_signups')        or 0),
        'total_paid':           int(row.get('total_paid')           or 0),
        'total_earnings_cents': int(row.get('total_earnings_cents') or 0),
    }


def credit_referral_first_payment(referee_user_id: int, plan_price_cents: int,
                                  membership_id: str = '') -> dict | None:
    """Credit the referee's referrer for a first paid subscription.

    Idempotent: the per-user flag ``referral_first_payment_credited`` is set
    once we credit, so subsequent webhook deliveries (e.g. ``went_valid``
    fired again on plan changes or renewals) do not double-pay.

    Returns the inserted ledger row dict, or ``None`` if no credit was made
    (unreferred user, missing referrer, already credited, self-referral, or
    the referrer was deleted).

    Read at call time:
        * commission rate from ``system_settings.affiliate_commission_pct``
          (default 20)
    """
    try:
        ruid = int(referee_user_id)
    except (TypeError, ValueError):
        return None
    referee = get_user_by_id(ruid) or {}
    if referee.get('referral_first_payment_credited'):
        return None
    referrer_id = referee.get('referred_by_user_id')
    if not referrer_id:
        return None
    try:
        referrer_id = int(referrer_id)
    except (TypeError, ValueError):
        return None
    if referrer_id == ruid:
        return None
    referrer = get_user_by_id(referrer_id)
    if not referrer:
        # Referrer account was deleted — mark credited so we never look again.
        update_user(ruid, referral_first_payment_credited=True)
        return None
    pct_raw = get_system_setting('affiliate_commission_pct', 20)
    try:
        pct = int(pct_raw)
    except (TypeError, ValueError):
        pct = 20
    if pct <= 0:
        update_user(ruid, referral_first_payment_credited=True)
        return None
    commission = max(0, int(plan_price_cents) * pct // 100)
    if commission <= 0:
        update_user(ruid, referral_first_payment_credited=True)
        return None
    row = record_referral_event(
        referrer_id, ruid, 'commission', commission,
        {
            'plan_price_cents': int(plan_price_cents),
            'pct':              pct,
            'whop_membership_id': membership_id or '',
        },
    )
    # Set the idempotency flag *after* the insert succeeds, so a transient DB
    # error here does not silently lose the credit forever.
    update_user(ruid, referral_first_payment_credited=True)
    return row


def bind_referrer_for_user(new_user_id: int, referral_code: str) -> bool:
    """Called from the signup flow. Resolves ``referral_code`` to a referrer
    and writes ``referred_by_user_id`` + ``referred_by_code`` onto the new
    user, then logs a ``signup`` event in ``referral_events``.

    Silent no-op if the code doesn't resolve, the user is referring
    themselves, or the new user is already bound to a referrer.
    """
    if not referral_code:
        return False
    try:
        nid = int(new_user_id)
    except (TypeError, ValueError):
        return False
    new_user = get_user_by_id(nid) or {}
    if new_user.get('referred_by_user_id'):
        return False  # already bound, don't overwrite
    referrer = get_user_by_referral_code(referral_code)
    if not referrer or int(referrer['id']) == nid:
        return False
    update_user(
        nid,
        referred_by_user_id=int(referrer['id']),
        referred_by_code=str(referral_code).strip().upper(),
    )
    record_referral_event(int(referrer['id']), nid, 'signup', 0,
                          {'code': str(referral_code).strip().upper()})
    return True


# ── System settings ────────────────────────────────────────────

def get_system_setting(key: str, default=None):
    row = pg.query_one("SELECT value FROM system_settings WHERE key = %s", (key,))
    if row is None:
        return default
    return row['value']


def _bust_settings_cache(key: str) -> None:
    """Notify ``core.whop`` that an admin changed this key so its 60s
    in-process settings cache drops the stale entry on the next read.
    Done lazily to avoid an import cycle (whop -> db -> whop)."""
    try:
        from . import whop as _whop
        _whop.clear_settings_cache(key)
    except Exception:
        pass


def set_system_setting(key: str, value) -> None:
    pg.execute(
        """INSERT INTO system_settings (key, value)
           VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (key, Json(value)),
    )
    _bust_settings_cache(key)


def get_all_system_settings() -> dict:
    rows = pg.query("SELECT key, value FROM system_settings")
    return {r['key']: r['value'] for r in rows}


# ── Supported Cities ────────────────────────────────────────────

_DEFAULT_SUPPORTED_CITIES = [
    {'city': 'Arlington',      'state': 'TX'},
    {'city': 'Austin',         'state': 'TX'},
    {'city': 'Corpus Christi', 'state': 'TX'},
    {'city': 'Dallas',         'state': 'TX'},
    {'city': 'Denton',         'state': 'TX'},
    {'city': 'El Paso',        'state': 'TX'},
    {'city': 'Fort Worth',     'state': 'TX'},
    {'city': 'Frisco',         'state': 'TX'},
    {'city': 'Garland',        'state': 'TX'},
    {'city': 'Houston',        'state': 'TX'},
    {'city': 'Irving',         'state': 'TX'},
    {'city': 'Lubbock',        'state': 'TX'},
    {'city': 'McKinney',       'state': 'TX'},
    {'city': 'Plano',          'state': 'TX'},
    {'city': 'San Antonio',    'state': 'TX'},
]


_CUSTOMER_VISIBLE_CITIES_CACHE: dict = {'ts': 0.0, 'value': []}
_CUSTOMER_VISIBLE_CITIES_TTL_SECS = 300  # 5-min in-process cache
_MIN_PERMITS_FOR_VISIBLE = 2

def _bust_customer_visible_cities_cache() -> None:
    """Drop the in-process customer-visible cache so the next read
    rebuilds from the freshly-mutated curated list. Called from every
    function that writes ``system_settings.supported_cities``."""
    _CUSTOMER_VISIBLE_CITIES_CACHE['ts']    = 0.0
    _CUSTOMER_VISIBLE_CITIES_CACHE['value'] = []


def get_supported_cities() -> list:
    stored = get_system_setting('supported_cities')
    if stored is None:
        set_system_setting('supported_cities', _DEFAULT_SUPPORTED_CITIES)
        return list(_DEFAULT_SUPPORTED_CITIES)
    return stored


def add_supported_city(city: str, state: str) -> bool:
    cities = get_supported_cities()
    if any(c['city'].lower() == city.lower() for c in cities):
        return False
    cities.append({'city': city.strip().title(), 'state': state.strip().upper()})
    cities.sort(key=lambda c: (c['state'], c['city']))
    set_system_setting('supported_cities', cities)
    _bust_customer_visible_cities_cache()
    return True


def remove_supported_city(city: str) -> bool:
    cities = get_supported_cities()
    new = [c for c in cities if c['city'].lower() != city.lower()]
    if len(new) == len(cities):
        return False
    set_system_setting('supported_cities', new)
    _bust_customer_visible_cities_cache()
    return True

def get_customer_visible_states(*, force_refresh: bool = False,
                                 min_permits: int | None = None) -> list[dict]:
    """States we sell on the customer-facing pickers (onboarding step 2
    and settings → coverage). A state graduates from "data exists" to
    "customer visible" once it has at least ``min_permits`` permit rows
    — defaults to 100 (the "solid" tier from /admin-panel/scraper-stats/).

    Returns ``[{'state': 'CA', 'name': 'California', 'count': 6473}, ...]``
    sorted by permit count DESC then name ASC. This replaces the old
    nested state→cities picker that ``get_customer_visible_cities()``
    fed — we now sell whole-state coverage at the new $79 / $149 / $349
    state-based tiers (May 2026).
    """
    if min_permits is None:
        min_permits = 100  # match the "solid" tier on the admin stats page
    # 30-day window so a state that stopped producing data months ago
    # falls out of the picker even if the all-time count is fine.
    rows = pg.query(
        """SELECT UPPER(state) AS state, COUNT(*) AS n
             FROM permits
            WHERE state IS NOT NULL AND state <> ''
              AND issued_date >= (CURRENT_DATE - INTERVAL '30 days')
            GROUP BY UPPER(state)
           HAVING COUNT(*) >= %s
            ORDER BY COUNT(*) DESC, UPPER(state) ASC""",
        (int(min_permits),),
    )
    # Lazy import to avoid a circular dependency at module-load time.
    from core import views as _v
    out = []
    for r in rows or []:
        st = (r['state'] or '').upper()
        if len(st) != 2:  # skip stray non-USPS values
            continue
        out.append({
            'state': st,
            'name':  _v._FULL_STATE_NAMES.get(st, st),
            'count': int(r['n']),
        })
    return out


def get_customer_visible_cities(*, force_refresh: bool = False) -> list:
    """Subset of ``get_supported_cities()`` that customers actually see in
    the /settings/ city picker and that the add-city POST validator accepts.

    A city only graduates from "supported" (admin curated) to "customer
    visible" once it has at least ``_MIN_PERMITS_FOR_VISIBLE`` real rows
    in the ``permits`` table. This keeps junk entries (one-off scrape
    typos like ``"405, CO"`` or ``"ac, rockwell, NC"``, plus cities the
    scraper was added for but never produced data) out of the customer
    picker — those previously generated support tickets the moment a
    customer subscribed and got an empty feed.

    Admin tooling continues to call ``get_supported_cities()`` directly
    so the curated list stays the source of truth and "city was added
    but data is still ramping up" stays diagnosable from the admin UI.

    Returns the same shape as ``get_supported_cities()``:
    ``[{'city': str, 'state': str}, ...]``. Result is cached in-process
    for ``_CUSTOMER_VISIBLE_CITIES_TTL_SECS`` to keep settings page
    loads cheap; pass ``force_refresh=True`` to invalidate.
    """
    import time
    now = time.time()
    cache = _CUSTOMER_VISIBLE_CITIES_CACHE
    if (not force_refresh
            and cache['value']
            and now - cache['ts'] < _CUSTOMER_VISIBLE_CITIES_TTL_SECS):
        return list(cache['value'])

    supported = get_supported_cities() or []
    if not supported:
        cache['value'] = []
        cache['ts']    = now
        return []

    # Single round-trip: count permits per (city, state) and keep only
    # those above the threshold. Compared case-insensitively because
    # the curated list is title-cased ("Fort Worth") while the scraper
    # writes whatever the source page used ("FORT WORTH" / "fort worth").
    try:
        rows = pg.query(
            """SELECT LOWER(city) AS city_lc, UPPER(state) AS state_uc, COUNT(*) AS n
               FROM permits
               GROUP BY LOWER(city), UPPER(state)
               HAVING COUNT(*) >= %s""",
            (_MIN_PERMITS_FOR_VISIBLE,),
        )
    except Exception:
        # On a DB hiccup, fail OPEN — return the full curated list
        # rather than blanking the picker, which would be worse UX
        # than the original problem we're trying to solve.
        return list(supported)

    eligible = {(r['city_lc'], r['state_uc']) for r in rows}
    visible  = [c for c in supported
                if (c['city'].lower(), c['state'].upper()) in eligible]
    cache['value'] = visible
    cache['ts']    = now
    return list(visible)


def bulk_remove_supported_cities(city_names: list) -> int:
    """Remove every city whose name (case-insensitive) appears in city_names.
    Returns the number actually removed."""
    if not city_names:
        return 0
    targets = {c.lower() for c in city_names if c}
    cities = get_supported_cities()
    new = [c for c in cities if c['city'].lower() not in targets]
    removed = len(cities) - len(new)
    if removed:
        set_system_setting('supported_cities', new)
        _bust_customer_visible_cities_cache()
    return removed


# ── Support Tickets ────────────────────────────────────────────

def _ticket_id() -> str:
    from datetime import datetime
    import random, string
    ts = datetime.now().strftime('%y%m%d')
    rnd = ''.join(random.choices(string.digits, k=4))
    return f'TKT-{ts}-{rnd}'


_PRIO_ORDER = {'urgent': 0, 'high': 1, 'normal': 2, 'low': 3}


def create_ticket(user_id: int, user_email: str, user_name: str,
                  subject: str, message: str, category: str = 'general',
                  priority: str = 'normal') -> dict:
    from datetime import datetime
    now = datetime.now().isoformat()
    rec = {
        'ticket_id':  _ticket_id(),
        'user_id':    int(user_id),
        'user_email': user_email,
        'user_name':  user_name or user_email,
        'subject':    subject.strip(),
        'category':   category,
        'status':     'open',
        'priority':   priority if priority in ('urgent', 'normal', 'low') else 'normal',
        'created_at': now,
        'updated_at': now,
        'messages': [
            {'sender': 'user', 'name': user_name or user_email,
             'text': message.strip(), 'ts': now}
        ],
    }
    row = pg.execute_returning(
        """INSERT INTO support_tickets
               (ticket_id, user_id, status, priority, data)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (rec['ticket_id'], int(user_id), rec['status'], rec['priority'], Json(rec)),
    )
    rec['id'] = row['id']
    return rec


def get_ticket(doc_id: int) -> dict | None:
    row = pg.query_one(
        "SELECT id, data FROM support_tickets WHERE id = %s",
        (int(doc_id),),
    )
    return _row_to_doc(row)


def get_tickets_for_user(user_id: int) -> list:
    rows = pg.query(
        """SELECT id, data FROM support_tickets
            WHERE user_id = %s
            ORDER BY data->>'updated_at' DESC NULLS LAST""",
        (int(user_id),),
    )
    out = [_row_to_doc(r) for r in rows]
    out.sort(key=lambda x: _PRIO_ORDER.get(x.get('priority', 'normal'), 2))
    return out


def get_ticket_status_counts() -> dict:
    """Return ``{'open': n, 'in_progress': n, 'resolved': n, 'closed': n,
    'all': n}`` in a single GROUP BY query.

    Replaces five sequential ``get_all_tickets()`` scans previously used to
    populate the admin support sidebar counters. Backed by the existing
    ``support_tickets_status_user_idx (status, user_id)`` index.
    """
    rows = pg.query(
        "SELECT status, COUNT(*) AS n FROM support_tickets GROUP BY status"
    )
    counts = {'open': 0, 'in_progress': 0, 'resolved': 0, 'closed': 0}
    total = 0
    for r in rows:
        n = int(r.get('n') or 0)
        total += n
        s = r.get('status')
        if s in counts:
            counts[s] = n
    counts['all'] = total
    return counts


def get_all_tickets(status_filter: str = '') -> list:
    if status_filter:
        rows = pg.query(
            """SELECT id, data FROM support_tickets
                WHERE status = %s
                ORDER BY data->>'updated_at' DESC NULLS LAST""",
            (status_filter,),
        )
    else:
        rows = pg.query(
            """SELECT id, data FROM support_tickets
                ORDER BY data->>'updated_at' DESC NULLS LAST"""
        )
    return [_row_to_doc(r) for r in rows]


def add_ticket_message(doc_id: int, sender: str, name: str, text: str) -> bool:
    """Atomically append a message to the ticket's messages array."""
    from datetime import datetime
    now = datetime.now().isoformat()
    msg = {'sender': sender, 'name': name, 'text': text.strip(), 'ts': now}
    row = pg.execute_returning(
        """UPDATE support_tickets
              SET data = jsonb_set(
                            jsonb_set(data, '{messages}',
                                COALESCE(data->'messages', '[]'::jsonb) || %s::jsonb,
                                true),
                            '{updated_at}', to_jsonb(%s::text), true)
            WHERE id = %s
            RETURNING id""",
        (Json([msg]), now, int(doc_id)),
    )
    return row is not None


def delete_ticket(doc_id: int) -> bool:
    try:
        n = pg.execute("DELETE FROM support_tickets WHERE id = %s", (int(doc_id),))
    except (TypeError, ValueError):
        return False
    return bool(n)


def update_ticket_status(doc_id: int, status: str) -> bool:
    from datetime import datetime
    if status not in {'open', 'in_progress', 'resolved', 'closed'}:
        return False
    now = datetime.now().isoformat()
    row = pg.execute_returning(
        """UPDATE support_tickets
              SET status = %s,
                  data   = jsonb_set(
                              jsonb_set(data, '{status}', to_jsonb(%s::text), true),
                              '{updated_at}', to_jsonb(%s::text), true)
            WHERE id = %s
            RETURNING id""",
        (status, status, now, int(doc_id)),
    )
    return row is not None


def update_ticket_priority(doc_id: int, priority: str) -> bool:
    from datetime import datetime
    if priority not in {'low', 'normal', 'high', 'urgent'}:
        return False
    now = datetime.now().isoformat()
    row = pg.execute_returning(
        """UPDATE support_tickets
              SET priority = %s,
                  data     = jsonb_set(
                                jsonb_set(data, '{priority}', to_jsonb(%s::text), true),
                                '{updated_at}', to_jsonb(%s::text), true)
            WHERE id = %s
            RETURNING id""",
        (priority, priority, now, int(doc_id)),
    )
    return row is not None


# ── Notifications ───────────────────────────────────────────────

_DEFAULT_NOTIF_PREFS = {
    'daily_digest':      True,
    'billing_reminders': True,
    'product_updates':   False,
}


_DEFAULT_DIGEST_SCHEDULE = {
    'time': '06:00',           # HH:MM 24h local clock
    'tz':   'America/New_York',
}


def get_digest_schedule(user_id: int) -> dict:
    """When the daily Email Digest should be delivered for this user.

    Stored under ``digest_schedule`` on the user JSONB. Every plan
    receives one daily email — the user picks the local clock time
    and timezone here, and the cron sender converts to UTC at send
    time.
    """
    user  = get_user_by_id(user_id) or {}
    saved = user.get('digest_schedule', {}) or {}
    return {k: (saved.get(k) or v) for k, v in _DEFAULT_DIGEST_SCHEDULE.items()}


def save_digest_schedule(user_id: int, time_hhmm: str, tz: str) -> bool:
    """Persist the user's chosen delivery time + timezone for the digest."""
    t = (time_hhmm or '').strip()
    z = (tz or '').strip()
    # Defensive parse — accept 'H:MM' or 'HH:MM' 24h, fall back to default.
    try:
        hh, mm = t.split(':', 1)
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        t = f'{hh:02d}:{mm:02d}'
    except (ValueError, AttributeError):
        t = _DEFAULT_DIGEST_SCHEDULE['time']
    if not z:
        z = _DEFAULT_DIGEST_SCHEDULE['tz']
    return update_user(user_id, digest_schedule={'time': t, 'tz': z})


def claim_welcome_email_slot(user_id: int) -> bool:
    """Atomically stamp ``welcome_email_sent_at`` on the user JSONB and
    return ``True`` only if THIS call was the one that did it.

    Race fix for PR #398 / #399: the welcome email is fired once,
    alongside the first payment-success receipt. The receipt is dedup'd
    on ``(membership_id, plan)`` but the welcome email was previously
    gated by a Python-side ``if not user.get('welcome_email_sent_at')``
    followed by a separate ``update_user`` write — non-atomic. Whop
    fires both a post-checkout redirect AND a webhook for the same
    payment, both call ``_maybe_fire_payment_success_email``, and if
    they hit the welcome gate before either has stamped the user we'd
    send two welcome emails.

    This single-roundtrip ``UPDATE … WHERE … IS NULL RETURNING id`` is
    serialized by Postgres's row lock, so exactly one caller — the
    winner — sees the ``RETURNING`` row and gets ``True``. Subsequent
    callers see no row and get ``False``, so they skip the send.
    Caller then runs ``_fire_welcome_email`` only when this returns
    ``True``.
    """
    from datetime import datetime as _dt, timezone as _tz
    stamp = _dt.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    row = pg.execute_returning(
        """UPDATE users
              SET data = jsonb_set(
                            data, '{welcome_email_sent_at}',
                            to_jsonb(%s::text), true)
            WHERE id = %s
              AND COALESCE(NULLIF(data->>'welcome_email_sent_at', ''), '') = ''
            RETURNING id""",
        (stamp, int(user_id)),
    )
    return row is not None


def create_notification(user_id: int, type_key: str, type_label: str,
                        subject: str, preview: str, recipient: str,
                        channel: str, status_key: str, status_label: str,
                        sent_at: str) -> dict:
    rec = {
        'user_id':      int(user_id),
        'type_key':     type_key,
        'type_label':   type_label,
        'subject':      subject,
        'preview':      preview,
        'recipient':    recipient,
        'channel':      channel,
        'status_key':   status_key,
        'status_label': status_label,
        'sent_at':      sent_at,
    }
    row = pg.execute_returning(
        """INSERT INTO notifications
               (user_id, type_key, status_key, sent_at, data)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (int(user_id), type_key, status_key, sent_at, Json(rec)),
    )
    rec['id'] = row['id']
    return rec


def get_notifications_for_user(user_id: int, limit: int = 10,
                                offset: int = 0,
                                type_filter: str = '') -> list:
    if type_filter and type_filter != 'all':
        rows = pg.query(
            """SELECT id, data FROM notifications
                WHERE user_id = %s AND type_key = %s
                ORDER BY sent_at DESC NULLS LAST
                LIMIT %s OFFSET %s""",
            (int(user_id), type_filter, int(limit), int(offset)),
        )
    else:
        rows = pg.query(
            """SELECT id, data FROM notifications
                WHERE user_id = %s
                ORDER BY sent_at DESC NULLS LAST
                LIMIT %s OFFSET %s""",
            (int(user_id), int(limit), int(offset)),
        )
    return [_row_to_doc(r) for r in rows]


def count_notifications_for_user(user_id: int, type_filter: str = '') -> int:
    if type_filter and type_filter != 'all':
        row = pg.query_one(
            "SELECT COUNT(*) AS n FROM notifications WHERE user_id = %s AND type_key = %s",
            (int(user_id), type_filter),
        )
    else:
        row = pg.query_one(
            "SELECT COUNT(*) AS n FROM notifications WHERE user_id = %s",
            (int(user_id),),
        )
    return int(row['n']) if row else 0


def get_all_notifications_for_user(user_id: int, type_filter: str = '') -> list:
    if type_filter and type_filter != 'all':
        rows = pg.query(
            """SELECT id, data FROM notifications
                WHERE user_id = %s AND type_key = %s
                ORDER BY sent_at DESC NULLS LAST""",
            (int(user_id), type_filter),
        )
    else:
        rows = pg.query(
            """SELECT id, data FROM notifications
                WHERE user_id = %s
                ORDER BY sent_at DESC NULLS LAST""",
            (int(user_id),),
        )
    return [_row_to_doc(r) for r in rows]


def mark_notification_opened(notif_id: int) -> bool:
    pg.execute(
        """UPDATE notifications
              SET status_key = 'opened',
                  data = jsonb_set(
                            jsonb_set(data, '{status_key}',   '"opened"'::jsonb, true),
                            '{status_label}', '"Opened"'::jsonb, true)
            WHERE id = %s""",
        (int(notif_id),),
    )
    return True


def get_notification_stats(user_id: int) -> dict:
    from datetime import datetime, timedelta
    rows = pg.query(
        "SELECT id, data FROM notifications WHERE user_id = %s",
        (int(user_id),),
    )
    all_rows = [_row_to_doc(r) for r in rows]
    total = len(all_rows)
    week_ago  = (datetime.now() - timedelta(days=7)).isoformat()
    two_weeks = (datetime.now() - timedelta(days=14)).isoformat()
    week_rows  = [r for r in all_rows if (r.get('sent_at') or '') >= week_ago]
    lweek_rows = [r for r in all_rows if two_weeks <= (r.get('sent_at') or '') < week_ago]
    this_week  = len(week_rows)
    week_delta = this_week - len(lweek_rows)
    if total > 0:
        opened = len([r for r in all_rows if r.get('status_key') == 'opened'])
        open_rate = round(opened / total * 100)
    else:
        open_rate = 0
    return {
        'total':      total,
        'this_week':  this_week,
        'week_delta': week_delta,
        'open_rate':  open_rate,
    }


def get_notif_prefs(user_id: int) -> dict:
    user  = get_user_by_id(user_id) or {}
    saved = user.get('alert_prefs', {}) or {}
    return {k: saved.get(k, v) for k, v in _DEFAULT_NOTIF_PREFS.items()}


def save_notif_prefs(user_id: int, prefs: dict) -> bool:
    merged = dict(_DEFAULT_NOTIF_PREFS)
    merged.update({k: bool(v) for k, v in prefs.items() if k in _DEFAULT_NOTIF_PREFS})
    return update_user(user_id, alert_prefs=merged)


def get_notif_channels(user_id: int) -> dict:
    user = get_user_by_id(user_id) or {}
    defaults = {'email': True, 'slack_webhook': '', 'webhook_url': '', 'sms_phone': ''}
    stored   = user.get('notif_channels', {}) or {}
    return {**defaults, **stored}


def save_notif_channel(user_id: int, key: str, value) -> bool:
    if key not in {'email', 'slack_webhook', 'webhook_url', 'sms_phone'}:
        return False
    channels      = get_notif_channels(user_id)
    channels[key] = value
    return update_user(user_id, notif_channels=channels)


# ── CRM integrations (per-user OAuth + Zapier webhook) ───────────────────────
#
# Stored under a single ``crm_integrations`` JSON field on the user record.
# Shape:
#   {
#     'hubspot': {access_token, refresh_token, expires_at, scope, hub_id,
#                 connected_at, account_label?},
#     'ghl':     {access_token, refresh_token, expires_at, scope, locationId,
#                 connected_at, account_label?},
#     'zapier':  {webhook_url, saved_at},
#   }

CRM_PROVIDERS = ('hubspot', 'ghl', 'zapier')


def get_crm_integrations(user_id: int) -> dict:
    user     = get_user_by_id(user_id) or {}
    stored   = user.get('crm_integrations', {}) or {}
    return {p: dict(stored.get(p, {}) or {}) for p in CRM_PROVIDERS}


def _patch_crm_provider(user_id: int, provider: str, new_record: dict) -> bool:
    """Replace just the ``provider`` key inside the user's ``crm_integrations``
    JSONB column atomically.

    Using ``jsonb_set`` (with ``COALESCE`` to seed an empty object when the
    column is null) means concurrent writes that touch *different* providers
    no longer overwrite each other — the previous read-modify-write pattern
    could clobber a HubSpot token save with a stale snapshot from a Zapier
    save in another tab. This still races with another writer touching the
    *same* provider, but that is the expected last-write-wins UX.
    """
    if provider not in CRM_PROVIDERS:
        return False
    # The user record is stored as a JSONB ``data`` blob (see update_user).
    # We patch ``data['crm_integrations'][provider]`` in a single statement so
    # two browser tabs editing different providers cannot overwrite each other.
    # ``jsonb_set`` with ``create_missing=true`` seeds the nested object on
    # first write.
    pg.execute(
        "UPDATE users "
        "SET data = jsonb_set("
        "    jsonb_set(data, ARRAY['crm_integrations']::text[], "
        "              COALESCE(data->'crm_integrations', '{}'::jsonb), true), "
        "    ARRAY['crm_integrations', %s]::text[], "
        "    %s::jsonb, true) "
        "WHERE id = %s",
        (provider, json.dumps(new_record or {}), int(user_id)),
    )
    return True


def set_crm_oauth_tokens(user_id: int, provider: str, tokens: dict, account_label: str = '') -> bool:
    """Persist normalized OAuth tokens for ``provider`` on the user record."""
    if provider not in {'hubspot', 'ghl'}:
        return False
    record                  = dict(tokens or {})
    record['connected_at']  = datetime.utcnow().isoformat()
    if account_label:
        record['account_label'] = account_label
    return _patch_crm_provider(user_id, provider, record)


def update_crm_provider_field(user_id: int, provider: str, **fields) -> bool:
    """Patch a few keys on a CRM provider record (used by token refresh)."""
    if provider not in CRM_PROVIDERS:
        return False
    integrations = get_crm_integrations(user_id)
    cur          = dict(integrations.get(provider, {}) or {})
    cur.update(fields)
    return _patch_crm_provider(user_id, provider, cur)


def save_zapier_webhook(user_id: int, url: str) -> bool:
    return _patch_crm_provider(user_id, 'zapier', {
        'webhook_url': (url or '').strip(),
        'saved_at':    datetime.utcnow().isoformat(),
    })


def disconnect_crm_provider(user_id: int, provider: str) -> bool:
    if provider not in CRM_PROVIDERS:
        return False
    return _patch_crm_provider(user_id, provider, {})


# ── permits ─────────────────────────────────────────────────────────────
#
# Source of truth for permit records pushed by the external scraper platform.
# Schema lives in scripts/init_permits_table.py — keep the column lists in sync.

# Mutable columns the scraper is allowed to populate / overwrite on upsert.
# `scraper_run_id` was added late so the admin can list / delete every permit
# a particular run created. The column is NULLABLE — older permits and any
# rows ingested via the public `/api/v1/permits/ingest/` endpoint won't have
# a run id, and that's fine. The FK is `ON DELETE SET NULL`, so deleting a
# run NEVER cascades into permits unless the admin explicitly opts in via
# `delete_scraper_run(... delete_permits=True)`.
_PERMIT_COLUMNS = (
    'permit_number', 'state', 'city', 'jurisdiction', 'address', 'zip',
    'latitude', 'longitude',
    'owner_name', 'contractor_name', 'contractor_phone', 'contractor_email',
    # Unified primary-contact pair derived from the two names above by
    # core.scraper_accela._normalise_permit (contractor wins, falls back
    # to owner). Stored as real columns so the admin grid + CSV export
    # can read them with a plain SELECT instead of CASE expressions.
    'contact_name', 'contact_type',
    'permit_type', 'description', 'trade', 'status',
    'valuation_cents', 'square_feet',
    'applied_date', 'issued_date', 'expires_date',
    'ai_score', 'ai_grade', 'ai_tier',
    'ai_model_version', 'ai_scored_at',
    'scraper_run_id',
    # Cross-source dedup fingerprint. upsert_permit() always rewrites
    # this from the canonical fields below, so callers don't need to
    # provide it — but listing it here lets bulk migrations / backfills
    # pass it through cleanly when they recompute hashes server-side.
    'dedup_hash',
)


# Fields that participate in the cross-source dedup fingerprint. The
# user spec for "no duplicates" is the composite the per-scraper
# permits table renders: Permit # / Type / Address / City / Contractor
# / Email / Phone / Date / Project value. We translate that to actual
# column names + tack on `state` so a permit number that legitimately
# repeats across two states (rare but real for federally-recycled
# numeric prefixes) doesn't false-positive collapse.
_DEDUP_FIELDS = (
    'permit_number', 'state', 'city',
    'address', 'trade',
    'contractor_name', 'contractor_email', 'contractor_phone',
    'issued_date', 'valuation_cents',
)


def _norm_for_dedup(value) -> str:
    """Lowercase + collapse whitespace + strip non-alphanum noise so two
    payloads that mean the same thing hash the same.

    Examples (all collapse to the same token):
        '123 Main St.'   ->  '123mainst'
        '  123 main st ' ->  '123mainst'
        '123-Main St'    ->  '123mainst'

    Equivalence guarantees that matter for cross-source dedup:
    - ``None`` / ``''`` / ``'   '`` / ``0`` / ``'0'`` / ``0.0`` /
      ``'0.00'`` all collapse to ``''`` — empty signal is *the same*
      empty signal regardless of how the source represents it. This
      matters for ``valuation_cents`` in particular: one source emits
      `null`, another emits `0`, and they must hash identically or the
      same physical permit splits across two rows.
    - Bools collapse to ``''`` — they have no business participating in
      the fingerprint and we never want ``True`` to alias ``'1'``.
    - Numerics canonicalize to integer-string when they are whole
      numbers (e.g. ``1500`` and ``1500.0`` and ``'1,500'`` and
      ``'1500.00'`` all hash as ``'1500'``), and to lossless decimal
      otherwise.
    - Dates/datetimes are always ISO ``YYYY-MM-DD`` so an agent that
      returned ``'5/3/2026'`` and a legacy parser that returned
      ``date(2026, 5, 3)`` agree.
    """
    if value is None or isinstance(value, bool):
        # Bool is intentional: ``isinstance(True, int)`` is True, so we
        # have to reject bools BEFORE the int branch or `True` aliases
        # `'1'`.
        return ''
    if isinstance(value, (date, datetime)):
        try:
            s = value.isoformat()[:10]
        except Exception:
            return ''
    elif isinstance(value, (int, float)):
        # Treat 0 / 0.0 / -0.0 / NaN as "no signal".
        try:
            if value != value or value == 0:  # NaN or zero
                return ''
        except Exception:
            return ''
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        s = (str(value) if isinstance(value, int)
             else repr(float(value))).strip().lower()
    else:
        s = str(value).strip().lower()
        if not s:
            return ''
        # If the string looks like a number ("1,500", "1500.00",
        # "$1,500.00") canonicalize it through float→int so it aliases
        # whatever upstream type was used. We DELIBERATELY do this only
        # for strings that, after stripping currency/comma/whitespace,
        # are purely numeric — anything else (e.g. permit numbers like
        # "B12-345") falls through to the alphanum collapse below.
        cleaned = s.replace(',', '').lstrip('$').strip()
        if cleaned and cleaned not in ('-', '.', '-.'):
            try:
                f = float(cleaned)
                if f != f or f == 0:
                    return ''
                if f.is_integer():
                    s = str(int(f))
                else:
                    s = repr(f)
            except (TypeError, ValueError):
                pass
    # Keep only [a-z0-9] so trivial typography differences don't change
    # the hash. Phone numbers reduce to digits, addresses lose
    # punctuation, dates lose hyphens (so '2026-04-01' aliases the
    # ISO of `date(2026,4,1)` after this collapse), and negative
    # numerics drop their sign so `-1500` and `'-1500'` agree.
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
    return ''.join(out)


def compute_permit_dedup_hash(permit: dict) -> str | None:
    """Return a stable SHA-256 fingerprint for the row's identifying
    fields, or ``None`` if the row lacks enough signal to confidently
    dedup against other sources.

    Rule: we need a permit_number AND at least one of (address,
    contractor_name, contractor_email, contractor_phone). Below that
    bar a row is too thin to risk collapsing into another scraper's
    output — keep it isolated under its native (source, source_permit_id).
    """
    if not isinstance(permit, dict):
        return None
    pnum = _norm_for_dedup(permit.get('permit_number'))
    if not pnum:
        return None
    secondary_keys = ('address', 'contractor_name',
                      'contractor_email', 'contractor_phone')
    if not any(_norm_for_dedup(permit.get(k)) for k in secondary_keys):
        return None
    parts = [_norm_for_dedup(permit.get(f)) for f in _DEDUP_FIELDS]
    raw = '|'.join(parts)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _normalize_permit_payload(p: dict) -> dict:
    """Trim/lowercase a few canonical fields and coerce types defensively.

    The scraper is treated as an untrusted source — we never trust strings to
    be the right shape and we silently coerce empties to None so the DB does
    not reject the row over cosmetic differences.
    """
    out = {k: p.get(k) for k in _PERMIT_COLUMNS}
    if out.get('state'):
        out['state'] = str(out['state']).strip().upper()[:8] or None
    if out.get('city'):
        out['city'] = str(out['city']).strip()[:120] or None
    if out.get('trade'):
        out['trade'] = str(out['trade']).strip().lower()[:60] or None
    if out.get('status'):
        out['status'] = str(out['status']).strip().lower()[:32] or None
    if out.get('ai_tier'):
        t = str(out['ai_tier']).strip().lower()
        out['ai_tier'] = t if t in ('hot', 'warm', 'cool') else None
    if out.get('ai_score') is not None:
        try:
            s = int(out['ai_score'])
            out['ai_score'] = max(0, min(100, s))
        except (TypeError, ValueError):
            out['ai_score'] = None
    if out.get('valuation_cents') is not None:
        try:
            out['valuation_cents'] = max(0, int(out['valuation_cents']))
        except (TypeError, ValueError):
            out['valuation_cents'] = None
    if out.get('square_feet') is not None:
        try:
            out['square_feet'] = max(0, int(out['square_feet']))
        except (TypeError, ValueError):
            out['square_feet'] = None
    # Clamp any future-dated permit dates to today. Scrapers occasionally
    # parse a wrong field (expiration → applied) or pull a permit whose
    # "issued" cell on the source portal is actually a scheduled future
    # date — those would show up on the dashboard as 2027-11-21 etc.,
    # which is obviously nonsense for a permit that was already filed.
    # We never allow a date in the future; if found, clamp to CURRENT_DATE
    # (UTC today). Strings, datetime.date and datetime.datetime are all
    # handled. Invalid date strings are left alone — the DB layer will
    # error loudly rather than silently mutate bad input.
    from datetime import date, datetime, timezone
    today = datetime.now(timezone.utc).date()
    for _col in ('applied_date', 'issued_date', 'expires_date'):
        v = out.get(_col)
        if not v:
            continue
        try:
            if isinstance(v, datetime):
                d = v.date()
            elif isinstance(v, date):
                d = v
            else:
                d = date.fromisoformat(str(v)[:10])
        except (TypeError, ValueError):
            continue
        if d > today:
            out[_col] = today.isoformat()
    return out


# ── Banned states (scraper ingest filter) ──────────────────────────
#
# Admin-managed list of 2-letter state codes that scrapers MUST NOT
# ingest. When a scraper pushes a permit whose state matches, the
# upsert is silently dropped (counted as ``banned`` in the batch
# result) so we never accept rows for states we don't sell. The list
# lives in ``system_settings.banned_states`` as a JSON array of
# upper-case codes; the helpers below are the only readers / writers.

def get_banned_states() -> list[str]:
    """Return upper-case 2-letter state codes banned from ingest."""
    raw = get_system_setting('banned_states') or []
    if isinstance(raw, str):
        # Defensive: legacy CSV form.
        raw = [s for s in raw.replace(',', ' ').split() if s]
    out: list[str] = []
    for s in raw:
        c = (str(s) or '').strip().upper()
        if len(c) == 2 and c.isalpha() and c not in out:
            out.append(c)
    return out


def set_banned_states(codes: list[str]) -> list[str]:
    """Persist the banned-states list and return the cleaned form."""
    cleaned: list[str] = []
    for s in codes or []:
        c = (str(s) or '').strip().upper()
        if len(c) == 2 and c.isalpha() and c not in cleaned:
            cleaned.append(c)
    set_system_setting('banned_states', cleaned)
    return cleaned


def add_banned_state(code: str) -> list[str]:
    return set_banned_states(get_banned_states() + [code])


def remove_banned_state(code: str) -> list[str]:
    code = (code or '').strip().upper()
    return set_banned_states([s for s in get_banned_states() if s != code])


def delete_permits_by_state(state: str) -> int:
    """Hard-delete every permit row matching the given state. Returns
    the number of rows removed. Caller must be admin (enforced at the
    view layer). Drops the /permits/ cache so user dashboards refresh."""
    code = (state or '').strip().upper()
    if not code:
        return 0
    row = pg.execute_returning(
        "WITH deleted AS (DELETE FROM permits WHERE UPPER(state) = %s RETURNING 1) "
        "SELECT COUNT(*) AS n FROM deleted",
        (code,),
    )
    n = int((row or {}).get('n') or 0)
    if n:
        _invalidate_permits_cache()
    return n


# ── junk_permits: known-junk lookup so re-scrapes don't re-spend AI ──
#
# A permit that has neither a contractor email nor phone is dead weight
# for the contractor user-base and gets dropped by ``upsert_permit``'s
# contact gate. BUT — without a record of "we already determined this
# permit_number is junk" — every subsequent scrape re-fetches the
# detail HTML and re-runs the LLM extraction just to reach the same
# verdict. Burnt $230 of inference in a single day this way. Solution:
# remember the junk verdict, keyed on the same ``(source, source_permit_id)``
# tuple ``permits`` uses, and short-circuit the per-detail loop BEFORE
# the Firecrawl/HTTP fetch + LLM call.
_JUNK_PERMITS_TABLE_READY = False


def _ensure_junk_permits_table() -> None:
    global _JUNK_PERMITS_TABLE_READY
    if _JUNK_PERMITS_TABLE_READY:
        return
    # Single-connection DDL with bounded lock_timeout — same pattern as
    # ``_ensure_scrapers_table`` so a busy ``permits`` table can't stall
    # the first scraper run on a fresh worker.
    try:
        with pg.conn() as _c:
            with _c.cursor() as _cur:
                _cur.execute("SET lock_timeout      = '2s'")
                _cur.execute("SET statement_timeout = '15s'")
                _cur.execute(
                    """CREATE TABLE IF NOT EXISTS junk_permits (
                          source            TEXT        NOT NULL,
                          source_permit_id  TEXT        NOT NULL,
                          permit_number     TEXT,
                          state             TEXT,
                          city              TEXT,
                          detail_url        TEXT,
                          reason            TEXT        NOT NULL DEFAULT 'no_contact',
                          created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                          PRIMARY KEY (source, source_permit_id)
                       )"""
                )
                _cur.execute(
                    "CREATE INDEX IF NOT EXISTS junk_permits_pnum_state_city_idx "
                    "ON junk_permits (LOWER(permit_number), UPPER(state), LOWER(city)) "
                    "WHERE permit_number IS NOT NULL"
                )
        _JUNK_PERMITS_TABLE_READY = True
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning(
            '_ensure_junk_permits_table: DDL skipped (%s)', _e.__class__.__name__)
        # Mark ready so misses don't keep re-hammering DDL during an
        # outage; the table is idempotent and will be retried next boot.
        _JUNK_PERMITS_TABLE_READY = True


def is_junk_permit(source: str, source_permit_id: str) -> bool:
    """Return True iff this (source, source_permit_id) was previously
    determined to be junk (no contractor email AND no contractor phone).

    Called by the scraper's per-grid-row pre-detail skip loop so we
    never re-pay the Firecrawl fetch + LLM extraction cost on a row
    we've already proven worthless.
    """
    if not source or not source_permit_id:
        return False
    _ensure_junk_permits_table()
    try:
        row = pg.query_one(
            "SELECT 1 FROM junk_permits "
            "WHERE source = %s AND source_permit_id = %s LIMIT 1",
            (source, source_permit_id),
        )
        return row is not None
    except Exception:
        return False


# Tokens that mark a contractor_name as a BUSINESS/company rather than a
# private person. When any of these appear (word-boundary, case-insensitive)
# the name is kept regardless of how many words it has — companies legitimately
# have one-word trade names ("Roto-Rooter"), so we only require first+last for
# names that look like a private individual.
_BUSINESS_NAME_TOKENS = frozenset({
    'llc', 'l.l.c', 'inc', 'inc.', 'incorporated', 'corp', 'corp.',
    'corporation', 'co', 'co.', 'company', 'ltd', 'lp', 'llp', 'pllc',
    'pc', 'plc', 'group', 'enterprises', 'enterprise', 'solutions',
    'systems', 'industries', 'associates', 'partners', 'holdings',
    'construction', 'contractors', 'contractor', 'contracting', 'builders',
    'building', 'plumbing', 'electric', 'electrical', 'hvac', 'mechanical',
    'heating', 'cooling', 'roofing', 'remodeling', 'remodel', 'services',
    'service', 'svcs', 'concrete', 'paving', 'landscaping', 'landscape',
    'painting', 'flooring', 'masonry', 'drywall', 'fencing', 'glass',
    'doors', 'windows', 'pools', 'pool', 'solar', 'restoration', 'design',
    'development', 'homes', 'home', 'properties', 'realty', 'management',
    'maintenance', 'utilities', 'energy', 'specialties', 'works',
})

# Consumer/free email providers. An email on one of these domains tells us
# NOTHING about whether the contact is a business — anybody can get a gmail
# address. An email on ANY OTHER domain is a custom/organisational domain
# (e.g. ``traviscrawfordhvac.com``, ``nvrinc.com``) and is therefore a
# strong company signal: real homeowners almost never own a vanity domain.
# Used by ``contractor_name_is_droppable_person`` so a legitimate one-word
# trade name backed by a business email survives the person gate.
_FREEMAIL_DOMAINS = frozenset({
    'gmail.com', 'googlemail.com', 'yahoo.com', 'ymail.com', 'rocketmail.com',
    'hotmail.com', 'outlook.com', 'live.com', 'msn.com', 'hotmail.co.uk',
    'aol.com', 'aim.com', 'icloud.com', 'me.com', 'mac.com',
    'comcast.net', 'att.net', 'sbcglobal.net', 'bellsouth.net', 'verizon.net',
    'cox.net', 'charter.net', 'earthlink.net', 'frontier.com', 'windstream.net',
    'protonmail.com', 'proton.me', 'gmx.com', 'mail.com', 'zoho.com',
    'yahoo.co.uk', 'yahoo.ca',
})


def contractor_name_is_droppable_person(name: str, *, email: str = '') -> bool:
    """Return True when ``name`` looks like a PRIVATE INDIVIDUAL who is
    missing a first OR last name (i.e. only one usable name word), and is
    NOT a business.

    Companies are always kept: a name containing any business token
    (``LLC``, ``Plumbing``, ``Construction``, ``&``, a digit, …) returns
    False even if it's a single word. Empty names also return False — the
    contact gate already handled "no contact"; a missing name alone is not
    grounds to drop a row that has a phone/email.

    A custom (non-freemail) ``email`` domain is *also* treated as a company
    signal: a one-word trade name like ``Crawford`` backed by
    ``permit@traviscrawfordhvac.com`` (or ``NVR`` / ``pbobbitt@nvrinc.com``)
    is a real business lead, not a homeowner, so it must survive this gate.

    Examples::

        contractor_name_is_droppable_person('John')              -> True
        contractor_name_is_droppable_person('Smith')             -> True
        contractor_name_is_droppable_person('John Smith')        -> False
        contractor_name_is_droppable_person('Smith Plumbing LLC')-> False
        contractor_name_is_droppable_person('Roto-Rooter Inc')   -> False
        contractor_name_is_droppable_person(
            'Crawford', email='permit@traviscrawfordhvac.com')   -> False
        contractor_name_is_droppable_person(
            'John', email='john@gmail.com')                      -> True
    """
    raw = (name or '').strip()
    if not raw:
        return False
    # A business email domain (anything that isn't a consumer/free provider)
    # is a strong company signal — keep the row regardless of name shape.
    em = (email or '').strip().lower()
    if '@' in em:
        domain = em.rsplit('@', 1)[-1].strip().strip('.')
        if domain and domain not in _FREEMAIL_DOMAINS:
            return False
    low = raw.lower()
    # Obvious company signals: ampersand or any embedded digit (e.g.
    # "ABC 123 Services", "5 Star Roofing").
    if '&' in raw or any(ch.isdigit() for ch in raw):
        return False
    # Split on whitespace / commas; keep word tokens that contain a letter.
    import re as _re
    words = [w for w in _re.split(r'[\s,]+', low) if w]
    if any(tok.strip('.,') in _BUSINESS_NAME_TOKENS for tok in words):
        return False
    # Count tokens that carry at least one alphabetic character (drops
    # stray initials-only artefacts like a lone "." but keeps "J." etc).
    name_words = [w for w in words if any(c.isalpha() for c in w)]
    # A private person needs BOTH a first and a last name word. One word
    # (or zero alpha words) = missing first or last -> droppable.
    return len(name_words) < 2


def mark_junk_permit(source: str, source_permit_id: str, *,
                     permit_number: str = '', state: str = '',
                     city: str = '', detail_url: str = '',
                     reason: str = 'no_contact') -> None:
    """Record a permit as junk so future scrapes skip it pre-fetch.
    Idempotent: ON CONFLICT DO NOTHING so reruns don't churn writes."""
    if not source or not source_permit_id:
        return
    _ensure_junk_permits_table()
    try:
        pg.execute(
            "INSERT INTO junk_permits "
            "  (source, source_permit_id, permit_number, state, city, detail_url, reason) "
            "VALUES (%s, %s, NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''), %s) "
            "ON CONFLICT (source, source_permit_id) DO NOTHING",
            (source, source_permit_id,
             (permit_number or '').strip(),
             (state or '').strip().upper()[:8],
             (city  or '').strip()[:120],
             (detail_url or '').strip(),
             (reason or 'no_contact')[:64]),
        )
    except Exception:
        import logging as _log
        _log.getLogger(__name__).exception(
            'mark_junk_permit failed for %s/%s', source, source_permit_id)


def is_junk_permit_by_number(permit_number: str, state: str, city: str) -> bool:
    """Triple-key junk check — same shape as the cross-source dedup in
    ``upsert_permit``. Used when the scraper knows the municipal
    permit_number + (state, city) but the lineage ``source`` was a
    different scraper id (e.g. someone re-imported the same city under
    a new scraper row). Cheaper than re-fetching the detail page just
    to discover we already proved it junk under a sibling source."""
    pn = (permit_number or '').strip()
    if not pn:
        return False
    _ensure_junk_permits_table()
    try:
        row = pg.query_one(
            "SELECT 1 FROM junk_permits "
            "WHERE LOWER(permit_number) = LOWER(%s) "
            "  AND UPPER(state) = UPPER(%s) "
            "  AND LOWER(city)  = LOWER(%s) "
            "LIMIT 1",
            (pn, (state or '').strip(), (city or '').strip()),
        )
        return row is not None
    except Exception:
        return False


def _permit_locked_cols(where: str, params: tuple) -> set:
    """Return the set of ``_PERMIT_COLUMNS`` an admin has hand-edited
    (and therefore locked against rescrape) for the first permit row
    matching ``where``. Best-effort: any failure (incl. the column not
    existing yet on an un-migrated DB) returns an empty set so the upsert
    behaves exactly as it did before the manual-edit feature existed."""
    try:
        r = pg.query_one(
            f"SELECT manual_fields FROM permits WHERE {where} LIMIT 1",
            params,
        )
    except Exception:
        return set()
    if not r:
        return set()
    mf = r.get('manual_fields')
    if isinstance(mf, str):
        try:
            mf = json.loads(mf)
        except Exception:
            mf = []
    if not isinstance(mf, (list, tuple)):
        return set()
    return {str(c) for c in mf if c in _PERMIT_COLUMNS}


def _raw_assign_preserving_locked(new_raw_sql: str,
                                  locked_cols) -> tuple[str, list]:
    """Build the SQL expression for a ``raw`` assignment that keeps any
    manually-locked keys intact inside the JSONB blob on rescrape.

    ``update_permit`` writes a hand-edited value to BOTH the materialized
    column and the ``raw`` JSONB so the two never disagree. Every rescrape
    UPDATE path already excludes locked *columns*, but a naive
    ``raw = EXCLUDED.raw`` (or ``raw = %s``) would still overwrite the
    whole JSONB document and silently re-clobber the edited value inside
    ``raw`` — leaving the column correct but ``raw`` stale.

    This returns ``(expr, extra_params)`` where ``expr`` takes the freshly
    scraped raw (``new_raw_sql`` — e.g. ``EXCLUDED.raw`` or ``%s``) and
    overlays the locked keys' values from the EXISTING ``permits.raw`` back
    on top, so ``raw`` stays in lockstep with the materialized columns.
    When nothing is locked the expression is unchanged (zero behaviour
    change for the overwhelmingly common no-edit case).
    """
    if not locked_cols:
        return new_raw_sql, []
    expr = (
        f"{new_raw_sql} || COALESCE("
        "(SELECT jsonb_object_agg(k, permits.raw -> k) "
        " FROM unnest(%s::text[]) AS k "
        " WHERE permits.raw ? k), '{}'::jsonb)"
    )
    return expr, [sorted(locked_cols)]


def upsert_permit(permit: dict, *, invalidate_cache: bool = True) -> tuple[str, int] | None:
    """Insert or update a single permit, keyed on ``(source, source_permit_id)``.

    Partial payloads are supported: on UPDATE we only overwrite columns the
    caller actually provided. Fields the caller omitted retain their previous
    value, so the scraper can push small patches (e.g. just an updated
    ``status`` or ``ai_score``) without nulling the rest of the row.

    ``invalidate_cache`` defaults to True so any standalone caller (admin
    tools, single-row patches) drops the /permits/ TTL entries that may
    now be stale. ``bulk_upsert_permits`` passes ``False`` to skip the
    per-row dict scan and invalidates exactly once at the end of the
    batch, which matters when a scraper pushes 50k rows in one go.

    Returns ``(action, permit_id)`` where action is ``'inserted'`` or
    ``'updated'``, or ``None`` if required identifying fields are missing.
    """
    source     = (permit.get('source') or '').strip()
    source_uid = (permit.get('source_permit_id') or '').strip()
    state      = (permit.get('state') or '').strip()
    city       = (permit.get('city') or '').strip()
    if not (source and source_uid and state and city):
        permit['_skip_reason'] = 'missing_identity'
        return None

    # Contractor-contact gate: a permit with neither a contractor email
    # NOR a contractor phone is dead weight for the contractor user-base
    # (no one to call, no one to message). Skip the insert/update entirely
    # so the /permits/ feed only ever shows actionable leads. This is the
    # central choke-point — every scraper (Accela + city-specific) goes
    # through ``upsert_permit`` (directly or via ``bulk_upsert_permits``),
    # so guarding here is enough.
    _email = (permit.get('contractor_email') or '').strip()
    _phone = (permit.get('contractor_phone') or '').strip()
    if not _email and not _phone:
        # Remember the junk verdict so a future scrape never re-fetches
        # the detail page or re-pays the LLM extraction for this same
        # (source, source_permit_id). Best-effort: a write failure here
        # must NOT block the gate from rejecting the row.
        try:
            mark_junk_permit(
                source, source_uid,
                permit_number=(permit.get('permit_number') or ''),
                state=state, city=city,
                detail_url=((permit.get('raw') or {}).get('detail_url')
                            if isinstance(permit.get('raw'), dict)
                            else (permit.get('detail_url') or '')),
                reason='no_contact',
            )
        except Exception:
            pass
        permit['_skip_reason'] = 'no_contact'
        return None

    # ── Person-vs-company name gate ────────────────────────────────
    # We only expose CONTRACTOR contacts, never homeowners. A
    # contractor_name that reads like a private individual but is
    # missing a first OR last name ("John", "Smith") is almost always a
    # mis-scraped homeowner / partial record, not a real business lead —
    # drop it. Company names (anything with a business token, "&", or a
    # digit) are always kept, even single-word trade names.
    if contractor_name_is_droppable_person(permit.get('contractor_name'),
                                           email=_email):
        try:
            mark_junk_permit(
                source, source_uid,
                permit_number=(permit.get('permit_number') or ''),
                state=state, city=city,
                detail_url=((permit.get('raw') or {}).get('detail_url')
                            if isinstance(permit.get('raw'), dict)
                            else (permit.get('detail_url') or '')),
                reason='person_no_name',
            )
        except Exception:
            pass
        permit['_skip_reason'] = 'person_no_name'
        return None

    # ── Strict triple-key dedup: (permit_number, state, city) ──────
    # User contract: a single municipal permit number within one
    # (state, city) is the SAME permit, regardless of which scraper
    # source ingested it. This catches the case the existing
    # `dedup_hash` path misses — when a row has a permit_number but
    # no address / contractor info (so `compute_permit_dedup_hash`
    # returns None and the cross-source dedup never fires). We do a
    # case-insensitive triple lookup BEFORE the INSERT; if a row
    # already exists under a different (source, source_permit_id),
    # we UPDATE it in place with whatever new fields this scrape
    # brought, and return its id — so the dashboard never shows two
    # rows for the same permit number in the same city.
    pnum_norm = (permit.get('permit_number') or '').strip()
    if pnum_norm:
        try:
            existing = pg.execute_returning(
                "SELECT id, source, source_permit_id FROM permits "
                "WHERE LOWER(permit_number) = LOWER(%s) "
                "  AND UPPER(state) = UPPER(%s) "
                "  AND LOWER(city)  = LOWER(%s) "
                "LIMIT 1",
                (pnum_norm, state, city),
            )
        except Exception:
            existing = None
        if existing and not (
            (existing.get('source') or '') == source and
            (existing.get('source_permit_id') or '') == source_uid
        ):
            # Triple-key match under DIFFERENT lineage. Route this
            # upsert to the existing row instead of creating a
            # duplicate. Only overwrite columns the caller actually
            # provided (same partial-write contract as the main
            # upsert path) and never touch lineage columns.
            _LINEAGE_ONLY_COLS_TRIPLE = {'scraper_run_id', 'source',
                                         'source_permit_id'}
            _triple_locked = _permit_locked_cols(
                "id = %s", (int(existing['id']),))
            triple_cols = [c for c in _PERMIT_COLUMNS
                           if c in permit
                           and c not in _LINEAGE_ONLY_COLS_TRIPLE
                           and c not in _triple_locked]
            triple_fields = _normalize_permit_payload(
                {k: permit.get(k) for k in triple_cols}
            )
            # Always (re)write state/city to the canonical values used in
            # the lookup so the matched row's casing/normalisation stays
            # consistent — BUT never override a value the admin has
            # manually locked (manual_fields wins forever, in every path).
            if 'state' not in _triple_locked:
                triple_fields['state'] = state.upper()[:8]
                if 'state' not in triple_cols:
                    triple_cols.append('state')
            if 'city' not in _triple_locked:
                triple_fields['city'] = city[:120]
                if 'city' not in triple_cols:
                    triple_cols.append('city')
            # Build the SET clause. Every materialized column may be
            # locked (manual edits) leaving triple_cols empty — in that
            # case we still refresh `raw` + `updated_at` so the audit
            # blob reflects the latest scrape without touching any locked
            # column. The leading comma is only emitted when we actually
            # have columns to assign.
            assigns = ', '.join([f'{c} = %s' for c in triple_cols])
            set_prefix = (assigns + ', ') if assigns else ''
            _traw_assign, _traw_extra = _raw_assign_preserving_locked(
                '%s::jsonb', _triple_locked)
            try:
                pg.execute_returning(
                    f"UPDATE permits SET {set_prefix}raw = {_traw_assign}, "
                    "updated_at = now() "
                    "WHERE id = %s "
                    "RETURNING id",
                    tuple([triple_fields[c] for c in triple_cols]
                          + [Json(permit.get('raw') or permit)]
                          + _traw_extra + [int(existing['id'])]),
                )
            except Exception:
                log.exception('triple-key dedup UPDATE failed for permit_id=%s', existing.get('id'))
            else:
                if invalidate_cache:
                    _invalidate_permits_cache()
                return ('updated', int(existing['id']))

    # Compute the cross-source dedup fingerprint BEFORE we trim down to
    # `provided_cols` so it always reflects everything the caller knows
    # about this row. Always overwrite whatever the caller may have
    # passed in `permit['dedup_hash']` — we don't trust client-side
    # hashes (and `bulk_upsert_permits` callers don't want to compute
    # them themselves).
    permit['dedup_hash'] = compute_permit_dedup_hash(permit)

    # Only consider columns the caller actually included in the payload —
    # this is what makes the upsert non-destructive. Allow-list against
    # _PERMIT_COLUMNS so column names can never be attacker-controlled.
    provided_cols = [c for c in _PERMIT_COLUMNS if c in permit]
    # state + city are required so they always count as provided.
    for required in ('state', 'city'):
        if required not in provided_cols:
            provided_cols.append(required)
    # dedup_hash is always written so the partial unique index stays
    # in sync with current field values (e.g. an UPDATE that fills in a
    # missing email must recompute the hash).
    if 'dedup_hash' not in provided_cols:
        provided_cols.append('dedup_hash')

    fields = _normalize_permit_payload({k: permit.get(k) for k in provided_cols})
    fields['state'] = state.upper()[:8]
    fields['city']  = city[:120]
    fields['dedup_hash'] = permit['dedup_hash']  # NULL-safe, no truncation

    placeholders = ', '.join(['%s'] * (len(provided_cols) + 3))  # +source, +source_permit_id, +raw
    col_sql      = ', '.join(['source', 'source_permit_id'] + provided_cols + ['raw'])
    # Lineage MUST stick to the run that ORIGINALLY created the permit, so
    # `scraper_run_id` is set on INSERT only. If a later run re-scrapes
    # this same (source, source_permit_id), we still update the data
    # columns but we leave the lineage alone — otherwise "delete this
    # run + permits" would wipe permits that an earlier run created and
    # this run merely refreshed, which is exactly the lineage bug the
    # architect flagged.
    _LINEAGE_ONLY_COLS = {'scraper_run_id'}
    # Columns a human hand-edited on this exact (source, source_permit_id)
    # row are locked: exclude them from the UPDATE so a rescrape preserves
    # the corrected value. (INSERT still writes every column; the lock
    # only matters on the ON CONFLICT DO UPDATE branch.)
    _locked_cols = _permit_locked_cols(
        "source = %s AND source_permit_id = %s", (source, source_uid))
    update_cols        = [c for c in provided_cols
                          if c not in _LINEAGE_ONLY_COLS
                          and c not in _locked_cols]
    update_assignments = ', '.join([f'{c} = EXCLUDED.{c}' for c in update_cols]) \
                          if update_cols else ''

    # Keep any hand-edited keys intact inside the `raw` JSONB too, not just
    # the materialized columns (see _raw_assign_preserving_locked).
    _raw_assign, _raw_extra = _raw_assign_preserving_locked(
        'EXCLUDED.raw', _locked_cols)

    if update_assignments:
        on_conflict = (
            "ON CONFLICT (source, source_permit_id) DO UPDATE SET\n"
            f"            {update_assignments},\n"
            f"            raw = {_raw_assign}"
        )
    else:
        # Pathological: caller only supplied lineage columns. Still need
        # ON CONFLICT so the RETURNING clause behaves uniformly, but we
        # only refresh `raw` (lineage stays sticky to the first run).
        on_conflict = (
            "ON CONFLICT (source, source_permit_id) DO UPDATE SET\n"
            f"            raw = {_raw_assign}"
        )

    sql = f"""
        INSERT INTO permits ({col_sql})
        VALUES ({placeholders})
        {on_conflict}
        RETURNING id, (xmax = 0) AS inserted
    """
    params = ([source, source_uid] + [fields[c] for c in provided_cols]
              + [Json(permit.get('raw') or permit)] + _raw_extra)
    try:
        row = pg.execute_returning(sql, tuple(params))
    except psycopg_errors.UniqueViolation as e:
        # Cross-source dedup hit: this row's `dedup_hash` matches an
        # already-stored permit that lives under a DIFFERENT (source,
        # source_permit_id). The Postgres ON CONFLICT clause above only
        # targets the (source, source_permit_id) constraint, so it
        # cannot resolve a collision against `permits_dedup_hash_uq`.
        # Fall back to UPDATE-by-hash so the existing row is enriched
        # with whatever new fields this scrape brought, but lineage and
        # the original (source, source_permit_id) stay put.
        _err = str(e)
        if 'permits_dedup_hash_uq' not in _err and 'dedup_hash' not in _err:
            raise
        existing_hash = permit['dedup_hash']
        if not existing_hash:
            raise
        # Skip lineage fields on cross-source updates too — same rule
        # as the ON CONFLICT clause above. Also honour any manual-edit
        # lock on the matched row (keyed by dedup_hash, which is a
        # different row than the (source, source_permit_id) target).
        _hash_locked = _permit_locked_cols(
            "dedup_hash = %s", (existing_hash,))
        cross_update_cols = [c for c in update_cols
                             if c != 'dedup_hash'
                             and c not in _hash_locked]
        _hraw_assign, _hraw_extra = _raw_assign_preserving_locked(
            '%s::jsonb', _hash_locked)
        if not cross_update_cols:
            row = pg.execute_returning(
                f"UPDATE permits SET raw = {_hraw_assign}, updated_at = now() "
                "WHERE dedup_hash = %s "
                "RETURNING id, FALSE AS inserted",
                tuple([Json(permit.get('raw') or permit)]
                      + _hraw_extra + [existing_hash]),
            )
        else:
            assigns = ', '.join([f'{c} = %s' for c in cross_update_cols])
            row = pg.execute_returning(
                f"UPDATE permits SET {assigns}, raw = {_hraw_assign}, "
                "updated_at = now() "
                "WHERE dedup_hash = %s "
                "RETURNING id, FALSE AS inserted",
                tuple([fields[c] for c in cross_update_cols]
                      + [Json(permit.get('raw') or permit)]
                      + _hraw_extra + [existing_hash]),
            )
    if not row:
        return None
    # Drop any cached /permits/ slices that might now be stale. Caller
    # can opt out (``invalidate_cache=False``) to amortise this across a
    # whole batch — see ``bulk_upsert_permits``.
    if invalidate_cache:
        _invalidate_permits_cache()
    return ('inserted' if row['inserted'] else 'updated'), int(row['id'])


def bulk_upsert_permits(permits: list[dict]) -> dict:
    """Upsert a batch of permits. Returns counts {inserted, updated, skipped, errors}.

    Tells ``upsert_permit`` to skip per-row /permits/ cache invalidation
    and does the invalidation exactly once after the batch finishes.
    Saves N-1 redundant TTL-dict scans on a 50k-row scrape.
    """
    inserted = updated = skipped = errors = banned = 0
    error_samples: list[str] = []
    any_write = False
    # Pull the banned-states list ONCE per batch (single
    # system_settings read) so a 50k-row ingest doesn't hammer the
    # DB. Anything whose `state` matches is dropped before upsert and
    # surfaced as a separate ``banned`` counter so the scraper logs
    # can tell admin-suppressed rows apart from regular skips.
    _banned = set(get_banned_states())
    for p in permits or []:
        if _banned:
            st = (str((p or {}).get('state') or '').strip().upper())
            if st in _banned:
                banned += 1
                continue
        try:
            res = upsert_permit(p, invalidate_cache=False)
        except Exception as e:  # noqa: BLE001 — single bad row should not abort the batch
            errors += 1
            if len(error_samples) < 3:
                error_samples.append(f'{e.__class__.__name__}: {e}')
            continue
        if res is None:
            skipped += 1
            continue
        any_write = True
        action, _id = res
        if action == 'inserted':
            inserted += 1
        else:
            updated += 1
    if any_write:
        # One scan for the entire batch.
        _invalidate_permits_cache()
        # Score the freshly-ingested rows immediately (only_null → just
        # the never-scored ones) so new permits get a real rank instead
        # of sorting last until the next daily refresh. Best-effort: a
        # scoring hiccup must never fail the ingest itself.
        try:
            refresh_permit_scores(only_null=True)
        except Exception:
            log.exception('bulk_upsert_permits: post-ingest score refresh failed')
    return {
        'inserted':      inserted,
        'updated':       updated,
        'skipped':       skipped,
        'banned':        banned,
        'errors':        errors,
        'error_samples': error_samples,
    }


def get_permit_by_id(permit_id: int) -> dict | None:
    row = pg.query_one("SELECT * FROM permits WHERE id = %s", (int(permit_id),))
    return dict(row) if row else None


def update_permit(permit_id: int, fields: dict, *,
                  mark_manual: bool = True) -> bool:
    """Admin manual edit of a single permit row.

    Writes the provided materialized columns (allow-listed against
    ``_PERMIT_COLUMNS``) and, when ``mark_manual`` is set, records every
    edited field name in the ``manual_fields`` JSONB array so future
    scraper upserts EXCLUDE those columns — i.e. the human-corrected value
    wins forever and a rescrape can never clobber it.

    The ``dedup_hash`` is recomputed from the merged row so an edit to an
    address / contractor / value can't desync the cross-source dedup
    index. If that recompute would collide with another row's hash we
    fall back to leaving the hash untouched rather than failing the edit.

    Returns True on success, False if the row doesn't exist or no
    recognised columns were supplied.
    """
    pid = int(permit_id)
    cols = [c for c in _PERMIT_COLUMNS if c in fields and c != 'dedup_hash']
    if not cols:
        return False

    existing = get_permit_by_id(pid)
    if not existing:
        return False

    norm = _normalize_permit_payload({k: fields.get(k) for k in cols})

    # Recompute dedup_hash from the merged (existing + edited) row.
    merged = dict(existing)
    for c in cols:
        merged[c] = norm[c]
    new_hash = compute_permit_dedup_hash(merged)

    # Keep the JSONB `raw` payload in lockstep with the materialized
    # columns so the column store and the JSON document never disagree
    # after a manual edit (the admin "View" modal + any consumer that
    # reads straight from `raw` would otherwise show the stale scraped
    # value). Coerce date/datetime to ISO strings so the merge is
    # JSON-serializable.
    from datetime import date as _date, datetime as _dt

    def _json_safe(v):
        if isinstance(v, (_date, _dt)):
            return v.isoformat()
        return v

    raw_patch = {c: _json_safe(norm[c]) for c in cols}

    def _run_update(include_hash: bool) -> bool:
        set_parts = [f'{c} = %s' for c in cols]
        params: list = [norm[c] for c in cols]
        set_parts.append("raw = COALESCE(raw, '{}'::jsonb) || %s::jsonb")
        params.append(Json(raw_patch))
        if include_hash:
            set_parts.append('dedup_hash = %s')
            params.append(new_hash)
        if mark_manual:
            # Merge the edited column names into manual_fields, de-duped.
            set_parts.append(
                "manual_fields = ("
                "  SELECT COALESCE(jsonb_agg(DISTINCT e), '[]'::jsonb) "
                "  FROM jsonb_array_elements_text("
                "         COALESCE(manual_fields, '[]'::jsonb) || %s::jsonb"
                "       ) AS e"
                ")"
            )
            params.append(Json(cols))
        set_parts.append('updated_at = now()')
        sql = (f"UPDATE permits SET {', '.join(set_parts)} "
               f"WHERE id = %s RETURNING id")
        row = pg.execute_returning(sql, tuple(params + [pid]))
        return bool(row)

    try:
        ok = _run_update(include_hash=new_hash is not None)
    except psycopg_errors.UniqueViolation:
        # Recomputed hash collided with another row — keep the existing
        # hash and still persist the column edits.
        try:
            ok = _run_update(include_hash=False)
        except Exception:
            log.exception('update_permit retry failed for id=%s', pid)
            return False
    except Exception:
        log.exception('update_permit failed for id=%s', pid)
        return False

    if ok:
        _invalidate_permits_cache()
    return ok


def get_permits(*, cities: list[str] | None = None, state: str | None = None,
                trade: str | None = None, status: str | None = None,
                tier: str | None = None, min_score: int | None = None,
                issued_after: str | None = None,
                limit: int = 50, offset: int = 0) -> list[dict]:
    """Filtered, paginated permit lookup. ``cities`` is a list of city *names*."""
    where: list[str] = []
    params: list = []
    if cities:
        where.append('lower(city) = ANY(%s)')
        params.append([c.lower() for c in cities])
    if state:
        where.append('state = %s'); params.append(state.upper())
    if trade:
        where.append('trade = %s'); params.append(trade.lower())
    if status:
        where.append('status = %s'); params.append(status.lower())
    if tier:
        where.append('ai_tier = %s'); params.append(tier.lower())
    if min_score is not None:
        where.append('ai_score >= %s'); params.append(int(min_score))
    if issued_after:
        where.append('issued_date >= %s'); params.append(issued_after)
    sql = "SELECT * FROM permits"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY issued_date DESC NULLS LAST, id DESC LIMIT %s OFFSET %s"
    params.extend([int(limit), int(offset)])
    rows = pg.query(sql, tuple(params))
    return [dict(r) for r in rows]


def count_permits() -> int:
    row = pg.query_one("SELECT COUNT(*) AS n FROM permits")
    return int(row['n']) if row else 0


# NOTE: ``seed_demo_notifications`` was removed by user request. The
# previous version seeded ~15 fake "Hot Alert / Digest / Report" rows
# into the notifications table for any user whose table was empty —
# which made it impossible to test what a brand-new user actually
# sees on /notifications/. The page now shows only real lifecycle
# events recorded by core.email_notifications (welcome, payment
# receipt, login alert, support reply, support status).




# ── permits → user-facing view helpers ─────────────────────────────────
#
# The `permits` table above (defined by scripts/init_permits_table.py
# and managed by upsert_permit / bulk_upsert_permits) is the production
# source of truth for permit records pushed by the external scraper
# platform. Until this PR the user-facing /permits/ dashboard and the
# public /v1/permits/ API both read from a Python in-memory list
# (`PERMIT_HISTORY` in core/views.py) instead — that list:
#   * held a full copy of every permit in every Python worker (2 GB
#     DigitalOcean droplet → OOM at ~50k rows of 2 KB each)
#   * forced filter logic into O(n) Python list comprehensions
#   * disappeared on every restart/redeploy
#
# These helpers route the user-facing reads through the real `permits`
# table while preserving the legacy dict shape templates and the
# public API expect (id/desc/issuedIso/issued/...). The 30 sample
# rows previously hard-coded in views.py are seeded once via
# bulk_upsert_permits with source='demo_seed'.

_PERMIT_DEMO_SOURCE = 'demo_seed'
_PERMITS_SEEDED = False


def _iso_to_mdy(iso: str) -> str:
    """YYYY-MM-DD → MM-DD-YYYY (display-only)."""
    p = (iso or '').split('-') if iso else []
    return f'{p[1]}-{p[2]}-{p[0]}' if len(p) == 3 else (iso or '')


def _date_to_iso(d) -> str:
    """Coerce whatever the DB hands back for a DATE column into YYYY-MM-DD.
    psycopg may return a `datetime.date` object — `.isoformat()` returns
    the canonical ISO form. Strings are returned untouched."""
    if not d:
        return ''
    try:
        return d.isoformat() if hasattr(d, 'isoformat') else str(d)
    except Exception:
        return str(d)


def _row_to_permit_view(row: dict | None) -> dict | None:
    """Shape a real `permits` row (rich scraper schema) into the legacy
    dict shape (`{id, type, desc, issued, issuedIso, ...}`) every caller
    in views.py / api_permits / templates expects. Lets us swap the
    storage engine underneath without touching the frontend or the
    public API contract.

    All text fields are coerced to empty strings rather than `None` —
    the legacy in-memory `PERMIT_HISTORY` always had string values, and
    `templates/core/permits.html` calls `.toLowerCase()` /
    `.includes()` directly on these fields. A NULL leaking through
    from a partially-populated scraper row would crash client-side
    filtering with `Cannot read property 'toLowerCase' of null`.
    """
    if row is None:
        return None
    s = lambda v: '' if v is None else str(v)  # noqa: E731 — local shorthand
    issued_iso  = _date_to_iso(row.get('issued_date'))
    expires_iso = _date_to_iso(row.get('expires_date'))
    # Project value is stored in cents to avoid float drift; surface
    # both the raw cents (for sorting / CSV export) and a pre-formatted
    # display string ($X,XXX) so the JS render path can show it without
    # re-implementing locale formatting in five places.
    val_cents = row.get('valuation_cents')
    if val_cents is None:
        val_display = ''
    else:
        try:
            val_display = '${:,}'.format(int(val_cents) // 100)
        except (TypeError, ValueError):
            val_display = ''
    # Customer-facing /permits/ "Owner / Contractor" column needs ONE
    # name with an explicit type pill so the user can tell at a glance
    # which party we found. We route this through the ingest-derived
    # `contact_name` / `contact_type` columns (populated by
    # core.scraper_accela._normalise_permit — contractor-first, owner
    # fallback) so the customer page shows the SAME labelled contact
    # as the admin scraper grid. Previously this re-derived owner-first
    # locally, causing rows like APP-UTIL-26-005301 to render as
    # "[owner] CB SUNSET…" here while admin showed "[contractor] NVR
    # Inc. DBA Ryan Homes" for the exact same permit.
    #
    # Fallback chain (only used for legacy rows ingested before the
    # contact_* columns existed):
    #   contact_name → contractor_name → owner_name
    # ...with type mirrored so we never label a contractor as an owner.
    owner_name      = s(row.get('owner_name'))
    contractor_name = s(row.get('contractor_name'))
    contact_name    = s(row.get('contact_name'))
    contact_type    = s(row.get('contact_type'))
    if contact_name:
        lead, lead_type = contact_name, (contact_type or
            ('contractor' if contractor_name else ('owner' if owner_name else '')))
    elif contractor_name:
        lead, lead_type = contractor_name, 'contractor'
    elif owner_name:
        lead, lead_type = owner_name, 'owner'
    else:
        lead, lead_type = '', ''
    out = {
        # `id` for the public API is the human-readable permit number,
        # not the internal BIGSERIAL row id — that's what the legacy
        # PERMIT_HISTORY exposed and what /v1/permits/<id>/ already
        # routes against.
        'id':          s(row.get('permit_number')),
        'type':        s(row.get('permit_type')),
        'desc':        s(row.get('description')),
        'status':      s(row.get('status')),
        'issuedIso':   issued_iso,
        'expiresIso':  expires_iso,
        'issued':      _iso_to_mdy(issued_iso),
        'expires':     _iso_to_mdy(expires_iso),
        'phone':       s(row.get('contractor_phone')),
        'email':       s(row.get('contractor_email')),
        # `project` historically held the property address (the small
        # subtitle line under the owner name). Kept under the legacy
        # key so older cached JS/CSV columns still work; the `address`
        # field below is the modern alias.
        'project':     s(row.get('address')),
        'address':     s(row.get('address')),
        'contractor':  contractor_name,
        # `owner` legacy field now falls back to contractor so the
        # column never goes blank when the Accela Applicant block was
        # the only contact info on the page (~75% of current rows).
        'owner':       lead,
        'lead':        lead,
        'lead_type':   lead_type,
        # The actual property-owner name (verbatim from the permit),
        # NOT the unified contact. Surfaced separately so the
        # row-detail modal can show a dedicated "Property Owner" line
        # — and only when one was actually parsed. Empty string when
        # the permit had no owner block.
        'owner_name':  owner_name,
        # Score / grade — derived server-side from the same 12-factor
        # formula the JS uses (``core.permit_score.derive_score``) so
        # what the user sees in the ring matches what the server
        # filters, sorts, and CSV-exports against. The DB columns
        # ``ai_score`` / ``ai_grade`` are kept around for diagnostics
        # but no longer feed the read path — see ``core/permit_score.py``
        # for the rationale (LLM was returning 100 for permits whose
        # heuristic factors averaged ~40, so display and filter
        # silently disagreed). Score is computed lazily below so the
        # row dict already has all the fields the deriver reads.
        'score':       0,  # filled in immediately after this dict literal
        'grade':       'F',
        'trade':       s(row.get('trade')),
        'city':        s(row.get('city')),
        'state':       s(row.get('state')),
        # Project value — `valueCents` keeps the raw integer so the
        # JS layer can sort numerically; `value` is the human display.
        'valueCents':  int(val_cents) if val_cents is not None else 0,
        'value':       val_display,
    }
    # Lazy import to avoid a circular dependency at module load — db.py
    # is imported by half the codebase, permit_score.py is a leaf.
    from core.permit_score import derive_score
    out['score'], out['grade'] = derive_score(out)
    return out


def query_permits_view(
    *,
    city_set: set | None = None,
    state: str = '',
    city: str = '',
    trade: str = '',
    status: str = '',
    tier: str = '',
    min_score: int | None = None,
    max_score: int | None = None,
    owner: str = '',
    keyword: str = '',
    issued_after: str = '',
    expires_before: str = '',
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """SQL-side filter+paginate against the production `permits` table.

    `city_set` (lowercase city names) restricts results to the user's
    subscribed cities, applied in addition to any explicit `city`/`state`
    filter. Returns `(rows_in_legacy_dict_shape, total_before_pagination)`.
    A `city_set` of `None` means "no per-user restriction" (demo key /
    admin); an empty set means "user has no cities yet" → empty result.
    """
    where: list[str] = []
    params: list = []
    if city_set is not None:
        if not city_set:
            return [], 0
        # ``city_set`` was repurposed in the May-2026 city→state pricing
        # migration to carry uppercase 2-letter state codes; keep the
        # legacy parameter name so existing API callers don't break.
        where.append("UPPER(state) = ANY(%s)")
        params.append([s.upper() for s in city_set])
    if state:
        where.append("UPPER(state) = %s")
        params.append(state.upper())
    if city:
        where.append("LOWER(city) = %s")
        params.append(city.lower())
    if trade:
        where.append("LOWER(trade) = %s")
        params.append(trade.lower())
    if status:
        where.append("LOWER(status) = %s")
        params.append(status.lower())
    if tier == 'hot':
        where.append("ai_score >= 80")
    elif tier == 'warm':
        where.append("ai_score BETWEEN 60 AND 79")
    elif tier == 'cool':
        where.append("ai_score < 60")
    if min_score is not None:
        where.append("ai_score >= %s")
        params.append(int(min_score))
    if max_score is not None:
        where.append("ai_score <= %s")
        params.append(int(max_score))
    if owner:
        where.append("owner_name ILIKE %s")
        params.append(f"%{owner}%")
    if keyword:
        where.append(
            "(permit_number ILIKE %s OR permit_type ILIKE %s OR description ILIKE %s "
            "OR address ILIKE %s OR owner_name ILIKE %s)"
        )
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw, kw])
    if issued_after:
        where.append("issued_date >= %s")
        params.append(issued_after)
    if expires_before:
        where.append("expires_date <= %s")
        params.append(expires_before)
    sql_where = ('WHERE ' + ' AND '.join(where)) if where else ''
    total_row = pg.query_one(
        f"SELECT COUNT(*) AS n FROM permits {sql_where}",
        tuple(params),
    )
    total = int(total_row['n']) if total_row else 0
    page = pg.query(
        f"""SELECT permit_number, permit_type, description, status,
                  issued_date, expires_date,
                  contractor_phone, contractor_email,
                  address, owner_name, contractor_name,
                  contact_name, contact_type,
                  ai_score, ai_grade, trade, city, state
              FROM permits
              {sql_where}
              ORDER BY issued_date DESC NULLS LAST, id DESC
              LIMIT %s OFFSET %s""",
        tuple(params) + (int(limit), int(offset)),
    )
    return [_row_to_permit_view(r) for r in page], total


def get_permit_by_number(permit_number: str, city_set: set | None = None) -> dict | None:
    """Look up one permit by its human-readable permit_number (the value
    the public API exposes as `id`). Optionally restrict to a user's
    subscribed cities so /v1/permits/<id>/ cannot leak permits a caller
    did not pay for. Returns the legacy dict shape (or None)."""
    if not permit_number:
        return None
    where = "WHERE permit_number = %s"
    params: list = [permit_number]
    if city_set is not None:
        if not city_set:
            return None
        # See ``query_permits_view`` — values are now state codes.
        where += " AND UPPER(state) = ANY(%s)"
        params.append([s.upper() for s in city_set])
    row = pg.query_one(
        f"""SELECT permit_number, permit_type, description, status,
                  issued_date, expires_date,
                  contractor_phone, contractor_email,
                  address, owner_name, contractor_name,
                  contact_name, contact_type,
                  ai_score, ai_grade, trade, city, state
              FROM permits {where}
              LIMIT 1""",
        tuple(params),
    )
    return _row_to_permit_view(row)


def get_distinct_cities_for_states(state_set) -> list[dict]:
    """Return [{'name': <city>, 'state': <ST>}, ...] for every distinct
    (city, state) pair that has at least one permit in the given state
    set. Used by the /permits/ and /dashboard/ filter panels to populate
    the cascading "State → City" dropdown. Returns an empty list when
    the state set is empty (frozen / unsubscribed accounts).

    Since the May-2026 pricing migration the user's ``data.cities`` JSON
    column holds 2-letter STATE codes (not "City, ST" strings), so the
    City dropdown can no longer be derived from the user record alone —
    we have to ask Postgres which real cities exist inside the user's
    paid-for states.
    """
    codes = [(s or '').strip().upper() for s in (state_set or [])
             if s and len((s or '').strip()) == 2]
    if not codes:
        return []
    rows = pg.query(
        """SELECT DISTINCT city, UPPER(state) AS state
             FROM permits
            WHERE UPPER(state) = ANY(%s)
              AND city IS NOT NULL AND city <> ''
            ORDER BY UPPER(state), city""",
        (codes,),
    )
    return [{'name': r['city'], 'state': r['state']} for r in rows]


def query_permits_for_dashboard(
    *,
    state_set: set | None = None,
    city_set: set | None = None,  # deprecated alias retained for in-flight callers
    history_days: int | None = None,
    f_state: str = '',
    f_city: str = '',
    f_trade: str = '',
    f_status: str = '',
    f_score_min: int | None = None,
    f_score_max: int | None = None,
    f_owner: str = '',
    f_phone_digits: str = '',
    f_email: str = '',
    f_issued_after: str = '',
    f_expires_before: str = '',
    f_keyword: str = '',
    f_tier: str = 'all',
    f_search: str = '',
    sort_key: str = 'score',
    sort_dir: str = 'desc',
    start: int = 0,
    length: int = 25,
    include_summary: bool = False,
) -> tuple[list[dict], int, int] | tuple[list[dict], int, int, dict]:
    """Hot path for the /permits/ DataTables AJAX endpoint. Pushes the
    filter, sort, and LIMIT/OFFSET clauses down into Postgres so the
    query returns exactly the visible page — no in-Python truncation,
    no in-Python filtering, no per-account cap.

    Returns ``(page_rows_in_legacy_dict_shape, records_total,
    records_filtered)``.

    * ``records_total`` is the count of permits visible to this user
      *before* any panel filter — i.e. their city set + per-plan
      history window. This is what DataTables shows as the "of N
      total" denominator and is the figure the page chip displays.
    * ``records_filtered`` is the count after every panel filter.

    Authorisation: the user's ``city_set`` is applied as the *first*
    WHERE clause on every query (count + filtered count + page slice),
    so a hand-crafted ``f_city`` / ``f_state`` cannot leak permits in
    cities the caller doesn't pay for. An empty ``city_set`` short-
    circuits to an empty result with no DB round-trip — the same UX
    as the legacy in-Python pipeline returned for frozen accounts.
    """
    # Accept either ``state_set`` (new) or the legacy ``city_set``
    # alias so we can roll out the state-based authz gate without
    # touching every caller in the same PR. Both are treated as
    # uppercase 2-letter state codes since the May-2026 city→state
    # pricing migration repurposed ``data.cities`` to hold states.
    _gate = state_set if state_set is not None else city_set
    if not _gate:
        if include_summary:
            return [], 0, 0, {'total': 0, 'hot': 0, 'avg': 0}
        return [], 0, 0

    # Make sure the materialized score column + index exist, and kick a
    # (debounced, non-blocking) daily re-score if today's hasn't run yet.
    # Both are cheap no-ops after the first call per day/process, so the
    # read path stays instant — it always serves from whatever
    # ``score_cache`` is currently materialized.
    _ensure_permit_score_columns()
    ensure_scores_fresh_async()

    # Base WHERE — the per-user authorisation gate. Always applied
    # *before* any panel filter so f_state='XX' for an unpaid state
    # can never widen the result set beyond the user's plan.
    base_where: list[str] = ["UPPER(state) = ANY(%s)"]
    base_params: list = [[s.upper() for s in _gate]]
    if history_days is not None:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=int(history_days))).isoformat()
        base_where.append("issued_date >= %s")
        base_params.append(cutoff)

    where = list(base_where)
    params: list = list(base_params)

    if f_state:
        where.append("UPPER(state) = %s")
        params.append(f_state.upper())
    if f_city:
        where.append("LOWER(city) = %s")
        params.append(f_city.lower())
    if f_trade:
        where.append("LOWER(trade) = %s")
        params.append(f_trade.lower())
    if f_status:
        where.append("LOWER(status) = %s")
        params.append(f_status.lower())
    # Score-range (f_score_min / f_score_max) and tier (f_tier) filters
    # run against the MATERIALIZED 12-factor score (``score_cache``,
    # refreshed daily by ``refresh_permit_scores``) — NOT the DB column
    # ``ai_score`` (Claude's ingest value diverged by 30+ points and made
    # the tier buttons hide leads displayed as 90 / show leads displayed
    # as 55). ``score_cache`` mirrors the same ``derive_score`` formula
    # the ring shows, so the filter matches what the user sees (modulo
    # the <1-day staleness the daily refresh allows). COALESCE→0 so a
    # brand-new, not-yet-scored row is simply treated as "cool".
    if f_tier == 'hot':
        where.append("COALESCE(score_cache, 0) >= 80")
    elif f_tier == 'warm':
        where.append("COALESCE(score_cache, 0) BETWEEN 60 AND 79")
    elif f_tier == 'cool':
        where.append("COALESCE(score_cache, 0) < 60")
    if f_score_min is not None and f_score_min > 0:
        where.append("COALESCE(score_cache, 0) >= %s")
        params.append(int(f_score_min))
    if f_score_max is not None and f_score_max < 100:
        where.append("COALESCE(score_cache, 0) <= %s")
        params.append(int(f_score_max))
    if f_owner:
        where.append("owner_name ILIKE %s")
        params.append(f"%{f_owner}%")
    if f_phone_digits:
        # Strip non-digits from contractor_phone and substring-match
        # the user's digits. Mirrors the legacy in-Python behaviour
        # where "(817) 542-9900" matches "8175429" or "5429900".
        where.append(
            "REGEXP_REPLACE(COALESCE(contractor_phone, ''), '[^0-9]', '', 'g') LIKE %s"
        )
        params.append(f"%{f_phone_digits}%")
    if f_email:
        where.append("LOWER(COALESCE(contractor_email, '')) LIKE %s")
        params.append(f"%{f_email.lower()}%")
    if f_issued_after:
        where.append("issued_date >= %s")
        params.append(f_issued_after)
    if f_expires_before:
        where.append("expires_date <= %s")
        params.append(f_expires_before)

    # Keyword + DataTables global search both run against the same
    # blob of fields. Two parameters can be active simultaneously
    # (panel keyword + table search box), so emit two separate clauses.
    def _kw_clause(_kw: str) -> tuple[str, list]:
        kwp = f"%{_kw.lower()}%"
        clause = (
            "(LOWER(COALESCE(permit_number,    '')) LIKE %s OR "
            " LOWER(COALESCE(permit_type,      '')) LIKE %s OR "
            " LOWER(COALESCE(description,      '')) LIKE %s OR "
            " LOWER(COALESCE(owner_name,       '')) LIKE %s OR "
            " LOWER(COALESCE(address,          '')) LIKE %s OR "
            " LOWER(COALESCE(contractor_phone, '')) LIKE %s OR "
            " LOWER(COALESCE(contractor_email, '')) LIKE %s)"
        )
        return clause, [kwp, kwp, kwp, kwp, kwp, kwp, kwp]

    if f_keyword:
        c, p = _kw_clause(f_keyword)
        where.append(c)
        params.extend(p)
    if f_search:
        c, p = _kw_clause(f_search)
        where.append(c)
        params.extend(p)

    # Tier filter (f_tier) is also applied in Python — see the
    # f_score_min/max comment above.

    # Every sort key — including ``score`` — is now pushed into Postgres.
    # ``score`` orders by the materialized ``score_cache`` column
    # (COALESCE→0 so unscored rows sort last), so the default
    # "best leads first" view paginates with a plain LIMIT/OFFSET and
    # never needs to fetch + re-score the whole set in Python.
    _SORT_SQL = {
        'id':         'permit_number',
        'city':       'city',
        'type':       'permit_type',
        'desc':       'description',
        'status':     'status',
        'issuedIso':  'issued_date',
        'expiresIso': 'expires_date',
        'valueCents': 'valuation_cents',
        'phone':      'contractor_phone',
        'email':      'contractor_email',
        'owner':      'owner_name',
        'score':      'COALESCE(score_cache, 0)',
    }
    sort_col = _SORT_SQL.get(sort_key, 'issued_date')
    sort_sql_dir = 'DESC' if (sort_dir or '').lower() == 'desc' else 'ASC'
    nulls_clause = 'NULLS LAST' if sort_sql_dir == 'DESC' else 'NULLS FIRST'

    sql_base_where = 'WHERE ' + ' AND '.join(base_where)
    sql_where      = 'WHERE ' + ' AND '.join(where)

    # `recordsTotal` denominator — bare authorisation WHERE, so the
    # page chip ("X of Y permits") shows the user's full plan size.
    total_row = pg.query_one(
        f"SELECT COUNT(*) AS n FROM permits {sql_base_where}",
        tuple(base_params),
    )
    records_total = int(total_row['n']) if total_row else 0

    # Columns selected for the visible page — the SAME shared list the
    # daily score materializer uses, so the live ring score and the
    # cached rank are computed from identical inputs (no drift).
    _SELECT_COLS = _PERMIT_VIEW_COLS

    # `recordsFiltered` — count after every panel filter (now including
    # the tier / score-range filters, which live in ``where`` against
    # ``score_cache``). Identical to ``records_total`` when nothing
    # narrowed the set, so skip the second COUNT in that common case.
    if where == base_where:
        records_filtered = records_total
    else:
        _filt = pg.query_one(
            f"SELECT COUNT(*) AS n FROM permits {sql_where}",
            tuple(params),
        )
        records_filtered = int(_filt['n']) if _filt else 0

    # ── Single SQL path: filter + sort + paginate entirely in Postgres ──
    # The default "best leads first" order now sorts on the materialized
    # ``score_cache`` column (see ``refresh_permit_scores``), so even the
    # score view fetches EXACTLY the visible page — no 25k-row scan, no
    # per-row Python derivation on every pagination click. The score in
    # each row's ring is still derived live by ``_row_to_permit_view``
    # so what the user reads is exact for today; only the ordering can be
    # up to a day stale (the daily-refresh trade the user approved).
    page_raw = pg.query(
        f"""SELECT {_SELECT_COLS}
              FROM permits
              {sql_where}
              ORDER BY {sort_col} {sort_sql_dir} {nulls_clause}, id DESC
              LIMIT %s OFFSET %s""",
        tuple(params) + (int(length), int(start)),
    )
    page_rows = [_row_to_permit_view(r) for r in page_raw]

    if include_summary:
        # Aggregate over the FULL filtered population (not the visible
        # page) straight from ``score_cache`` — exact, with no row cap.
        _agg = pg.query_one(
            f"""SELECT COALESCE(ROUND(AVG(COALESCE(score_cache, 0))), 0) AS avg,
                       COUNT(*) FILTER (WHERE COALESCE(score_cache, 0) >= 80) AS hot
                  FROM permits {sql_where}""",
            tuple(params),
        )
        summary = {
            'total': records_filtered,
            'hot':   int(_agg['hot']) if _agg else 0,
            'avg':   int(_agg['avg']) if _agg else 0,
        }
        return page_rows, records_total, records_filtered, summary

    return page_rows, records_total, records_filtered


def get_recent_permits_for_cities(
    city_set: set,
    *,
    history_days: int | None = None,
    limit: int = 200,
) -> list[dict]:
    """Hot path for the /permits/ dashboard. Returns recent permits in
    the user's cities, optionally restricted to the last `history_days`
    days (per-plan history window). Legacy dict shape, newest first."""
    if not city_set:
        return []
    # Process-local TTL cache. Keyed on the exact (cities, window, limit)
    # combo so two users on the same plan + city share a single fetch.
    # 60 s lifetime keeps the page warm-fast (<5 ms) without serving rows
    # noticeably older than the scraper cycle; the scraper invalidates
    # the prefix on every upsert so a fresh permit lands on the dashboard
    # immediately, not 60 s later.
    #
    # IMPORTANT: serialise the city set with `json.dumps` rather than a
    # delimiter join. Permit rows are city-scoped and a cache hit is
    # effectively an authorisation gate, so a key collision between two
    # different city sets would leak permits across users. A naïve
    # ``'|'.join(sorted(...))`` collides on city names containing ``|``
    # (``{"a|b","c"}`` vs ``{"a","b|c"}``); JSON encoding is unambiguous
    # because each element is independently quoted and escaped.
    _ckey_cities = json.dumps(
        sorted(c.lower() for c in city_set),
        separators=(',', ':'),
        ensure_ascii=False,
    )
    _ckey = f"permits_cities:{_ckey_cities}:d{history_days}:l{int(limit)}"
    cached = _ttl_get(_ckey)
    if cached is not None:
        # Defensive copies all the way down: callers are free to mutate
        # the rows they get back without poisoning a future cache hit.
        return [dict(r) for r in cached]
    where = ["LOWER(city) = ANY(%s)"]
    params: list = [[c.lower() for c in city_set]]
    if history_days is not None:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=history_days)).isoformat()
        where.append("issued_date >= %s")
        params.append(cutoff)
    rows = pg.query(
        f"""SELECT permit_number, permit_type, description, status,
                  issued_date, expires_date,
                  contractor_phone, contractor_email,
                  address, owner_name, contractor_name, valuation_cents,
                  contact_name, contact_type,
                  ai_score, ai_grade, trade, city, state
              FROM permits
              WHERE {' AND '.join(where)}
              ORDER BY issued_date DESC NULLS LAST, id DESC
              LIMIT %s""",
        tuple(params) + (int(limit),),
    )
    out = [_row_to_permit_view(r) for r in rows]
    _ttl_set(_ckey, out, ttl=60.0)
    # Same defensive copy as the hit branch — the cached list keeps the
    # canonical rows untouched, callers get a fresh top-level list AND
    # fresh row dicts they can freely mutate without poisoning a future
    # cache hit. (json.dumps in the /permits/ view is read-only, but
    # any future caller that does `row['flag'] = ...` would otherwise
    # silently taint subsequent dashboard renders for 60 s.)
    return [dict(r) for r in out]


def ensure_demo_permits_seeded():
    """Insert the sample permits ONCE on first deploy of a brand-new
    `permits` table. Subsequent calls are a fast no-op (one indexed
    `EXISTS` query plus a process-local flag).

    Gating rule: we only seed when the ENTIRE `permits` table is empty
    — not just the `source='demo_seed'` slice. This prevents the demo
    dataset from being injected alongside real scraper rows after a
    deploy. Once any real permit lands (or once the demo seed has
    already run), this function is a permanent no-op.

    Sample data is written through the same `bulk_upsert_permits()`
    pipeline real scrapers use, with `source='demo_seed'` so the
    rows can be identified and removed wholesale once real scraper
    feeds are ready to replace the demo dataset:

        DELETE FROM permits WHERE source = 'demo_seed';
    """
    global _PERMITS_SEEDED
    if _PERMITS_SEEDED:
        return
    try:
        existing = pg.query_one("SELECT 1 FROM permits LIMIT 1")
    except Exception:
        # Table may not exist yet in a fresh dev environment — caller
        # should run scripts/init_permits_table.py first. Don't crash
        # imports.
        log.exception("ensure_demo_permits_seeded: probe query failed")
        return
    if existing:
        # Either the demo seed already ran on a previous deploy or
        # real scraper data is present — either way, do not pollute
        # the table with demo rows.
        _PERMITS_SEEDED = True
        return
    payload = []
    for p in _DEMO_PERMITS_SEED:
        payload.append({
            'source':           _PERMIT_DEMO_SOURCE,
            'source_permit_id': p['id'],
            'permit_number':    p['id'],
            'permit_type':      p['type'],
            'description':      p['desc'],
            'status':           p['status'],
            'issued_date':      p['issuedIso'],
            'expires_date':     p['expiresIso'],
            'contractor_phone': p['phone'],
            'contractor_email': p['email'],
            'address':          p['project'],
            'owner_name':       p['owner'],
            'ai_score':         p['score'],
            'ai_grade':         p['grade'],
            'trade':            p['trade'],
            'city':             p['city'],
            'state':            p['state'],
        })
    try:
        bulk_upsert_permits(payload)
    except Exception:
        log.exception("ensure_demo_permits_seeded: bulk_upsert_permits failed")
        return
    _PERMITS_SEEDED = True


# Initial seed data — same 30 rows previously hard-coded in
# core/views.py PERMIT_HISTORY. Written through bulk_upsert_permits
# on first call to ensure_demo_permits_seeded() so they live alongside
# real scraper data in the same schema.
_DEMO_PERMITS_SEED: list[dict] = [
    {"id":"PW26-12642","type":"Encroachment Permit","desc":"Allen/Coran ROW encroachment","status":"approved","issuedIso":"2026-04-13","expiresIso":"2026-06-01","phone":"(714) 330-1763","email":"contact@bgeinc.com","project":"Allen/Coran ROW","owner":"Public ROW Dept","score":84,"grade":"A-","trade":"civil","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12643","type":"Residential Roofing","desc":"Full roof replacement","status":"approved","issuedIso":"2026-04-13","expiresIso":"2026-10-13","phone":"(817) 542-9900","email":"mike@texasroof.com","project":"3421 Magnolia Ave","owner":"Sarah T. Monroe","score":96,"grade":"A+","trade":"roofing","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12644","type":"HVAC Replacement","desc":"4-ton split system","status":"approved","issuedIso":"2026-04-13","expiresIso":"2026-10-13","phone":"(817) 290-4411","email":"james@cooltech.com","project":"907 Westbrook Dr","owner":"David Okonkwo","score":82,"grade":"A-","trade":"hvac","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12645","type":"New Residential Build","desc":"2,400 sqft single-family home","status":"review","issuedIso":"2026-04-13","expiresIso":"2026-04-27","phone":"(817) 663-1122","email":"build@fortworth.dev","project":"1105 Ridgecrest Blvd","owner":"Meridian Homes LLC","score":91,"grade":"A","trade":"civil","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12646","type":"Plumbing Repair","desc":"Water main replacement","status":"approved","issuedIso":"2026-04-13","expiresIso":"2026-07-13","phone":"(817) 445-7730","email":"dan@dplumbing.net","project":"2218 Oak Hill Ln","owner":"Greg Fischer","score":74,"grade":"B+","trade":"plumbing","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12647","type":"Commercial Electric","desc":"Panel upgrade 400A","status":"approved","issuedIso":"2026-04-13","expiresIso":"2026-08-13","phone":"(214) 980-3355","email":"info@voltmaster.com","project":"5560 Commerce St","owner":"Vault Storage LLC","score":67,"grade":"B","trade":"electrical","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12648","type":"Residential Roofing","desc":"Storm damage repair","status":"approved","issuedIso":"2026-04-13","expiresIso":"2026-10-13","phone":"(817) 201-8844","email":"storms@rapidroofing.com","project":"711 Cypress Creek Rd","owner":"Cynthia Park","score":88,"grade":"A","trade":"roofing","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12649","type":"HVAC Replacement","desc":"Ductless mini-split system","status":"expired","issuedIso":"2026-01-05","expiresIso":"2026-04-05","phone":"(817) 111-2233","email":"hvac@fwcool.com","project":"4402 McCart Ave","owner":"Tony Salazar","score":44,"grade":"D+","trade":"hvac","city":"Fort Worth","state":"TX"},
    {"id":"PW26-12650","type":"Solar Installation","desc":"16-panel grid-tie system","status":"approved","issuedIso":"2026-04-17","expiresIso":"2026-10-17","phone":"(817) 880-0012","email":"solar@sunnytx.com","project":"2002 Wedgwood Dr","owner":"Amanda Foster","score":95,"grade":"A+","trade":"solar","city":"Fort Worth","state":"TX"},
    {"id":"AR26-09211","type":"Residential Roofing","desc":"Full shingle replacement","status":"approved","issuedIso":"2026-04-14","expiresIso":"2026-10-14","phone":"(817) 329-4422","email":"roof@arlingtontx.net","project":"5519 Pioneer Pkwy","owner":"Maria Delgado","score":89,"grade":"A","trade":"roofing","city":"Arlington","state":"TX"},
    {"id":"AR26-09212","type":"HVAC Replacement","desc":"3-ton mini-split system","status":"pending","issuedIso":"2026-04-14","expiresIso":"2026-10-14","phone":"(817) 554-2233","email":"hvac@coolaire.com","project":"2210 Lamar Blvd","owner":"Robert Nwachukwu","score":77,"grade":"B+","trade":"hvac","city":"Arlington","state":"TX"},
    {"id":"AR26-09213","type":"Plumbing Installation","desc":"New water service line","status":"approved","issuedIso":"2026-04-12","expiresIso":"2026-07-12","phone":"(817) 662-0099","email":"pipes@arlington.com","project":"788 Fielder Rd","owner":"James Korte","score":65,"grade":"B","trade":"plumbing","city":"Arlington","state":"TX"},
    {"id":"AR26-09214","type":"Commercial Roofing","desc":"Flat roof membrane recovery","status":"approved","issuedIso":"2026-04-11","expiresIso":"2026-10-11","phone":"(817) 391-5500","email":"bids@commercialroof.io","project":"1040 Six Flags Dr","owner":"Triton Retail Group","score":93,"grade":"A","trade":"roofing","city":"Arlington","state":"TX"},
    {"id":"AR26-09215","type":"Electrical Upgrade","desc":"Service entrance panel replacement","status":"expired","issuedIso":"2026-01-10","expiresIso":"2026-04-10","phone":"(817) 208-4411","email":"elec@powerpro.net","project":"321 Center St","owner":"Franklin Properties LLC","score":51,"grade":"C","trade":"electrical","city":"Arlington","state":"TX"},
    {"id":"AR26-09216","type":"Solar Installation","desc":"12-panel carport array","status":"approved","issuedIso":"2026-04-17","expiresIso":"2026-10-17","phone":"(817) 665-4422","email":"solar@arlingtonsun.com","project":"6603 Matlock Rd","owner":"Priya Shah","score":88,"grade":"A","trade":"solar","city":"Arlington","state":"TX"},
    {"id":"DA26-33401","type":"Commercial HVAC","desc":"Rooftop unit replacement","status":"approved","issuedIso":"2026-04-15","expiresIso":"2026-10-15","phone":"(214) 555-0110","email":"maint@dallashq.com","project":"7400 Greenville Ave","owner":"Greenville Office Park","score":86,"grade":"A","trade":"hvac","city":"Dallas","state":"TX"},
    {"id":"DA26-33402","type":"Residential Roofing","desc":"Hail damage full replacement","status":"approved","issuedIso":"2026-04-15","expiresIso":"2026-10-15","phone":"(214) 333-8877","email":"claims@stormfix.com","project":"4812 Mockingbird Ln","owner":"Linda Osei","score":92,"grade":"A","trade":"roofing","city":"Dallas","state":"TX"},
    {"id":"DA26-33403","type":"Solar Installation","desc":"20-panel rooftop array","status":"review","issuedIso":"2026-04-14","expiresIso":"2026-10-14","phone":"(214) 890-0023","email":"solar@sunvolt.io","project":"2299 Swiss Ave","owner":"Marcus Webb","score":79,"grade":"B+","trade":"solar","city":"Dallas","state":"TX"},
    {"id":"DA26-33404","type":"Plumbing Repair","desc":"Sewer line camera & repair","status":"approved","issuedIso":"2026-04-12","expiresIso":"2026-07-12","phone":"(214) 432-1199","email":"sewer@txpipe.com","project":"905 W Davis St","owner":"Roberto Garza","score":60,"grade":"B-","trade":"plumbing","city":"Dallas","state":"TX"},
    {"id":"DA26-33405","type":"New Commercial Build","desc":"5,000 sqft retail shell","status":"pending","issuedIso":"2026-04-10","expiresIso":"2026-10-10","phone":"(214) 778-9900","email":"proj@dallasbuild.com","project":"11200 Inwood Rd","owner":"Nexgen Properties","score":87,"grade":"A","trade":"civil","city":"Dallas","state":"TX"},
    {"id":"DA26-33406","type":"Electrical Upgrade","desc":"Panel replacement 200A","status":"approved","issuedIso":"2026-04-09","expiresIso":"2026-08-09","phone":"(214) 220-5544","email":"elec@dallaspower.com","project":"3318 Live Oak St","owner":"Calvin James","score":71,"grade":"B","trade":"electrical","city":"Dallas","state":"TX"},
    {"id":"DA26-33407","type":"Residential Roofing","desc":"Flat roof membrane replacement","status":"expired","issuedIso":"2025-12-15","expiresIso":"2026-03-15","phone":"(214) 101-5566","email":"roof@dallasflat.com","project":"7711 Royal Ln","owner":"Harold Stein","score":48,"grade":"D+","trade":"roofing","city":"Dallas","state":"TX"},
    {"id":"AU26-55781","type":"Residential Roofing","desc":"Tile roof full replacement","status":"approved","issuedIso":"2026-04-16","expiresIso":"2026-10-16","phone":"(512) 444-2211","email":"roof@atxroof.com","project":"2910 Enfield Rd","owner":"Patricia Kwon","score":94,"grade":"A","trade":"roofing","city":"Austin","state":"TX"},
    {"id":"AU26-55782","type":"HVAC Replacement","desc":"Variable speed heat pump","status":"approved","issuedIso":"2026-04-16","expiresIso":"2026-10-16","phone":"(512) 301-8822","email":"hvac@austin-air.com","project":"4500 Duval St","owner":"Chen Enterprises","score":83,"grade":"A-","trade":"hvac","city":"Austin","state":"TX"},
    {"id":"AU26-55783","type":"Solar Installation","desc":"Tesla Powerwall + 18 panels","status":"approved","issuedIso":"2026-04-15","expiresIso":"2026-10-15","phone":"(512) 600-1234","email":"solar@greentx.io","project":"811 W 6th St","owner":"Natasha Patel","score":90,"grade":"A","trade":"solar","city":"Austin","state":"TX"},
    {"id":"AU26-55784","type":"New Residential Build","desc":"3br/2ba bungalow new build","status":"review","issuedIso":"2026-04-13","expiresIso":"2026-10-13","phone":"(512) 897-0032","email":"build@atxhomes.com","project":"5518 Caswell Ave","owner":"Derek Simmons","score":76,"grade":"B+","trade":"civil","city":"Austin","state":"TX"},
    {"id":"AU26-55785","type":"Commercial Electric","desc":"EV charging station install","status":"pending","issuedIso":"2026-04-12","expiresIso":"2026-08-12","phone":"(512) 775-3310","email":"ev@chargeup.com","project":"900 S Congress Ave","owner":"Tesla Retail LLC","score":68,"grade":"B","trade":"electrical","city":"Austin","state":"TX"},
    {"id":"AU26-55786","type":"Plumbing Repair","desc":"Gas line inspection & repair","status":"approved","issuedIso":"2026-04-11","expiresIso":"2026-07-11","phone":"(512) 222-4455","email":"gas@safetyplumb.com","project":"3401 Speedway","owner":"UT Property Mgmt","score":57,"grade":"C+","trade":"plumbing","city":"Austin","state":"TX"},
    {"id":"AU26-55787","type":"Commercial Roofing","desc":"TPO membrane installation","status":"approved","issuedIso":"2026-04-18","expiresIso":"2026-10-18","phone":"(512) 433-1100","email":"bids@roofsouth.com","project":"1919 S Lamar Blvd","owner":"Lamar Plaza LLC","score":91,"grade":"A","trade":"roofing","city":"Austin","state":"TX"},
]


# ═════════════════════════════════════════════════════════════════════
# ── Accela scraper system  ───────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════
#
# A "scraper" is a saved Accela CapDetail URL that Permitlify can run
# on demand or on cron. Each run is recorded in `scraper_runs` so the
# admin UI can show progress bars and per-day stats. Permits scraped
# are pushed into the existing `permits` table with
# `source = 'accela:<scraper_id>'` so the detail page can list and
# filter them with the same helpers used by the public dashboard.

_SCRAPERS_TABLE_READY = False

# ── Materialized permit score (precomputed once per day) ─────────────
# The customer-facing /permits/ default sort is the 12-factor derived
# score (``core.permit_score.derive_score``), which Postgres can't
# compute — so ranking the whole filtered set used to mean fetching +
# scoring up to 25 000 rows in Python on EVERY page load. To make the
# table load one page at a time, the derived score is materialized into
# ``permits.score_cache`` (refreshed daily) and Postgres does the
# ORDER BY / tier / range / summary directly. The per-row ring the user
# sees is still derived LIVE for the visible page only, so each row's
# score is always exact for today; only the ORDERING can be up to a day
# stale (a deliberate, user-approved trade for instant loads).
_SCORE_COLS_READY = False


def _ensure_permit_score_columns() -> None:
    """Idempotently add ``score_cache`` / ``score_day`` + the ranking
    index to ``permits``. Cheap no-op after the first call per process
    (module flag). Same lock-guarded pattern as ``_ensure_scrapers_table``
    so a busy ``permits`` table can't stall the read path on DDL."""
    global _SCORE_COLS_READY
    if _SCORE_COLS_READY:
        return
    try:
        with pg.conn() as _c:
            with _c.cursor() as _cur:
                _cur.execute("SET lock_timeout      = '2s'")
                _cur.execute("SET statement_timeout = '15s'")
                _cur.execute(
                    "ALTER TABLE permits "
                    "ADD COLUMN IF NOT EXISTS score_cache SMALLINT")
                _cur.execute(
                    "ALTER TABLE permits "
                    "ADD COLUMN IF NOT EXISTS score_day DATE")
                # DESC index backs the default "best leads first" sort;
                # the leading id keeps it usable as a stable tie-break.
                _cur.execute(
                    "CREATE INDEX IF NOT EXISTS permits_score_cache_idx "
                    "ON permits (score_cache DESC NULLS LAST, id DESC)")
        _SCORE_COLS_READY = True
    except psycopg_errors.UndefinedTable:
        # permits not provisioned yet — scripts/init_permits_table.py
        # carries the canonical schema; retry on next call.
        pass
    except Exception as _e:  # noqa: BLE001 — DDL contention must not break reads
        log.warning('_ensure_permit_score_columns: deferred DDL skipped (%s) '
                    '— will retry next call', _e.__class__.__name__)


# THE column list for shaping a permit row through ``_row_to_permit_view``.
# Shared by BOTH the visible-page read (query_permits_for_dashboard) and
# the daily score materializer (refresh_permit_scores) so the two can
# never drift: ``score_cache`` is computed from exactly the columns the
# live ring is, guaranteeing the cached rank matches what the user sees
# (same calendar day). ``valuation_cents`` MUST stay here — derive_score
# reads it; leaving it out silently depresses every score.
_PERMIT_VIEW_COLS = """id, permit_number, permit_type, description, status,
                  issued_date, expires_date,
                  contractor_phone, contractor_email,
                  address, owner_name, contractor_name, valuation_cents,
                  contact_name, contact_type,
                  ai_score, ai_grade, trade, city, state"""
_SCORE_SRC_COLS = _PERMIT_VIEW_COLS  # back-compat alias


def refresh_permit_scores(*, only_stale: bool = True, only_null: bool = False,
                          batch: int = 4000, limit: int | None = None) -> int:
    """Recompute ``permits.score_cache`` from the 12-factor derived score.

    * ``only_null``  — score ONLY rows that have never been scored
      (``score_day IS NULL``). Cheap; used right after an ingest so new
      permits get an immediate rank instead of sorting last.
    * ``only_stale`` — score every row whose ``score_day`` isn't today
      (the daily calendar refresh: the freshness / expiry / seasonal
      factors move with the date). Ignored when ``only_null`` is set.
    * Neither flag's WHERE matches → full re-score of the table.

    Works in bounded batches so a 25k-row refresh never opens one giant
    transaction. Returns the number of rows updated.
    """
    _ensure_permit_score_columns()
    # Cursor-paginate by primary key for ALL modes. Paging by id (rather
    # than re-running ``... LIMIT n`` from the top) keeps progress
    # deterministic and can never loop forever — even on a full re-score
    # where the rows don't drop out of the predicate after the UPDATE.
    if only_null:
        pred = "score_day IS NULL AND id > %s"
    elif only_stale:
        pred = "score_day IS DISTINCT FROM CURRENT_DATE AND id > %s"
    else:
        pred = "id > %s"
    total = 0
    cursor_id = 0
    while True:
        take = batch if limit is None else min(batch, limit - total)
        if take <= 0:
            break
        rows = pg.query(
            f"SELECT {_SCORE_SRC_COLS} FROM permits WHERE {pred} "
            f"ORDER BY id LIMIT %s",
            (cursor_id, int(take)),
        )
        if not rows:
            break
        pairs = [(int(r['id']), int(_row_to_permit_view(r)['score']))
                 for r in rows]
        # Bulk UPDATE via VALUES join — one round-trip per batch.
        pg.execute(
            "UPDATE permits AS p "
            "SET score_cache = v.sc, score_day = CURRENT_DATE "
            "FROM (VALUES %s) AS v(pid, sc) "
            "WHERE p.id = v.pid" % ', '.join(['(%s, %s)'] * len(pairs)),
            tuple(x for pair in pairs for x in pair),
        )
        total += len(rows)
        cursor_id = int(rows[-1]['id'])
    return total


# ── Daily-refresh self-heal (no external cron required) ──────────────
# (``threading`` is already imported at module top.)
_score_refresh_lock = threading.Lock()
_score_refresh_state: dict = {'day': None, 'running': False}


def ensure_scores_fresh_async() -> None:
    """Kick a background daily re-score if today's hasn't run yet.

    Non-blocking: callers (the /permits/ read path) serve instantly from
    the existing ``score_cache`` while one daemon thread per process
    recomputes the day's scores. Debounced to once per calendar day; a
    failed run simply retries on the next request. This makes the
    "refreshed once a day" guarantee hold even with no external cron
    wired up — a management command (refresh_permit_scores) is also
    provided for deterministic scheduling."""
    from datetime import date as _date
    today = _date.today().isoformat()
    if _score_refresh_state['day'] == today:
        return
    with _score_refresh_lock:
        if _score_refresh_state['day'] == today or _score_refresh_state['running']:
            return
        _score_refresh_state['running'] = True

    def _worker():
        try:
            refresh_permit_scores(only_stale=True)
            _score_refresh_state['day'] = today
        except Exception:
            log.exception('ensure_scores_fresh_async: daily re-score failed')
        finally:
            _score_refresh_state['running'] = False

    threading.Thread(target=_worker, daemon=True).start()


def _ensure_scrapers_table():
    """Create the `scrapers` and `scraper_runs` tables on first use.

    Wrapped in a hard ``lock_timeout`` / ``statement_timeout`` guard so
    that an in-flight background scraper (which can hold a brief
    RowExclusiveLock on ``permits`` while upserting) can NEVER stall
    the admin dashboard for tens of seconds the way it did pre-PR
    #452: the ALTER TABLE permits ADD COLUMN was waiting on an
    AccessExclusiveLock and hung the entire request thread.
    On a lock-timeout we mark the table "ready" anyway — the additive
    DDL is idempotent and the next worker boot (or scraper restart)
    will retry. The dashboard only needs the ``scrapers`` row read,
    which the base CREATE TABLE IF NOT EXISTS already covers.
    """
    global _SCRAPERS_TABLE_READY
    if _SCRAPERS_TABLE_READY:
        return
    # 2 s to acquire any lock, 15 s overall ceiling per statement.
    # We use a single pooled connection so SET (autocommit) sticks
    # for every subsequent DDL in this function. The connection is
    # released back to the pool with default timeouts restored.
    try:
        with pg.conn() as _c:
            with _c.cursor() as _cur:
                _cur.execute("SET lock_timeout      = '2s'")
                _cur.execute("SET statement_timeout = '15s'")
            _ensure_scrapers_table_impl(_c)
        _SCRAPERS_TABLE_READY = True
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning(
            '_ensure_scrapers_table: deferred DDL skipped (%s) — '
            'will retry on next worker boot', _e.__class__.__name__)
        # Mark ready so subsequent dashboard hits don't re-hang on
        # the same lock contention. Idempotent CREATE TABLE IF NOT
        # EXISTS means the basic shape is already in place from a
        # prior boot; only additive ALTERs may have been skipped.
        _SCRAPERS_TABLE_READY = True
    return


def _ensure_scrapers_table_impl(_c):
    """Original DDL body — runs on a single connection so the
    ``SET lock_timeout`` / ``SET statement_timeout`` configured by
    the caller applies to every statement below."""
    def _ex(sql, params=()):
        with _c.cursor() as cur:
            cur.execute(sql, params)
    def _q1(sql, params=()):
        with _c.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
            return dict(row) if row else None
    pg_execute_orig, pg_query_one_orig = pg.execute, pg.query_one
    pg.execute, pg.query_one = _ex, _q1
    try:
        _ensure_scrapers_table_body()
    finally:
        pg.execute, pg.query_one = pg_execute_orig, pg_query_one_orig


def _ensure_scrapers_table_body():
    """Idempotent DDL — must only be called from
    :func:`_ensure_scrapers_table_impl`, which rebinds ``pg.execute``
    / ``pg.query_one`` onto a single connection for the duration."""
    pg.execute(
        """CREATE TABLE IF NOT EXISTS scrapers (
              id              BIGSERIAL PRIMARY KEY,
              name            TEXT        NOT NULL,
              source          TEXT        NOT NULL DEFAULT 'accela',
              url             TEXT        NOT NULL,
              agency_code     TEXT,
              module          TEXT,
              cap_id_template JSONB       NOT NULL DEFAULT '{}'::jsonb,
              city            TEXT,
              state           TEXT,
              enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
              last_run_at     TIMESTAMPTZ,
              last_run_status TEXT,
              total_permits   INTEGER     NOT NULL DEFAULT 0,
              config          JSONB       NOT NULL DEFAULT '{}'::jsonb,
              created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
           )"""
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS scrapers_enabled_idx ON scrapers(enabled)"
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS scrapers_last_run_idx "
        "ON scrapers(last_run_at DESC NULLS LAST)"
    )
    # One Accela permit-search portal per (state, city). Partial so it
    # only constrains source='accela' rows — other future sources may
    # legitimately need multiple scrapers per city. LOWER(city) is a
    # belt-and-braces guard against case drift even though
    # _create_or_dedup_accela_scraper already normalizes to title-case.
    # The app-level dedup helper is the primary gate; this index is
    # the last line of defence if a future code path bypasses it.
    # Self-healing: if an older deployment already created
    # scrapers_accela_one_per_city_uidx with a looser predicate
    # (e.g. without the IS NOT NULL guards), CREATE INDEX IF NOT
    # EXISTS would silently no-op and leave the old predicate in
    # place. Inspect pg_indexes and drop+recreate iff the stored
    # definition doesn't already contain both NOT NULL clauses.
    _expected_pred_markers = ('state IS NOT NULL', 'city IS NOT NULL')
    _existing = pg.query_one(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'scrapers_accela_one_per_city_uidx'"
    )
    if _existing:
        _def = (_existing.get('indexdef') or '')
        if not all(m in _def for m in _expected_pred_markers):
            pg.execute('DROP INDEX scrapers_accela_one_per_city_uidx')
    pg.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS scrapers_accela_one_per_city_uidx "
        "ON scrapers (state, LOWER(city)) "
        "WHERE source = 'accela' AND state IS NOT NULL AND city IS NOT NULL"
    )
    pg.execute(
        """CREATE TABLE IF NOT EXISTS scraper_runs (
              id              BIGSERIAL PRIMARY KEY,
              scraper_id      BIGINT      NOT NULL REFERENCES scrapers(id) ON DELETE CASCADE,
              kind            TEXT        NOT NULL DEFAULT 'manual',
              mode            TEXT        NOT NULL DEFAULT 'single',
              status          TEXT        NOT NULL DEFAULT 'queued',
              started_at      TIMESTAMPTZ,
              finished_at     TIMESTAMPTZ,
              date_from       DATE,
              date_to         DATE,
              total_targets   INTEGER     NOT NULL DEFAULT 0,
              processed       INTEGER     NOT NULL DEFAULT 0,
              succeeded       INTEGER     NOT NULL DEFAULT 0,
              failed          INTEGER     NOT NULL DEFAULT 0,
              current_step    TEXT,
              error           JSONB       NOT NULL DEFAULT '[]'::jsonb,
              step_log        JSONB       NOT NULL DEFAULT '[]'::jsonb,
              created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
           )"""
    )
    # ── Additive migrations for already-deployed instances ────────
    # `step_log` was added after the table shipped — add it in place
    # so existing prod rows keep their primary key + history while
    # gaining the new CLI-style step stream column. Idempotent.
    pg.execute(
        "ALTER TABLE scraper_runs "
        "ADD COLUMN IF NOT EXISTS step_log JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    # Cooperative-cancel flag. The worker polls this between pages /
    # per-detail extractions and bails out cleanly when set, so the
    # admin can stop a runaway job without killing the gunicorn worker.
    pg.execute(
        "ALTER TABLE scraper_runs "
        "ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # ── Worker process / thread provenance (PR #222) ─────────────────
    # We stamp the worker's OS pid + Python thread ident on the run row
    # the moment the daemon thread starts. Lets the admin UI show
    # "pid 1234 · tid 139…" next to a running scraper, and lets the
    # Force-stop endpoint find the right thread (when same gunicorn
    # worker) or print a kill -SIGTERM hint (when not) instead of just
    # silently no-oping.
    pg.execute(
        "ALTER TABLE scraper_runs "
        "ADD COLUMN IF NOT EXISTS worker_pid INTEGER"
    )
    pg.execute(
        "ALTER TABLE scraper_runs "
        "ADD COLUMN IF NOT EXISTS worker_tid BIGINT"
    )
    # ── Worker heartbeat (orphan detection) ─────────────────────────
    # Pid-based orphan detection breaks when the new server process
    # happens to be assigned the same OS pid as the dead worker
    # (common on container restarts where pid 1, 7, 23… get reused).
    # A liveness heartbeat is the only reliable signal: the worker
    # bumps `heartbeat_at` every ~15s; if a row says status='running'
    # but heartbeat_at is older than 60s, no live worker exists. One
    # cheap UPDATE every 15s — negligible DB load.
    pg.execute(
        "ALTER TABLE scraper_runs "
        "ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ"
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS scraper_runs_scraper_idx "
        "ON scraper_runs(scraper_id, created_at DESC)"
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS scraper_runs_status_idx "
        "ON scraper_runs(status, created_at DESC)"
    )

    # ── permits.scraper_run_id lineage column + FK ────────────────
    # Lets the admin pull "every permit this run created" and either
    # browse them or delete them in one shot. The FK is `ON DELETE
    # SET NULL` (NOT cascade) so deleting a run row can never
    # accidentally vapourise the permits — cascade only happens when
    # the admin opts in via `delete_scraper_run(delete_permits=True)`,
    # which deletes the permits FIRST and the run row second.
    #
    # We do this here (rather than in the canonical permits init
    # script) so brand-new installs get the column the first time
    # the scraper module loads — no separate migration step.
    #
    # IMPORTANT: this used to be a single
    #     ALTER TABLE ... ADD COLUMN IF NOT EXISTS ... REFERENCES ...
    # but Postgres skips the ENTIRE clause (FK and all) when the
    # column already exists, so the FK could silently never get
    # added on installs where init_permits_table.py created the
    # column without one. We now split it into two idempotent
    # steps: add the column if missing, then add the FK constraint
    # if missing (Postgres has no `ADD CONSTRAINT IF NOT EXISTS`,
    # so we look it up in pg_constraint first).
    #
    # We catch only `UndefinedTable` to handle the bootstrap case
    # where the permits table doesn't exist yet (init_permits_table.py
    # hasn't been run). Any other DB error is real and should
    # surface, not be silently swallowed.
    try:
        pg.execute(
            "ALTER TABLE permits "
            "ADD COLUMN IF NOT EXISTS scraper_run_id BIGINT"
        )
        # Add FK separately — only if not already present.
        fk_row = pg.query_one(
            """SELECT 1 AS exists
                 FROM pg_constraint
                WHERE conrelid = 'permits'::regclass
                  AND conname  = 'permits_scraper_run_id_fkey'"""
        )
        if not fk_row:
            pg.execute(
                "ALTER TABLE permits "
                "ADD CONSTRAINT permits_scraper_run_id_fkey "
                "FOREIGN KEY (scraper_run_id) REFERENCES scraper_runs(id) "
                "ON DELETE SET NULL"
            )
        pg.execute(
            "CREATE INDEX IF NOT EXISTS permits_scraper_run_idx "
            "ON permits(scraper_run_id) WHERE scraper_run_id IS NOT NULL"
        )
    except psycopg_errors.UndefinedTable:
        # permits table not yet provisioned — init_permits_table.py
        # already includes the column + FK in its canonical schema,
        # so the next-time install path will pick it up.
        pass

    # ── permits.manual_fields lock column ─────────────────────────
    # JSONB array of field names an admin hand-edited via the manual
    # record editor. ``upsert_permit`` reads this and EXCLUDES those
    # columns from the UPDATE so a later rescrape can never clobber a
    # value a human deliberately corrected. Stored as a real column
    # (not inside ``raw``) because the upsert always overwrites
    # ``raw = EXCLUDED.raw``, which would otherwise wipe the marker.
    try:
        pg.execute(
            "ALTER TABLE permits "
            "ADD COLUMN IF NOT EXISTS manual_fields JSONB "
            "NOT NULL DEFAULT '[]'::jsonb"
        )
    except psycopg_errors.UndefinedTable:
        pass

    # ── One-shot backfill: scraper.state from name suffix ────────
    # Convention everywhere is "{City} {ST}" — but the inline Edit
    # modal used to ship a blank State input, so admins easily saved
    # rows like "Fremont CA" with state=NULL. Recover those by
    # parsing the trailing 2-letter token of the name AND validating
    # against the canonical US state list (rejects junk suffixes
    # like "GO" or "AI"). Idempotent: WHERE state IS NULL skips
    # already-populated rows on every subsequent boot.
    try:
        from .us_cities_top import US_STATES
        codes = [c for c, _ in US_STATES]
        # Build the regex once: ` (CA|TX|NY|...)$` — strict trailing
        # match, single space separator. POSIX regex so it runs as
        # a single set-based UPDATE rather than row-by-row in Python.
        regex = r' (' + '|'.join(codes) + r')$'
        pg.execute(
            "UPDATE scrapers "
            "SET state = UPPER(SUBSTRING(name FROM %s)), "
            "    updated_at = NOW() "
            "WHERE state IS NULL "
            "  AND name ~ %s",
            (regex, regex),
        )
    except Exception:
        import logging as _log
        _log.getLogger(__name__).exception(
            'scrapers state backfill failed (non-fatal)')

    _SCRAPERS_TABLE_READY = True


# ── scrapers CRUD ────────────────────────────────────────────────────

def create_scraper(*, name: str, url: str, source: str = 'accela',
                   agency_code: str = '', module: str = '',
                   cap_id_template: dict | None = None,
                   city: str = '', state: str = '',
                   enabled: bool = True, config: dict | None = None) -> int:
    """Insert a new scraper row and return its id."""
    _ensure_scrapers_table()
    row = pg.query_one(
        """INSERT INTO scrapers (name, source, url, agency_code, module,
                                  cap_id_template, city, state, enabled, config)
           VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
           RETURNING id""",
        (
            (name or '').strip()[:200] or 'Untitled Scraper',
            source.strip().lower() or 'accela',
            (url or '').strip(),
            (agency_code or '').strip().upper() or None,
            (module or '').strip() or None,
            json.dumps(cap_id_template or {}),
            (city or '').strip().title() or None,
            (state or '').strip().upper() or None,
            bool(enabled),
            json.dumps(config or {}),
        ),
    )
    return int(row['id'])


def get_scraper(scraper_id: int) -> dict | None:
    _ensure_scrapers_table()
    row = pg.query_one(
        "SELECT * FROM scrapers WHERE id = %s",
        (int(scraper_id),),
    )
    return dict(row) if row else None


def list_scrapers(query: str = '', page: int = 1, per_page: int = 20):
    """Return (rows, total, total_pages, page) for the admin list view.

    Kept for any non-DataTable consumer (e.g. cron scripts). The admin
    web UI now uses :func:`list_scrapers_dt` for server-side DataTable
    paging instead.
    """
    _ensure_scrapers_table()
    per_page = max(1, min(int(per_page or 20), 100))
    page = max(1, int(page or 1))
    q = (query or '').strip()
    where = ''
    params: list = []
    if q:
        where = "WHERE name ILIKE %s OR url ILIKE %s OR city ILIKE %s OR agency_code ILIKE %s"
        like = f'%{q}%'
        params = [like, like, like, like]
    total_row = pg.query_one(
        f"SELECT COUNT(*) AS n FROM scrapers {where}",
        tuple(params) if params else None,
    )
    total = int((total_row or {}).get('n') or 0)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    rows = pg.query(
        f"""SELECT * FROM scrapers
            {where}
            ORDER BY enabled DESC, last_run_at DESC NULLS LAST, id DESC
            LIMIT %s OFFSET %s""",
        tuple(params + [per_page, offset]),
    ) or []
    # NB: returning the *clamped* page so callers can render an
    # honest pager — request `?page=999` would otherwise show
    # "page 999 of 3" while the rows are actually from page 3.
    return [dict(r) for r in rows], total, total_pages, page


def list_scrapers_dt(*, search: str = '', state: str = '', city: str = '',
                     status: str = '',
                     start: int = 0, length: int = 25,
                     order_col: str = 'last_run', order_dir: str = 'desc'):
    """DataTables server-side data source for the admin scrapers list.

    Returns ``(rows, total_unfiltered, total_filtered)``.

    ``state`` / ``city`` apply exact-match filters (uppercased /
    title-cased to match how :func:`create_scraper` stores them).
    ``search`` runs an ILIKE across name, URL, city and agency_code,
    matching the legacy ?q= behaviour. ``order_col`` is whitelisted
    so a malicious AJAX param can never inject SQL.
    """
    _ensure_scrapers_table()
    length = max(1, min(int(length or 25), 200))
    start = max(0, int(start or 0))

    # Whitelist of orderable columns — value is the SQL column /
    # expression used in ORDER BY, never raw user input. Bad keys
    # fall back to the default sort. NULLS LAST is appended below
    # *after* the direction (the only legal ordering for that suffix).
    # NOTE: `permits` sorts by the live subquery alias (`live_permits`)
    # added to the SELECT below, NOT the cached `total_permits` column
    # on the scrapers table — that counter drifts whenever permits
    # are deleted/wiped outside of refresh_scraper_total_permits().
    order_map = {
        'name':     'name',
        'url':      'url',
        'city':     'city',
        'state':    'state',
        'last_run': 'last_run_at',
        'permits':  'live_permits',
        'status':   'last_run_status',
    }
    order_sql = order_map.get((order_col or 'last_run').lower(),
                              'last_run_at')
    order_dir_sql = 'ASC' if str(order_dir or '').lower() == 'asc' else 'DESC'
    # Always send NULLs to the bottom regardless of direction — an
    # admin sorting by Last Run wants the freshly-run scrapers up top
    # whether they ascend or descend, and *also* doesn't want a wall
    # of "never run" rows pinned to the top in DESC mode.
    order_clause = f"{order_sql} {order_dir_sql} NULLS LAST"

    total_row = pg.query_one('SELECT COUNT(*) AS n FROM scrapers')
    total_unfiltered = int((total_row or {}).get('n') or 0)

    where: list[str] = []
    params: list = []
    s = (search or '').strip()
    if s:
        where.append(
            "(name ILIKE %s OR url ILIKE %s OR city ILIKE %s "
            "OR state ILIKE %s OR agency_code ILIKE %s)"
        )
        like = f'%{s}%'
        params.extend([like, like, like, like, like])
    st = (state or '').strip().upper()
    if st:
        where.append('state = %s')
        params.append(st)
    ct = (city or '').strip().title()
    if ct:
        where.append('city = %s')
        params.append(ct)
    fst = (status or '').strip().lower()
    if fst == 'idle':
        # An "idle" pill means: enabled scraper that's never run AND
        # whose last_run_status is empty/null. Disabled rows render as
        # "disabled" and have their own filter below — keep them out.
        where.append('enabled = TRUE')
        where.append('(last_run_status IS NULL OR last_run_status = %s)')
        params.append('')
    elif fst == 'disabled':
        where.append('enabled = FALSE')
    elif fst in ('success', 'failed', 'running', 'cancelled', 'partial'):
        where.append('last_run_status = %s')
        params.append(fst)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    # Skip the second COUNT round-trip when nothing is filtering.
    if not where:
        total_filtered = total_unfiltered
    else:
        cnt_row = pg.query_one(
            f"SELECT COUNT(*) AS n FROM scrapers {where_sql}",
            tuple(params),
        )
        total_filtered = int((cnt_row or {}).get('n') or 0)

    # `live_permits` is a per-row correlated COUNT against the permits
    # table, keyed on the `accela:<scraper_id>` source tag that the
    # ingest path stamps on every permit (see _scraper_source_tag). We
    # surface it as the authoritative permit count and overwrite the
    # cached `total_permits` column below so the JSON payload (and any
    # ORDER BY on the alias) always reflects the actual DB row count
    # — independent of whether refresh_scraper_total_permits has been
    # called recently. Cheap because permits.source is indexed.
    rows = pg.query(
        f"""SELECT scrapers.*,
                   COALESCE((SELECT COUNT(*) FROM permits
                              WHERE source = 'accela:' || scrapers.id),
                            0) AS live_permits
              FROM scrapers
            {where_sql}
            ORDER BY {order_clause}, id DESC
            LIMIT %s OFFSET %s""",
        tuple(params + [length, start]),
    ) or []
    out = []
    for r in rows:
        d = dict(r)
        d['total_permits'] = int(d.pop('live_permits', 0) or 0)
        out.append(d)
    return out, total_unfiltered, total_filtered


def list_scraper_state_city_options() -> dict:
    """Distinct states + cities-by-state used to populate the filter
    dropdowns on the scrapers list page. Cheap (one COUNT-bounded scan
    of the scrapers table — typically dozens of rows, never millions)
    so we just rebuild it on every page render rather than caching.

    Returns ``{'states': [{'code': 'CA', 'count': 7}, ...],
              'cities_by_state': {'CA': ['Lancaster', 'Ontario', ...]}}``
    """
    _ensure_scrapers_table()
    state_rows = pg.query(
        """SELECT state, COUNT(*) AS n
           FROM scrapers
           WHERE state IS NOT NULL AND length(trim(state)) > 0
           GROUP BY state
           ORDER BY state"""
    ) or []
    states = [{'code': r['state'], 'count': int(r['n'])} for r in state_rows]

    city_rows = pg.query(
        """SELECT state, city
           FROM scrapers
           WHERE state IS NOT NULL AND city IS NOT NULL
             AND length(trim(state)) > 0 AND length(trim(city)) > 0
           GROUP BY state, city
           ORDER BY state, city"""
    ) or []
    cities_by_state: dict[str, list[str]] = {}
    for r in city_rows:
        cities_by_state.setdefault(r['state'], []).append(r['city'])
    return {'states': states, 'cities_by_state': cities_by_state}


def update_scraper(scraper_id: int, **fields) -> bool:
    """Patch arbitrary fields on a scraper. Unknown keys are silently
    ignored to keep the JSONB column safe from typos."""
    _ensure_scrapers_table()
    allowed = {'name', 'url', 'agency_code', 'module', 'city', 'state',
               'enabled', 'last_run_at', 'last_run_status', 'total_permits',
               'cap_id_template', 'config'}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ('cap_id_template', 'config'):
            sets.append(f'{k} = %s::jsonb')
            vals.append(json.dumps(v or {}))
        else:
            sets.append(f'{k} = %s')
            vals.append(v)
    if not sets:
        return False
    sets.append('updated_at = NOW()')
    vals.append(int(scraper_id))
    pg.execute(
        f"UPDATE scrapers SET {', '.join(sets)} WHERE id = %s",
        tuple(vals),
    )
    return True


def delete_scraper(scraper_id: int) -> bool:
    """Delete a scraper AND every permit it ingested.

    Cascade contract (admin asked 2026-05-24): deleting a scraper now
    also removes every `permits` row whose `source` equals this
    scraper's source tag (`accela:<id>`), so the All Permit Data
    table never contains zombie rows whose origin scraper no longer
    exists. Also sweeps any pre-existing orphaned permits so old
    deletions are healed retroactively in one pass.
    """
    _ensure_scrapers_table()
    sid = int(scraper_id)
    src = _scraper_source_tag(sid)
    n_permits = pg.execute("DELETE FROM permits WHERE source = %s", (src,))
    pg.execute("DELETE FROM scrapers WHERE id = %s", (sid,))
    if n_permits:
        try:    _invalidate_permits_cache()
        except Exception:
            log.exception('delete_scraper: cache invalidation failed (sid=%s)', sid)
    # Opportunistic orphan sweep — cheap (one DELETE with anti-join)
    # and means callers don't have to remember to run it separately.
    try:    delete_orphaned_permits()
    except Exception:
        log.exception('delete_scraper: orphan sweep failed (sid=%s)', sid)
    return True


def bulk_delete_scrapers(scraper_ids: list[int]) -> int:
    """Delete many scrapers AND every permit they ingested in one
    round-trip per table. Returns the number of scraper rows deleted
    (silently skips ids that don't exist).

    Same cascade contract as :func:`delete_scraper` — see its docstring.
    Also runs the orphan-permit sweep once at the end so the global
    permits table never accumulates rows whose scraper has been
    forgotten.
    """
    _ensure_scrapers_table()
    ids = [int(i) for i in (scraper_ids or []) if str(i).strip().lstrip('-').isdigit()]
    if not ids:
        return 0
    src_tags = [_scraper_source_tag(i) for i in ids]
    n_permits = pg.execute(
        "DELETE FROM permits WHERE source = ANY(%s)", (src_tags,),
    )
    row = pg.query_one(
        "WITH d AS (DELETE FROM scrapers WHERE id = ANY(%s) RETURNING 1) "
        "SELECT COUNT(*) AS n FROM d",
        (ids,),
    )
    if n_permits:
        try:    _invalidate_permits_cache()
        except Exception:
            log.exception('bulk_delete_scrapers: cache invalidation failed')
    try:    delete_orphaned_permits()
    except Exception:
        log.exception('bulk_delete_scrapers: orphan sweep failed')
    return int((row or {}).get('n') or 0)


def delete_orphaned_permits() -> int:
    """One-shot cleanup: delete every permit whose `source` tag points
    at an `accela:<id>` scraper that no longer exists in the scrapers
    table. Returns the number of rows deleted.

    Called automatically at the end of every scraper-delete path so
    the global permits table self-heals, and also safe to invoke from
    an admin sweep if needed.
    """
    _ensure_scrapers_table()
    # Anti-join: any permit whose source matches the `accela:<n>`
    # pattern but whose <n> is not in `scrapers.id` is orphaned.
    # `regexp_replace` extracts the trailing digits; rows whose
    # source doesn't match the pattern get an empty string and
    # are filtered out by the WHERE.
    n = pg.execute(
        """
        DELETE FROM permits
         WHERE source ~ '^accela:[0-9]+$'
           AND NOT EXISTS (
               SELECT 1 FROM scrapers s
                WHERE s.id = (regexp_replace(permits.source,
                                             '^accela:', ''))::bigint
           )
        """,
    )
    if n:
        try:    _invalidate_permits_cache()
        except Exception:
            log.exception('delete_orphaned_permits: cache invalidation failed')
        log.info('delete_orphaned_permits: removed %s orphan permits', n)
    return int(n or 0)


# ── scraper_runs CRUD ────────────────────────────────────────────────

def create_scraper_run(scraper_id: int, *, kind: str = 'manual',
                       mode: str = 'single', total_targets: int = 0,
                       date_from=None, date_to=None) -> int:
    _ensure_scrapers_table()
    row = pg.query_one(
        """INSERT INTO scraper_runs (scraper_id, kind, mode, status,
                                      total_targets, date_from, date_to,
                                      started_at, current_step)
           VALUES (%s, %s, %s, 'queued', %s, %s, %s, NOW(), 'queued')
           RETURNING id""",
        (int(scraper_id), kind, mode, int(total_targets or 0),
         date_from, date_to),
    )
    return int(row['id'])


def update_scraper_run(run_id: int, **fields) -> bool:
    _ensure_scrapers_table()
    allowed = {'status', 'finished_at', 'total_targets', 'processed',
               'succeeded', 'failed', 'current_step', 'error', 'step_log',
               'cancel_requested', 'worker_pid', 'worker_tid'}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ('error', 'step_log'):
            sets.append(f'{k} = %s::jsonb')
            vals.append(json.dumps(v or []))
        else:
            sets.append(f'{k} = %s')
            vals.append(v)
    if not sets:
        return False
    vals.append(int(run_id))
    pg.execute(
        f"UPDATE scraper_runs SET {', '.join(sets)} WHERE id = %s",
        tuple(vals),
    )
    return True


def append_scraper_run_step(run_id: int, message: str,
                            level: str = 'info') -> None:
    """Atomically append one CLI-style step entry to a run's step_log.

    Used by the worker to stream live progress that the admin UI's
    terminal panel polls every 1.5s. We use jsonb concatenation
    (``||``) so concurrent appends from different code paths in the
    worker don't clobber each other (though in practice the worker
    is single-threaded per run).

    Each entry has ``{ts, level, msg}``; level is one of
    ``info|ok|warn|err`` and the UI colour-codes accordingly.
    """
    _ensure_scrapers_table()
    entry = json.dumps([{
        'ts':    datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'level': str(level or 'info'),
        'msg':   str(message or ''),
    }])
    try:
        pg.execute(
            "UPDATE scraper_runs "
            "   SET step_log = COALESCE(step_log, '[]'::jsonb) || %s::jsonb "
            " WHERE id = %s",
            (entry, int(run_id)),
        )
    except Exception:
        # Never let a failed log-append crash the worker — the run
        # itself is the user's main concern, the terminal panel is
        # auxiliary debugging output.
        pass


def heartbeat_scraper_run(run_id: int) -> None:
    """Bump ``heartbeat_at = NOW()`` on a running scraper_run row.
    Called every ~15s by the worker's heartbeat thread so a dead
    worker can be detected from the row alone (no pid guesswork).
    Best-effort — a failed heartbeat write must never crash the run.
    """
    try:
        pg.execute(
            "UPDATE scraper_runs SET heartbeat_at = NOW() WHERE id = %s",
            (int(run_id),),
        )
    except Exception:
        pass


def get_scraper_run(run_id: int) -> dict | None:
    _ensure_scrapers_table()
    row = pg.query_one(
        "SELECT * FROM scraper_runs WHERE id = %s", (int(run_id),)
    )
    return dict(row) if row else None


def request_cancel_scraper_run(run_id: int) -> dict:
    """Flip the cancel_requested flag on a queued/running run.

    Returns ``{'ok': bool, 'status': str|None, 'already_finished': bool}``
    so the admin endpoint can give an honest reply (no-op vs flagged
    vs already-done). Idempotent — pressing Stop twice is harmless.
    """
    _ensure_scrapers_table()
    row = pg.query_one(
        "SELECT status, finished_at FROM scraper_runs WHERE id = %s",
        (int(run_id),),
    )
    if not row:
        return {'ok': False, 'status': None, 'already_finished': False}
    status = (row.get('status') or '').strip().lower()
    if row.get('finished_at') or status not in ('queued', 'running'):
        return {'ok': False, 'status': status, 'already_finished': True}
    pg.execute(
        "UPDATE scraper_runs SET cancel_requested = TRUE WHERE id = %s",
        (int(run_id),),
    )
    return {'ok': True, 'status': status, 'already_finished': False}


def is_cancel_requested(run_id: int) -> bool:
    """Cheap polling helper for the worker. Returns False on any DB
    error so a transient hiccup never accidentally kills a healthy run."""
    try:
        row = pg.query_one(
            "SELECT cancel_requested FROM scraper_runs WHERE id = %s",
            (int(run_id),),
        )
    except Exception:
        return False
    return bool(row and row.get('cancel_requested'))


def get_latest_scraper_run(scraper_id: int) -> dict | None:
    """Most-recent run for a scraper (any status). Used by the detail
    page to preload the terminal panel with the last known transcript
    when no run is currently active, so the admin lands on context
    instead of an empty box."""
    _ensure_scrapers_table()
    row = pg.query_one(
        """SELECT * FROM scraper_runs
            WHERE scraper_id = %s
            ORDER BY created_at DESC
            LIMIT 1""",
        (int(scraper_id),),
    )
    return dict(row) if row else None


def list_scraper_runs(scraper_id: int, limit: int = 20) -> list:
    _ensure_scrapers_table()
    rows = pg.query(
        """SELECT * FROM scraper_runs
            WHERE scraper_id = %s
            ORDER BY created_at DESC
            LIMIT %s""",
        (int(scraper_id), int(limit)),
    ) or []
    return [dict(r) for r in rows]


def list_recent_scraper_runs(limit: int = 50) -> list:
    """Recent runs across all scrapers. Used by the stats page."""
    _ensure_scrapers_table()
    rows = pg.query(
        """SELECT r.*, s.name AS scraper_name
            FROM scraper_runs r
            JOIN scrapers s ON s.id = r.scraper_id
            ORDER BY r.created_at DESC
            LIMIT %s""",
        (int(limit),),
    ) or []
    return [dict(r) for r in rows]


def count_permits_for_run(run_id: int) -> int:
    """How many permit rows currently point at this run via the
    `scraper_run_id` lineage column. Used by the per-run admin card to
    show "X permits will be deleted" before the cascade button is
    clicked."""
    _ensure_scrapers_table()
    row = pg.query_one(
        "SELECT COUNT(*) AS n FROM permits WHERE scraper_run_id = %s",
        (int(run_id),),
    )
    return int((row or {}).get('n') or 0)


def list_permits_for_run(run_id: int, limit: int = 200) -> list:
    """Return up to `limit` permits this run created (most-recently
    scraped first). Surfaced by the inline run-log modal so the admin
    can spot-check what the run actually produced before deleting."""
    _ensure_scrapers_table()
    rows = pg.query(
        """SELECT id, permit_number, address, city, state,
                  contractor_name, applied_date, issued_date,
                  valuation_cents, ai_score, ai_grade
             FROM permits
            WHERE scraper_run_id = %s
            ORDER BY scraped_at DESC NULLS LAST, id DESC
            LIMIT %s""",
        (int(run_id), int(limit)),
    ) or []
    return [dict(r) for r in rows]


class ScraperRunBusy(Exception):
    """Raised when an admin tries to delete a run that's still queued or
    actively running. Surfaces as a 409 from the admin endpoint so the
    user gets a readable error instead of a silent half-cascade racing
    against the worker."""


def delete_scraper_run(run_id: int, *, delete_permits: bool = False) -> dict:
    """Delete one scraper_runs row. When `delete_permits=True`, also
    nukes every permit whose `scraper_run_id` matches.

    Returns ``{'run_deleted': bool, 'permits_deleted': int}`` with HONEST
    rowcounts (read directly off the cursor, not estimated by a separate
    SELECT) so the admin UI can show a truthful confirmation toast.

    Refuses to delete a run that's still ``queued`` or ``running`` —
    the worker would race the cascade and either leave orphan rows
    (no FK) or trigger an FK violation (with FK). Caller should cancel
    the run first if they really need it gone.

    Everything happens in ONE transaction (single pooled connection,
    no auto-commit until the `with` block exits cleanly) with a row
    lock on the run row, so:
      • the worker can't upsert new permits between the count and
        the delete (FOR UPDATE blocks any other writer);
      • either both deletes commit, or neither does on exception;
      • we report the actual rowcount the cursor saw, not a separate
        SELECT that could have raced.
    """
    _ensure_scrapers_table()
    rid = int(run_id)
    permits_deleted = 0
    run_deleted     = False
    with pg.conn() as c, c.cursor() as cur:
        # Row-lock the run; reject if it's still in flight.
        cur.execute(
            "SELECT id, status FROM scraper_runs WHERE id = %s FOR UPDATE",
            (rid,),
        )
        row = cur.fetchone()
        if not row:
            return {'run_deleted': False, 'permits_deleted': 0}
        status = (row.get('status') or '').strip().lower()
        if status in ('queued', 'running'):
            raise ScraperRunBusy(
                f"scraper_run id={rid} is {status!r}; cancel it before deleting"
            )
        if delete_permits:
            cur.execute(
                "DELETE FROM permits WHERE scraper_run_id = %s",
                (rid,),
            )
            permits_deleted = cur.rowcount or 0
        cur.execute("DELETE FROM scraper_runs WHERE id = %s", (rid,))
        run_deleted = (cur.rowcount or 0) > 0
    return {
        'run_deleted':     bool(run_deleted),
        'permits_deleted': int(permits_deleted),
    }


# ── Stale scraper-run reaper ─────────────────────────────────────────
#
# Daemon threads can die mid-flight (gunicorn worker recycle, OOM kill,
# Firecrawl hanging past the urllib timeout). When that happens the
# scraper_runs row is left frozen in ``status='running'`` forever, the
# scraper detail page shows a permanent fake "in progress" run, and the
# delete-run cascade refuses to touch it because it looks live. Reap
# any run that hasn't moved in ``max_age_minutes`` and mark it failed
# so the UI tells the truth.
#
# Cheap (indexed UPDATE), so the scrapers list view calls it on every
# admin page load. Returns the rowcount for logging.
def reap_stale_scraper_runs(max_age_minutes: int = 30) -> int:
    _ensure_scrapers_table()
    mins = max(1, int(max_age_minutes))
    with pg.conn() as c, c.cursor() as cur:
        cur.execute(
            f"""UPDATE scraper_runs
                   SET status       = 'failed',
                       finished_at  = NOW(),
                       current_step = 'reaped: stale (>{mins}m, no progress)'
                 WHERE status IN ('queued', 'running')
                   AND COALESCE(started_at, created_at)
                       < NOW() - INTERVAL '{mins} minutes'"""
        )
        return int(cur.rowcount or 0)


# ── Cron batches ─────────────────────────────────────────────────────
#
# A "cron batch" wraps one click of the admin "Run cron now" button. It
# runs every enabled scraper in series, recording the order of child
# run_ids so the UI can stream progress for the whole batch — even
# across gunicorn workers, since everything is in Postgres.

_CRON_BATCHES_TABLE_READY = False


def _ensure_cron_batches_table():
    global _CRON_BATCHES_TABLE_READY
    if _CRON_BATCHES_TABLE_READY:
        return
    pg.execute(
        """CREATE TABLE IF NOT EXISTS cron_batches (
              id              BIGSERIAL PRIMARY KEY,
              started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              finished_at     TIMESTAMPTZ,
              status          TEXT        NOT NULL DEFAULT 'running',
              run_ids         JSONB       NOT NULL DEFAULT '[]'::jsonb,
              kicked_by       BIGINT,
              note            TEXT,
              coordinator_pid BIGINT
           )"""
    )
    # Back-compat: older deployments created the table without
    # ``coordinator_pid``. ADD COLUMN IF NOT EXISTS is idempotent and
    # required so the subprocess-coordinator migration can stamp the
    # PID for survive-restart liveness checks.
    pg.execute(
        "ALTER TABLE cron_batches "
        "ADD COLUMN IF NOT EXISTS coordinator_pid BIGINT"
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS cron_batches_started_idx "
        "ON cron_batches(started_at DESC)"
    )
    _CRON_BATCHES_TABLE_READY = True


def create_cron_batch(kicked_by: int | None = None) -> int:
    _ensure_cron_batches_table()
    row = pg.query_one(
        """INSERT INTO cron_batches (kicked_by)
                VALUES (%s)
             RETURNING id""",
        (int(kicked_by) if kicked_by else None,),
    )
    return int(row['id'])


def get_cron_batch(batch_id: int) -> dict | None:
    _ensure_cron_batches_table()
    row = pg.query_one(
        "SELECT * FROM cron_batches WHERE id = %s", (int(batch_id),)
    )
    return dict(row) if row else None


def update_cron_batch(batch_id: int, **fields) -> bool:
    """Patch a cron_batches row. Mirrors update_scraper_run's signature
    so coordinator-thread code reads cleanly."""
    if not fields:
        return False
    _ensure_cron_batches_table()
    cols = []
    vals = []
    for k, v in fields.items():
        cols.append(f"{k} = %s")
        # JSONB columns need json-encoded strings
        if k == 'run_ids' and v is not None and not isinstance(v, str):
            import json as _json
            vals.append(_json.dumps(v))
        else:
            vals.append(v)
    vals.append(int(batch_id))
    sql = f"UPDATE cron_batches SET {', '.join(cols)} WHERE id = %s"
    with pg.conn() as c, c.cursor() as cur:
        cur.execute(sql, tuple(vals))
        return (cur.rowcount or 0) > 0


# ── Finder batches (Accela search USA rotation) ──────────────────────
#
# Tracks server-side "Run All States" jobs on the Accela Search page.
# Each row = one full USA rotation.  The worker thread updates progress
# after every city so the polling endpoint can render a CMD-style panel.

_FINDER_BATCHES_TABLE_READY = False


def _ensure_finder_batches_table():
    global _FINDER_BATCHES_TABLE_READY
    if _FINDER_BATCHES_TABLE_READY:
        return
    pg.execute(
        """CREATE TABLE IF NOT EXISTS finder_batches (
              id             BIGSERIAL PRIMARY KEY,
              started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              finished_at    TIMESTAMPTZ,
              status         TEXT        NOT NULL DEFAULT 'running',
              kicked_by      BIGINT,
              thread_name    TEXT,
              total_cities   INT NOT NULL DEFAULT 0,
              processed      INT NOT NULL DEFAULT 0,
              succeeded      INT NOT NULL DEFAULT 0,
              failed         INT NOT NULL DEFAULT 0,
              current_state  TEXT,
              current_city   TEXT,
              states_done    INT NOT NULL DEFAULT 0,
              states_total   INT NOT NULL DEFAULT 0,
              log            JSONB NOT NULL DEFAULT '[]'::jsonb,
              note           TEXT
           )"""
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS finder_batches_started_idx "
        "ON finder_batches(started_at DESC)"
    )
    _FINDER_BATCHES_TABLE_READY = True


def create_finder_batch(*, kicked_by=None, total_cities=0,
                        states_total=0, thread_name='') -> int:
    _ensure_finder_batches_table()
    row = pg.query_one(
        """INSERT INTO finder_batches
                  (kicked_by, total_cities, states_total, thread_name)
           VALUES (%s, %s, %s, %s)
           RETURNING id""",
        (int(kicked_by) if kicked_by else None,
         int(total_cities), int(states_total), thread_name),
    )
    return int(row['id'])


def get_finder_batch(batch_id: int) -> dict | None:
    _ensure_finder_batches_table()
    row = pg.query_one(
        "SELECT * FROM finder_batches WHERE id = %s", (int(batch_id),)
    )
    return dict(row) if row else None


def update_finder_batch(batch_id: int, **fields) -> bool:
    if not fields:
        return False
    _ensure_finder_batches_table()
    cols = []
    vals = []
    for k, v in fields.items():
        cols.append(f"{k} = %s")
        if k == 'log' and v is not None and not isinstance(v, str):
            import json as _json
            vals.append(_json.dumps(v))
        else:
            vals.append(v)
    vals.append(int(batch_id))
    sql = f"UPDATE finder_batches SET {', '.join(cols)} WHERE id = %s"
    with pg.conn() as c, c.cursor() as cur:
        cur.execute(sql, tuple(vals))
        return (cur.rowcount or 0) > 0


def append_finder_batch_log(batch_id: int, entry: dict) -> bool:
    _ensure_finder_batches_table()
    import json as _json
    with pg.conn() as c, c.cursor() as cur:
        cur.execute(
            """UPDATE finder_batches
                  SET log = log || %s::jsonb
                WHERE id = %s""",
            (_json.dumps([entry]), int(batch_id)),
        )
        return (cur.rowcount or 0) > 0


# ── Finder request log ────────────────────────────────────────────────
#
# Every Accela finder call (single city or batch) is logged here with
# full request/response details so the admin can debug failures without
# having to reproduce them.

_FINDER_REQUESTS_TABLE_READY = False


def _ensure_finder_requests_table():
    global _FINDER_REQUESTS_TABLE_READY
    if _FINDER_REQUESTS_TABLE_READY:
        return
    pg.execute(
        """CREATE TABLE IF NOT EXISTS finder_requests (
              id             BIGSERIAL PRIMARY KEY,
              created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              city           TEXT,
              state          TEXT,
              model          TEXT,
              search_query   TEXT,
              search_results JSONB,
              search_count   INT DEFAULT 0,
              prompt         TEXT,
              system_prompt  TEXT,
              raw_response   TEXT,
              parsed_json    JSONB,
              url_found      TEXT,
              confidence     TEXT,
              reason         TEXT,
              error          TEXT,
              latency_ms     INT,
              search_ms      INT,
              inference_ms   INT,
              input_tokens   INT DEFAULT 0,
              output_tokens  INT DEFAULT 0,
              source         TEXT DEFAULT 'accela_finder',
              step_log       TEXT
           )"""
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS finder_requests_created_idx "
        "ON finder_requests(created_at DESC)"
    )
    for col_def in [
        ("search_query",   "TEXT"),
        ("search_results", "JSONB"),
        ("search_count",   "INT DEFAULT 0"),
        ("search_ms",      "INT"),
        ("inference_ms",   "INT"),
        ("step_log",       "TEXT"),
    ]:
        try:
            pg.execute(
                f"ALTER TABLE finder_requests ADD COLUMN IF NOT EXISTS "
                f"{col_def[0]} {col_def[1]}"
            )
        except Exception:
            pass
    _FINDER_REQUESTS_TABLE_READY = True


def record_finder_request(*, city=None, state=None, model=None,
                          search_query=None, search_results=None,
                          search_count=0, prompt=None, system_prompt=None,
                          raw_response=None, parsed_json=None,
                          url_found=None, confidence=None,
                          reason=None, error=None, latency_ms=None,
                          search_ms=None, inference_ms=None,
                          input_tokens=0, output_tokens=0,
                          source='accela_finder',
                          step_log=None) -> int | None:
    try:
        _ensure_finder_requests_table()
        import json as _json
        row = pg.query_one(
            """INSERT INTO finder_requests
                  (city, state, model, search_query, search_results,
                   search_count, prompt, system_prompt,
                   raw_response, parsed_json, url_found, confidence,
                   reason, error, latency_ms, search_ms, inference_ms,
                   input_tokens, output_tokens, source, step_log)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (
                (str(city)[:80])     if city else None,
                (str(state)[:40])    if state else None,
                (str(model)[:64])    if model else None,
                (str(search_query)[:500])    if search_query else None,
                _json.dumps(search_results) if search_results else None,
                int(search_count or 0),
                (str(prompt)[:16000]) if prompt else None,
                (str(system_prompt)[:8000]) if system_prompt else None,
                (str(raw_response)[:16000]) if raw_response else None,
                _json.dumps(parsed_json)    if parsed_json else None,
                (str(url_found)[:500])      if url_found else None,
                (str(confidence)[:10])       if confidence else None,
                (str(reason)[:500])          if reason else None,
                (str(error)[:2000])          if error else None,
                int(latency_ms)              if latency_ms is not None else None,
                int(search_ms)               if search_ms is not None else None,
                int(inference_ms)            if inference_ms is not None else None,
                max(0, int(input_tokens or 0)),
                max(0, int(output_tokens or 0)),
                (str(source)[:32])           if source else 'accela_finder',
                (str(step_log)[:16000])      if step_log else None,
            ),
        )
        return int(row['id']) if row else None
    except Exception:
        import logging as _log
        _log.getLogger(__name__).exception('record_finder_request failed')
        return None


def list_finder_requests(limit: int = 100, offset: int = 0) -> list[dict]:
    _ensure_finder_requests_table()
    rows = pg.query(
        "SELECT * FROM finder_requests ORDER BY created_at DESC "
        "LIMIT %s OFFSET %s",
        (min(500, max(1, int(limit))), max(0, int(offset))),
    )
    return [dict(r) for r in rows]


def count_finder_requests() -> int:
    _ensure_finder_requests_table()
    row = pg.query_one("SELECT COUNT(*) AS cnt FROM finder_requests")
    return int(row['cnt']) if row else 0


# ── Firecrawl + Claude API call tracking ─────────────────────────────
#
# Every Firecrawl HTTP call and every Anthropic call gets one row here
# so the admin "Firecrawl Usage" / "Claude Usage" pages can chart
# volume, success rate, latency, and rough cost. Both tables are
# write-mostly (one INSERT per call, no updates) and indexed by
# called_at + scraper_run_id for the typical query patterns.

_FIRECRAWL_CALLS_TABLE_READY = False
_CLAUDE_CALLS_TABLE_READY    = False


def _ensure_firecrawl_calls_table():
    global _FIRECRAWL_CALLS_TABLE_READY
    if _FIRECRAWL_CALLS_TABLE_READY:
        return
    pg.execute(
        """CREATE TABLE IF NOT EXISTS firecrawl_calls (
              id              BIGSERIAL PRIMARY KEY,
              called_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              scraper_run_id  BIGINT,
              source          TEXT,                  -- 'accela' | 'blog' | 'accela_finder'
              mode            TEXT,                  -- 'detail' | 'list' | 'blog' | 'agent'
              url             TEXT,
              status_code     INTEGER,
              latency_ms      INTEGER,
              response_bytes  INTEGER,
              error           TEXT
           )"""
    )
    # ── city/state columns (added later — IF NOT EXISTS so it is safe
    #    to call on every boot AND on a fresh database). They let the
    #    Firecrawl Usage page filter / sort by location for finder runs
    #    AND for per-scraper agent runs (which also know city+state).
    pg.execute(
        "ALTER TABLE firecrawl_calls ADD COLUMN IF NOT EXISTS city  TEXT"
    )
    pg.execute(
        "ALTER TABLE firecrawl_calls ADD COLUMN IF NOT EXISTS state TEXT"
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS firecrawl_calls_called_idx "
        "ON firecrawl_calls(called_at DESC)"
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS firecrawl_calls_run_idx "
        "ON firecrawl_calls(scraper_run_id) WHERE scraper_run_id IS NOT NULL"
    )
    # Composite index keyed by state then city — matches the typical
    # filter shape on the usage page (state dropdown narrows first,
    # city dropdown narrows second). Partial index keeps it tiny: only
    # rows that actually carry a state are included, so the legacy
    # detail/list calls (no city/state) don't bloat it.
    pg.execute(
        "CREATE INDEX IF NOT EXISTS firecrawl_calls_state_city_called_idx "
        "ON firecrawl_calls(state, city, called_at DESC) "
        "WHERE state IS NOT NULL"
    )
    # One-shot backfill — older accela_finder rows were logged with
    # the city/state concatenated into the `url` column as
    # "City, ST". Parse them back into the dedicated columns so the
    # filter dropdowns + recent-calls table cover historical runs too.
    # Idempotent: WHERE city IS NULL AND state IS NULL skips already-
    # backfilled rows, and the regex only matches rows whose url
    # actually looks like "Word, XX".
    try:
        pg.execute(
            """UPDATE firecrawl_calls
                  SET city  = TRIM(SPLIT_PART(url, ',', 1)),
                      state = UPPER(TRIM(SPLIT_PART(url, ',', 2)))
                WHERE source = 'accela_finder'
                  AND city  IS NULL
                  AND state IS NULL
                  AND url   ~ '^[^,]+,\\s*[A-Za-z]{2}$'"""
        )
    except Exception:
        import logging as _log
        _log.getLogger(__name__).exception(
            'firecrawl_calls finder backfill failed (non-fatal)')
    _FIRECRAWL_CALLS_TABLE_READY = True


def _ensure_claude_calls_table():
    global _CLAUDE_CALLS_TABLE_READY
    if _CLAUDE_CALLS_TABLE_READY:
        return
    pg.execute(
        """CREATE TABLE IF NOT EXISTS claude_calls (
              id               BIGSERIAL PRIMARY KEY,
              called_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              scraper_run_id   BIGINT,
              source           TEXT,                 -- 'accela' | 'blog'
              model            TEXT,
              status_code      INTEGER,
              latency_ms       INTEGER,
              input_tokens     INTEGER NOT NULL DEFAULT 0,
              output_tokens    INTEGER NOT NULL DEFAULT 0,
              total_tokens     INTEGER NOT NULL DEFAULT 0,
              error            TEXT
           )"""
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS claude_calls_called_idx "
        "ON claude_calls(called_at DESC)"
    )
    pg.execute(
        "CREATE INDEX IF NOT EXISTS claude_calls_run_idx "
        "ON claude_calls(scraper_run_id) WHERE scraper_run_id IS NOT NULL"
    )
    _CLAUDE_CALLS_TABLE_READY = True


def record_firecrawl_call(*, scraper_run_id=None, source='accela',
                          mode=None, url='', status_code=None,
                          latency_ms=None, response_bytes=None,
                          error=None, city=None, state=None) -> None:
    """One INSERT per Firecrawl HTTP call. Never raises — usage
    tracking must not break the actual scrape if the table or
    connection is briefly unavailable.

    ``city`` and ``state`` are stored as dedicated columns so the
    Firecrawl Usage page can filter / sort the history without having
    to parse them out of the ``url`` column. Both are normalised here
    (trimmed; state upper-cased and length-capped to 2 chars) so the
    state filter dropdown gets clean values regardless of the caller.
    """
    try:
        _ensure_firecrawl_calls_table()
        # Normalise city / state once, here — keep the dropdowns on
        # the usage page tidy and the partial state index small.
        city_v = (str(city).strip())[:120] if city else None
        state_v = (str(state).strip().upper())[:2] if state else None
        if state_v and not state_v.isalpha():
            # Reject obviously bogus state codes ("12", "—", etc.) so
            # the dropdown isn't littered with junk values.
            state_v = None
        pg.execute(
            """INSERT INTO firecrawl_calls
                  (scraper_run_id, source, mode, url, status_code,
                   latency_ms, response_bytes, error, city, state)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                int(scraper_run_id) if scraper_run_id else None,
                str(source or '')[:32],
                str(mode or '')[:32] if mode else None,
                (str(url or ''))[:2048],
                int(status_code) if status_code is not None else None,
                int(latency_ms) if latency_ms is not None else None,
                int(response_bytes) if response_bytes is not None else None,
                (str(error)[:500]) if error else None,
                city_v,
                state_v,
            ),
        )
    except Exception:
        import logging as _log
        _log.getLogger(__name__).exception('record_firecrawl_call failed')


def record_claude_call(*, scraper_run_id=None, source='accela',
                       model=None, status_code=None, latency_ms=None,
                       input_tokens=0, output_tokens=0,
                       error=None) -> None:
    try:
        _ensure_claude_calls_table()
        it = max(0, int(input_tokens or 0))
        ot = max(0, int(output_tokens or 0))
        pg.execute(
            """INSERT INTO claude_calls
                  (scraper_run_id, source, model, status_code,
                   latency_ms, input_tokens, output_tokens,
                   total_tokens, error)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                int(scraper_run_id) if scraper_run_id else None,
                str(source or '')[:32],
                (str(model or '')[:64]) if model else None,
                int(status_code) if status_code is not None else None,
                int(latency_ms) if latency_ms is not None else None,
                it, ot, it + ot,
                (str(error)[:500]) if error else None,
            ),
        )
    except Exception:
        import logging as _log
        _log.getLogger(__name__).exception('record_claude_call failed')


def claude_usage_summary(days: int = 30) -> dict:
    """Mirror of firecrawl_usage_summary but for Claude. Adds tokens
    in/out as the primary KPI since cost scales with tokens, not call
    count."""
    _ensure_claude_calls_table()
    days = max(1, min(int(days or 30), 90))
    totals = {
        'calls_24h': 0, 'calls_7d': 0, 'calls_30d': 0,
        'success_24h': 0, 'failed_24h': 0,
        'input_tokens_30d': 0, 'output_tokens_30d': 0,
        'total_tokens_30d': 0,
        'p50_ms_30d': 0, 'p95_ms_30d': 0,
    }
    row = pg.query_one(
        """SELECT
              COUNT(*) FILTER (WHERE called_at > NOW() - INTERVAL '24 hours') AS calls_24h,
              COUNT(*) FILTER (WHERE called_at > NOW() - INTERVAL '7 days')   AS calls_7d,
              COUNT(*) FILTER (WHERE called_at > NOW() - INTERVAL '30 days')  AS calls_30d,
              COUNT(*) FILTER (WHERE called_at > NOW() - INTERVAL '24 hours' AND error IS NULL) AS success_24h,
              COUNT(*) FILTER (WHERE called_at > NOW() - INTERVAL '24 hours' AND error IS NOT NULL) AS failed_24h,
              COALESCE(SUM(input_tokens)  FILTER (WHERE called_at > NOW() - INTERVAL '30 days'), 0) AS in_30d,
              COALESCE(SUM(output_tokens) FILTER (WHERE called_at > NOW() - INTERVAL '30 days'), 0) AS out_30d,
              COALESCE(SUM(total_tokens)  FILTER (WHERE called_at > NOW() - INTERVAL '30 days'), 0) AS tot_30d,
              PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY latency_ms)
                FILTER (WHERE called_at > NOW() - INTERVAL '30 days' AND latency_ms IS NOT NULL) AS p50,
              PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY latency_ms)
                FILTER (WHERE called_at > NOW() - INTERVAL '30 days' AND latency_ms IS NOT NULL) AS p95
           FROM claude_calls"""
    ) or {}
    if row:
        totals.update({
            'calls_24h':         int(row.get('calls_24h') or 0),
            'calls_7d':          int(row.get('calls_7d') or 0),
            'calls_30d':         int(row.get('calls_30d') or 0),
            'success_24h':       int(row.get('success_24h') or 0),
            'failed_24h':        int(row.get('failed_24h') or 0),
            'input_tokens_30d':  int(row.get('in_30d') or 0),
            'output_tokens_30d': int(row.get('out_30d') or 0),
            'total_tokens_30d':  int(row.get('tot_30d') or 0),
            'p50_ms_30d':        int(row.get('p50') or 0),
            'p95_ms_30d':        int(row.get('p95') or 0),
        })

    per_day_rows = pg.query(
        f"""SELECT
              to_char(date_trunc('day', called_at), 'YYYY-MM-DD') AS day,
              COALESCE(SUM(input_tokens),  0) AS in_tok,
              COALESCE(SUM(output_tokens), 0) AS out_tok,
              COUNT(*) AS calls,
              COUNT(*) FILTER (WHERE error IS NOT NULL) AS failed
           FROM claude_calls
          WHERE called_at > NOW() - INTERVAL '{days} days'
          GROUP BY 1
          ORDER BY 1 ASC"""
    ) or []
    per_day = [{
        'day':         r['day'],
        'input_tok':   int(r.get('in_tok') or 0),
        'output_tok':  int(r.get('out_tok') or 0),
        'calls':       int(r.get('calls') or 0),
        'failed':      int(r.get('failed') or 0),
    } for r in per_day_rows]

    recent_failures = pg.query(
        """SELECT id, called_at, scraper_run_id, source, model,
                   status_code, latency_ms, error
             FROM claude_calls
            WHERE error IS NOT NULL
            ORDER BY called_at DESC
            LIMIT 20"""
    ) or []
    return {
        'totals':          totals,
        'per_day':         per_day,
        'recent_failures': [dict(r) for r in recent_failures],
    }


def inference_stats(*, extraction_only: bool = True) -> dict:
    """Aggregate stats for the Scrapers → Inference Stats page.

    Returns counts of LLM calls (== HTML pages processed) and token
    sums bucketed by today / 7d / 30d / month-to-date / total, plus
    per-model breakdown and 30-day daily series. Caller turns the
    token counts into dollar costs using the (editable) per-model
    price table — keeping pricing out of SQL means admins can change
    rates without touching the DB.

    ``extraction_only`` excludes the URL-finder calls
    (``source='accela_finder'``) so the "pages processed" KPI counts
    real permit-detail extractions only, which is what the admin
    asked for.
    """
    _ensure_claude_calls_table()
    where_extract = "WHERE source IS DISTINCT FROM 'accela_finder'" if extraction_only else ""
    totals = pg.query_one(
        f"""SELECT
              COUNT(*) FILTER (WHERE called_at >= date_trunc('day',  NOW()))                                          AS calls_today,
              COUNT(*) FILTER (WHERE called_at >= NOW() - INTERVAL '7 days')                                          AS calls_7d,
              COUNT(*) FILTER (WHERE called_at >= NOW() - INTERVAL '30 days')                                         AS calls_30d,
              COUNT(*) FILTER (WHERE called_at >= date_trunc('month', NOW()))                                         AS calls_mtd,
              COUNT(*)                                                                                                AS calls_total,
              COALESCE(SUM(input_tokens)  FILTER (WHERE called_at >= date_trunc('day',  NOW())),         0)           AS in_today,
              COALESCE(SUM(output_tokens) FILTER (WHERE called_at >= date_trunc('day',  NOW())),         0)           AS out_today,
              COALESCE(SUM(input_tokens)  FILTER (WHERE called_at >= NOW() - INTERVAL '7 days'),         0)           AS in_7d,
              COALESCE(SUM(output_tokens) FILTER (WHERE called_at >= NOW() - INTERVAL '7 days'),         0)           AS out_7d,
              COALESCE(SUM(input_tokens)  FILTER (WHERE called_at >= NOW() - INTERVAL '30 days'),        0)           AS in_30d,
              COALESCE(SUM(output_tokens) FILTER (WHERE called_at >= NOW() - INTERVAL '30 days'),        0)           AS out_30d,
              COALESCE(SUM(input_tokens)  FILTER (WHERE called_at >= date_trunc('month', NOW())),        0)           AS in_mtd,
              COALESCE(SUM(output_tokens) FILTER (WHERE called_at >= date_trunc('month', NOW())),        0)           AS out_mtd,
              COALESCE(SUM(input_tokens),  0)                                                                         AS in_total,
              COALESCE(SUM(output_tokens), 0)                                                                         AS out_total
             FROM claude_calls {where_extract}"""
    ) or {}

    # Per-model breakdown over the last 30 days — the table the admin
    # will look at most often to compare 20b vs 120b vs 5-nano spend.
    per_model_rows = pg.query(
        f"""SELECT
                 COALESCE(model, '(unknown)') AS model,
                 COUNT(*)                     AS calls,
                 COALESCE(SUM(input_tokens),  0) AS in_tok,
                 COALESCE(SUM(output_tokens), 0) AS out_tok
             FROM claude_calls
             {where_extract}
              {'AND' if extraction_only else 'WHERE'} called_at >= NOW() - INTERVAL '30 days'
            GROUP BY COALESCE(model, '(unknown)')
            ORDER BY COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) DESC"""
    ) or []

    # 30-day daily series for the bar chart. generate_series fills
    # zero days so the chart x-axis is continuous.
    per_day_rows = pg.query(
        f"""SELECT d::date AS day,
                  COALESCE(c.calls,   0) AS calls,
                  COALESCE(c.in_tok,  0) AS in_tok,
                  COALESCE(c.out_tok, 0) AS out_tok
             FROM generate_series((NOW() - INTERVAL '29 days')::date,
                                  NOW()::date, '1 day') d
             LEFT JOIN (
                SELECT date_trunc('day', called_at)::date AS day,
                       COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens),  0) AS in_tok,
                       COALESCE(SUM(output_tokens), 0) AS out_tok
                  FROM claude_calls
                  {where_extract}
                   {'AND' if extraction_only else 'WHERE'} called_at >= (NOW() - INTERVAL '29 days')::date
                 GROUP BY 1
             ) c ON c.day = d::date
            ORDER BY d ASC"""
    ) or []

    return {
        'totals':    {k: int(v or 0) for k, v in (totals or {}).items()},
        'per_model': [dict(r) for r in per_model_rows],
        'per_day':   [dict(r) for r in per_day_rows],
    }


def recent_cron_batches(limit: int = 20) -> list:
    """Most recent cron batches with their child counts derived from
    the run_ids array. Used by the Cron sub-page history table."""
    _ensure_cron_batches_table()
    n = max(1, min(int(limit or 20), 100))
    rows = pg.query(
        """SELECT id, started_at, finished_at, status, note,
                   jsonb_array_length(run_ids) AS child_count, run_ids
             FROM cron_batches
            ORDER BY id DESC
            LIMIT %s""",
        (n,),
    ) or []
    return [dict(r) for r in rows]


def list_scrapers_by_ids(scraper_ids: list) -> list:
    """Lightweight projection (id, name, url, city, state, enabled) for
    a caller-supplied list of scraper ids. Used by the "Run Selected"
    bulk action on /admin-panel/scrapers/ — admin's explicit selection
    is honoured regardless of the ``enabled`` flag. Order matches the
    input list so the batch UI shows scrapers in the order chosen."""
    _ensure_scrapers_table()
    ids: list[int] = []
    seen: set[int] = set()
    for tok in (scraper_ids or []):
        try:
            n = int(tok)
        except (TypeError, ValueError):
            continue
        if n in seen:
            continue
        seen.add(n)
        ids.append(n)
    if not ids:
        return []
    rows = pg.query(
        """SELECT id, name, url, city, state, enabled
             FROM scrapers
            WHERE id = ANY(%s)""",
        (ids,),
    ) or []
    by_id = {int(r['id']): dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def list_enabled_scrapers_all() -> list:
    """Every enabled scraper, no pagination — used by the cron
    coordinator so it can't silently skip rows past the list_scrapers()
    100/page clamp. Lightweight projection (no JSONB blobs) since the
    coordinator only needs id + name."""
    _ensure_scrapers_table()
    rows = pg.query(
        """SELECT id, name, url, city, state, enabled
             FROM scrapers
            WHERE enabled = TRUE
            ORDER BY id ASC"""
    ) or []
    return [dict(r) for r in rows]


def reset_stuck_running_scrapers() -> int:
    """Flip *every* scraper without a live scraper_runs row back to
    ``last_run_status='idle'`` so the next cron pass starts with a
    clean slate. Returns the number of rows changed.

    Called at the start of every cron pass. The cron-trigger
    concurrency guard already proved no other batch is in flight, so
    any non-idle status (running / failed / cancelled / partial) is
    historical — it represents the *previous* pass's outcome, not a
    live state. Per user request: "when no cron is running, set all
    to idle so cron will then run them at the same time".

    Idempotent: a freshly-idle row is skipped via the `!= 'idle'`
    guard so we don't burn an UPDATE on every scraper every time."""
    _ensure_scrapers_table()
    res = pg.execute(
        """UPDATE scrapers s
              SET last_run_status = 'idle'
            WHERE COALESCE(s.last_run_status, '') <> 'idle'
              AND NOT EXISTS (
                  SELECT 1 FROM scraper_runs r
                   WHERE r.scraper_id = s.id
                     AND r.status = 'running'
                     AND r.finished_at IS NULL
              )
        """
    )
    try:
        return int(res or 0)
    except (TypeError, ValueError):
        return 0


def reap_stale_cron_batches(max_age_minutes: int = 60) -> int:
    """Mirror of reap_stale_scraper_runs for cron_batches. If the
    coordinator daemon thread dies (gunicorn worker recycle, OOM kill)
    the batch row sits in 'running' forever and the UI polls a phantom
    in-flight batch. Reap any batch with no finished_at older than
    ``max_age_minutes`` so the list view tells the truth.

    Cap is generous (60 min default) because a real batch with N
    enabled scrapers can legitimately take 30 min × N. Bump if you
    routinely scrape many scrapers in one pass."""
    _ensure_cron_batches_table()
    mins = max(1, int(max_age_minutes))
    with pg.conn() as c, c.cursor() as cur:
        cur.execute(
            f"""UPDATE cron_batches
                   SET status      = 'failed',
                       finished_at = NOW(),
                       note        = COALESCE(NULLIF(note, ''), '') ||
                                     ' (reaped: coordinator went away >'
                                     || {mins} || 'm ago)'
                 WHERE status = 'running'
                   AND finished_at IS NULL
                   AND started_at < NOW() - INTERVAL '{mins} minutes'"""
        )
        return int(cur.rowcount or 0)


def append_cron_batch_run_id(batch_id: int, run_id: int) -> None:
    """Append a scraper_runs.id to the batch's run_ids array. Done as a
    JSONB concat in SQL so two coordinator threads can't race a
    read-modify-write at the application layer (though we only ever
    spawn one coordinator per batch, this keeps it honest)."""
    _ensure_cron_batches_table()
    pg.execute(
        """UPDATE cron_batches
              SET run_ids = run_ids || to_jsonb(%s::bigint)
            WHERE id = %s""",
        (int(run_id), int(batch_id)),
    )


def list_scrapers_with_run_counts(query: str = '', page: int = 1, per_page: int = 50):
    """Same shape as ``list_scrapers`` but each row gets ``runs_total``
    and ``runs_failed`` joined in via a single LEFT JOIN.

    Used by the new "Scraper logs" index so the admin sees at a glance
    which scrapers have run history worth diving into. We deliberately
    do NOT filter by run age — all-time counts are what makes "this
    scraper has 412 runs" meaningful.
    """
    _ensure_scrapers_table()
    per_page = max(1, min(int(per_page or 50), 200))
    page     = max(1, int(page or 1))
    q        = (query or '').strip()
    where  = ''
    params: list = []
    if q:
        where = "WHERE s.name ILIKE %s OR s.url ILIKE %s OR s.city ILIKE %s OR s.agency_code ILIKE %s"
        like  = f'%{q}%'
        params = [like, like, like, like]
    total_row = pg.query_one(
        f"SELECT COUNT(*) AS n FROM scrapers s {where}",
        tuple(params) if params else None,
    )
    total       = int((total_row or {}).get('n') or 0)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page        = min(page, total_pages)
    offset      = (page - 1) * per_page
    rows = pg.query(
        f"""SELECT s.id, s.name, s.url, s.city, s.state, s.agency_code,
                   s.enabled, s.last_run_at, s.last_run_status,
                   COALESCE(rc.runs_total,  0)::int  AS runs_total,
                   COALESCE(rc.runs_failed, 0)::int  AS runs_failed,
                   rc.last_run_at_actual
              FROM scrapers s
              LEFT JOIN (
                SELECT scraper_id,
                       COUNT(*)                                              AS runs_total,
                       COUNT(*) FILTER (WHERE status = 'failed')             AS runs_failed,
                       MAX(created_at)                                        AS last_run_at_actual
                  FROM scraper_runs
                 GROUP BY scraper_id
              ) rc ON rc.scraper_id = s.id
            {where}
            ORDER BY rc.runs_total DESC NULLS LAST, s.last_run_at DESC NULLS LAST, s.id DESC
            LIMIT %s OFFSET %s""",
        tuple(params + [per_page, offset]),
    ) or []
    return [dict(r) for r in rows], total, total_pages, page


def bulk_delete_scraper_runs(scraper_id: int, run_ids: list,
                             *, delete_permits: bool = False) -> dict:
    """Delete many ``scraper_runs`` rows in a single transaction,
    skipping any that are queued/running (so the worker isn't raced).

    Constraints (deliberately strict):
      • Every ``run_ids`` entry must already belong to ``scraper_id``;
        IDs that don't are reported under ``not_found`` and skipped.
        This stops a forged ``sid`` from torching another scraper's
        history if the form is tampered with.
      • Queued/running runs are reported under ``busy`` and skipped —
        not raised — because partial success is the right UX for a
        bulk action (vs a single-row delete where it's a hard error).

    Returns::

        {
          'requested':        int,
          'runs_deleted':     int,
          'permits_deleted':  int,
          'busy':             [run_id, …],   # skipped, still in flight
          'not_found':        [run_id, …],   # didn't exist or wrong scraper
        }
    """
    _ensure_scrapers_table()
    sid    = int(scraper_id)
    wanted = sorted({int(r) for r in (run_ids or []) if r is not None})
    if not wanted:
        return {'requested': 0, 'runs_deleted': 0, 'permits_deleted': 0,
                'busy': [], 'not_found': []}
    runs_deleted    = 0
    permits_deleted = 0
    busy: list[int]      = []
    not_found: list[int] = []
    with pg.conn() as c, c.cursor() as cur:
        # Single SELECT … FOR UPDATE locks every targeted row so the
        # worker can't flip status between this check and the DELETE.
        cur.execute(
            """SELECT id, status
                 FROM scraper_runs
                WHERE id = ANY(%s) AND scraper_id = %s
                FOR UPDATE""",
            (wanted, sid),
        )
        rows = cur.fetchall() or []
        present = {int(r['id']) for r in rows}
        not_found = [r for r in wanted if r not in present]
        deletable: list[int] = []
        for r in rows:
            status = (r.get('status') or '').strip().lower()
            if status in ('queued', 'running'):
                busy.append(int(r['id']))
            else:
                deletable.append(int(r['id']))
        if deletable:
            if delete_permits:
                cur.execute(
                    "DELETE FROM permits WHERE scraper_run_id = ANY(%s)",
                    (deletable,),
                )
                permits_deleted = cur.rowcount or 0
            cur.execute(
                "DELETE FROM scraper_runs WHERE id = ANY(%s)",
                (deletable,),
            )
            runs_deleted = cur.rowcount or 0
    return {
        'requested':       len(wanted),
        'runs_deleted':    int(runs_deleted),
        'permits_deleted': int(permits_deleted),
        'busy':            busy,
        'not_found':       not_found,
    }


# ── stats helpers ────────────────────────────────────────────────────

def get_scraper_daily_stats(days: int = 30) -> list:
    """Return [{day, runs, succeeded, failed, permits}] for the last
    ``days`` days, oldest first, with zero-fill for empty days."""
    _ensure_scrapers_table()
    rows = pg.query(
        """SELECT DATE(started_at) AS day,
                  COUNT(*)         AS runs,
                  COALESCE(SUM(succeeded), 0) AS permits,
                  COUNT(*) FILTER (WHERE status = 'success') AS ok,
                  COUNT(*) FILTER (WHERE status IN ('failed','partial')) AS fail
            FROM scraper_runs
            WHERE started_at >= NOW() - (%s || ' days')::interval
            GROUP BY 1
            ORDER BY 1""",
        (str(int(days)),),
    ) or []
    by_day = {str(r['day']): r for r in rows}
    out = []
    today = date.today()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.isoformat()
        r = by_day.get(key)
        out.append({
            'day':      key,
            'label':    d.strftime('%b %d'),
            'runs':     int((r or {}).get('runs') or 0),
            'permits':  int((r or {}).get('permits') or 0),
            'ok':       int((r or {}).get('ok') or 0),
            'fail':     int((r or {}).get('fail') or 0),
        })
    return out


def get_permits_by_state(*, daily_window_hours: int = 24) -> list:
    """Per-state permit counts: today (rolling 24h), 7d, 30d, all-time.

    Powers the per-state stats table on /admin-panel/scraper-stats/
    so the admin can see at a glance which states have enough volume
    to justify a state-priced subscription tier. ``state`` is the raw
    permits.state value (upper-cased, NULLs grouped under '—').
    Sorted by all-time DESC so the biggest markets surface first.
    """
    rows = pg.query(
        """SELECT COALESCE(NULLIF(UPPER(state), ''), '—') AS state,
                  COUNT(*) FILTER (WHERE created_at >= NOW() - (%s || ' hours')::interval) AS today,
                  COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')          AS d7,
                  COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days')         AS d30,
                  COUNT(*)                                                                  AS total
             FROM permits
            GROUP BY 1
            ORDER BY total DESC""",
        (str(int(daily_window_hours)),),
    ) or []
    return [{
        'state': r['state'],
        'today': int(r['today'] or 0),
        'd7':    int(r['d7']    or 0),
        'd30':   int(r['d30']   or 0),
        'total': int(r['total'] or 0),
    } for r in rows]


def get_scraper_summary() -> dict:
    """Top-line numbers for the stats page hero cards."""
    _ensure_scrapers_table()
    s_total = (pg.query_one("SELECT COUNT(*) AS n FROM scrapers") or {}).get('n') or 0
    s_on    = (pg.query_one("SELECT COUNT(*) AS n FROM scrapers WHERE enabled") or {}).get('n') or 0
    # NB: pass the LIKE pattern as a bound parameter — psycopg3's
    # placeholder parser rejects bare `%` characters inside SQL literals.
    p_total = (pg.query_one(
        "SELECT COUNT(*) AS n FROM permits WHERE source LIKE %s",
        ('accela:%',),
    ) or {}).get('n') or 0
    runs_24 = (pg.query_one(
        "SELECT COUNT(*) AS n FROM scraper_runs WHERE created_at >= NOW() - INTERVAL '24 hours'"
    ) or {}).get('n') or 0
    runs_24_ok = (pg.query_one(
        "SELECT COUNT(*) AS n FROM scraper_runs "
        "WHERE created_at >= NOW() - INTERVAL '24 hours' AND status = 'success'"
    ) or {}).get('n') or 0
    success_pct = (round(100 * runs_24_ok / runs_24, 1) if runs_24 else None)
    return {
        'scrapers_total':    int(s_total),
        'scrapers_enabled':  int(s_on),
        'permits_total':     int(p_total),
        'runs_last_24h':     int(runs_24),
        'success_pct_24h':   success_pct,
    }


# ── scraper-scoped permit listing ────────────────────────────────────

def _scraper_source_tag(scraper_id: int) -> str:
    """The `permits.source` value used for permits scraped by a given
    scraper. Stable so we can list/filter later."""
    return f'accela:{int(scraper_id)}'


def list_permits_for_scraper(scraper_id: int, *,
                             date_from: str | None = None,
                             date_to: str | None = None,
                             has_email: bool | None = None,
                             has_phone: bool | None = None,
                             query: str = '',
                             page: int = 1,
                             per_page: int = 25):
    """Return (rows, total, total_pages) for the per-scraper detail
    page. Filters mirror the admin spec (date range on `applied_date`
    fallback `issued_date`, plus `has_email`/`has_phone`/keyword)."""
    _ensure_scrapers_table()
    per_page = max(1, min(int(per_page or 25), 100))
    page = max(1, int(page or 1))
    where = ['source = %s']
    params: list = [_scraper_source_tag(scraper_id)]
    if date_from:
        where.append("COALESCE(applied_date, issued_date) >= %s")
        params.append(date_from)
    if date_to:
        where.append("COALESCE(applied_date, issued_date) <= %s")
        params.append(date_to)
    if has_email is True:
        where.append("contractor_email IS NOT NULL AND length(trim(contractor_email)) > 0")
    elif has_email is False:
        where.append("(contractor_email IS NULL OR length(trim(contractor_email)) = 0)")
    if has_phone is True:
        where.append("contractor_phone IS NOT NULL AND length(trim(contractor_phone)) > 0")
    elif has_phone is False:
        where.append("(contractor_phone IS NULL OR length(trim(contractor_phone)) = 0)")
    if query:
        where.append(
            "(permit_number ILIKE %s OR address ILIKE %s OR owner_name ILIKE %s "
            "OR contractor_name ILIKE %s OR description ILIKE %s)"
        )
        like = f'%{query}%'
        params.extend([like, like, like, like, like])
    where_sql = ' AND '.join(where)
    total_row = pg.query_one(
        f"SELECT COUNT(*) AS n FROM permits WHERE {where_sql}",
        tuple(params),
    )
    total = int((total_row or {}).get('n') or 0)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    rows = pg.query(
        f"""SELECT * FROM permits
            WHERE {where_sql}
            ORDER BY COALESCE(applied_date, issued_date) DESC NULLS LAST, id DESC
            LIMIT %s OFFSET %s""",
        tuple(params + [per_page, offset]),
    ) or []
    # Return the clamped page so the view's pager links match the
    # rows actually rendered (see list_scrapers note).
    return [dict(r) for r in rows], total, total_pages, page


def list_permits_for_scraper_dt(scraper_id: int, *,
                                date_from: str | None = None,
                                date_to: str | None = None,
                                has_email: bool | None = None,
                                has_phone: bool | None = None,
                                query: str = '',
                                # Per-field filters (added 2026-05). Each
                                # is an optional substring match (ILIKE)
                                # ANDed with the rest, letting the admin
                                # narrow on multiple columns at once.
                                permit_number: str = '',
                                email: str = '',
                                phone: str = '',
                                contractor: str = '',
                                owner: str = '',
                                city_q: str = '',
                                type_q: str = '',
                                min_score: int | None = None,
                                max_score: int | None = None,
                                start: int = 0,
                                length: int = 50,
                                order_col: str = 'date',
                                order_dir: str = 'desc'):
    """DataTables server-side data source for the per-scraper detail
    page. Returns ``(rows, total_unfiltered, total_filtered)``.

    Two counts are needed because DataTables surfaces both: the total
    number of permits the scraper has scraped (``recordsTotal``, no
    filters) and how many match the active filters (``recordsFiltered``).

    ``order_col`` is whitelisted against a fixed map so the caller's
    AJAX param can never inject SQL through the ORDER BY clause.
    ``order_dir`` is normalised to ``ASC`` / ``DESC``.

    Performance: every WHERE includes ``source = %s`` which is the
    leading column of the ``permits_source_uid_uq`` UNIQUE index,
    so even on a million-row permits table the planner can index-
    scan to just this scraper's rows before applying the rest of
    the predicates and the ORDER BY.
    """
    _ensure_scrapers_table()
    length = max(1, min(int(length or 50), 200))
    start = max(0, int(start or 0))

    # Whitelist of orderable columns. The values are SQL fragments
    # — never raw user input — so a bad ``order_col`` from the
    # AJAX request just falls back to the default sort.
    order_map = {
        'permit':     'permit_number',
        'type':       'permit_type',
        'address':    'address',
        'city':       'city',
        'contractor': 'contractor_name',
        'email':      'contractor_email',
        'phone':      'contractor_phone',
        'date':       'COALESCE(applied_date, issued_date)',
        'value':      'valuation_cents',
        'score':      'ai_score',
    }
    order_sql = order_map.get((order_col or 'date').lower(),
                              'COALESCE(applied_date, issued_date)')
    order_dir_sql = 'ASC' if str(order_dir or '').lower() == 'asc' else 'DESC'

    # Total unfiltered count for this scraper (DataTables recordsTotal).
    # Cheap because of the source-prefix index above.
    src = _scraper_source_tag(scraper_id)
    total_row = pg.query_one(
        "SELECT COUNT(*) AS n FROM permits WHERE source = %s", (src,),
    )
    total_unfiltered = int((total_row or {}).get('n') or 0)

    # Filtered WHERE / params.
    where = ['source = %s']
    params: list = [src]
    if date_from:
        where.append("COALESCE(applied_date, issued_date) >= %s")
        params.append(date_from)
    if date_to:
        where.append("COALESCE(applied_date, issued_date) <= %s")
        params.append(date_to)
    if has_email is True:
        where.append("contractor_email IS NOT NULL AND length(trim(contractor_email)) > 0")
    elif has_email is False:
        where.append("(contractor_email IS NULL OR length(trim(contractor_email)) = 0)")
    if has_phone is True:
        where.append("contractor_phone IS NOT NULL AND length(trim(contractor_phone)) > 0")
    elif has_phone is False:
        where.append("(contractor_phone IS NULL OR length(trim(contractor_phone)) = 0)")
    if query:
        # Generic search — now also includes email / phone / permit_type
        # so the admin can paste a fragment of any of those into the
        # main Search box without choosing a specific column filter.
        where.append(
            "(permit_number ILIKE %s OR address ILIKE %s OR owner_name ILIKE %s "
            "OR contractor_name ILIKE %s OR description ILIKE %s "
            "OR COALESCE(contractor_email,'') ILIKE %s "
            "OR COALESCE(contractor_phone,'') ILIKE %s "
            "OR COALESCE(permit_type,'')      ILIKE %s)"
        )
        like = f'%{query}%'
        params.extend([like] * 8)

    # ── Per-field ILIKE filters ────────────────────────────────────
    # Each filter is a free-form substring; whitespace-only values are
    # treated as "no filter" so an empty input field doesn't sneak a
    # `LIKE '%%'` into the predicate.
    for col_sql, value in (
        ('permit_number',                    permit_number),
        ("COALESCE(contractor_email,'')",    email),
        ("COALESCE(contractor_phone,'')",    phone),
        ("COALESCE(contractor_name,'')",     contractor),
        ("COALESCE(owner_name,'')",          owner),
        ("COALESCE(city,'')",                city_q),
        ("COALESCE(permit_type,'')",         type_q),
    ):
        v = (value or '').strip()
        if v:
            where.append(f"{col_sql} ILIKE %s")
            params.append(f'%{v}%')

    # Score range (ai_score is a 0-100 integer in the permits table).
    if min_score is not None:
        where.append("COALESCE(ai_score, 0) >= %s")
        params.append(int(min_score))
    if max_score is not None:
        where.append("COALESCE(ai_score, 0) <= %s")
        params.append(int(max_score))

    where_sql = ' AND '.join(where)

    # Skip the second COUNT when no filters are active — the unfiltered
    # count we just computed is already the answer, saving one round-
    # trip on the most common case (admin opening the page with no
    # filters and just paging through).
    if len(where) == 1:
        total_filtered = total_unfiltered
    else:
        f_row = pg.query_one(
            f"SELECT COUNT(*) AS n FROM permits WHERE {where_sql}",
            tuple(params),
        )
        total_filtered = int((f_row or {}).get('n') or 0)

    # Project only the columns the table renders; saves bandwidth on
    # tables with hundreds of permits and avoids dragging the raw HTML
    # blob each row carries.
    rows = pg.query(
        f"""SELECT id, permit_number, permit_type, address, city, state,
                   contractor_name, contractor_email, contractor_phone,
                   applied_date, issued_date, valuation_cents,
                   ai_grade, ai_score,
                   -- Surface the jurisdiction's own permit-detail URL so
                   -- the admin Source column can offer a one-click
                   -- "open original on Accela" link per row. The
                   -- agent-extracted dict and the top-level raw envelope
                   -- both carry it; COALESCE picks whichever is
                   -- populated. Cast to text so empty JSONB strings
                   -- come back as '' (not the literal "null") and the
                   -- view code can treat them uniformly.
                   COALESCE(
                     NULLIF(raw->>'detail_url', ''),
                     NULLIF(raw->'agent_extracted'->>'detail_url', ''),
                     NULLIF(raw->>'list_url', ''),
                     ''
                   ) AS detail_url
              FROM permits
             WHERE {where_sql}
             ORDER BY {order_sql} {order_dir_sql} NULLS LAST, id DESC
             LIMIT %s OFFSET %s""",
        tuple(params + [length, start]),
    ) or []
    return [dict(r) for r in rows], total_unfiltered, total_filtered


def _all_permits_filter_where(*, query='', permit_number='', email='', phone='',
                              contractor='', owner='', city_q='', state_q='',
                              type_q='', status_q='', source_q='',
                              date_from=None, date_to=None,
                              scraped_from=None, scraped_to=None,
                              has_email=None, has_phone=None,
                              min_score=None, max_score=None,
                              min_value=None, max_value=None):
    """Build the shared WHERE clause + params for the global permits
    table (admin /admin-panel/permits/). Used by both ``list_all_permits_dt``
    (browse) and ``delete_all_permits_matching`` (hard delete) so the two
    can never drift — what you see filtered is exactly what a "delete all
    matching" removes. All predicates are ANDed; empty / None are skipped.
    Returns ``(where_list, params)``.
    """
    where: list = []
    params: list = []
    if date_from:
        where.append("COALESCE(applied_date, issued_date) >= %s")
        params.append(date_from)
    if date_to:
        where.append("COALESCE(applied_date, issued_date) <= %s")
        params.append(date_to)
    if scraped_from:
        where.append("scraped_at::date >= %s")
        params.append(scraped_from)
    if scraped_to:
        where.append("scraped_at::date <= %s")
        params.append(scraped_to)
    if has_email is True:
        where.append("contractor_email IS NOT NULL AND length(trim(contractor_email)) > 0")
    elif has_email is False:
        where.append("(contractor_email IS NULL OR length(trim(contractor_email)) = 0)")
    if has_phone is True:
        where.append("contractor_phone IS NOT NULL AND length(trim(contractor_phone)) > 0")
    elif has_phone is False:
        where.append("(contractor_phone IS NULL OR length(trim(contractor_phone)) = 0)")
    if query:
        where.append(
            "(permit_number ILIKE %s OR address ILIKE %s OR owner_name ILIKE %s "
            "OR contractor_name ILIKE %s OR COALESCE(description,'') ILIKE %s "
            "OR COALESCE(contractor_email,'') ILIKE %s "
            "OR COALESCE(contractor_phone,'') ILIKE %s "
            "OR COALESCE(permit_type,'')      ILIKE %s "
            "OR COALESCE(city,'')             ILIKE %s "
            "OR COALESCE(source,'')           ILIKE %s)"
        )
        like = f'%{query}%'
        params.extend([like] * 10)
    for col_sql, value in (
        ('permit_number',                    permit_number),
        ("COALESCE(contractor_email,'')",    email),
        ("COALESCE(contractor_phone,'')",    phone),
        ("COALESCE(contractor_name,'')",     contractor),
        ("COALESCE(owner_name,'')",          owner),
        ("COALESCE(city,'')",                city_q),
        ("COALESCE(state,'')",               state_q),
        ("COALESCE(permit_type,'')",         type_q),
        ("COALESCE(status,'')",              status_q),
        ("COALESCE(source,'')",              source_q),
    ):
        v = (value or '').strip()
        if v:
            where.append(f"{col_sql} ILIKE %s")
            params.append(f'%{v}%')
    if min_score is not None:
        where.append("COALESCE(ai_score, 0) >= %s"); params.append(int(min_score))
    if max_score is not None:
        where.append("COALESCE(ai_score, 0) <= %s"); params.append(int(max_score))
    if min_value is not None:
        where.append("COALESCE(valuation_cents, 0) >= %s"); params.append(int(min_value))
    if max_value is not None:
        where.append("COALESCE(valuation_cents, 0) <= %s"); params.append(int(max_value))
    return where, params


def delete_all_permits_matching(**filters) -> int:
    """Hard-delete EVERY permit row matching the same filter set the admin
    permits DataTable shows (see ``_all_permits_filter_where``). Admin-only,
    no source gate — this is the "delete all matching" path. Returns the
    rowcount actually deleted.

    Guard: refuses to run with an EMPTY filter set so a stray call can't
    silently wipe the whole table — use the dedicated "Wipe All Permits"
    utility for that.
    """
    where, params = _all_permits_filter_where(**filters)
    if not where:
        raise ValueError('refusing to delete with no filters — use Wipe All Permits')
    sql = f"DELETE FROM permits WHERE {' AND '.join(where)}"
    n = pg.execute(sql, tuple(params))
    if n:
        try:
            _invalidate_permits_cache()
        except Exception:
            log.exception('cache invalidation after delete_all_permits_matching failed')
    return int(n or 0)


def delete_all_permits_for_scraper(scraper_id: int) -> int:
    """Hard-delete every permit belonging to one scraper (``source`` tag).

    The per-scraper detail page's "select all" only ticks the rows in the
    CURRENT DataTables page (server-side paging renders one page at a time),
    so a checkbox delete can never clear a multi-page scraper. This deletes
    the whole set in one statement. Returns the rowcount deleted.
    """
    src = _scraper_source_tag(int(scraper_id))
    n = pg.execute("DELETE FROM permits WHERE source = %s", (src,))
    if n:
        try:
            _invalidate_permits_cache()
        except Exception:
            log.exception('cache invalidation after delete_all_permits_for_scraper failed')
    return int(n or 0)


def count_junk_permits() -> int:
    """Row count of the junk_permits blacklist (DB Utilities display)."""
    _ensure_junk_permits_table()
    row = pg.query_one("SELECT COUNT(*) AS n FROM junk_permits")
    return int((row or {}).get('n') or 0)


def wipe_junk_permits() -> int:
    """Empty the junk_permits blacklist entirely. Returns rows removed.

    junk_permits is a "known-junk" cache that stops scrapers re-fetching
    rows previously judged non-actionable. Wiping it lets the next run
    re-evaluate everything from scratch (at the cost of re-paying for
    those detail fetches).
    """
    _ensure_junk_permits_table()
    row = pg.query_one("SELECT COUNT(*) AS n FROM junk_permits")
    n = int((row or {}).get('n') or 0)
    pg.execute("DELETE FROM junk_permits")
    return n


def list_all_permits_dt(*, query: str = '',
                        permit_number: str = '',
                        email: str = '',
                        phone: str = '',
                        contractor: str = '',
                        owner: str = '',
                        city_q: str = '',
                        state_q: str = '',
                        type_q: str = '',
                        status_q: str = '',
                        source_q: str = '',
                        date_from: str | None = None,
                        date_to: str | None = None,
                        scraped_from: str | None = None,
                        scraped_to: str | None = None,
                        has_email: bool | None = None,
                        has_phone: bool | None = None,
                        min_score: int | None = None,
                        max_score: int | None = None,
                        min_value: int | None = None,
                        max_value: int | None = None,
                        start: int = 0,
                        length: int = 50,
                        order_col: str = 'date',
                        order_dir: str = 'desc'):
    """DataTables backend for the admin /admin-panel/permits/ page —
    the GLOBAL permits table (no scraper / no user-subscription gate).

    Mirrors ``list_permits_for_scraper_dt`` but drops the ``source = %s``
    prefix filter so the admin can browse / filter every row in
    ``permits`` from a single place. Returns
    ``(rows, total_unfiltered, total_filtered)``.

    All filters are ANDed; empty / None values are ignored. ``order_col``
    is whitelisted (same map as the per-scraper variant + 'source')
    so the AJAX param can never inject SQL through ORDER BY.
    """
    length = max(1, min(int(length or 50), 1000))
    start  = max(0, int(start or 0))

    order_map = {
        'permit':     'permit_number',
        'type':       'permit_type',
        'address':    'address',
        'city':       'city',
        'state':      'state',
        'contractor': 'contractor_name',
        'owner':      'owner_name',
        'email':      'contractor_email',
        'phone':      'contractor_phone',
        'status':     'status',
        'source':     'source',
        'date':       'COALESCE(applied_date, issued_date)',
        'scraped':    'scraped_at',
        'value':      'valuation_cents',
        'score':      'ai_score',
    }
    order_sql = order_map.get((order_col or 'date').lower(),
                              'COALESCE(applied_date, issued_date)')
    order_dir_sql = 'ASC' if str(order_dir or '').lower() == 'asc' else 'DESC'

    total_row = pg.query_one("SELECT COUNT(*) AS n FROM permits")
    total_unfiltered = int((total_row or {}).get('n') or 0)

    where, params = _all_permits_filter_where(
        query=query, permit_number=permit_number, email=email, phone=phone,
        contractor=contractor, owner=owner, city_q=city_q, state_q=state_q,
        type_q=type_q, status_q=status_q, source_q=source_q,
        date_from=date_from, date_to=date_to,
        scraped_from=scraped_from, scraped_to=scraped_to,
        has_email=has_email, has_phone=has_phone,
        min_score=min_score, max_score=max_score,
        min_value=min_value, max_value=max_value,
    )

    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    if not where:
        total_filtered = total_unfiltered
    else:
        f_row = pg.query_one(
            f"SELECT COUNT(*) AS n FROM permits{where_sql}",
            tuple(params),
        )
        total_filtered = int((f_row or {}).get('n') or 0)

    rows = pg.query(
        f"""SELECT id, source, source_permit_id, permit_number, permit_type,
                   address, city, state, jurisdiction, status,
                   owner_name, contractor_name, contractor_email,
                   contractor_phone, applied_date, issued_date,
                   valuation_cents, ai_grade, ai_score, scraped_at,
                   COALESCE(
                     NULLIF(raw->>'detail_url', ''),
                     NULLIF(raw->'agent_extracted'->>'detail_url', ''),
                     NULLIF(raw->>'list_url', ''),
                     ''
                   ) AS detail_url
              FROM permits{where_sql}
             ORDER BY {order_sql} {order_dir_sql} NULLS LAST, id DESC
             LIMIT %s OFFSET %s""",
        tuple(params + [length, start]),
    ) or []
    return [dict(r) for r in rows], total_unfiltered, total_filtered


def count_permits_for_scraper(scraper_id: int) -> int:
    _ensure_scrapers_table()
    row = pg.query_one(
        "SELECT COUNT(*) AS n FROM permits WHERE source = %s",
        (_scraper_source_tag(scraper_id),),
    )
    return int((row or {}).get('n') or 0)


def refresh_scraper_total_permits(scraper_id: int) -> int:
    """Recount and persist `total_permits` for a scraper. Called at the
    end of every run so the list view shows accurate counts without an
    expensive join on each page render."""
    n = count_permits_for_scraper(scraper_id)
    update_scraper(scraper_id, total_permits=n)
    return n


# ── Bulk delete from the user's permits dashboard ────────────────────

def bulk_delete_permits_by_ids(ids: list, *,
                               allowed_source: str | None = None) -> int:
    """Delete permit rows by primary id, gated by `permits.source` so a
    crafted POST cannot reach permits owned by a different scraper.

    Powers the "Select all → Delete selected" toolbar on the admin
    scraper-detail page. ``allowed_source`` is the scraper's source tag
    (``_scraper_source_tag(sid)``); rows whose ``source`` doesn't match
    are silently skipped by the WHERE clause — `n` returned is the
    rows actually deleted, not the count submitted.
    """
    safe_ids: list[int] = []
    for x in (ids or []):
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v > 0:
            safe_ids.append(v)
    if not safe_ids:
        return 0

    where = ["id = ANY(%s)"]
    params: list = [safe_ids]
    if allowed_source is not None:
        where.append("source = %s")
        params.append(allowed_source)

    sql = f"DELETE FROM permits WHERE {' AND '.join(where)}"
    n = pg.execute(sql, tuple(params))
    if n:
        try:
            _invalidate_permits_cache()
        except Exception:
            log.exception('cache invalidation after bulk_delete_permits_by_ids failed')
    return int(n or 0)


# ────────────────────────────────────────────────────────────────────
# Marketing — testimonials, recovery email queue, social-proof counters
# ────────────────────────────────────────────────────────────────────

def _ensure_testimonials_table() -> None:
    pg.execute("""
        CREATE TABLE IF NOT EXISTS testimonials (
            id            SERIAL PRIMARY KEY,
            quote         TEXT      NOT NULL,
            author_name   TEXT      NOT NULL,
            author_title  TEXT      DEFAULT '',
            company       TEXT      DEFAULT '',
            is_published  BOOLEAN   DEFAULT FALSE,
            sort_order    INTEGER   DEFAULT 0,
            created_at    TIMESTAMP DEFAULT NOW(),
            updated_at    TIMESTAMP DEFAULT NOW()
        )
    """)


def _initials_for(name: str) -> str:
    parts = (name or '').strip().split()
    if not parts:
        return '??'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def list_testimonials(published_only: bool = False, limit: int = 50) -> list:
    _ensure_testimonials_table()
    where = "WHERE is_published = TRUE" if published_only else ""
    rows = pg.query(
        f"""SELECT id, quote, author_name, author_title, company,
                   is_published, sort_order, created_at
              FROM testimonials {where}
             ORDER BY sort_order ASC, id ASC
             LIMIT %s""", (int(limit),))
    out = []
    for r in rows:
        r['initials'] = _initials_for(r['author_name'])
        out.append(r)
    return out


def get_testimonial(tid: int):
    _ensure_testimonials_table()
    row = pg.query_one(
        """SELECT id, quote, author_name, author_title, company,
                  is_published, sort_order
             FROM testimonials WHERE id = %s""", (int(tid),))
    return row


def upsert_testimonial(*, tid=None, quote, author_name, author_title='',
                       company='', is_published=False, sort_order=0) -> int:
    _ensure_testimonials_table()
    if tid:
        pg.execute(
            """UPDATE testimonials
                  SET quote=%s, author_name=%s, author_title=%s,
                      company=%s, is_published=%s, sort_order=%s,
                      updated_at=NOW()
                WHERE id=%s""",
            (quote, author_name, author_title, company,
             bool(is_published), int(sort_order), int(tid)))
        return int(tid)
    row = pg.query_one(
        """INSERT INTO testimonials
               (quote, author_name, author_title, company,
                is_published, sort_order)
             VALUES (%s, %s, %s, %s, %s, %s)
          RETURNING id""",
        (quote, author_name, author_title, company,
         bool(is_published), int(sort_order)))
    return int(row['id']) if row else 0


def delete_testimonial(tid: int) -> int:
    _ensure_testimonials_table()
    return int(pg.execute("DELETE FROM testimonials WHERE id = %s",
                          (int(tid),)) or 0)


def permits_ingested_stats() -> dict:
    """Bucketed permit-ingestion counts for the Inference Stats KPI cards.

    Each row in ``permits`` corresponds to one real-world permit the
    scraper parsed (cross-source deduped via ``dedup_hash``) — this
    is the honest "HTML pages processed" metric. The previous
    implementation counted ``claude_calls`` rows, but the Accela
    scraper agent makes several LLM calls per permit (list-page
    browse + tool-use turns + the final extraction), so the LLM-call
    count over-reported real throughput by ~5x. See PR following
    #441 for the swap.

    Returns the same shape as the ``totals`` block of
    ``inference_stats()`` so the template can stay unchanged:
    today / 7d / 30d / mtd / total, plus a 30-day per-day series.
    """
    totals = pg.query_one(
        """SELECT
              COUNT(*) FILTER (WHERE created_at >= date_trunc('day',   NOW())) AS today,
              COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS d7,
              COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS d30,
              COUNT(*) FILTER (WHERE created_at >= date_trunc('month', NOW())) AS mtd,
              COUNT(*)                                                         AS total
             FROM permits"""
    ) or {}
    per_day = pg.query(
        """SELECT d::date AS day,
                  COALESCE(c.n, 0) AS calls
             FROM generate_series((NOW() - INTERVAL '29 days')::date,
                                  NOW()::date, '1 day') d
             LEFT JOIN (
                SELECT date_trunc('day', created_at)::date AS day,
                       COUNT(*) AS n
                  FROM permits
                 WHERE created_at >= (NOW() - INTERVAL '29 days')::date
                 GROUP BY 1
             ) c ON c.day = d::date
            ORDER BY d ASC"""
    ) or []
    return {
        'today': int(totals.get('today') or 0),
        'd7':    int(totals.get('d7')    or 0),
        'd30':   int(totals.get('d30')   or 0),
        'mtd':   int(totals.get('mtd')   or 0),
        'total': int(totals.get('total') or 0),
        'per_day': [{'day': r['day'], 'calls': int(r['calls'] or 0)} for r in per_day],
    }


def permits_count_last_24h() -> int:
    """Total permits ingested in the last 24 hours.

    Used on the homepage hero & trade landing pages as a real-time
    social-proof counter ("17,243 permits delivered in the last 24h").
    Cheap query — the permits table has an index on created_at.
    """
    try:
        row = pg.query_one(
            """SELECT COUNT(*) AS n FROM permits
                WHERE created_at > NOW() - INTERVAL '24 hours'""")
        return int(row['n']) if row else 0
    except Exception:
        return 0


# ── Recovery email queue ────────────────────────────────────────────

def _ensure_recovery_table() -> None:
    pg.execute("""
        CREATE TABLE IF NOT EXISTS recovery_queue (
            id            SERIAL PRIMARY KEY,
            user_id       INTEGER   NOT NULL,
            trigger       TEXT      NOT NULL,
            step          INTEGER   NOT NULL,
            fire_at       TIMESTAMP NOT NULL,
            sent_at       TIMESTAMP,
            status        TEXT      DEFAULT 'pending',
            template      JSONB     DEFAULT '{}',
            trial_link    TEXT      DEFAULT '',
            note          TEXT      DEFAULT '',
            created_at    TIMESTAMP DEFAULT NOW()
        )
    """)
    # Older deployments may have the table without trial_link — best-effort add.
    try:
        pg.execute("ALTER TABLE recovery_queue ADD COLUMN IF NOT EXISTS trial_link TEXT DEFAULT ''")
    except Exception:
        pass
    pg.execute(
        "CREATE INDEX IF NOT EXISTS idx_recovery_due "
        "ON recovery_queue (status, fire_at)")


# ── Email campaigns (bulk Resend, CSV-driven) ──────────────────────

def _ensure_campaigns_tables() -> None:
    """Create campaigns + recipients + suppression tables on first use.
    Idempotent; safe to call repeatedly.
    """
    pg.execute("""
        CREATE TABLE IF NOT EXISTS email_campaigns (
            id                  SERIAL PRIMARY KEY,
            name                TEXT      NOT NULL,
            subject             TEXT      NOT NULL,
            body_html           TEXT      NOT NULL DEFAULT '',
            daily_cap           INTEGER   NOT NULL DEFAULT 200,
            status              TEXT      NOT NULL DEFAULT 'draft',
            skip_existing_users BOOLEAN   DEFAULT TRUE,
            created_by          INTEGER,
            created_at          TIMESTAMP DEFAULT NOW(),
            started_at          TIMESTAMP,
            last_send_at        TIMESTAMP,
            total               INTEGER   DEFAULT 0,
            sent_count          INTEGER   DEFAULT 0,
            failed_count        INTEGER   DEFAULT 0,
            bounced_count       INTEGER   DEFAULT 0,
            complained_count    INTEGER   DEFAULT 0,
            unsubscribed_count  INTEGER   DEFAULT 0,
            skipped_count       INTEGER   DEFAULT 0,
            opened_count        INTEGER   DEFAULT 0
        )
    """)
    pg.execute("""
        CREATE TABLE IF NOT EXISTS email_campaign_recipients (
            id           SERIAL PRIMARY KEY,
            campaign_id  INTEGER NOT NULL REFERENCES email_campaigns(id) ON DELETE CASCADE,
            email        TEXT NOT NULL,
            name         TEXT DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'pending',
            sent_at      TIMESTAMP,
            delivered_at TIMESTAMP,
            opened_at    TIMESTAMP,
            error        TEXT DEFAULT '',
            message_id   TEXT DEFAULT '',
            created_at   TIMESTAMP DEFAULT NOW()
        )
    """)
    pg.execute("CREATE INDEX IF NOT EXISTS idx_camp_recip_due ON email_campaign_recipients (campaign_id, status)")
    pg.execute("CREATE INDEX IF NOT EXISTS idx_camp_recip_msgid ON email_campaign_recipients (message_id)")
    pg.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_camp_recip_email ON email_campaign_recipients (campaign_id, lower(email))")
    # Global (cross-campaign) lookups by address — powers the contractor-email
    # pool's per-row "times_emailed" count.
    pg.execute("CREATE INDEX IF NOT EXISTS idx_camp_recip_email_lower ON email_campaign_recipients (lower(email), status)")
    pg.execute("""
        CREATE TABLE IF NOT EXISTS email_suppressions (
            email      TEXT PRIMARY KEY,
            reason     TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            note       TEXT DEFAULT ''
        )
    """)
    # Daily auto-pull: when ``auto_pull_count`` > 0 the campaigns_tick cron
    # tops the campaign up with that many fresh, highest-scored contractor
    # emails once per day. ``last_auto_pull_at`` enforces the daily cadence.
    pg.execute(
        "ALTER TABLE email_campaigns "
        "ADD COLUMN IF NOT EXISTS auto_pull_count INTEGER NOT NULL DEFAULT 0"
    )
    pg.execute(
        "ALTER TABLE email_campaigns "
        "ADD COLUMN IF NOT EXISTS last_auto_pull_at TIMESTAMP"
    )


def campaign_create(name: str, subject: str, body_html: str,
                    daily_cap: int = 200,
                    skip_existing_users: bool = True,
                    created_by: int = 0) -> int:
    _ensure_campaigns_tables()
    row = pg.query_one(
        """INSERT INTO email_campaigns
             (name, subject, body_html, daily_cap, skip_existing_users, created_by)
           VALUES (%s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (name, subject, body_html, max(1, int(daily_cap)),
         bool(skip_existing_users), int(created_by) or None),
    )
    return int(row['id']) if row else 0


def campaign_update(cid: int, **fields) -> None:
    _ensure_campaigns_tables()
    allowed = {'name', 'subject', 'body_html', 'daily_cap',
               'skip_existing_users', 'status', 'started_at',
               'auto_pull_count', 'last_auto_pull_at'}
    sets = [f"{k} = %s" for k in fields if k in allowed]
    if not sets:
        return
    vals = [fields[k] for k in fields if k in allowed] + [cid]
    pg.execute(f"UPDATE email_campaigns SET {', '.join(sets)} WHERE id = %s", tuple(vals))


def campaign_get(cid: int) -> dict | None:
    _ensure_campaigns_tables()
    return pg.query_one("SELECT * FROM email_campaigns WHERE id = %s", (cid,))


def campaign_delete(cid: int) -> None:
    _ensure_campaigns_tables()
    pg.execute("DELETE FROM email_campaigns WHERE id = %s", (cid,))


def campaigns_list(limit: int = 200) -> list:
    _ensure_campaigns_tables()
    return pg.query(
        "SELECT * FROM email_campaigns ORDER BY id DESC LIMIT %s", (limit,)
    )


def campaign_recipients_bulk_insert(cid: int, rows: list) -> dict:
    """Insert recipients in batches. Skips duplicates (per-campaign unique
    on lower(email)) silently. Returns counts.
    """
    _ensure_campaigns_tables()
    inserted = duplicates = invalid = 0
    seen = set()
    for r in rows:
        em = (r.get('email') or '').strip().lower()
        if not em or '@' not in em or em in seen:
            if not em or '@' not in em:
                invalid += 1
            else:
                duplicates += 1
            continue
        seen.add(em)
        nm = (r.get('name') or '').strip()[:200]
        try:
            res = pg.query_one(
                """INSERT INTO email_campaign_recipients (campaign_id, email, name)
                   VALUES (%s, %s, %s)
                   ON CONFLICT DO NOTHING
                   RETURNING id""",
                (cid, em, nm),
            )
            if res:
                inserted += 1
            else:
                duplicates += 1
        except Exception:
            invalid += 1
    pg.execute(
        """UPDATE email_campaigns
              SET total = (SELECT COUNT(*) FROM email_campaign_recipients WHERE campaign_id = %s)
            WHERE id = %s""",
        (cid, cid),
    )
    return {'inserted': inserted, 'duplicates': duplicates, 'invalid': invalid}


def campaign_recipients_page(cid: int, offset: int = 0, limit: int = 50,
                             search: str = '', status: str = '') -> tuple[list, int, int]:
    """Server-side DataTables page. Returns (rows, filtered_total, total)."""
    _ensure_campaigns_tables()
    base_where = "campaign_id = %s"
    base_args  = [cid]
    where = base_where
    args  = list(base_args)
    if status:
        where += " AND status = %s"; args.append(status)
    if search:
        where += " AND (lower(email) LIKE %s OR lower(name) LIKE %s)"
        like = '%' + search.lower() + '%'
        args.extend([like, like])
    total_row = pg.query_one(f"SELECT COUNT(*) AS n FROM email_campaign_recipients WHERE {base_where}", tuple(base_args))
    filtered_row = pg.query_one(f"SELECT COUNT(*) AS n FROM email_campaign_recipients WHERE {where}", tuple(args))
    args2 = list(args) + [int(limit), int(offset)]
    rows = pg.query(
        f"""SELECT id, email, name, status, sent_at, delivered_at, opened_at,
                   error, message_id, created_at
              FROM email_campaign_recipients
             WHERE {where}
             ORDER BY id ASC
             LIMIT %s OFFSET %s""",
        tuple(args2),
    )
    return rows, int(filtered_row['n'] or 0), int(total_row['n'] or 0)


def campaign_recipients_claim(cid: int, limit: int) -> list:
    """Atomically claim up to ``limit`` pending rows by flipping them to
    'sending'. Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` semantics
    (emulated via a single UPDATE ... WHERE id IN (SELECT ... FOR UPDATE
    SKIP LOCKED) ... RETURNING) so two concurrent cron ticks never grab
    the same row and the per-campaign daily cap is enforced even under
    parallel runs.
    """
    _ensure_campaigns_tables()
    return pg.query(
        """UPDATE email_campaign_recipients r
              SET status = 'sending'
            WHERE r.id IN (
                SELECT id FROM email_campaign_recipients
                 WHERE campaign_id = %s AND status = 'pending'
                 ORDER BY id ASC
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
            )
          RETURNING r.id, r.email, r.name""",
        (cid, int(limit)),
    )


def campaign_recipient_mark(rid: int, status: str, error: str = '',
                            message_id: str = '') -> None:
    _ensure_campaigns_tables()
    pg.execute(
        """UPDATE email_campaign_recipients
              SET status = %s,
                  sent_at = CASE WHEN %s IN ('sent','failed','skipped') THEN NOW() ELSE sent_at END,
                  error = %s,
                  message_id = COALESCE(NULLIF(%s,''), message_id)
            WHERE id = %s""",
        (status, status, (error or '')[:500], message_id or '', rid),
    )


def campaign_recipient_mark_by_message(message_id: str, status: str,
                                       event_field: str = '') -> dict | None:
    """Update a recipient row by Resend message_id (webhook callback).
    event_field: 'delivered_at' | 'opened_at' | '' (status only).
    Returns the updated row (with campaign_id) or None if not found.
    """
    _ensure_campaigns_tables()
    if not message_id:
        return None
    row = pg.query_one(
        "SELECT id, campaign_id, status FROM email_campaign_recipients WHERE message_id = %s",
        (message_id,),
    )
    if not row:
        return None
    extra_sql = ''
    if event_field in ('delivered_at', 'opened_at'):
        extra_sql = f", {event_field} = NOW()"
    pg.execute(
        f"UPDATE email_campaign_recipients SET status = %s{extra_sql} WHERE id = %s",
        (status, row['id']),
    )
    return row


def campaign_today_sent_count(cid: int) -> int:
    _ensure_campaigns_tables()
    row = pg.query_one(
        """SELECT COUNT(*) AS n
             FROM email_campaign_recipients
            WHERE campaign_id = %s
              AND status IN ('sent','bounced','complained','delivered')
              AND sent_at >= NOW() - INTERVAL '24 hours'""",
        (cid,),
    )
    return int(row['n'] or 0)


def campaign_recalc_stats(cid: int) -> None:
    _ensure_campaigns_tables()
    pg.execute(
        """UPDATE email_campaigns c
              SET sent_count         = COALESCE(s.sent,0),
                  failed_count       = COALESCE(s.failed,0),
                  bounced_count      = COALESCE(s.bounced,0),
                  complained_count   = COALESCE(s.complained,0),
                  unsubscribed_count = COALESCE(s.unsubscribed,0),
                  skipped_count      = COALESCE(s.skipped,0),
                  opened_count       = COALESCE(s.opened,0)
             FROM (
                SELECT
                  SUM(CASE WHEN status IN ('sent','delivered','opened') THEN 1 ELSE 0 END) AS sent,
                  SUM(CASE WHEN status = 'failed'        THEN 1 ELSE 0 END) AS failed,
                  SUM(CASE WHEN status = 'bounced'       THEN 1 ELSE 0 END) AS bounced,
                  SUM(CASE WHEN status = 'complained'    THEN 1 ELSE 0 END) AS complained,
                  SUM(CASE WHEN status = 'unsubscribed'  THEN 1 ELSE 0 END) AS unsubscribed,
                  SUM(CASE WHEN status = 'skipped'       THEN 1 ELSE 0 END) AS skipped,
                  SUM(CASE WHEN opened_at IS NOT NULL    THEN 1 ELSE 0 END) AS opened
                FROM email_campaign_recipients WHERE campaign_id = %s
             ) s
            WHERE c.id = %s""",
        (cid, cid),
    )


def suppression_add(email: str, reason: str, note: str = '') -> None:
    _ensure_campaigns_tables()
    em = (email or '').strip().lower()
    if not em:
        return
    pg.execute(
        """INSERT INTO email_suppressions (email, reason, note)
           VALUES (%s, %s, %s)
           ON CONFLICT (email) DO UPDATE
             SET reason = EXCLUDED.reason, note = EXCLUDED.note""",
        (em, reason[:40], (note or '')[:300]),
    )


def suppression_check(email: str) -> str:
    _ensure_campaigns_tables()
    em = (email or '').strip().lower()
    if not em:
        return ''
    row = pg.query_one("SELECT reason FROM email_suppressions WHERE email = %s", (em,))
    return (row or {}).get('reason', '') or ''


def suppression_list(limit: int = 500) -> list:
    _ensure_campaigns_tables()
    return pg.query(
        "SELECT * FROM email_suppressions ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )


def suppression_remove(email: str) -> bool:
    """Drop an address from the global suppression list so future campaigns
    can reach it again. Returns True if a row was removed."""
    _ensure_campaigns_tables()
    em = (email or '').strip().lower()
    if not em:
        return False
    n = pg.execute("DELETE FROM email_suppressions WHERE email = %s", (em,))
    return bool(n)


def email_exists_as_user(email: str) -> bool:
    em = (email or '').strip().lower()
    if not em:
        return False
    row = pg.query_one("SELECT id FROM users WHERE lower(email) = %s LIMIT 1", (em,))
    return bool(row)


# ── Contractor email pool (for campaign sourcing) ────────────────────
#
# All campaigns are seeded from the contractor emails we scrape into the
# ``permits`` table. This pool grows daily as scrapers run. The admin
# "Contractor Email Pool" page shows one row per DISTINCT email (the best
# permit per email wins for the displayed name/phone/city/score), and the
# "pull top N into a campaign" action grabs the highest-AI-score emails
# that haven't been contacted in that campaign yet.

# Reusable base: one row per distinct (lowercased) contractor email, with
# the representative fields taken from that email's highest-scoring permit.
# ``position('@' in ...) > 1`` cheaply drops blanks / garbage. The
# functional index ``permits_contractor_email_lower_idx`` backs the GROUP BY.
# Shared row filter — must stay in sync between the plain distinct-count and
# the full aggregate so totals match what the table renders.
_CONTRACTOR_EMAIL_WHERE = """
    contractor_email IS NOT NULL
      AND btrim(contractor_email) <> ''
      AND position('@' in contractor_email) > 1
"""

_CONTRACTOR_EMAIL_AGG = f"""
    SELECT
        lower(contractor_email)                                       AS email_key,
        max(contractor_email)                                         AS email,
        (array_agg(contractor_name  ORDER BY ai_score DESC NULLS LAST))[1] AS name,
        (array_agg(contractor_phone ORDER BY ai_score DESC NULLS LAST))[1] AS phone,
        (array_agg(city             ORDER BY ai_score DESC NULLS LAST))[1] AS city,
        (array_agg(state            ORDER BY ai_score DESC NULLS LAST))[1] AS state,
        count(*)                                                      AS permit_count,
        max(ai_score)                                                 AS best_score,
        max(created_at)                                               AS last_seen
    FROM permits
    WHERE {_CONTRACTOR_EMAIL_WHERE}
    GROUP BY lower(contractor_email)
"""


def contractor_emails_dt(offset: int = 0, limit: int = 50, search: str = '',
                         order_col: int = 5, order_dir: str = 'desc'
                         ) -> tuple[list, int, int]:
    """Server-side DataTables page over the distinct contractor-email pool.

    Returns ``(rows, filtered_total, total)``. Each row carries the
    representative name/phone/city/state plus permit_count, best_score,
    last_seen, a ``suppressed`` flag and ``times_emailed`` (how often this
    address has already been sent across all campaigns).
    """
    _ensure_campaigns_tables()
    # Column index -> ORDER BY expression. Mirrors the table header order
    # in templates/core/admin_contractor_emails.html.
    order_map = {
        0: 'email',
        1: 'name',
        2: 'phone',
        3: 'state',
        4: 'permit_count',
        5: 'best_score',
        6: 'last_seen',
    }
    order_expr = order_map.get(int(order_col), 'best_score')
    direction = 'ASC' if str(order_dir).lower() == 'asc' else 'DESC'
    # Stable tiebreaker so pagination is deterministic.
    order_sql = f"{order_expr} {direction} NULLS LAST, email_key ASC"

    where = ''
    args: list = []
    if search:
        where = ("WHERE (a.email_key LIKE %s OR lower(coalesce(a.name,'')) LIKE %s "
                 "OR lower(coalesce(a.phone,'')) LIKE %s "
                 "OR lower(coalesce(a.city,'')) LIKE %s "
                 "OR lower(coalesce(a.state,'')) LIKE %s)")
        like = '%' + search.lower() + '%'
        args = [like, like, like, like, like]

    # Total = plain distinct count over permits (cheap; no array_agg build).
    total_row = pg.query_one(
        f"SELECT count(DISTINCT lower(contractor_email)) AS n "
        f"FROM permits WHERE {_CONTRACTOR_EMAIL_WHERE}"
    )
    total = int((total_row or {}).get('n') or 0)
    # Only pay for the full aggregate count when a search is actually applied;
    # with no filter the filtered count equals the total.
    if search:
        filtered_row = pg.query_one(
            f"SELECT count(*) AS n FROM ({_CONTRACTOR_EMAIL_AGG}) a {where}",
            tuple(args),
        )
        filtered = int((filtered_row or {}).get('n') or 0)
    else:
        filtered = total
    rows = pg.query(
        f"""
        SELECT a.*,
               EXISTS(
                   SELECT 1 FROM email_suppressions s
                    WHERE lower(s.email) = a.email_key
               ) AS suppressed,
               (
                   SELECT count(*) FROM email_campaign_recipients r
                    WHERE lower(r.email) = a.email_key
                      AND r.status IN ('sent','delivered','opened','bounced')
               ) AS times_emailed
          FROM ({_CONTRACTOR_EMAIL_AGG}) a
          {where}
         ORDER BY {order_sql}
         LIMIT %s OFFSET %s
        """,
        tuple(args) + (int(limit), int(offset)),
    )
    return rows, filtered, total


def contractor_emails_pool_count() -> int:
    """Total distinct contractor emails currently in the pool."""
    _ensure_campaigns_tables()
    row = pg.query_one(
        f"SELECT count(DISTINCT lower(contractor_email)) AS n "
        f"FROM permits WHERE {_CONTRACTOR_EMAIL_WHERE}"
    )
    return int((row or {}).get('n') or 0)


def contractor_emails_top_for_campaign(cid: int, limit: int) -> list:
    """Top ``limit`` contractor emails (highest AI score first) that are
    NOT suppressed and NOT already recipients of campaign ``cid``.

    Returns ``[{'email': ..., 'name': ...}, ...]`` ready for
    ``campaign_recipients_bulk_insert``. Running this daily naturally
    picks up newly-scraped emails the campaign hasn't seen yet.
    """
    _ensure_campaigns_tables()
    rows = pg.query(
        f"""
        SELECT a.email, a.name
          FROM ({_CONTRACTOR_EMAIL_AGG}) a
         WHERE NOT EXISTS(
                   SELECT 1 FROM email_suppressions s
                    WHERE lower(s.email) = a.email_key
               )
           AND NOT EXISTS(
                   SELECT 1 FROM email_campaign_recipients r
                    WHERE r.campaign_id = %s
                      AND lower(r.email) = a.email_key
               )
         ORDER BY a.best_score DESC NULLS LAST, a.last_seen DESC, a.email_key ASC
         LIMIT %s
        """,
        (int(cid), int(limit)),
    )
    return [{'email': r['email'], 'name': r.get('name') or ''} for r in rows]


def campaigns_due_for_auto_pull(min_hours: int = 20) -> list:
    """Campaigns flagged for daily auto-pull (``auto_pull_count`` > 0) that
    are actively sending and haven't been topped up in the last
    ``min_hours`` hours. The cadence gate makes the cron idempotent — even
    if campaigns_tick runs every 15 min, a given campaign is pulled at most
    once per day.
    """
    _ensure_campaigns_tables()
    return pg.query(
        """SELECT * FROM email_campaigns
            WHERE auto_pull_count > 0
              AND status = 'sending'
              AND (last_auto_pull_at IS NULL
                   OR last_auto_pull_at < NOW() - make_interval(hours => %s))
            ORDER BY id ASC""",
        (int(min_hours),),
    )


def campaign_mark_auto_pulled(cid: int) -> None:
    """Stamp the last successful auto-pull so the daily cadence gate holds."""
    _ensure_campaigns_tables()
    pg.execute(
        "UPDATE email_campaigns SET last_auto_pull_at = NOW() WHERE id = %s",
        (int(cid),),
    )


def recovery_enqueue(user_id: int, trigger: str, steps: list,
                     trial_link: str = '') -> int:
    """Insert one row per step. ``steps`` is a list of dicts shaped like
    {'step': 1, 'delay_hours': 1, 'subject': '...', 'body': '...'}.
    Returns the number of rows actually queued (enabled & non-empty)."""
    _ensure_recovery_table()
    # Cancel any previously-pending rows for the same (user, trigger) —
    # if the user re-triggers (e.g. starts another signup) we want a
    # fresh sequence, not duplicates.
    pg.execute(
        """UPDATE recovery_queue SET status='skipped', note='superseded'
            WHERE user_id=%s AND trigger=%s AND status='pending'""",
        (int(user_id), trigger))
    n = 0
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    for s in steps or []:
        if not s.get('enabled', True) or not s.get('subject') or not s.get('body'):
            continue
        delay_h = max(0, int(s.get('delay_hours') or 0))
        fire_at = now + timedelta(hours=delay_h)
        tpl = {
            'subject':     s['subject'],
            'body':        s['body'],
            'delay_hours': delay_h,
        }
        pg.execute(
            """INSERT INTO recovery_queue
                   (user_id, trigger, step, fire_at, template, trial_link)
                 VALUES (%s, %s, %s, %s, %s, %s)""",
            (int(user_id), trigger, int(s.get('step') or 0),
             fire_at, Json(tpl), trial_link or ''))
        n += 1
    return n


def recovery_due_rows(limit: int = 200) -> list:
    _ensure_recovery_table()
    rows = pg.query(
        """SELECT id, user_id, trigger, step, fire_at, template, trial_link
             FROM recovery_queue
            WHERE status='pending' AND fire_at <= NOW()
            ORDER BY fire_at ASC LIMIT %s""", (int(limit),))
    return rows or []


def recovery_mark(rid: int, status: str, note: str = '') -> None:
    _ensure_recovery_table()
    if status == 'sent':
        pg.execute(
            "UPDATE recovery_queue SET status='sent', sent_at=NOW(), "
            "note=%s WHERE id=%s", (note[:240], int(rid)))
    else:
        pg.execute(
            "UPDATE recovery_queue SET status=%s, note=%s WHERE id=%s",
            (status, note[:240], int(rid)))


def recovery_stats() -> dict:
    _ensure_recovery_table()
    out = {'queued': 0, 'sent_7d': 0, 'failed_7d': 0, 'recovered': 0}
    try:
        r = pg.query_one(
            "SELECT COUNT(*) AS n FROM recovery_queue WHERE status='pending'")
        out['queued'] = int(r['n']) if r else 0
        r = pg.query_one(
            """SELECT COUNT(*) AS n FROM recovery_queue
                WHERE status='sent' AND sent_at > NOW() - INTERVAL '7 days'""")
        out['sent_7d'] = int(r['n']) if r else 0
        r = pg.query_one(
            """SELECT COUNT(*) AS n FROM recovery_queue
                WHERE status='failed' AND created_at > NOW() - INTERVAL '7 days'""")
        out['failed_7d'] = int(r['n']) if r else 0
        r = pg.query_one(
            """SELECT COUNT(DISTINCT rq.user_id) AS n
                 FROM recovery_queue rq
                 JOIN users u ON u.id = rq.user_id
                WHERE rq.status='sent'
                  AND COALESCE((u.data->>'subscription_active')::bool, FALSE) = TRUE""")
        out['recovered'] = int(r['n']) if r else 0
    except Exception:
        pass
    return out


def recovery_recent(limit: int = 50) -> list:
    _ensure_recovery_table()
    rows = pg.query(
        """SELECT rq.id, rq.user_id, rq.trigger, rq.step, rq.fire_at,
                  rq.sent_at, rq.status,
                  COALESCE(u.email, '?') AS email
             FROM recovery_queue rq
        LEFT JOIN users u ON u.id = rq.user_id
            ORDER BY rq.id DESC LIMIT %s""", (int(limit),))
    out = []
    for r in rows or []:
        r['fire_at_label'] = r['fire_at'].strftime('%b %d, %H:%M') if r.get('fire_at') else ''
        r['sent_at_label'] = r['sent_at'].strftime('%b %d, %H:%M') if r.get('sent_at') else ''
        out.append(r)
    return out


# ── A/B pricing test stats (LEGACY — feature removed) ─────────────
#
# The /admin-panel/marketing/ab-pricing/ page and its middleware were
# removed. This helper is kept as dead code in case future analytics
# want to slice historical signups by the ``signup_pricing_variant``
# tag that was stamped on user records while the test was live.

def ab_pricing_stats() -> dict:
    """Legacy: signup + paid counts split by pricing variant (A vs B).

    The A/B test UI has been removed; this helper still works for any
    historical user records that were tagged with ``signup_pricing_variant``
    while the test was live. Safe on a virgin DB: returns zeros.
    """
    out = {
        'a_signups': 0, 'b_signups': 0,
        'a_paid':    0, 'b_paid':    0,
        'a_rate':  0.0, 'b_rate':  0.0,
        'total_signups': 0,
    }
    try:
        rows = pg.query(
            """SELECT data->>'signup_pricing_variant' AS v,
                      COUNT(*) AS n,
                      COUNT(*) FILTER (
                          WHERE COALESCE((data->>'subscription_active')::bool, FALSE)
                      ) AS paid
                 FROM users
                WHERE data ? 'signup_pricing_variant'
                GROUP BY v""")
        for r in rows or []:
            v = (r.get('v') or '').lower()
            if v not in ('a', 'b'):
                continue
            out[f'{v}_signups'] = int(r['n'] or 0)
            out[f'{v}_paid']    = int(r['paid'] or 0)
        out['total_signups'] = out['a_signups'] + out['b_signups']
        if out['a_signups']:
            out['a_rate'] = round(100.0 * out['a_paid'] / out['a_signups'], 1)
        if out['b_signups']:
            out['b_rate'] = round(100.0 * out['b_paid'] / out['b_signups'], 1)
    except Exception:
        pass
    return out
