---
name: Person-vs-company save gate
description: Why upsert_permit drops rows, and the email-domain rule that keeps one-word business names
---

`upsert_permit` (core/db.py) silently returns `None` (row not saved) at three
save-time gates, each of which writes a `junk_permits` row with a `reason` and
now also stamps `permit['_skip_reason']`:
- `missing_identity` — source / source_permit_id / state / city missing
- `no_contact` — no contractor email AND no phone
- `person_no_name` — `contractor_name_is_droppable_person` judged the name a
  bare private individual (one alpha word, no business token/&/digit)

**Lesson:** name-shape alone is NOT a reliable person-vs-business signal. A real
contractor often has a one-word trade name ("Crawford", "NVR"). The reliable
discriminator is the **email domain**: a custom (non-freemail) domain is a
company; only freemail (`_FREEMAIL_DOMAINS`) single-word names are dropped.

**Why:** a scraper saved 0/85 because the original gate dropped legit
businesses (e.g. an HVAC company and a homebuilder, both with single-word
trade names but custom, non-freemail email domains) as `person_no_name`. The `junk_permits` table is the source of truth for *which*
gate fired — query `reason` there, don't trust the scraper's old catch-all
"required identity fields not satisfied" message (it printed that for ALL None
returns regardless of cause).

**How to apply:** when "scraper saves 0 / drops everything," query
`junk_permits` grouped by `reason` for that `source` to find the real gate, and
remember the active Accela path re-normalises via the OLD
`core/scraper_accela.py._normalise_permit` callback even though parsing lives in
`core/scrapers/accela.py`. Clearing wrongly-created junk markers (DELETE by
reason) is required to make falsely-dropped permits re-scrapeable; re-junking is
idempotent.

**DB note:** the app's real data is in Supabase (`SUPABASE_DATABASE_URL`), NOT
the built-in Replit Postgres that the `executeSql` code-exec helper targets.
Query Supabase via psycopg + that env var.
