"""
One-shot migration script: TinyDB (db.json) -> Supabase Postgres.

Creates the schema (idempotent) and copies every record from db.json into
the corresponding Postgres table, preserving doc_ids as the SERIAL primary key.

Usage:
    python3 scripts/migrate_to_supabase.py            # creates schema + migrates
    python3 scripts/migrate_to_supabase.py --schema   # schema only
    python3 scripts/migrate_to_supabase.py --reset    # DROP & recreate (DANGER)
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'permitdaily.settings')
django.setup()

from core import pg  # noqa: E402


SCHEMA_SQL = """
-- ── users ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    reset_token TEXT,
    data        JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS users_reset_token_idx
    ON users(reset_token) WHERE reset_token IS NOT NULL;
-- Functional indexes for hot JSONB lookups (Google OAuth, referral codes,
-- API keys). The GIN index uses jsonb_path_ops because we only need
-- containment (@>) lookups on data->'api_keys'.
CREATE INDEX IF NOT EXISTS users_google_sub_idx
    ON users ((data->>'google_sub'))
    WHERE data->>'google_sub' IS NOT NULL;
CREATE INDEX IF NOT EXISTS users_referral_code_idx
    ON users ((data->>'referral_code'))
    WHERE data->>'referral_code' IS NOT NULL;
CREATE INDEX IF NOT EXISTS users_api_keys_gin_idx
    ON users USING gin ((data->'api_keys') jsonb_path_ops);

-- ── banned_emails ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS banned_emails (
    id    SERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    data  JSONB NOT NULL
);

-- ── sessions ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    session_key TEXT NOT NULL,
    data        JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_id_idx     ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_session_key_idx ON sessions(session_key);
CREATE INDEX IF NOT EXISTS sessions_user_key_idx    ON sessions(user_id, session_key);

-- ── login_history ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS login_history (
    id      SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    data    JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS login_history_user_id_idx ON login_history(user_id);

-- ── system_settings ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_settings (
    key   TEXT PRIMARY KEY,
    value JSONB
);

-- ── support_tickets ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS support_tickets (
    id        SERIAL PRIMARY KEY,
    ticket_id TEXT UNIQUE,
    user_id   INTEGER NOT NULL,
    status    TEXT,
    priority  TEXT,
    data      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS support_tickets_user_id_idx     ON support_tickets(user_id);
CREATE INDEX IF NOT EXISTS support_tickets_status_user_idx  ON support_tickets(status, user_id);

-- ── notifications ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    type_key   TEXT,
    status_key TEXT,
    sent_at    TEXT,
    data       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_user_id_idx        ON notifications(user_id);
CREATE INDEX IF NOT EXISTS notifications_user_sent_idx       ON notifications(user_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS notifications_user_type_sent_idx  ON notifications(user_id, type_key, sent_at DESC);

-- ── invoices ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS invoices (
    id              SERIAL PRIMARY KEY,
    invoice_id      TEXT UNIQUE,
    user_id         INTEGER NOT NULL,
    period_start_ts BIGINT,
    data            JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS invoices_user_id_idx ON invoices(user_id);
"""


DROP_SQL = """
DROP TABLE IF EXISTS users, banned_emails, sessions, login_history,
    system_settings, support_tickets, notifications, invoices CASCADE;
"""


def create_schema():
    with pg.conn() as c, c.cursor() as cur:
        for stmt in SCHEMA_SQL.strip().split(';'):
            s = stmt.strip()
            if s:
                cur.execute(s)
    print('  schema OK')


def reset_schema():
    with pg.conn() as c, c.cursor() as cur:
        cur.execute(DROP_SQL)
    print('  dropped all tables')
    create_schema()


def migrate_data():
    # Accept either the live TinyDB file or the post-migration backup.
    candidates = ['db.json', 'db.json.tinydb_backup']
    db_path = next((p for p in candidates if os.path.exists(p)), None)
    if db_path is None:
        raise SystemExit(
            f'No source TinyDB file found (looked for: {", ".join(candidates)}). '
            f'Use --schema if you only want to (re)create the schema.'
        )
    print(f'  source: {db_path}')
    raw = json.load(open(db_path))

    # TinyDB v4 stores tables as: { "<table>": { "<doc_id_str>": {...} } }
    # Older (default table format) is { "_default": {...} }.

    def _iter_rows(table_dict):
        if not isinstance(table_dict, dict):
            return
        for doc_id_str, doc in table_dict.items():
            try:
                yield int(doc_id_str), dict(doc)
            except (ValueError, TypeError):
                continue

    with pg.conn() as c, c.cursor() as cur:
        # ── users
        rows = list(_iter_rows(raw.get('users', {})))
        for doc_id, doc in rows:
            email = (doc.get('email') or '').lower().strip()
            if not email:
                continue
            cur.execute(
                """INSERT INTO users (id, email, reset_token, data)
                   VALUES (%s, %s, %s, %s::jsonb)
                   ON CONFLICT (id) DO UPDATE
                     SET email = EXCLUDED.email,
                         reset_token = EXCLUDED.reset_token,
                         data = EXCLUDED.data""",
                (doc_id, email, doc.get('reset_token'), json.dumps(doc)),
            )
        if rows:
            cur.execute("SELECT setval('users_id_seq', GREATEST(MAX(id),1)) FROM users")
        print(f'  users: {len(rows)}')

        # ── banned_emails
        rows = list(_iter_rows(raw.get('banned_emails', {})))
        for doc_id, doc in rows:
            email = (doc.get('email') or '').lower().strip()
            if not email:
                continue
            cur.execute(
                """INSERT INTO banned_emails (id, email, data)
                   VALUES (%s, %s, %s::jsonb)
                   ON CONFLICT (id) DO NOTHING""",
                (doc_id, email, json.dumps(doc)),
            )
        if rows:
            cur.execute("SELECT setval('banned_emails_id_seq', GREATEST(MAX(id),1)) FROM banned_emails")
        print(f'  banned_emails: {len(rows)}')

        # ── sessions
        rows = list(_iter_rows(raw.get('sessions', {})))
        for doc_id, doc in rows:
            cur.execute(
                """INSERT INTO sessions (id, user_id, session_key, data)
                   VALUES (%s, %s, %s, %s::jsonb)
                   ON CONFLICT (id) DO NOTHING""",
                (doc_id, int(doc.get('user_id', 0)),
                 doc.get('session_key', ''), json.dumps(doc)),
            )
        if rows:
            cur.execute("SELECT setval('sessions_id_seq', GREATEST(MAX(id),1)) FROM sessions")
        print(f'  sessions: {len(rows)}')

        # ── login_history
        rows = list(_iter_rows(raw.get('login_history', {})))
        for doc_id, doc in rows:
            cur.execute(
                """INSERT INTO login_history (id, user_id, data)
                   VALUES (%s, %s, %s::jsonb)
                   ON CONFLICT (id) DO NOTHING""",
                (doc_id, int(doc.get('user_id', 0)), json.dumps(doc)),
            )
        if rows:
            cur.execute("SELECT setval('login_history_id_seq', GREATEST(MAX(id),1)) FROM login_history")
        print(f'  login_history: {len(rows)}')

        # ── system_settings
        rows = list(_iter_rows(raw.get('system_settings', {})))
        for _doc_id, doc in rows:
            key = doc.get('key')
            if not key:
                continue
            cur.execute(
                """INSERT INTO system_settings (key, value)
                   VALUES (%s, %s::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                (key, json.dumps(doc.get('value'))),
            )
        print(f'  system_settings: {len(rows)}')

        # ── support_tickets
        rows = list(_iter_rows(raw.get('support_tickets', {})))
        for doc_id, doc in rows:
            cur.execute(
                """INSERT INTO support_tickets
                       (id, ticket_id, user_id, status, priority, data)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (id) DO NOTHING""",
                (doc_id, doc.get('ticket_id'),
                 int(doc.get('user_id', 0)),
                 doc.get('status'), doc.get('priority'),
                 json.dumps(doc)),
            )
        if rows:
            cur.execute("SELECT setval('support_tickets_id_seq', GREATEST(MAX(id),1)) FROM support_tickets")
        print(f'  support_tickets: {len(rows)}')

        # ── notifications
        rows = list(_iter_rows(raw.get('notifications', {})))
        for doc_id, doc in rows:
            cur.execute(
                """INSERT INTO notifications
                       (id, user_id, type_key, status_key, sent_at, data)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (id) DO NOTHING""",
                (doc_id, int(doc.get('user_id', 0)),
                 doc.get('type_key'), doc.get('status_key'),
                 doc.get('sent_at'), json.dumps(doc)),
            )
        if rows:
            cur.execute("SELECT setval('notifications_id_seq', GREATEST(MAX(id),1)) FROM notifications")
        print(f'  notifications: {len(rows)}')

        # ── invoices
        rows = list(_iter_rows(raw.get('invoices', {})))
        for doc_id, doc in rows:
            cur.execute(
                """INSERT INTO invoices
                       (id, invoice_id, user_id, period_start_ts, data)
                   VALUES (%s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (id) DO NOTHING""",
                (doc_id, doc.get('invoice_id'),
                 int(doc.get('user_id', 0)),
                 int(doc.get('period_start_ts') or 0) or None,
                 json.dumps(doc)),
            )
        if rows:
            cur.execute("SELECT setval('invoices_id_seq', GREATEST(MAX(id),1)) FROM invoices")
        print(f'  invoices: {len(rows)}')


if __name__ == '__main__':
    args = sys.argv[1:]
    if '--reset' in args:
        print('RESET: dropping & recreating all tables…')
        reset_schema()
    else:
        print('Creating schema (idempotent)…')
        create_schema()

    if '--schema' not in args:
        print('Migrating data from db.json…')
        migrate_data()

    print('Done.')
