"""Static phrase library for permit AI scoring output.

Why this exists
---------------
The AI used to return a free-form ``ai_reasoning`` paragraph and a
free-form ``ai_subscore_reasons`` dict (one short sentence per
sub-score). That cost ~150-200 output tokens per permit, and the
phrasing was effectively repeating itself across thousands of permits.

This module replaces that AI output with a static phrase library:

  * Every sub-score (0-100) maps to one of 5 buckets
    (high / good / mid / low / dead).
  * Each (dimension, bucket) pair has 3 phrase VARIANTS, written in
    contractor voice with deliberately different wording so output
    feels human and varied across permits.
  * The variant is picked DETERMINISTICALLY from a stable seed
    (the permit number). Same permit → same sentence every time,
    so the UI never flickers when a row is re-scored. Different
    permits in the same bucket → different sentences, so the feed
    doesn't read like ten thousand copies of the same paragraph.
  * Total phrase library: 9 dimensions × 5 buckets × 3 variants
    = **135 hand-written sentences**. Zero AI cost, zero drift.

Result: model output shrinks from ~200 tokens to ~40 tokens of pure
JSON numbers, and the rendered reasoning still reads like an analyst
wrote it.

Server-side scoring math
------------------------
Because the model now returns only the 9 raw sub-scores, the composite
``ai_score``, ``ai_tier`` and ``ai_grade`` are also computed here —
deterministic, never drifts, never costs a token. The formula matches
the rubric the prompt used to ask the model to apply (weights +
null-renormalisation + low-confidence shrink + clamp).
"""

from __future__ import annotations
import hashlib
from typing import Optional


# Weights for the composite — must sum to 1.0. data_confidence is NOT
# in the composite, only used for the post-hoc <50 adjustment, per the
# original prompt rubric.
WEIGHTS = {
    'lead_quality':         0.25,
    'urgency':              0.18,
    'status_actionability': 0.15,
    'contact_completeness': 0.12,
    'project_value':        0.12,
    'intent_signal':        0.08,
    'trade_fit':            0.05,
    'geographic':           0.05,
}

# Short-key alias map so the model can emit ``{"lq":80,"ur":75,...}``
# — saves ~80 tokens per response vs. the long form.
SHORT_KEYS = {
    'lq': 'lead_quality',
    'ur': 'urgency',
    'sa': 'status_actionability',
    'cc': 'contact_completeness',
    'pv': 'project_value',
    'is': 'intent_signal',
    'tf': 'trade_fit',
    'ge': 'geographic',
    'dc': 'data_confidence',
}


def _bucket(score: Optional[int]) -> Optional[str]:
    """Score → bucket key (5 levels). None stays None so the phrase
    lookup skips sub-scores the model couldn't compute. Thresholds
    line up with the rubric language in the prompt: 80+ = strong,
    60+ = solid, 40+ = decent, 20+ = weak, below 20 = effectively
    dead. Five buckets give finer gradation than the original four
    so the rendered phrasing tracks reality more closely."""
    if score is None:
        return None
    if score >= 80: return 'high'
    if score >= 60: return 'good'
    if score >= 40: return 'mid'
    if score >= 20: return 'low'
    return 'dead'


# ── The phrase library ─────────────────────────────────────────────
# Every (dimension, bucket) pair has THREE variants. Voice: short,
# contractor-facing, present-tense, ≤100 chars per variant. Variant
# is picked deterministically via _pick(variants, seed) so each
# permit consistently shows the same sentence but the feed as a
# whole reads naturally varied.

PHRASES = {
    'lead_quality': {
        'high': (
            'Top-shelf lead — issued permit, named contact, real budget on the table.',
            'Premium opportunity — every signal is green: permit live, contact known, scope sized.',
            'A-tier lead worth chasing today — issued, contactable, and properly capitalised.',
        ),
        'good': (
            'Solid lead — active permit with usable contact info on file.',
            'Healthy opportunity — permit is moving and there is a real contact to call.',
            'Worth a call — the basics line up: permit, contact, and a workable scope.',
        ),
        'mid': (
            'Decent lead but not the strongest — some signals are thin.',
            'Workable opportunity if you are filling pipeline, though it is not a slam dunk.',
            'Mid-pack lead — fine for outreach but expect to qualify hard on the call.',
        ),
        'low': (
            'Marginal lead — thin contact info or a small ticket on the scope.',
            'Long shot — too little here to expect much from cold outreach.',
            'Low-conviction lead — keep expectations modest if you decide to pursue.',
        ),
        'dead': (
            'Dead lead — permit cancelled, voided, or denied.',
            'Skip this one — the permit will not produce billable work.',
            'No path forward here — the record is closed or rejected.',
        ),
    },
    'urgency': {
        'high': (
            'Filed in the last week — call today before competitors see it.',
            'Brand-new filing — the homeowner is still in research mode, get in first.',
            'Hot off the press — fewer than seven days old, prime outreach window.',
        ),
        'good': (
            'Filed within the last month — still inside the warm window.',
            'Recent enough to matter — the decision-maker is likely still shopping.',
            'Two to four weeks out — outreach is timely without being intrusive.',
        ),
        'mid': (
            'Filed about a month ago — call soon while the project is still fresh.',
            'Mid-aged filing — the timing window is closing but not gone.',
            'A few weeks stale — worth a touch, expect some homeowners already in talks.',
        ),
        'low': (
            'Filed one to three months back — homeowner may already have a contractor.',
            'Cooling lead — too much time has passed to be the first call, but try.',
            'Older filing — your pitch needs to assume they have heard from others.',
        ),
        'dead': (
            'Filed months ago — likely already engaged with another contractor.',
            'Cold lead — by this stage most homeowners have signed with someone.',
            'Stale filing — outreach now is a Hail Mary at best.',
        ),
    },
    'status_actionability': {
        'high': (
            'Status is Issued or Approved — work is greenlit and ready to bid.',
            'Permit is live — the homeowner can break ground the moment they sign with you.',
            'Fully approved status — no admin friction left between you and the job.',
        ),
        'good': (
            'Ready to issue or in final plan check — clearance is days away.',
            'Approval is in sight — perfect time to be the contractor they call.',
            'Close to greenlight — get the relationship started before the permit drops.',
        ),
        'mid': (
            'Under review — early stage but worth a relationship call now.',
            'In plan check — they are committed enough to file, that already separates them.',
            'Mid-process status — long sales cycle but high conviction from the homeowner.',
        ),
        'low': (
            'On Hold or Corrections Required — wait for the next status update.',
            'Stuck in revisions — pursue only if you can help unblock the plan-check issue.',
            'Suspended status — this one will idle for weeks unless something changes.',
        ),
        'dead': (
            'Permit is expired, cancelled, or withdrawn — not actionable.',
            'Status is closed out — the homeowner moved on or never broke ground.',
            'Permit is dead on arrival — pick another lead.',
        ),
    },
    'contact_completeness': {
        'high': (
            'Full contact triple on file — name, phone, and email.',
            'Every channel is open — call, text, or email, you choose.',
            'Complete contact record — no hunting required to reach the decision-maker.',
        ),
        'good': (
            'Name plus one channel (phone or email) on file.',
            'Solid contact — you have a name and a way to reach them directly.',
            'Workable contact info — enough to start the conversation today.',
        ),
        'mid': (
            'Partial contact info — enough to try but expect some manual lookup.',
            'Name without channel, or channel without name — usable with a small lift.',
            'Mid-strength contact record — supplement with a quick web search before calling.',
        ),
        'low': (
            'Only a phone or email, no name — outreach will feel cold.',
            'Sparse contact info — you will be guessing who to ask for.',
            'Thin contact data — possible to reach someone but the pitch is generic.',
        ),
        'dead': (
            'No contact info on the permit — owner-record only.',
            'Reachability is the blocker here — nothing to dial.',
            'Contact block is empty — skip unless you can match the address externally.',
        ),
    },
    'project_value': {
        'high': (
            'High-value job — $100k+ valuation on the permit.',
            'Big-ticket scope — well into six figures, worth fighting for.',
            'Premium project size — margin and reputation upside both on offer.',
        ),
        'good': (
            'Mid-range job — $25k to $100k valuation.',
            'Healthy ticket size — solid revenue without the heavy-pursuit overhead.',
            'Real-budget project — the homeowner has committed real money on paper.',
        ),
        'mid': (
            'Smaller job — five to twenty-five thousand range.',
            'Modest budget — fine for fill-in work between bigger projects.',
            'Mid-tier ticket — quick close potential, lower revenue ceiling.',
        ),
        'low': (
            'Tiny job — under five thousand on the valuation.',
            'Small-ticket scope — only pursue if you have route density nearby.',
            'Low-dollar permit — barely worth the windshield time on its own.',
        ),
        'dead': (
            'Valuation listed as $0 — likely an admin or no-cost permit.',
            'Zero-dollar filing — probably a re-issue, transfer, or paperwork-only event.',
            'No real budget attached — treat as informational, not a sales opportunity.',
        ),
    },
    'intent_signal': {
        'high': (
            'Clear replace/install/new-build scope — buyer is ready to spend.',
            'Hard-intent scope words — the homeowner is buying, not browsing.',
            'Unambiguous build intent — money is going to leave their account.',
        ),
        'good': (
            'Remodel or upgrade scope — meaningful work to bid on.',
            'Improvement-intent scope — not a teardown but real construction is planned.',
            'Renovation-scale intent — solid project to quote.',
        ),
        'mid': (
            'Repair or service scope — smaller ticket, faster decision.',
            'Service-intent permit — homeowner wants a fix, not a transformation.',
            'Maintenance-type scope — bid it lean and turn it around fast.',
        ),
        'low': (
            'Inspection or minor work only — limited revenue potential.',
            'Marginal scope — code corrections or paperwork dominate the description.',
            'Low-intent permit — homeowner is checking a box, not buying a project.',
        ),
        'dead': (
            'Demolition or temporary-use permit — not a buyer.',
            'Tear-down or admin filing — no construction work to win here.',
            'Scope is removal or housekeeping — nothing to quote.',
        ),
    },
    'trade_fit': {
        'high': (
            'Single, unambiguous trade match for your business.',
            'Trade fit is clean — one discipline, exactly your wheelhouse.',
            'Perfectly scoped to one trade — no scope creep risk on the bid.',
        ),
        'good': (
            'Dominant trade is clear with a little adjacent work attached.',
            'Mostly in your lane with a sub-trade or two — manageable.',
            'Primary trade is yours; the rest is light add-ons.',
        ),
        'mid': (
            'Multiple trades bundled — likely a general contractor lead.',
            'Mixed-trade scope — you will share the job with other subs.',
            'Cross-trade permit — fits if you are a GC, otherwise plan to sub.',
        ),
        'low': (
            'Trade tag is generic — manual review needed to confirm fit.',
            'Loose trade match — read the description before deciding to pursue.',
            'Fit is uncertain — could be yours, could be someone else entirely.',
        ),
        'dead': (
            'Trade unclear from the permit text — extraction was thin.',
            'No usable trade signal — treat the trade field as unreliable here.',
            'Trade fit cannot be determined from the available data.',
        ),
    },
    'geographic': {
        'high': (
            'Full street + city + state + zip — route directly to the job.',
            'Address is complete — you can drop it straight into your scheduler.',
            'Clean geocode-ready address with optional coordinates.',
        ),
        'good': (
            'Address present but missing zip or a minor field.',
            'Mostly complete address — a quick lookup fills the gap.',
            'Routable address with a small piece of normalisation needed.',
        ),
        'mid': (
            'Partial address — street and city but state or zip is loose.',
            'Workable address — drivable, with a small risk of mis-routing.',
            'Mid-quality location data — verify before sending a crew.',
        ),
        'low': (
            'Street-only or city-only address — hard to route precisely.',
            'Sparse location data — you will have to call to confirm where to go.',
            'Address is thin — fine for cold outreach, not for dispatch.',
        ),
        'dead': (
            'No usable address on the permit.',
            'Location block is empty — there is nowhere to send a truck.',
            'Address data missing entirely — only the parties are identifiable.',
        ),
    },
    'data_confidence': {
        # data_confidence is a meta-signal — only surface it as a
        # caveat when extraction was actually weak. Empty string for
        # high/good means compose_reasoning naturally skips it.
        'high': ('', '', ''),
        'good': ('', '', ''),
        'mid':  (
            'Heads-up — a couple of fields had to be inferred.',
            'Note — minor gaps in the source page, double-check before final outreach.',
            'Caveat — extraction was good but not exhaustive.',
        ),
        'low': (
            'Heads-up — several fields were missing from the source page.',
            'Note — the permit page was thin, verify the details before pitching.',
            'Caveat — extraction confidence is low, treat the breakdown as approximate.',
        ),
        'dead': (
            'Heads-up — source page was nearly empty, double-check everything.',
            'Note — extraction failed on most fields, this row is mostly inference.',
            'Caveat — almost nothing usable was on the source page.',
        ),
    },
}


def _pick(variants: tuple, seed: str) -> str:
    """Deterministically pick a variant from a tuple. The seed is
    typically the permit number plus the dimension key, so a permit
    consistently renders the same sentence on every re-score (no
    flicker in the UI) while different permits in the same bucket
    naturally rotate through the variants.

    Empty-string variants (used for data_confidence's high/good
    buckets where we want silence) are passed through untouched."""
    if not variants:
        return ''
    h = hashlib.blake2s(seed.encode('utf-8'), digest_size=4).digest()
    idx = int.from_bytes(h, 'big') % len(variants)
    return variants[idx]


def grade_for(score: Optional[int]) -> Optional[str]:
    """ai_score → letter grade. Matches the prompt's old rubric so the
    UI thresholds (badge colors, A-list filters) keep working."""
    if score is None:
        return None
    if score >= 90: return 'A'
    if score >= 85: return 'A-'
    if score >= 80: return 'B+'
    if score >= 75: return 'B'
    if score >= 70: return 'B-'
    if score >= 65: return 'C+'
    if score >= 60: return 'C'
    if score >= 55: return 'C-'
    if score >= 50: return 'D+'
    if score >= 45: return 'D'
    return 'F'


def tier_for(score: Optional[int]) -> Optional[str]:
    """ai_score → tier. Same buckets the dashboard hot/warm/cool filters
    have always used."""
    if score is None:
        return None
    if score >= 80: return 'hot'
    if score >= 60: return 'warm'
    return 'cool'


def composite_score(subs: Optional[dict]) -> Optional[int]:
    """Weighted sum of the 8 in-composite sub-scores with null
    renormalisation, then a 15% shrink if data_confidence is weak,
    then clamp 0-100. Mirrors the formula the prompt used to ask the
    LLM to apply — now done deterministically and for free."""
    if not isinstance(subs, dict):
        return None
    have, total_weight, weighted = False, 0.0, 0.0
    for k, w in WEIGHTS.items():
        v = subs.get(k)
        if v is None:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        weighted += iv * w
        total_weight += w
        have = True
    if not have or total_weight <= 0:
        return None
    score = weighted / total_weight
    dc = subs.get('data_confidence')
    try:
        if dc is not None and int(dc) < 50:
            score *= 0.85
    except (TypeError, ValueError):
        pass
    return max(0, min(100, round(score)))


def compose_subscore_reasons(subs: Optional[dict],
                             seed: str = '') -> Optional[dict]:
    """Return {dimension: phrase} for every sub-score the model gave
    us. Used to populate ``permits.raw.ai_subscore_reasons`` so the
    detail-modal renderer needs zero changes. The seed (typically the
    permit number) ensures the same permit always renders the same
    variant — re-scoring never flips the wording on the user."""
    if not isinstance(subs, dict):
        return None
    out = {}
    for k in WEIGHTS.keys():
        v = subs.get(k)
        bk = _bucket(v if isinstance(v, int) else None)
        if not bk:
            out[k] = None
            continue
        variants = PHRASES.get(k, {}).get(bk, ())
        phrase = _pick(variants, f'{seed}|{k}')
        out[k] = phrase or None
    # data_confidence rendered too, but only the mid/low/dead buckets
    # produce a phrase (high/good buckets are intentionally empty).
    dc = subs.get('data_confidence')
    bk = _bucket(dc if isinstance(dc, int) else None)
    if bk:
        variants = PHRASES['data_confidence'].get(bk, ())
        phrase = _pick(variants, f'{seed}|data_confidence')
        out['data_confidence'] = phrase or None
    else:
        out['data_confidence'] = None
    return out


def compose_reasoning(subs: Optional[dict],
                      seed: str = '',
                      max_chars: int = 240) -> str:
    """Pick the most informative 1-3 phrases and join them into the
    final ``ai_reasoning`` headline.

    Strategy:
      1. Headline = phrase for the highest non-null sub-score (the
         strongest reason this lead matters).
      2. If any sub-score is <35 (a real weakness), add its phrase as
         the caveat — that's the "but" the reader needs.
      3. Otherwise add a second positive phrase from the next-highest
         sub-score so the headline doesn't read one-dimensional.
      4. If data_confidence is low, append the confidence caveat.
      5. Stop at ``max_chars`` so we never blow the column width.

    The seed (permit number) deterministically selects which variant
    of each bucket's phrase tuple to use, so the same permit always
    produces the same sentence on every re-score.
    """
    if not isinstance(subs, dict):
        return ''
    rated = []
    for k in WEIGHTS.keys():
        v = subs.get(k)
        if isinstance(v, int):
            rated.append((k, v))
    if not rated:
        return ''
    rated.sort(key=lambda kv: kv[1], reverse=True)
    parts = []

    def _phrase_for(dim_key, score):
        bk = _bucket(score)
        if not bk:
            return ''
        variants = PHRASES.get(dim_key, {}).get(bk, ())
        return _pick(variants, f'{seed}|{dim_key}')

    # Headline — strongest sub-score.
    top_key, top_score = rated[0]
    headline = _phrase_for(top_key, top_score)
    if headline:
        parts.append(headline)
    # Caveat — weakest sub-score if it is genuinely weak.
    lowest_key, lowest_score = rated[-1]
    if lowest_score < 35 and lowest_key != top_key:
        caveat = _phrase_for(lowest_key, lowest_score)
        if caveat and caveat not in parts:
            parts.append(caveat)
    elif len(rated) > 1:
        # No real weakness — add second positive for texture.
        second_key, second_score = rated[1]
        second = _phrase_for(second_key, second_score)
        if second and second not in parts:
            parts.append(second)
    # data_confidence caveat — only mid/low/dead buckets emit text.
    dc = subs.get('data_confidence')
    if isinstance(dc, int):
        bk = _bucket(dc)
        if bk:
            variants = PHRASES['data_confidence'].get(bk, ())
            dc_phrase = _pick(variants, f'{seed}|data_confidence')
            if dc_phrase and dc_phrase not in parts:
                parts.append(dc_phrase)
    out = ' '.join(parts).strip()
    if len(out) > max_chars:
        out = out[:max_chars - 1].rstrip() + '…'
    return out


def normalise_short_keys(d: Optional[dict]) -> Optional[dict]:
    """Accept either the new compact ``{"lq":80,"ur":75,...}`` shape
    OR the legacy long-key shape ``{"lead_quality":80,...}`` and
    return the long-key version. Lets us roll the prompt change out
    without breaking on cached responses or model drift."""
    if not isinstance(d, dict):
        return None
    out = {}
    for k, v in d.items():
        long_key = SHORT_KEYS.get(k, k)
        if long_key in WEIGHTS or long_key == 'data_confidence':
            out[long_key] = v
    return out or None
