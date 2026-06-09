"""Ready-made campaign email templates for the marketing campaign editor.

Each template is a **complete, standalone HTML email** — its own Permitlify
masthead, content, and footer with one-click unsubscribe. They are rendered
as-is (NOT wrapped in the transactional branded shell), so the editor preview
shows exactly the design and nothing else stacked on top.

Everything here is email-safe: inline styles only, table-based layout for
Outlook, system-font fallbacks, no ``<style>`` blocks, no web fonts, no remote
images. Personalisation uses the placeholders the send path substitutes:
``{{name}}`` (first name), ``{{email}}``, ``{{unsubscribe_url}}``. The literal
token ``__YEAR__`` is replaced with the current year in ``get_campaign_templates``.

The three directions mirror the approved designs:
  * ``classic``    — Clean checklist
  * ``product``    — Product / score-forward (dashboard preview)
  * ``editorial``  — Editorial (colored intro band + numbered list)
"""

import datetime as _dt

_FONT = "'DM Sans','Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif"
_MONO = "'JetBrains Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace"
_CTA_URL = "https://permitlify.com/?utm_source=email&utm_medium=campaign"


def _cta(label: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:4px auto 10px"><tr>'
        '<td align="center" style="border-radius:12px;background:#1d4ed8;'
        'background:linear-gradient(135deg,#1d4ed8 0%,#059669 130%);'
        'box-shadow:0 8px 20px rgba(29,78,216,.28)">'
        f'<a href="{_CTA_URL}" style="display:inline-block;padding:15px 32px;'
        f'font-family:{_FONT};font-size:15px;font-weight:800;color:#ffffff;'
        'text-decoration:none;border-radius:12px;letter-spacing:.2px">'
        f'{label}</a></td></tr></table>'
    )


def _cta_sub(text: str) -> str:
    return (
        f'<p style="text-align:center;margin:0;font-family:{_FONT};font-size:12.5px;'
        f'color:#94a3b8">{text}</p>'
    )


def _proof(text: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:0 auto"><tr><td style="background:#f8fafc;'
        'border:1px solid rgba(15,23,42,.08);border-radius:99px;padding:9px 18px;'
        f'font-family:{_FONT};font-size:13px;color:#334155">'
        '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;'
        'background:#059669;vertical-align:middle;margin-right:8px"></span>'
        f'{text}</td></tr></table>'
    )


def _sign() -> str:
    return (
        f'<p style="font-family:{_FONT};font-size:14.5px;color:#334155;margin:0">'
        '&mdash; The Permitlify Team</p>'
    )


def _check_row(title: str, desc: str) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="margin:0 0 14px"><tr>'
        '<td width="34" valign="top" style="padding-top:2px">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td width="26" height="26" align="center" valign="middle" '
        'style="background:#e7f5ef;border-radius:8px;color:#059669;font-size:15px;'
        'font-weight:800;line-height:26px">&#10003;</td></tr></table></td>'
        f'<td valign="top" style="padding-left:12px;font-family:{_FONT}">'
        f'<div style="font-size:15px;font-weight:700;color:#0f172a">{title}</div>'
        f'<div style="font-size:13.5px;color:#475569;line-height:1.55">{desc}</div>'
        '</td></tr></table>'
    )


def _permit_row(score: str, bg: str, fg: str, addr: str, kind: str) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="margin:0 0 8px"><tr>'
        '<td width="50" valign="middle">'
        f'<div style="width:44px;height:44px;line-height:44px;text-align:center;'
        f'border-radius:10px;background:{bg};color:{fg};font-weight:800;'
        f'font-size:16px;font-family:{_FONT}">{score}</div></td>'
        f'<td valign="middle" style="padding-left:12px;font-family:{_FONT}">'
        f'<div style="font-weight:700;color:#0f172a;font-size:14px">{addr}</div>'
        f'<div style="color:#64748b;font-size:12.5px">{kind}</div></td>'
        '<td align="right" valign="middle">'
        '<span style="background:#e9f0fd;color:#1d4ed8;font-weight:700;font-size:12px;'
        f'font-family:{_FONT};padding:6px 13px;border-radius:99px">Call</span></td>'
        '</tr></table>'
    )


def _stat(n: str, label: str) -> str:
    return (
        '<td width="33%" valign="top" style="padding:5px">'
        '<div style="border:1px solid rgba(15,23,42,.09);border-radius:10px;'
        'padding:14px 8px;text-align:center">'
        f'<div style="font-family:{_FONT};font-weight:800;color:#1d4ed8;font-size:15px">{n}</div>'
        f'<div style="font-family:{_FONT};color:#64748b;font-size:11.5px;margin-top:3px;'
        f'line-height:1.4">{label}</div></div></td>'
    )


def _num_row(idx: str, title: str, desc: str) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="margin:0 0 14px"><tr>'
        '<td width="42" valign="top">'
        f'<div style="font-family:{_MONO};font-size:16px;font-weight:800;'
        f'color:#1d4ed8">{idx}</div></td>'
        f'<td valign="top" style="padding-left:6px;font-family:{_FONT}">'
        f'<div style="font-size:15px;font-weight:700;color:#0f172a">{title}</div>'
        f'<div style="font-size:13.5px;color:#475569;line-height:1.55">{desc}</div>'
        '</td></tr></table>'
    )


def _p(text: str, mb: int = 14) -> str:
    return (
        f'<p style="font-family:{_FONT};font-size:15px;line-height:1.62;color:#334155;'
        f'margin:0 0 {mb}px">{text}</p>'
    )


def _h1(text: str) -> str:
    return (
        f'<h1 style="font-family:{_FONT};font-size:23px;line-height:1.25;'
        'font-weight:800;color:#0f172a;margin:0 0 14px;letter-spacing:-.4px">'
        f'{text}</h1>'
    )


def _eyebrow() -> str:
    return (
        f'<p style="font-family:{_FONT};font-size:14px;color:#64748b;margin:0 0 10px">'
        'Hi {{name}},</p>'
    )


def _proof_center(text: str) -> str:
    return '<div style="text-align:center">' + _proof(text) + '</div>'


def _doc(preheader: str, inner: str) -> str:
    """Wrap inner card content into a complete, standalone, email-safe document
    with a Permitlify masthead and footer (incl. one-click unsubscribe).
    """
    return (
        '<!DOCTYPE html>\n'
        '<html lang="en"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<meta name="color-scheme" content="light only">'
        '<title>Permitlify</title></head>'
        f'<body style="margin:0;padding:0;width:100%;background:#f0f4f8;font-family:{_FONT};color:#0f172a">'
        '<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
        f'font-size:1px;line-height:1px;color:#f0f4f8">{preheader}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="background:#f0f4f8"><tr>'
        '<td align="center" style="padding:30px 14px">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'border="0" style="max-width:600px;width:100%">'
        # masthead
        '<tr><td style="padding:0 4px 18px;text-align:center">'
        f'<span style="font-family:{_FONT};font-size:21px;font-weight:800;'
        'letter-spacing:-.3px;color:#1d4ed8">Permit'
        '<span style="color:#059669">lify</span></span></td></tr>'
        # content card
        '<tr><td style="background:#ffffff;border:1px solid rgba(15,23,42,.07);'
        'border-radius:16px;padding:30px 32px;'
        'box-shadow:0 1px 3px rgba(15,23,42,.05)">'
        f'{inner}'
        '</td></tr>'
        # footer
        '<tr><td style="padding:22px 12px 4px;text-align:center;'
        f'font-family:{_FONT};font-size:12px;color:#94a3b8;line-height:1.6">'
        '&copy; __YEAR__ <span style="font-weight:700;color:#475569">Permitlify</span> '
        '&middot; <a href="https://permitlify.com" style="color:#64748b;'
        'text-decoration:none">permitlify.com</a><br>'
        "You're receiving this because your business was identified as a likely "
        'fit for Permitlify.<br>'
        '<a href="{{unsubscribe_url}}" style="color:#64748b;text-decoration:underline">'
        'Unsubscribe in one click</a> &middot; we&rsquo;ll never email you again.'
        '</td></tr>'
        '</table></td></tr></table>'
        '</body></html>'
    )


def _hr(mt: int = 24, mb: int = 24) -> str:
    return (f'<div style="border-top:1px solid rgba(15,23,42,.09);'
            f'margin:{mt}px 0 {mb}px"></div>')


# ── Direction 1 — Clean checklist ────────────────────────────────────────────
_CLASSIC = _doc(
    "Be the first contractor to call — fresh permits, scored daily.",
    "".join([
        _eyebrow(),
        _h1("Be the first contractor to call."),
        _p("Every day, new building permits are filed in your area &mdash; each one a "
           "homeowner or business about to pay for work like yours. <strong "
           "style=\"color:#0f172a\">By the time most contractors hear about a job, three "
           "competitors have already called.</strong>"),
        _p("Permitlify pulls fresh permits daily, scores each one 0&ndash;100 with AI, and "
           "drops the best leads in your dashboard before the workday starts &mdash; so you "
           "call first.", mb=22),
        _check_row("Daily 6 AM delivery", "Fresh permits waiting before your first coffee."),
        _check_row("AI lead scoring", "Every permit ranked 0&ndash;100 so you know exactly who to call first."),
        _check_row("Real contact details", "Reach out the same day &mdash; not three days late."),
        '<div style="height:8px"></div>',
        _proof_center("<strong style=\"color:#0f172a\">2,000+</strong> permits scored daily"),
        '<div style="height:22px"></div>',
        _cta("Start your free trial &rarr;"),
        _cta_sub("No contracts &middot; cancel anytime &middot; free trial on every plan."),
        _hr(),
        _sign(),
    ]),
)

# ── Direction 2 — Product / score-forward ────────────────────────────────────
_PRODUCT = _doc(
    "New permits near you — scored and ready by 6 AM.",
    "".join([
        _eyebrow(),
        _h1("The best leads are filed today.<br>You&rsquo;ll have them by 6 AM."),
        _p("By the time most contractors hear about a job, three competitors have already "
           "called. Permitlify scores every new permit with AI and lines up your best calls "
           "before the workday starts. Here&rsquo;s what&rsquo;s waiting in a dashboard like "
           "yours:", mb=20),
        # dashboard card
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:0 0 22px"><tr><td style="border:1px solid rgba(15,23,42,.10);'
        'border-radius:14px;padding:16px 16px 8px;background:#ffffff">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:0 0 12px"><tr>'
        f'<td style="font-family:{_FONT};font-size:13px;font-weight:800;color:#0f172a;'
        'text-transform:uppercase;letter-spacing:.4px">Fresh permits nearby</td>'
        f'<td align="right" style="font-family:{_MONO};font-size:11.5px;color:#94a3b8">'
        'Today &middot; 6:00 AM</td></tr></table>'
        + _permit_row("94", "#fee2e2", "#b91c1c", "1420 Oakridge Dr", "Kitchen remodel &middot; $48k est.")
        + _permit_row("81", "#fef3c7", "#a16207", "38 Harborview Ave", "Roof replacement &middot; $26k est.")
        + _permit_row("67", "#e0f2fe", "#0369a1", "905 Lincoln St", "Bathroom addition &middot; $19k est.")
        + '</td></tr></table>',
        # stat grid
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:0 0 22px"><tr>'
        + _stat("6:00 AM", "Delivered daily, before work")
        + _stat("0&ndash;100", "AI score on every permit")
        + _stat("Same day", "Real contact details included")
        + '</tr></table>',
        _cta("See what&rsquo;s filing near you &rarr;"),
        _cta_sub("Takes two minutes &middot; free trial on every plan &middot; cancel anytime."),
        _hr(),
        _sign(),
    ]),
)

# ── Direction 3 — Editorial (colored intro band + numbered list) ──────────────
_EDITORIAL = _doc(
    "Three competitors already called. You didn’t know the job existed.",
    "".join([
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:0 0 22px"><tr><td style="border-radius:14px;background:#1d4ed8;'
        'background:linear-gradient(135deg,#1d4ed8 0%,#059669 120%);padding:26px 24px">'
        f'<div style="font-family:{_FONT};font-size:20px;font-weight:800;color:#ffffff;'
        'line-height:1.3">Three competitors already called.<br>'
        '<span style="font-weight:600;font-style:italic;color:rgba(255,255,255,.92)">'
        'You didn&rsquo;t know the job existed.</span></div></td></tr></table>',
        _eyebrow(),
        _p("Every day, new building permits are filed in your area &mdash; each one a "
           "homeowner or business about to pay for work like yours. The trouble is timing: "
           "most contractors find out three days late."),
        _p("<strong style=\"color:#0f172a\">Permitlify fixes that.</strong> We pull fresh "
           "permits every morning, score each one 0&ndash;100 with AI, and put them in your "
           "dashboard before 6 AM. You skip the guesswork and call the best leads first.", mb=22),
        _num_row("01", "Daily 6 AM delivery", "New permits waiting before your first coffee."),
        _num_row("02", "AI lead scoring", "Every permit ranked 0&ndash;100 &mdash; call the hottest jobs first."),
        _num_row("03", "Real contact details", "Reach out the same day, not three days late."),
        '<div style="height:8px"></div>',
        _proof_center(
            "<strong style=\"color:#0f172a\">2,000+</strong> permits scored daily &mdash; "
            "contractors aren&rsquo;t working harder, just getting there first."),
        '<div style="height:22px"></div>',
        _cta("Start your free trial &rarr;"),
        _cta_sub("No contracts &middot; cancel anytime &middot; free trial on every plan."),
        _hr(),
        _sign(),
    ]),
)


CAMPAIGN_TEMPLATES = [
    {
        "key": "classic",
        "name": "Clean checklist",
        "desc": "By-the-book SaaS: headline, 3-point checklist, single CTA. Safe all-rounder.",
        "subject": "Be the first contractor to call",
        "body_html": _CLASSIC,
    },
    {
        "key": "product",
        "name": "Score-forward",
        "desc": "Shows a sample lead dashboard with AI scores. Best for proving the product.",
        "subject": "New permits near you — scored and ready by 6 AM",
        "body_html": _PRODUCT,
    },
    {
        "key": "editorial",
        "name": "Editorial",
        "desc": "Bold colored hero line + numbered story. Most eye-catching / brand-forward.",
        "subject": "Three competitors already called. You didn’t know the job existed.",
        "body_html": _EDITORIAL,
    },
]


def get_campaign_templates() -> list:
    """Return the campaign starter templates (safe copies, year resolved)."""
    year = str(_dt.datetime.utcnow().year)
    out = []
    for t in CAMPAIGN_TEMPLATES:
        t = dict(t)
        t["body_html"] = t["body_html"].replace("__YEAR__", year)
        out.append(t)
    return out
