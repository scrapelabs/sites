"""Server-side mirror of the 12-factor permit-scoring formula that
lives in ``templates/core/base.html`` (``aiFactors`` +
``computeOverallScore``).

Why this file exists
--------------------
The customer-facing score the user sees in the /permits/ table ring and
in the row-detail modal is the *equal-weighted mean* of 12 deterministic
factors derived from permit fields (status, trade, description, dates,
phone/email, owner, city). The DB column ``ai_score`` — set by Claude
during ingest — is ignored on the read path because the LLM was
occasionally returning 100 for permits whose heuristic sub-factors
averaged ~40 (mismatch reported by user; see ``computeOverallScore``
docstring in base.html for the full history).

That worked fine for *display*, but the /permits/ DataTable's tier
filter (Hot 80+ / Warm 60-79 / Cool <60) and the score-range slider
were still filtering server-side against the raw DB ``ai_score``. So
clicking "Hot 80+" hid leads displayed as 90 (DB said 65) and showed
leads displayed as 55 (DB said 88). User correctly flagged this as
"filters not working properly".

This module mirrors the JS verbatim so the server can filter, sort,
and CSV-export against the SAME score the user sees in the ring.

Maintenance contract
--------------------
Keep this file in lock-step with the JS in ``templates/core/base.html``.
Any change to the factor weights, regex patterns, or status/trade
buckets there MUST be mirrored here (and vice versa). Pure-function
design — no side effects, no DB access — makes it trivial to unit-test
both implementations against the same fixture and assert they agree.

Output range matches the JS: score is an int clamped to [0, 100];
grade is one of A/B/C/D/F using the same 90/80/70/60 thresholds.
"""
from __future__ import annotations

import re
from datetime import date


# Presentation-lift constants — mirror of SCORE_LIFT_* in the aiFactors()
# JS in templates/core/base.html. Keep in lock-step. Contractors read
# sub-70 lead scores as "junk", so every factor is rescaled onto a
# confidence-building band via a single AFFINE transform
# (score' = FLOOR + SLOPE*score). Affine => the equal-weight composite
# equals the same transform of the raw mean, so the server score still
# matches the ring + per-factor breakdown the client renders.
SCORE_LIFT_FLOOR = 55
SCORE_LIFT_SLOPE = 0.45


# Regexes compiled once at import — the score derivation runs against
# potentially tens of thousands of rows per page load (agency users
# with 15 cities × 90-day history), so the per-row cost matters.
# Patterns mirror the JS RegExp literals in ``aiFactors`` exactly.
_RE_COMMERCIAL_DESC = re.compile(r'commercial|retail|office|industrial|sqft|sq ft|5,000|10,000')
_RE_COMMERCIAL_OWNER = re.compile(r'llc|corp|group|park|properties|retail')
_RE_FULLSCOPE       = re.compile(r'full|complete|replacement|new build|new install|new service|entire|whole')
_RE_HASSIZE         = re.compile(r'\d+-panel|\d+ panel|\d+a\b|\d+-ton|\d+ ton|sq\s?ft|\d+,\d{3}')
_RE_ENTITY          = re.compile(r'llc|corp|inc\b|group|properties|partners|holdings|management|mgmt|retail|enterprises')
_RE_KW_FULL         = re.compile(r'full replacement|complete replacement|new build|new install|new service|entire roof|whole system|ground-up')
_RE_KW_SIZE         = re.compile(r'\d+-panel|\d+ panel|\d+a\b|\d+-ton|\d+ ton|sq\s?ft|\d+,\d{3}|400a|200a')
_RE_KW_TYPE         = re.compile(r'shingle|membrane|tpo|split|ductless|grid-tie|powerwall|sewer|water main|panel upgrade|ev charging|encroachment')
_RE_BIG_METRO       = re.compile(r'^(dallas|austin|houston|san antonio|fort worth|phoenix|denver|atlanta|chicago|los angeles|seattle|miami)$', re.I)
_RE_MID_MARKET      = re.compile(r'^(arlington|plano|irving|garland|lubbock|el paso|corpus christi|laredo|gilbert|scottsdale|tempe)$', re.I)


def _parse_iso(s: str | None) -> date | None:
    """Tolerant ISO-date parse — accepts 'YYYY-MM-DD' (and longer strings
    like full timestamps, ignoring the trailing chars). Returns None for
    anything unparseable so the caller can substitute a sensible default
    rather than crash on bad ingest data."""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def derive_score(p: dict, *, today: date | None = None) -> tuple[int, str]:
    """Compute the 12-factor composite score + letter grade for one
    permit-view dict (the shape produced by ``_row_to_permit_view`` in
    core/db.py).

    Required keys on ``p`` (all tolerated as missing/empty):
        status, trade, desc, phone, email, owner, city,
        issuedIso (YYYY-MM-DD), expiresIso (YYYY-MM-DD).

    ``today`` is injectable for deterministic unit tests; production
    callers omit it and get ``date.today()``.

    Returns ``(score, grade)`` where score is an int in [0, 100].
    """
    _today  = today or date.today()
    status  = (p.get('status') or '').lower()
    trade   = (p.get('trade')  or '').lower()
    desc    = (p.get('desc')   or '').lower()
    owner   = (p.get('owner')  or '').lower()
    phone   = p.get('phone') or ''
    email   = p.get('email') or ''
    city    = p.get('city')  or ''

    iss = _parse_iso(p.get('issuedIso')) or _today
    exp = _parse_iso(p.get('expiresIso')) or _today
    # JS uses ``Math.floor((today - iss) / 86400000)`` — Python's
    # date subtraction returns whole days, so the result is identical
    # (date arithmetic has no time component to floor).
    d_old  = max(0, (_today - iss).days)
    d_left = (exp - _today).days
    mo     = _today.month - 1  # JS getMonth(): 0=Jan

    scores: list[int] = []

    # 1. Permit Status
    if status == 'approved':   s1 = 100
    elif status == 'pending':  s1 = 62
    elif status == 'review':   s1 = 42
    else:                      s1 = 8
    scores.append(s1)

    # 2. Trade Demand Index
    if trade == 'roofing':      s2 = 96
    elif trade == 'solar':      s2 = 93
    elif trade == 'hvac':       s2 = 88
    elif trade == 'electrical': s2 = 70
    elif trade == 'plumbing':   s2 = 62
    else:                       s2 = 52
    scores.append(s2)

    # 3. Estimated Job Value
    is_commercial = bool(_RE_COMMERCIAL_DESC.search(desc) or _RE_COMMERCIAL_OWNER.search(owner))
    is_full_scope = bool(_RE_FULLSCOPE.search(desc))
    has_size      = bool(_RE_HASSIZE.search(desc))
    if is_commercial and is_full_scope: s3 = 97
    elif is_commercial:                 s3 = 84
    elif is_full_scope and has_size:    s3 = 88
    elif is_full_scope:                 s3 = 76
    elif has_size:                      s3 = 68
    else:                               s3 = 48
    scores.append(s3)

    # 4. Contact Completeness
    hp = bool(phone) and len(str(phone)) > 5
    he = bool(email) and '@' in str(email)
    if   hp and he: s4 = 100
    elif hp:        s4 = 68
    elif he:        s4 = 48
    else:           s4 = 10
    scores.append(s4)

    # 5. Owner Accessibility
    is_entity = bool(_RE_ENTITY.search(owner))
    if not is_entity and hp and he:    s5 = 100
    elif not is_entity and hp:         s5 = 82
    elif not is_entity:                s5 = 64
    elif is_entity and hp and he:      s5 = 52
    elif is_entity:                    s5 = 35
    else:                              s5 = 45
    scores.append(s5)

    # 6. Lead Freshness
    if   d_old <= 1:  s6 = 100
    elif d_old <= 3:  s6 = 85
    elif d_old <= 7:  s6 = 68
    elif d_old <= 14: s6 = 42
    elif d_old <= 30: s6 = 24
    else:             s6 = 10
    scores.append(s6)

    # 7. Permit Expiry Window
    if   d_left < 0:    s7 = 0
    elif d_left >= 150: s7 = 88
    elif d_left >= 90:  s7 = 74
    elif d_left >= 30:  s7 = 52
    elif d_left >= 7:   s7 = 28
    else:               s7 = 12
    scores.append(s7)

    # 8. Competition Risk
    comp_base = 20 if d_old <= 2 else 45 if d_old <= 7 else 65 if d_old <= 14 else 80
    trade_comp = {'roofing': 15, 'solar': 10, 'hvac': 12, 'electrical': 8,
                  'plumbing': 10, 'civil': 5}.get(trade, 8)
    comp_risk = min(100, comp_base + trade_comp)
    s8 = max(5, 100 - comp_risk)
    scores.append(s8)

    # 9. Seasonal Demand Fit (JS month buckets: spring=2-4, summer=5-7,
    # fall=8-10, winter=11 or 0-1)
    spring = 2 <= mo <= 4
    summer = 5 <= mo <= 7
    fall   = 8 <= mo <= 10
    winter = mo == 11 or mo <= 1
    if trade == 'roofing':
        s9 = 100 if spring else 90 if summer else 72 if fall else 45
    elif trade == 'solar':
        s9 = 95 if (spring or summer) else 70 if fall else 52
    elif trade == 'hvac':
        if spring:   s9 = 92
        elif fall:   s9 = 88
        elif summer: s9 = 65
        else:        s9 = 58
    elif trade == 'electrical':
        s9 = 72
    elif trade == 'plumbing':
        s9 = 80 if winter else 65
    else:
        s9 = 82 if (spring or summer) else 55
    scores.append(s9)

    # 10. Project Scope Clarity
    clarity = (30 if _RE_KW_FULL.search(desc) else 0) + \
              (35 if _RE_KW_SIZE.search(desc) else 0) + \
              (25 if _RE_KW_TYPE.search(desc) else 0) + 10
    s10 = min(100, clarity)
    scores.append(s10)

    # 11. Market Saturation
    big_metro  = bool(_RE_BIG_METRO.match(city))
    mid_market = bool(_RE_MID_MARKET.match(city))
    if   big_metro and trade == 'roofing': s11 = 48
    elif big_metro:                        s11 = 55
    elif mid_market:                       s11 = 72
    else:                                  s11 = 85
    scores.append(s11)

    # 12. Outreach Conversion Probability (weighted blend of factors
    # 1, 4, 5, 6, 8 — mirrors the JS literal exactly)
    base_conv = (s1 * 0.25) + (s4 * 0.20) + (s5 * 0.15) + (s6 * 0.20) + (s8 * 0.20)
    # int(v + 0.5) == JS Math.round for the non-negative value here (base_conv
    # is clamped to >=5). Python's built-in round() uses banker's rounding,
    # which disagrees with the JS twin by 1 on .5 ties and can cross a tier
    # boundary — the exact client/server drift this mirror file exists to avoid.
    s12 = int(min(100, max(5, base_conv)) + 0.5)
    scores.append(s12)

    # Presentation lift — mirror of the per-factor lift loop in the
    # aiFactors() JS. int(v + 0.5) matches JS Math.round for the
    # non-negative values produced here, keeping client/server in sync.
    scores = [max(0, min(100, int(SCORE_LIFT_FLOOR + SCORE_LIFT_SLOPE * s + 0.5)))
              for s in scores]

    # Composite: equal-weight mean, rounded, clamped. Matches JS:
    # ``Math.round(sum / factors.length)`` then ``Math.max(0, Math.min(100, avg))``.
    # int(v + 0.5) mirrors JS Math.round for the non-negative mean (vs Python
    # round()'s banker's rounding, which would drift from the client by 1).
    avg = int(sum(scores) / len(scores) + 0.5)
    score = max(0, min(100, int(avg)))
    grade = ('A' if score >= 90 else
             'B' if score >= 80 else
             'C' if score >= 70 else
             'D' if score >= 60 else 'F')
    return score, grade
