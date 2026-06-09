---
name: Permits list pagination & materialized derived score
description: How /permits/ paginates fast despite a Python-only derived score (materialized score_cache + daily refresh)
---

# /permits/ pagination vs. the derived score

The customer-facing permit score (the ring on `/permits/` + `/dashboard/`) is a
12-factor composite computed in **Python** (`core/permit_score.py derive_score`),
NOT the DB `ai_score` column. `ai_score` diverged from it badly, so the read
path deliberately ignores `ai_score` for display/sort/filter. Several factors
depend on **today's date** (freshness, expiry, seasonal), so the score changes
daily.

**Problem it solved:** the DEFAULT sort is by this derived score, which Postgres
can't compute. The old design fetched up to 25k rows and re-derived all scores in
Python on EVERY load (cold ~3.8s), with a 300s Django ordered-id cache band-aid.

**Current design — materialized `score_cache` (refreshed once a day):**
- `permits.score_cache SMALLINT` + `score_day DATE` + index
  `permits_score_cache_idx (score_cache DESC NULLS LAST, id DESC)`, created by
  `_ensure_permit_score_columns()` (idempotent, lock/statement-timeout guarded).
- `refresh_permit_scores(only_stale|only_null|batch|limit)` recomputes the column
  via `_row_to_permit_view`→`derive_score`, cursor-paginated by `id > cursor` in
  ALL modes (so a full re-score can't loop). `only_null` = just-ingested rows;
  `only_stale` = `score_day IS DISTINCT FROM CURRENT_DATE` (the daily calendar run).
- `ensure_scores_fresh_async()` — daemon thread, once-per-day debounce; called
  (non-blocking) from `query_permits_for_dashboard` so the read path always serves
  instantly from whatever is materialized. Management command
  `manage.py refresh_permit_scores [--all|--only-null]` for deterministic cron.
- `bulk_upsert_permits` calls `refresh_permit_scores(only_null=True)` best-effort
  after an ingest so new permits get an immediate rank.
- `query_permits_for_dashboard` is now a SINGLE SQL page query for every sort:
  `_SORT_SQL['score'] = 'COALESCE(score_cache, 0)'`; tier/range filters pushed into
  the SQL `WHERE` via `COALESCE(score_cache,0)`; summary via SQL `AVG` +
  `COUNT FILTER (... >= 80)`. The old 25k fetch / Python filter+sort / Django rank
  cache are all gone.

**Invariant — cache vs. live ring parity:** the per-row ring is still derived LIVE
per visible row (`_row_to_permit_view`), so each row's score is exact for today;
only the ORDERING can be up to a day stale (user-approved trade). `_PERMIT_VIEW_COLS`
is the ONE shared column list used by both the read page AND `refresh_permit_scores`
— they MUST stay identical or the cached rank drifts from the ring. `valuation_cents`
must stay in that list (derive_score reads it; omitting it silently depresses scores).

**Auth invariant:** the gate `UPPER(state) = ANY(%s)` is still the FIRST WHERE on
every count/page/summary query, applied before the score/tier/range filters.

**Gotcha — timezone:** `derive_score` uses `date.today()`, so a bare
`python3 -c` test (system UTC) computes a different score than the full Django app
(`settings.TIME_ZONE = America/Chicago`) near the UTC/local day boundary. The web
server AND the refresh both run in full-app context, so cache==ring there. Verify
parity inside `manage.py shell`, never a bare script, or you'll chase a phantom bug.
