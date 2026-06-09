---
name: Accela free parser (parse_accela_detail)
description: How the token-saving regex parser for Accela permit pages works, its layout pitfalls, and how to validate changes against stored ground truth.
---

# Accela free parser — `core/helpers/accela_parser.py::parse_accela_detail`

`parse_accela_detail` is the **token-saving fast path** that lets the Accela
scraper skip GPT-OSS inference when it can lift both `contractor_email` and
`contractor_phone` straight out of the cleaned page text. The caller
(`core/scrapers/accela.py::_process_one`) short-circuits the LLM only when the
parser returns BOTH fields, tags the row `llm_model='parser (no inference)'`,
and still runs `_merge_grid_into_llm` so list-grid fields (address, type…) are
preserved even on the fast path.

**Editing boundary:** everything ABOVE the `── End of verbatim reference ──`
marker (~line 164) is the user's verbatim GPT-OSS reference and must NOT be
touched. `parse_accela_detail` and its helpers/regexes live BELOW it and are
agent-owned — safe to edit. There is no counterpart in the
`attached_assets/accela_parser_test_*.py` files (those are the standalone
GPT-OSS script, needs `openai`+DO key, not unit tests of this function).

## Accela layouts vary a lot — handle all of these
- **Record number** is stacked: a lone `Record` / `Record No` line then the id
  on the NEXT line, no colon. The inline `Record <id>:` regex also wrongly
  matches the field label `Record Status:` → require the captured id to contain
  a digit.
- **Contact headers** differ by deployment: older skins use `Applicant:` /
  `Contractor:`; newer ones use `Licensed Professional:`, sometimes plus a
  separate `Additional Contact Information` / `Related Contacts` block that
  holds the reachable phone+email. Headers may carry an `Info` / `Information`
  / `Details` suffix (`Applicant Info:`, `Licensed Professional Info:`).
- **Contractor-only rule:** take contractor fields ONLY from contractor-type
  headers, in priority order Applicant > Contractor > Licensed Professional >
  Additional Contact. Owner / `Property Owner Info` blocks feed `owner_name`
  ONLY — never the contractor fields. Junk `Owner/Email/Address/Status` column
  headers leak in from the "Related Records" tree; filter those label words.

## The phone-regex newline trap (the big one)
The US phone regex must use `[ \t.\-]` (space/tab/dot/dash) as the inter-group
separator, NEVER `\s`. With `\s`, a match spans a newline and glues a trailing
digit run on one line to the street number on the next
(`312995\n2121 N CALIFORNIA BLVD` → bogus `(312) 995-2121`). Also add a leading
`(?<!\d)` and NANP `[2-9]\d{2}` for area+exchange to reject parcel/tax-map ids
and dates that are otherwise phone-shaped. This single bug was the largest
precision killer.

## Validating parser changes (no LLM needed)
Ground truth = stored `contractor_email` / `contractor_phone` (what GPT-OSS
already extracted) vs the parser run on the stored cleaned text:
`raw->'llm_debug'->>'cleaned_html'`. Pull a few hundred rows across MANY
`source='accela:<id>'` scrapers (layouts differ per scraper), run the parser,
and measure: both-correct %, short-circuit rate, and **precision when
short-circuiting** (most important — a wrong short-circuit is worse than
calling GPT-OSS). Remaining mismatches are usually an alternative VALID
contractor contact (Contractor vs Tradesman, LP vs Additional Contact), not a
homeowner leak. Use the project DB via `core.pg`, NOT the executeSql tool
(that hits the wrong Replit-default DB with no `permits` table).

## Fast-path now makes a slim SCORING-ONLY AI call (not zero inference)
The parser short-circuit used to skip inference entirely → those rows had
`ai_score = NULL` (the gap: `parser (no inference)` rows were 0/137 scored).
Now, when the parser lifts both contact fields, `_process_one` makes ONE tiny
call (`source='accela_scraper_score'`, `get_scoring_only_prompt()`) asking ONLY
for the 9 sub-scores, tags the row `llm_model='parser + score-only'`, and
injects them so downstream scoring "just works". On scoring failure it falls
back to the old unscored `parser (no inference)` row (never lose the permit).

**Why one AI call still scores correctly:** `_normalise_permit`
(`core/scraper_accela.py`) ALWAYS recomputes the composite from the sub-scores —
it reads `raw['s']` (short-key) OR `raw['ai_subscores']` (long-key), normalises,
applies `ai_phrases.WEIGHTS`, and composes reasoning from a phrase library. The
model's own `ai_score/grade/tier` are ignored. So the slim call only needs to
return the sub-score object; everything else is deterministic server-side.

## The ACTIVE extraction prompt is an admin OVERRIDE, not the file
`get_extraction_prompt()` returns `system_setting('extraction_prompt')` when set,
falling back to `core/helpers/accela_parser_prompt.txt`. **They differ in
format:** the file default uses short-keys with a `SCORING — "s" object` section
and `"s":{lq,ur,...}`; the live override uses a `SCORING RUBRICS` section with
LONG keys and instructs emitting `ai_subscores:{lead_quality,...}`. So
`get_scoring_only_prompt()` slices on `SCORING RUBRICS` first, then
`SCORING — "s" object`, then bare `SCORING`, and drops everything from
`COMPOSITE` onward (server-only). Parsing accepts BOTH `ai_subscores` and `s`.
**Gotcha:** gpt-5-nano bills hidden reasoning tokens, so a slim call can still
show ~2.5k output_tokens even though the visible JSON is tiny — the win is
avoiding the full 50-field *visible* generation, not zeroing output tokens.
