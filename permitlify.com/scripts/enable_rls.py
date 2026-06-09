"""
Enable Row Level Security on every table in the public schema.

Why this matters:
    Supabase auto-exposes every public.* table through PostgREST to the
    `anon` and `authenticated` roles. Permitlify never uses PostgREST —
    all access goes through the Django backend with the trusted `postgres`
    role via SUPABASE_DATABASE_URL. So we want to *deny* every external
    role and let only the Django connection in.

The fix:
    `ALTER TABLE ... ENABLE ROW LEVEL SECURITY;` with **zero policies**.
    With RLS enabled and no policy, Postgres denies every SELECT / INSERT
    / UPDATE / DELETE for all roles *except* table owner & superuser. The
    Django connection (postgres role) bypasses RLS, so backend reads /
    writes keep working unchanged. The Supabase advisor warning clears.

This script is fully idempotent — running it twice is a no-op.

Usage:
    python3 scripts/enable_rls.py            # enable RLS on every public table
    python3 scripts/enable_rls.py --status   # just print which tables have RLS

It auto-discovers tables from pg_tables so it stays correct even if new
tables are added later (e.g. permits, referral_events).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Standalone — no Django bootstrap needed; core.pg only depends on the
# SUPABASE_DATABASE_URL env var. This lets the script run from anywhere
# (CI, ad-hoc shell) without requiring DJANGO_SECRET_KEY etc.
from core import pg  # noqa: E402


def list_public_tables():
    """Return [(table_name, rls_enabled_bool), ...] for every public table."""
    rows = pg.query("""
        SELECT c.relname AS table_name,
               c.relrowsecurity AS rls_enabled
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'        -- ordinary tables only
         ORDER BY c.relname
    """)
    return [(r['table_name'], r['rls_enabled']) for r in rows]


def enable_rls():
    from psycopg import sql
    rows = list_public_tables()
    if not rows:
        print("No tables found in public schema — nothing to do.")
        return

    print(f"Found {len(rows)} table(s) in public schema.\n")
    enabled, skipped = [], []
    with pg.conn() as cn, cn.cursor() as cur:
        for name, has_rls in rows:
            if has_rls:
                skipped.append(name)
                print(f"  ✓ {name:<24}  RLS already enabled")
                continue
            # Use parameterised identifier formatting to avoid SQL injection
            # via table names.
            cur.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY")
                        .format(sql.Identifier(name)))
            # Explicit no-policy stance: deny everything to non-bypass roles.
            # Belt-and-braces: FORCE RLS so even the table owner is subject
            # to the (empty) policy set. The Django connection (superuser
            # `postgres`) still bypasses RLS, so backend reads/writes work
            # exactly as before.
            cur.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY")
                        .format(sql.Identifier(name)))
            enabled.append(name)
            print(f"  → {name:<24}  RLS enabled (+ FORCE)")

    print()
    print(f"Done.  Newly enabled: {len(enabled)}.  Already on: {len(skipped)}.")
    if enabled:
        print("Tables locked down this run:")
        for n in enabled:
            print(f"    - public.{n}")


def show_status():
    rows = list_public_tables()
    if not rows:
        print("No public tables found.")
        return
    print(f"{'TABLE':<28} RLS")
    print(f"{'-' * 28} ----")
    for name, has_rls in rows:
        print(f"public.{name:<20} {'ON' if has_rls else 'OFF'}")


if __name__ == '__main__':
    if '--status' in sys.argv:
        show_status()
    else:
        enable_rls()
