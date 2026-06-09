"""Database latency probe.

Run this ON the server that hosts the app to see how far (in milliseconds)
each database round-trip actually travels. If the app is far from the database
(e.g. app in Tunisia, Supabase in the US/EU), every query pays this latency and
pages that make many queries feel slow even though each query is cheap.

    python db_ping.py

It prints: time to connect, the median round-trip for a trivial SELECT 1, and
the time for the homepage's city-count aggregation. A healthy same-datacenter
setup is ~1-5 ms per round-trip; ~150-400 ms means the database is far away.
"""

import os
import time


def main() -> None:
    url = os.environ.get('SUPABASE_DATABASE_URL')
    if not url:
        print('SUPABASE_DATABASE_URL is not set in this shell.')
        return

    try:
        import psycopg
    except ImportError:
        print('psycopg not installed. Activate the venv first, e.g.')
        print(r'    .venv\Scripts\python db_ping.py')
        return

    t0 = time.time()
    conn = psycopg.connect(url, connect_timeout=15)
    print(f'connect:            {(time.time() - t0) * 1000:7.0f} ms')

    cur = conn.cursor()
    samples = []
    for _ in range(10):
        t = time.time()
        cur.execute('SELECT 1')
        cur.fetchone()
        samples.append((time.time() - t) * 1000)
    samples.sort()
    median = samples[len(samples) // 2]
    print(f'round-trip median:  {median:7.0f} ms  (per query)')
    print(f'round-trip min/max: {samples[0]:7.0f} / {samples[-1]:.0f} ms')

    t = time.time()
    cur.execute(
        "SELECT LOWER(city), UPPER(state), COUNT(*) FROM permits "
        "GROUP BY LOWER(city), UPPER(state) HAVING COUNT(*) >= 2")
    cur.fetchall()
    print(f'city aggregation:   {(time.time() - t) * 1000:7.0f} ms')

    conn.close()
    print()
    if median > 100:
        print('=> The database is FAR from this server. Each page that runs')
        print('   several queries will be slow. See the options your developer')
        print('   gave you (local Postgres / closer region / caching).')
    else:
        print('=> Latency looks fine; the slowness is elsewhere.')


if __name__ == '__main__':
    main()
