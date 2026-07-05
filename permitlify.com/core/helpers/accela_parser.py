"""
Accela Permit Parser — verbatim copy of the user's reference parser.

Source of truth lives in this file. The runtime scrapers
(``core/scrapers/base.py`` and ``core/scraper_accela.py``) import
``_VisibleTextExtractor``, ``_NOISE_LINES``, ``extract_permit_text``,
``record_number_from_url``, ``clean_json``, and ``SYSTEM_PROMPT`` from
here so any change to the standalone reference is automatically picked
up by production. Per the project rule:

    "USE HIS REFERENCE PARSER VERBATIM" — and the user rule "don't
    reinvent — put helpers in helpers".

DO NOT "improve" this file. If a behaviour change is needed, agree it
with the user, change the standalone reference at
``attached_assets/accela_parser_test_*.py`` first, then mirror the
edit here so the two stay in lock-step.

The original reference file is preserved at
``attached_assets/accela_parser_test_1777840057046.py`` for diff
auditing.

Two pieces of the original reference (``DO_API_KEY`` module constant
and the ``parse_permit`` / ``main`` entry points) are intentionally
omitted from re-export — production reads ``DO_API_KEY`` via
``core.scrapers.base._do_api_key`` (system_setting → env fallback) and
calls the LLM via ``oss_complete`` so the same usage logging /
per-run cost accounting / cancel-handling applies. The text-cleaning
pipeline and the prompt below ARE imported and used verbatim.
"""

# ── Original reference parser (verbatim) ───────────────────────────────────────
# Everything from the next line down to the matching "── End of verbatim
# reference ──" comment is a 1:1 copy of attached_assets/
# accela_parser_test_1777840057046.py. Keep this block byte-identical
# to the reference except for:
#   * DO_API_KEY default literal redacted (production reads via
#     core.scrapers.base._do_api_key — system_setting → env). The
#     constant remains for any future direct caller.
#   * Module imports trimmed to those actually used by re-exported
#     symbols (no `sys`, no `from openai import OpenAI`) so importing
#     this module never pulls in the OpenAI SDK at Django startup.
import os
import re
from html.parser import HTMLParser

# ── Config ─────────────────────────────────────────────────────────────────────

DO_API_KEY  = os.getenv("DO_API_KEY", "")
DO_BASE_URL = "https://inference.do-ai.run/v1"
MODEL_ID    = "openai-gpt-oss-20b"
MAX_TOKENS  = 2000

# ── HTML → clean visible text ──────────────────────────────────────────────────

# UI chrome that adds zero permit information — strip these lines after tag removal
_NOISE_LINES = {
    '☰', 'Login', 'Register for an Account', '|', 'Recent Searches:',
    'No recent searches available', 'Home', 'Building Permits',
    'Property Maintenance', 'License', 'Planning',
    'Search for a Building Permit.', 'Message Bar',
    'Create a New Collection', '*', 'Name:', 'Description:', 'Add', 'Cancel',
    'Record Info', 'Permit Details', 'Permit Status', 'Attachments',
    'Inspections', 'Payments', 'Fees', 'More Details',
    'Application Information', 'Loading...',
    'Upcoming', 'You have not added any inspections.',
    'Completed', 'There are no completed inspections on this record.',
    'The maximum file size allowed is', '1000 MB', '.',
    'View People Attachments', 'View Record Attachments',
    'If you can see this text, your browser does not support iframes.',
    'View the content of this inline frame', 'within your browser.',
    'Save', 'Sorry, your browser does not support iframes; try a browser that supports W3 standards.',
    'Remove All', 'Custom Component',
    'No ROWM data available at this time.',
    'Type', 'Contact', 'Start Date', 'End Date', 'ROWM Website',
    'Right Of Way Management',
    'Intake Review', 'Building Review', 'Planning Review',
    'Public Works Review', 'Issue Permits', 'Inspection',
    'Certificate of Occupancy', 'Parcel Information as of Case Date',
    'No records found.', 'TBD',
    'are disallowed file types to upload.',
    # fragment lines produced by Accela's status block
    ', assigned to', 'Marked as', 'on', 'by',
}


class _VisibleTextExtractor(HTMLParser):
    """Strip all tags; collect only visible text nodes."""
    def __init__(self):
        super().__init__()
        self._lines: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "head"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "head"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            t = data.strip()
            if t:
                self._lines.append(t)

    def get_lines(self) -> list[str]:
        return self._lines


def extract_permit_text(html: str) -> str:
    """
    Convert raw Accela HTML to a compact, clean text block the model can read.

    Strategy:
      1. Strip all tags via HTMLParser (keeps only visible text nodes)
      2. Drop known UI-chrome lines (_NOISE_LINES)
      3. Drop blank / single-char lines
      4. Collapse the result — typically ~300-700 chars vs 80k+ for raw HTML
    """
    p = _VisibleTextExtractor()
    p.feed(html)

    filtered = []
    for line in p.get_lines():
        if line in _NOISE_LINES:
            continue
        if len(line) <= 1:
            continue
        filtered.append(line)

    return "\n".join(filtered)


# ── Extraction prompt ──────────────────────────────────────────────────────────

from pathlib import Path as _Path
_PROMPT_FILE = _Path(__file__).with_name("accela_parser_prompt.txt")
SYSTEM_PROMPT = _PROMPT_FILE.read_text(encoding="utf-8")


# ── Helpers ────────────────────────────────────────────────────────────────────

def record_number_from_url(url: str) -> str:
    """Extract capID1-capID2-capID3 from Accela detail URL."""
    if not url:
        return ""
    m = re.search(r'capID1=(\d+)&capID2=(\d+)&capID3=(\d+)', url, re.IGNORECASE)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def clean_json(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    return text.strip()


# ── End of verbatim reference ─────────────────────────────────────────────────


# ── Free parser (token-saving fast path) ──────────────────────────────────────
#
# Accela permit detail pages follow a fairly consistent visual layout no
# matter which agency hosts the deployment. ``parse_accela_detail`` walks
# the cleaned visible-text representation that ``_http_fetch_page`` already
# produces and pulls out the structured permit fields with regex + section
# matching — no LLM round-trip. The scraper pipeline calls this first and
# only falls back to Claude/OSS inference when contractor email AND phone
# are both missing (the two leadgen-critical fields). For typical Accela
# pages with a populated Applicant + Contractor block this skips Claude
# entirely, saving ~60% of inference tokens.
#
# Patterns the parser targets (these are the lines Accela markdown
# consistently emits for every record we've audited):
#   * Record number      —  "Record SF-CON-2024-12345:" / "Record BLD24-..."
#   * Permit type        —  the heading line right after the record number
#   * Status             —  "Record Status: Issued" / "Application Status:"
#   * Date Applied/Issued/Expires — "Date Applied:" / "Date Issued:"
#   * Project Description —  "Project Description:\n<text>"
#   * Work Location block — "Work Location:\n<addr line>\n<city>, <ST> <zip>"
#   * Contact blocks      — "Applicant:" / "Contractor:" / "Owner:" headers
#                           followed by name, address, phone lines, email
#
# Contact selection mirrors the Claude prompt's rule
# (Applicant > Contractor > Owner takes the first party with a reachable
# phone+email pair), so swapping to the free parser does not regress lead
# quality on contractor_email / contractor_phone.

import re as _re

# US phone — matches "(602) 555-1234", "602-555-1234", "602.555.1234",
# "+1 602 555 1234" and the bare "6025551234" Accela stores. The
# separators are space/tab/dot/dash only (NOT "\s") so a match can never
# span a newline — that was producing bogus numbers by gluing a trailing
# digit run on one line ("312995") to the street number on the next
# ("2121 N CALIFORNIA BLVD" -> "(312) 995-2121"). NANP rules (area and
# exchange must start 2-9) plus the leading look-behind reject parcel
# numbers, tax-map ids and dates that would otherwise look phone-shaped.
_PHONE_RX = _re.compile(
    r"""(?x)
    (?<!\d)                  # not glued to a preceding digit run
    (?:\+?1[ \t.\-]?)?       # optional country code
    \(?([2-9]\d{2})\)?       # area  (NANP: leading 2-9)
    [ \t.\-]?([2-9]\d{2})    # exch  (NANP: leading 2-9)
    [ \t.\-]?(\d{4})         # subs
    (?!\d)                   # not part of a longer digit run
    """
)
_EMAIL_RX = _re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)
_DATE_RX = _re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
_ZIP_RX  = _re.compile(r"\b([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b")
_RECORD_RX = _re.compile(r"Record\s+([A-Z0-9][A-Z0-9\-_/]{3,40}):", _re.I)
_MONEY_RX = _re.compile(
    r"(?<![\w.])\$?\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.([0-9]{1,2}))?(?![\w.])"
)

# Section headers we split the cleaned text on. Order matters for the
# "contact priority" selection below.
_SECTION_HEADERS = [
    'Licensed Professional', 'Applicant', 'Contractor',
    'Related Contacts', 'Additional Contact Information', 'Additional Contact',
    'Owner', 'Property Owner Info', 'Property Owner', 'Additional Information',
    'Project Information', 'Project Description', 'Work Location', 'Address',
    'Parcel Information', 'License Information',
]
# Contact-block headers, in the priority order we trust them for the
# *contractor* fields. Accela deployments vary: older skins use
# "Applicant:" / "Contractor:" blocks, newer ones use a
# "Licensed Professional:" block (sometimes with a separate
# "Additional Contact Information" / "Related Contacts" block that holds
# the reachable phone+email). Owner blocks feed owner_name only and are
# NEVER used for the contractor_* fields — we expose contractor contact
# only, never the homeowner.
_CONTRACTOR_HEADERS = (
    'Applicant', 'Contractor', 'Licensed Professional',
    'Additional Contact Information', 'Additional Contact', 'Related Contacts',
)
_OWNER_HEADERS = ('Owner', 'Property Owner Info', 'Property Owner')
# Back-compat alias (kept for any external importer of this module).
_CONTACT_HEADERS = _CONTRACTOR_HEADERS + _OWNER_HEADERS
# Field-label words that must never be mistaken for an entity name or a
# record id (they leak in from Accela's "Related Records" tree, whose
# column headers are "Owner / Email / Address / Status / Type / Date").
_CONTACT_LABEL_WORDS = {
    'Email', 'Address', 'Status', 'Type', 'Date', 'Phone', 'Name',
    'Contact', 'Owner', 'Record', 'Number', 'Additional Information',
    'Project Information', 'Parcel Information', 'License Information',
}
# An id-shaped token (permit/record number) — used to grab the value on
# the line *after* a lone "Record" / "Record No" header.
_RECORD_ID_RX = _re.compile(r'^[A-Z0-9][A-Z0-9\-_/]{3,40}$', _re.I)
# Relationship/role markers that flag a contact entry as the OWNER /
# homeowner. Defence-in-depth guard for the now-primary parser: a
# *multi-party* block (Related Contacts / Additional Contact) must never
# feed the contractor_* fields from an owner-labelled entry. Applicant /
# Contractor / Licensed Professional blocks are deliberately NOT gated
# this way — on owner-builder permits those legitimately carry the owner
# as the permit-puller, which is what the LLM extracts as the contractor
# too, so gating them would both regress parity and drop the only contact.
_OWNER_ROLE_RX = _re.compile(
    r'^\s*(?:property\s+owner|home\s*owner|owner\s*[/\-]\s*builder|owner)\s*:?\s*$',
    _re.I,
)
# Headers that can list several parties in one block (so an owner could
# appear alongside the contractor) — only these are owner-role gated.
_MULTIPARTY_HEADERS = (
    'Related Contacts', 'Additional Contact Information', 'Additional Contact',
)
_BUSINESS_NAME_TOKENS = {
    'llc', 'l.l.c', 'inc', 'inc.', 'incorporated', 'corp', 'corp.',
    'corporation', 'co', 'co.', 'company', 'ltd', 'lp', 'llp', 'pllc',
    'group', 'partners', 'construction', 'contractors', 'contractor',
    'contracting', 'builders', 'building', 'plumbing', 'electric',
    'electrical', 'hvac', 'mechanical', 'heating', 'cooling', 'roofing',
    'remodeling', 'services', 'service', 'solar', 'restoration', 'design',
    'development', 'homes', 'properties', 'maintenance', 'energy',
    'sunrun',
}


def _normalize_phone(match) -> str:
    """Format a 10-digit phone tuple as ``(NNN) NNN-NNNN``."""
    a, b, c = match.group(1), match.group(2), match.group(3)
    return f"({a}) {b}-{c}"


def _first_phone(text: str) -> str:
    m = _PHONE_RX.search(text or '')
    return _normalize_phone(m) if m else ''


def _first_email(text: str) -> str:
    m = _EMAIL_RX.search(text or '')
    return m.group(0) if m else ''


def _to_iso_date(s: str) -> str:
    """Convert MM/DD/YYYY or M/D/YY to YYYY-MM-DD. Returns '' on no match."""
    if not s:
        return ''
    m = _DATE_RX.search(s)
    if not m:
        return ''
    mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100:
        yr += 2000 if yr < 70 else 1900
    if not (1 <= mo <= 12 and 1 <= da <= 31 and 1900 <= yr <= 2100):
        return ''
    return f"{yr:04d}-{mo:02d}-{da:02d}"


def _money_to_cents(s: str) -> int:
    """Convert an Accela money string like ``$3,500.00`` to cents."""
    m = _MONEY_RX.search(s or '')
    if not m:
        return 0
    dollars = int((m.group(1) or '0').replace(',', ''))
    cents_text = (m.group(2) or '').ljust(2, '0')[:2]
    cents = int(cents_text or '0')
    return max(0, dollars * 100 + cents)


def _money_after_label(lines: list[str], label_regex: str,
                       within: int = 4, require_colon: bool = False) -> int:
    """Find a money value on/after a labelled Accela field line."""
    colon = r"\s*:\s*" if require_colon else r"\s*:?\s*"
    rx = _re.compile(rf"^(?:{label_regex}){colon}(.*)$", _re.I)
    for i, line in enumerate(lines):
        m = rx.match((line or '').strip())
        if not m:
            continue
        tail = (m.group(1) or '').strip()
        cents = _money_to_cents(tail)
        if cents:
            return cents
        for j in range(i + 1, min(i + 1 + within, len(lines))):
            cents = _money_to_cents(lines[j].strip())
            if cents:
                return cents
    return 0


def _value_after(lines: list[str], label_regex: str,
                 within: int = 3) -> str:
    """Find a label line (e.g. ``Date Applied:``) and return either the
    value glued to the same line after ``:`` OR the next non-empty line.
    ``within`` bounds how many lines forward we'll scan."""
    rx = _re.compile(rf"^(?:{label_regex})\s*:?\s*(.*)$", _re.I)
    for i, line in enumerate(lines):
        m = rx.match(line.strip())
        if not m:
            continue
        tail = (m.group(1) or '').strip()
        if tail:
            return tail
        # Look at the next ``within`` lines for the value.
        for j in range(i + 1, min(i + 1 + within, len(lines))):
            cand = lines[j].strip()
            if cand:
                return cand
    return ''


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    """Partition the cleaned text into ``{section_header: [body lines]}``.
    A section runs from its header to the next header we recognise (or
    EOF). Headers may end with ``:`` or stand alone on their own line.
    """
    header_rx = _re.compile(
        r"^\s*(" + "|".join(_re.escape(h) for h in _SECTION_HEADERS) +
        r")(?:\s+(?:Info|Information|Details))?\s*:?\s*$", _re.I,
    )
    sections: dict[str, list[str]] = {}
    current = ''
    for raw in lines:
        line = (raw or '').strip()
        if not line:
            continue
        m = header_rx.match(line)
        if m:
            current = m.group(1).title()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return sections


def _parse_address_block(lines: list[str]) -> dict:
    """Pull street/city/state/zip out of an address-shaped sequence of
    lines. We look for the first ``CITY, ST ZIP`` line and treat the
    preceding line as the street.
    """
    out = {'address': '', 'city': '', 'state': '', 'zip': ''}
    for i, line in enumerate(lines):
        m = _ZIP_RX.search(line)
        if not m:
            continue
        st, zp = m.group(1), m.group(2)
        # The city is the part before the comma on this line (or the line
        # itself if no comma).
        before = line[:m.start()].rstrip().rstrip(',').strip()
        city = before
        if ',' in before:
            # Sometimes the line is "Street, City, ST ZIP" all on one row.
            parts = [p.strip() for p in before.split(',') if p.strip()]
            if len(parts) >= 2:
                city = parts[-1]
                out['address'] = ', '.join(parts[:-1])
        # Street usually lives on the preceding line.
        if not out['address'] and i > 0:
            out['address'] = lines[i - 1].strip()
        out['city'], out['state'], out['zip'] = city, st, zp
        return out
    return out


def _parse_contact_block(body: list[str], *, prefer_business: bool = True) -> dict:
    """Pull name + phone + email from a contact-section body."""
    text = '\n'.join(body)
    out = {'name': '', 'phone': _first_phone(text), 'email': _first_email(text)}
    header_words = {h.lower() for h in _SECTION_HEADERS}

    def _valid_name_line(line: str) -> str:
        clean_line = (line or '').strip()
        if not clean_line:
            return ''
        if clean_line.lower().rstrip(':') in header_words:
            return ''
        if clean_line.title() in _CONTACT_LABEL_WORDS:
            return ''
        if ':' in clean_line:
            return ''
        if _PHONE_RX.search(clean_line) or _EMAIL_RX.search(clean_line):
            return ''
        if _ZIP_RX.search(clean_line):
            return ''
        # Skip obvious address fragments (start with a number).
        if clean_line[:1].isdigit():
            return ''
        return clean_line

    def _looks_business_name(line: str) -> bool:
        low = (line or '').lower()
        if '&' in low or any(ch.isdigit() for ch in low):
            return True
        words = [w.strip('.,') for w in _re.split(r'[\s,]+', low) if w.strip('.,')]
        return any(w in _BUSINESS_NAME_TOKENS for w in words)

    candidates = []
    for line in body:
        candidate = _valid_name_line(line)
        if candidate:
            candidates.append(candidate)

    # Applicant/LP blocks commonly render as:
    #   Firstname\nLastname\nBusiness Inc\nAddress...
    # For contractor lead quality, prefer the business line over the
    # person's first name. This avoids false homeowner/private-name drops.
    if prefer_business:
        for candidate in candidates:
            if _looks_business_name(candidate):
                out['name'] = candidate[:200]
                return out

    if candidates:
        first = candidates[0]
        if len(candidates) >= 2:
            # If Accela split a person's first/last names across lines,
            # preserve both so the save gate does not see a one-word person.
            if (_re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", first)
                    and _re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", candidates[1])):
                out['name'] = f'{first} {candidates[1]}'[:200]
                return out
        out['name'] = first[:200]
    return out


def parse_accela_detail(text: str, source_url: str = '') -> dict:
    """Parse an Accela CapDetail page into the same dict shape as
    ``claude_extract``. Returns an empty dict if the input is unusably
    short. Callers should treat missing fields as ``''`` / ``None`` —
    the scraper will fall back to LLM inference when the two leadgen-
    critical fields (``contractor_email`` and ``contractor_phone``) are
    both empty.
    """
    raw = (text or '').strip()
    if len(raw) < 40:
        return {}
    lines = [ln for ln in raw.splitlines() if ln.strip()]

    out: dict = {
        'permit_number': '', 'permit_type': '', 'description': '',
        'status': '',
        'applied_date': '', 'issued_date': '', 'expires_date': '',
        'address': '', 'city': '', 'state': '', 'zip': '',
        'owner_name': '', 'contractor_name': '',
        'contractor_phone': '', 'contractor_email': '',
        'trade': '', 'valuation_cents': 0, 'square_feet': 0,
    }

    # Record number. Two Accela layouts:
    #   (a) inline  — "Record SF-CON-2024-12345:"            (older skins)
    #   (b) stacked — a lone "Record" / "Record No" header line
    #                  followed by the id on the next line, no colon
    #                  (newer skins). The inline regex must NOT match
    #                  field labels like "Record Status:" — a real record
    #                  id always contains a digit.
    for line in lines:
        m = _RECORD_RX.search(line)
        if m:
            cand = m.group(1).strip()
            if _re.search(r'\d', cand) and cand.title() not in _CONTACT_LABEL_WORDS:
                out['permit_number'] = cand
                break
    if not out['permit_number']:
        for i, line in enumerate(lines):
            if _re.match(r'^\s*Record(?:\s+(?:No|Number|#))?\s*:?\s*$', line, _re.I):
                for j in range(i + 1, min(i + 3, len(lines))):
                    cand = lines[j].strip()
                    if _RECORD_ID_RX.match(cand) and _re.search(r'\d', cand):
                        out['permit_number'] = cand
                        break
                if out['permit_number']:
                    break
    if not out['permit_number']:
        # Some Accela skins render the record id as a standalone line near
        # the top (for example: "Building" / "26BC12056" / "RES Reroof")
        # without any "Record" label. Prefer the first id-shaped top-line
        # token before falling back to the URL composite.
        for cand in lines[:80]:
            value = cand.strip()
            if (_RECORD_ID_RX.match(value)
                    and _re.search(r'\d', value)
                    and value.title() not in _CONTACT_LABEL_WORDS):
                out['permit_number'] = value
                break
    if not out['permit_number']:
        # URL composite is the documented fallback.
        out['permit_number'] = record_number_from_url(source_url)

    # Status
    status = _value_after(lines, r"(?:Record|Application)\s+Status")
    if status:
        # Strip Accela's "assigned to / on / by" suffix the prompt also drops.
        status = _re.split(r",|assigned to|on \d|by ", status)[0].strip()
        out['status'] = status[:80]

    # Dates
    out['applied_date']  = _to_iso_date(_value_after(lines, r"Date\s+Applied|Application\s+Date|Applied\s+Date"))
    out['issued_date']   = _to_iso_date(_value_after(lines, r"Date\s+Issued|Issue\s+Date|Issued\s+Date"))
    out['expires_date']  = _to_iso_date(_value_after(lines, r"Expir(?:e|ation)\s+Date|Date\s+Expires"))

    # Description
    desc = _value_after(lines, r"(?:Project\s+)?Description|Project\s+Name")
    if desc:
        out['description'] = desc[:500]

    out['valuation_cents'] = _money_after_label(
        lines,
        r"Job\s+Value(?:\s*\(\$\))?|Project\s+Value|Declared\s+Value",
    )
    if not out['valuation_cents']:
        out['valuation_cents'] = _money_after_label(
            lines, r"Valuation", require_colon=True,
        )

    # Sections (Work Location / contact blocks)
    sections = _split_sections(lines)

    # Address — prefer Work Location, fall back to scanning all lines.
    for hdr in ('Work Location', 'Address'):
        if hdr in sections:
            addr = _parse_address_block(sections[hdr])
            if addr.get('zip'):
                out.update({k: v for k, v in addr.items() if v})
                break
    if not out['zip']:
        addr = _parse_address_block(lines)
        out.update({k: v for k, v in addr.items() if v})

    # Contacts. Contractor fields come from contractor-type blocks in the
    # priority order of _CONTRACTOR_HEADERS: first the block that has BOTH
    # a reachable phone and email, else the first with either. Owner /
    # Property-Owner blocks feed owner_name ONLY — we never expose the
    # homeowner's contact as the contractor.
    contractor_contacts: dict[str, dict] = {}
    for hdr in _CONTRACTOR_HEADERS:
        if hdr not in sections:
            continue
        body = sections[hdr]
        # Defence-in-depth: never let an owner-labelled entry inside a
        # multi-party block become the contractor. Route its name to
        # owner_name (if not already set) and skip it for contractor
        # selection. Single-party Applicant/Contractor/LP blocks are not
        # gated (see _OWNER_ROLE_RX note re: owner-builder permits).
        if hdr in _MULTIPARTY_HEADERS and any(
            _OWNER_ROLE_RX.match(ln) for ln in body
        ):
            nm = _parse_contact_block(body, prefer_business=False).get('name', '')
            if nm and not out['owner_name'] and nm.title() not in _CONTACT_LABEL_WORDS:
                out['owner_name'] = nm
            continue
        contractor_contacts[hdr] = _parse_contact_block(body)

    for hdr in _OWNER_HEADERS:
        if hdr in sections:
            nm = _parse_contact_block(sections[hdr], prefer_business=False).get('name', '')
            if nm and nm.title() not in _CONTACT_LABEL_WORDS:
                out['owner_name'] = nm
                break

    chosen = None
    for hdr in _CONTRACTOR_HEADERS:
        c = contractor_contacts.get(hdr)
        if c and c.get('phone') and c.get('email'):
            chosen = c
            break
    if chosen is None:
        # Fall back to the first contractor block with at least one field.
        for hdr in _CONTRACTOR_HEADERS:
            c = contractor_contacts.get(hdr)
            if c and (c.get('phone') or c.get('email')):
                chosen = c
                break
    if chosen:
        out['contractor_name']  = chosen.get('name', '')
        out['contractor_phone'] = chosen.get('phone', '')
        out['contractor_email'] = chosen.get('email', '')

    # Permit type — heuristic: the line immediately after the record
    # number that isn't itself a status/label line.
    for i, line in enumerate(lines):
        if _re.match(r'^\s*Record\s+Status\s*:', line, _re.I):
            continue
        if _RECORD_RX.search(line):
            for j in range(i + 1, min(i + 4, len(lines))):
                cand = lines[j].strip()
                if (cand and ':' not in cand and
                        not _DATE_RX.search(cand) and
                        not cand.lower().startswith(('record ', 'status'))):
                    out['permit_type'] = cand[:120]
                    break
            break
    if not out['permit_type'] and out.get('permit_number'):
        for i, line in enumerate(lines):
            if line.strip() != out['permit_number']:
                continue
            for j in range(i + 1, min(i + 4, len(lines))):
                cand = lines[j].strip()
                if (cand and ':' not in cand and
                        not _DATE_RX.search(cand) and
                        not cand.lower().startswith(('record ', 'status'))):
                    out['permit_type'] = cand[:120]
                    break
            break
    return out


# ── Admin-configurable per-field XPath extraction ─────────────────────────────
#
# The scraper detail page lets an admin pin an XPath to any permit field.
# At scrape time those XPaths run against the RAW HTML (before it's reduced
# to cleaned text) and the values they return are trusted FIRST — the regex
# parser fills any gaps, and the AI agent is only asked for whatever is still
# empty afterwards. ``PERMIT_FIELDS`` is the canonical, ordered list of fields
# the editor renders and the pipeline knows how to fill. Keys match the dict
# shape ``parse_accela_detail`` returns so the two layers compose cleanly.

PERMIT_FIELDS: tuple[tuple[str, str], ...] = (
    ('permit_number',    'Permit Number'),
    ('permit_type',      'Permit Type'),
    ('status',           'Status'),
    ('description',      'Description'),
    ('applied_date',     'Date Applied'),
    ('issued_date',      'Date Issued'),
    ('expires_date',     'Date Expires'),
    ('address',          'Address'),
    ('city',             'City'),
    ('state',            'State'),
    ('zip',              'Zip'),
    ('owner_name',       'Owner Name'),
    ('contractor_name',  'Contractor Name'),
    ('contractor_phone', 'Contractor Phone'),
    ('contractor_email', 'Contractor Email'),
    ('trade',            'Trade'),
    ('valuation_cents',  'Valuation (cents)'),
    ('square_feet',      'Square Feet'),
)

# Fast membership / validation set of the keys above.
PERMIT_FIELD_KEYS: frozenset[str] = frozenset(k for k, _ in PERMIT_FIELDS)


def _xpath_node_to_text(node) -> str:
    """Coerce one XPath result node to a clean string. lxml can return
    element objects (for ``//div`` style paths), attribute / text strings
    (for ``.../@class`` or ``text()`` paths), or smart-strings — handle
    all three and collapse interior whitespace."""
    if node is None:
        return ''
    # Strings (attribute values, text() nodes, smart-strings) stringify
    # directly; elements expose their visible text via .text_content().
    if isinstance(node, str):
        text = node
    else:
        tc = getattr(node, 'text_content', None)
        if callable(tc):
            try:
                text = tc()
            except Exception:
                text = ''
        else:
            text = str(node)
    return _re.sub(r'\s+', ' ', (text or '')).strip()


def extract_fields_by_xpath(raw_html: str,
                            field_xpaths: dict | None) -> dict:
    """Apply an admin-defined ``{field: xpath}`` map to raw permit HTML.

    Returns ``{field: value}`` for every field whose XPath matched at least
    one non-empty value. Fields with no configured XPath, an XPath that
    matched nothing, or an XPath that failed to compile are simply omitted
    (never raised) so a bad selector can never break a scrape — it just
    falls through to the regex parser + AI fallback like an unconfigured
    field. Only keys in ``PERMIT_FIELD_KEYS`` are honoured so the config
    can't smuggle arbitrary column names into the pipeline.
    """
    if not raw_html or not isinstance(field_xpaths, dict) or not field_xpaths:
        return {}
    # Keep only real, non-empty selectors for known fields.
    selectors = {
        f: xp.strip()
        for f, xp in field_xpaths.items()
        if f in PERMIT_FIELD_KEYS and isinstance(xp, str) and xp.strip()
    }
    if not selectors:
        return {}
    try:
        from lxml import html as _lxml_html
    except Exception:
        return {}
    try:
        tree = _lxml_html.fromstring(raw_html)
    except Exception:
        return {}

    out: dict = {}
    for field, xp in selectors.items():
        try:
            res = tree.xpath(xp)
        except Exception:
            # Invalid XPath expression — skip this field silently.
            continue
        if res is None:
            continue
        if not isinstance(res, (list, tuple)):
            # Scalar results (e.g. count(...) / string(...)).
            res = [res]
        parts = [_xpath_node_to_text(n) for n in res]
        value = ' '.join(p for p in parts if p).strip()
        if value:
            out[field] = value
    return out


__all__ = [
    'DO_API_KEY', 'DO_BASE_URL', 'MODEL_ID', 'MAX_TOKENS',
    '_NOISE_LINES', '_VisibleTextExtractor',
    'extract_permit_text', 'record_number_from_url', 'clean_json',
    'SYSTEM_PROMPT',
    'parse_accela_detail',
    'PERMIT_FIELDS', 'PERMIT_FIELD_KEYS', 'extract_fields_by_xpath',
]
