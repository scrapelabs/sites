"""
Idempotent initializer for the `permits` table.

Run with:
    python scripts/init_permits_table.py

Creates the table if missing and ensures all indexes exist. Safe to re-run.
The table is designed to receive data pushed by the external scraper platform
via POST /api/v1/permits/ingest/. Each permit is uniquely identified by the
combination (source, source_permit_id) so the same permit re-pushed by the
scraper is upserted rather than duplicated.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'permitdaily.settings')
django.setup()

from core import pg


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS permits (
    id                  BIGSERIAL PRIMARY KEY,

    -- Provenance (which scraper produced this row, and its native id)
    source              TEXT        NOT NULL,
    source_permit_id    TEXT        NOT NULL,

    -- Identification & location
    permit_number       TEXT,
    state               TEXT        NOT NULL,
    city                TEXT        NOT NULL,
    jurisdiction        TEXT,
    address             TEXT,
    zip                 TEXT,
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,

    -- Parties
    owner_name          TEXT,
    contractor_name     TEXT,
    contractor_phone    TEXT,
    contractor_email    TEXT,
    -- Unified primary contact for the permit. Populated by the
    -- normaliser as: contractor_name if present else owner_name (per
    -- user request "name it contact name and put either the contractor
    -- name or owner name then add contact type = owner or contractor").
    -- contact_type is one of '', 'contractor', 'owner'.
    contact_name        TEXT,
    contact_type        TEXT,

    -- Work
    permit_type         TEXT,
    description         TEXT,
    trade               TEXT,
    status              TEXT,
    valuation_cents     BIGINT,
    square_feet         INTEGER,

    -- Dates
    applied_date        DATE,
    issued_date         DATE,
    expires_date        DATE,
    scraped_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- AI scoring (filled by scraper or by an enrichment job)
    ai_score            INTEGER,
    ai_grade            TEXT,
    ai_tier             TEXT,
    ai_reasoning        TEXT,
    ai_model_version    TEXT,
    ai_scored_at        TIMESTAMPTZ,

    -- Lineage: which scraper_runs row produced this permit. Nullable
    -- because (a) older permits pre-date the column, and (b) permits
    -- ingested via POST /api/v1/permits/ingest/ don't belong to any
    -- in-app run. The FK is added by core/db._ensure_scrapers_table()
    -- (it can only attach once the scraper_runs table exists), so this
    -- canonical CREATE just declares the bare BIGINT column to keep
    -- standalone init_permits_table.py runs idempotent.
    scraper_run_id      BIGINT,

    -- Cross-source dedup fingerprint. SHA-256 of the normalised
    -- composite (permit_number | trade | address | city | contractor
    -- name | email | phone | issued_date | valuation_cents). Computed
    -- by core.db.compute_permit_dedup_hash() at upsert time. NULL when
    -- the row lacks enough signal to identify it (e.g. only a
    -- permit_number with no other fields) — those rows are still kept
    -- but won't participate in cross-source dedup. The matching
    -- partial unique index below enforces "no two rows with the same
    -- non-NULL fingerprint", which catches the same physical permit
    -- showing up under multiple `source` tags (e.g. two scrapers
    -- pointed at overlapping Accela jurisdictions).
    dedup_hash          TEXT,

    -- Full original payload from scraper (for debugging / re-processing)
    raw                 JSONB       NOT NULL DEFAULT '{}'::jsonb,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT permits_source_uid_uq UNIQUE (source, source_permit_id),
    CONSTRAINT permits_ai_score_range CHECK (ai_score IS NULL OR (ai_score BETWEEN 0 AND 100)),
    CONSTRAINT permits_ai_tier_vals   CHECK (ai_tier  IS NULL OR ai_tier IN ('hot','warm','cool'))
);

-- Idempotent additive migration for installs that already had the
-- table before scraper_run_id / dedup_hash existed. ALTER TABLE …
-- IF NOT EXISTS is a no-op when the column is already present.
ALTER TABLE permits ADD COLUMN IF NOT EXISTS scraper_run_id BIGINT;
ALTER TABLE permits ADD COLUMN IF NOT EXISTS dedup_hash     TEXT;
ALTER TABLE permits ADD COLUMN IF NOT EXISTS contact_name   TEXT;
ALTER TABLE permits ADD COLUMN IF NOT EXISTS contact_type   TEXT;

-- One-shot backfill for rows ingested before the contact_* columns
-- existed. Idempotent: only fills NULLs, never overwrites a value the
-- normaliser already wrote on a fresh upsert.
UPDATE permits
   SET contact_name = COALESCE(NULLIF(contractor_name, ''), NULLIF(owner_name, '')),
       contact_type = CASE
           WHEN COALESCE(contractor_name, '') <> '' THEN 'contractor'
           WHEN COALESCE(owner_name,      '') <> '' THEN 'owner'
           ELSE ''
       END
 WHERE contact_name IS NULL OR contact_type IS NULL;

CREATE INDEX IF NOT EXISTS permits_state_city_idx     ON permits (state, lower(city));
CREATE INDEX IF NOT EXISTS permits_trade_idx          ON permits (trade);
CREATE INDEX IF NOT EXISTS permits_issued_date_idx    ON permits (issued_date DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS permits_ai_score_idx       ON permits (ai_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS permits_ai_tier_idx        ON permits (ai_tier);
CREATE INDEX IF NOT EXISTS permits_status_idx         ON permits (status);
CREATE INDEX IF NOT EXISTS permits_scraper_run_idx    ON permits (scraper_run_id) WHERE scraper_run_id IS NOT NULL;

-- Cross-source dedup. Partial UNIQUE so rows that genuinely cannot be
-- fingerprinted (dedup_hash IS NULL — too few identifying fields) are
-- still allowed to coexist; rows with a hash are guaranteed unique
-- across the whole permits table regardless of `source`. upsert_permit
-- catches the resulting UniqueViolation and converts it into an
-- UPDATE-by-hash so the existing row is enriched instead of dropped.
CREATE UNIQUE INDEX IF NOT EXISTS permits_dedup_hash_uq
    ON permits (dedup_hash) WHERE dedup_hash IS NOT NULL;

-- Helpful lookup indexes for the per-scraper detail page filters
-- (search by permit number / contractor / email). Lower()-wrapped so
-- the case-insensitive ILIKE/lower() probes the existing UI emits can
-- index-scan instead of seq-scanning the whole table once volumes grow.
CREATE INDEX IF NOT EXISTS permits_permit_number_lower_idx
    ON permits (lower(permit_number)) WHERE permit_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS permits_contractor_email_lower_idx
    ON permits (lower(contractor_email)) WHERE contractor_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS permits_contractor_name_lower_idx
    ON permits (lower(contractor_name)) WHERE contractor_name IS NOT NULL;

-- Auto-bump updated_at on every UPDATE.
CREATE OR REPLACE FUNCTION permits_set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS permits_set_updated_at ON permits;
CREATE TRIGGER permits_set_updated_at
    BEFORE UPDATE ON permits
    FOR EACH ROW
    EXECUTE FUNCTION permits_set_updated_at();
"""


def main():
    pg.execute(SCHEMA_SQL)
    rows = pg.query(
        """SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = 'permits' ORDER BY ordinal_position"""
    )
    print(f'permits table has {len(rows)} columns:')
    for r in rows:
        print(f'  - {r["column_name"]:<22} {r["data_type"]}')
    idx = pg.query(
        """SELECT indexname FROM pg_indexes
            WHERE tablename = 'permits' ORDER BY indexname"""
    )
    print(f'\nindexes ({len(idx)}):')
    for r in idx:
        print(f'  - {r["indexname"]}')


if __name__ == '__main__':
    main()
