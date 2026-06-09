"""
Server-rendered invoice PDF (ReportLab).

Why this exists:
The on-screen `invoice_print.html` is gorgeous but `window.print()` produces
inconsistent results across browsers — some users get tiny margins, some get
browser headers stamped on the PDF, some get blurry rasterised gradients.

This module generates a real PDF with **selectable text** that always looks
the same. We deliberately don't try to render HTML (no WeasyPrint / no
xhtml2pdf — both pull `pycairo` which fails on this build environment).
Instead we draw the invoice with ReportLab Platypus + canvas. Slightly more
code, zero system dependencies, ships everywhere.

Public surface: ``build_invoice_pdf(ctx) -> bytes``.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont  # noqa: F401  (kept for future custom fonts)
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


# ── Brand palette (mirrors the on-screen template) ────────────────
NAVY      = colors.HexColor('#0f172a')
BLUE      = colors.HexColor('#1d4ed8')
BLUE_SOFT = colors.HexColor('#3b82f6')
EMERALD   = colors.HexColor('#059669')
LIME      = colors.HexColor('#bef264')

INK       = colors.HexColor('#0f172a')
INK2      = colors.HexColor('#475569')
INK3      = colors.HexColor('#94a3b8')
LINE      = colors.HexColor('#e2e8f0')
LINE_SOFT = colors.HexColor('#f1f5f9')
PAPER     = colors.HexColor('#ffffff')
SOFT_BG   = colors.HexColor('#f8fafc')

PAID_GR   = colors.HexColor('#16a34a')

# Layout
PAGE_W, PAGE_H = LETTER
M_X = 0.5 * inch          # outer page margin (left/right)
HERO_H = 1.55 * inch      # hero block height


def _styles() -> dict[str, ParagraphStyle]:
    """ParagraphStyle factory — Helvetica family is built into ReportLab so
    we don't ship any TTF binaries. Plus Jakarta would be prettier but
    embedding fonts inflates each PDF by ~120kB and complicates packaging.
    Helvetica-Bold for headings, Helvetica for body — universally readable."""
    base = dict(fontName='Helvetica', leading=14, textColor=INK)
    return {
        'mono':       ParagraphStyle('mono', **{**base, 'fontName':'Courier', 'fontSize':9, 'leading':12, 'textColor':INK3}),
        'h_label':    ParagraphStyle('h_label', **{**base, 'fontSize':8, 'leading':10, 'textColor':colors.HexColor('#cbd5e1'), 'fontName':'Helvetica-Bold'}),
        'h_white_lg': ParagraphStyle('h_white_lg', **{**base, 'fontSize':16, 'leading':18, 'textColor':colors.white, 'fontName':'Helvetica-Bold'}),
        'h_white_xl': ParagraphStyle('h_white_xl', **{**base, 'fontSize':28, 'leading':30, 'textColor':colors.white, 'fontName':'Helvetica-Bold'}),
        'h_brand':    ParagraphStyle('h_brand', **{**base, 'fontSize':16, 'leading':18, 'textColor':colors.white, 'fontName':'Helvetica-Bold'}),
        'h_brand_sm': ParagraphStyle('h_brand_sm', **{**base, 'fontSize':8, 'leading':10, 'textColor':colors.HexColor('#cbd5e1'), 'fontName':'Helvetica'}),
        'h_meta_k':   ParagraphStyle('h_meta_k', **{**base, 'fontSize':8, 'leading':10, 'textColor':INK3, 'fontName':'Helvetica-Bold'}),
        'h_meta_v':   ParagraphStyle('h_meta_v', **{**base, 'fontSize':10, 'leading':14, 'textColor':INK}),
        'h_meta_v_b': ParagraphStyle('h_meta_v_b', **{**base, 'fontSize':10, 'leading':14, 'textColor':INK, 'fontName':'Helvetica-Bold'}),
        'item_t':     ParagraphStyle('item_t', **{**base, 'fontSize':11, 'leading':14, 'textColor':INK, 'fontName':'Helvetica-Bold'}),
        'item_s':     ParagraphStyle('item_s', **{**base, 'fontSize':9, 'leading':12, 'textColor':INK3}),
        'amt':        ParagraphStyle('amt', **{**base, 'fontSize':11, 'leading':13, 'textColor':INK, 'fontName':'Helvetica-Bold', 'alignment':2}),
        'amt_lite':   ParagraphStyle('amt_lite', **{**base, 'fontSize':10, 'leading':13, 'textColor':INK2, 'alignment':2}),
        'qty':        ParagraphStyle('qty', **{**base, 'fontSize':10, 'leading':13, 'textColor':INK2, 'alignment':1}),
        'foot':       ParagraphStyle('foot', **{**base, 'fontSize':9, 'leading':13, 'textColor':INK2}),
        'foot_sig':   ParagraphStyle('foot_sig', **{**base, 'fontSize':8, 'leading':12, 'textColor':INK3, 'fontName':'Courier', 'alignment':2}),
        'pay_b':      ParagraphStyle('pay_b', **{**base, 'fontSize':10, 'leading':13, 'textColor':INK, 'fontName':'Helvetica-Bold'}),
        'pay_s':      ParagraphStyle('pay_s', **{**base, 'fontSize':9, 'leading':12, 'textColor':INK2}),
    }


# ── Hero band: drawn directly on canvas (gradients / rotated text) ─

def _draw_hero(c: rl_canvas.Canvas, ctx: dict[str, Any]) -> None:
    """Dark navy → blue gradient band at the top of the first page,
    with brand mark on the left, INVOICE label + number on the right,
    a status pill, and a divider with amount + period below it."""
    top = PAGE_H - 0
    bottom = PAGE_H - HERO_H

    # Vertical gradient: navy at the bottom, blue at the top, with a soft
    # green wash overlaid on the right. ReportLab has no native gradient
    # so we fake it by painting many thin horizontal slices.
    slices = 70
    for i in range(slices):
        t = i / (slices - 1)  # 0 at top, 1 at bottom
        # interpolate NAVY → BLUE
        r = (1-t)*0x1d/255 + t*0x0f/255
        g = (1-t)*0x4e/255 + t*0x17/255
        b = (1-t)*0xd8/255 + t*0x2a/255
        c.setFillColorRGB(r, g, b)
        h = HERO_H / slices
        c.rect(0, bottom + (slices - 1 - i) * h, PAGE_W, h + 0.6, stroke=0, fill=1)

    # Soft emerald glow in top-right corner (faked with a few translucent
    # circles since reportlab has no gradient fills).
    c.saveState()
    for radius, alpha in [(2.4*inch, 0.10), (1.7*inch, 0.14), (1.0*inch, 0.18)]:
        c.setFillColor(colors.Color(0.06, 0.72, 0.51, alpha=alpha))
        c.circle(PAGE_W - 0.4*inch, top - 0.2*inch, radius, stroke=0, fill=1)
    c.restoreState()

    # ── Left: brand mark ──────────────────────────────────────────
    bx, by = M_X, top - 0.45*inch
    # rounded square mark
    c.setFillColor(colors.Color(1, 1, 1, alpha=0.12))
    c.setStrokeColor(colors.Color(1, 1, 1, alpha=0.22))
    c.setLineWidth(0.6)
    c.roundRect(bx, by - 0.18*inch, 0.42*inch, 0.42*inch, 0.10*inch, stroke=1, fill=1)
    # shield icon inside the mark
    c.setStrokeColor(colors.white)
    c.setLineWidth(1.4)
    cx, cy = bx + 0.21*inch, by + 0.03*inch
    p = c.beginPath()
    p.moveTo(cx, cy + 0.13*inch)
    p.lineTo(cx - 0.11*inch, cy + 0.05*inch)
    p.lineTo(cx - 0.11*inch, cy - 0.05*inch)
    p.curveTo(cx - 0.11*inch, cy - 0.13*inch,
              cx, cy - 0.16*inch,
              cx, cy - 0.16*inch)
    p.curveTo(cx, cy - 0.16*inch,
              cx + 0.11*inch, cy - 0.13*inch,
              cx + 0.11*inch, cy - 0.05*inch)
    p.lineTo(cx + 0.11*inch, cy + 0.05*inch)
    p.close()
    c.drawPath(p, stroke=1, fill=0)

    # Brand name "Permitlify" (white "Permit" + lime "lify")
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 16)
    name_x = bx + 0.55*inch
    name_y = by + 0.11*inch
    c.drawString(name_x, name_y, 'Permit')
    permit_w = c.stringWidth('Permit', 'Helvetica-Bold', 16)
    c.setFillColor(LIME)
    c.drawString(name_x + permit_w, name_y, 'lify')

    # tagline below brand
    c.setFillColor(colors.Color(1, 1, 1, alpha=0.7))
    c.setFont('Courier', 8)
    c.drawString(name_x, name_y - 0.16*inch, 'permitlify.com  -  billing@permitlify.com')

    # ── Right: INVOICE label + number + status pill ───────────────
    label = 'INVOICE'
    num   = ctx['inv_id']
    status_lower = (ctx.get('inv_status') or 'paid').lower()

    c.setFillColor(colors.Color(1, 1, 1, alpha=0.65))
    c.setFont('Helvetica-Bold', 9)
    c.drawRightString(PAGE_W - M_X, top - 0.36*inch, label)

    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 18)
    c.drawRightString(PAGE_W - M_X, top - 0.58*inch, num)

    # status pill
    pill_text = {'paid':'Paid', 'upcoming':'Upcoming', 'pending':'Upcoming',
                 'failed':'Failed - Retried'}.get(status_lower, status_lower.title())
    pill_fg = {'paid':LIME, 'upcoming':colors.HexColor('#93c5fd'),
               'pending':colors.HexColor('#93c5fd'),
               'failed':colors.HexColor('#fca5a5')}.get(status_lower, LIME)
    pill_bg = colors.Color(*pill_fg.rgb(), alpha=0.18)
    pill_w = c.stringWidth(pill_text, 'Helvetica-Bold', 9) + 24
    pill_x = PAGE_W - M_X - pill_w
    pill_y = top - 0.86*inch
    c.setFillColor(pill_bg)
    c.setStrokeColor(pill_fg)
    c.setLineWidth(0.6)
    c.roundRect(pill_x, pill_y, pill_w, 0.22*inch, 0.11*inch, stroke=1, fill=1)
    # pulse dot
    c.setFillColor(pill_fg)
    c.circle(pill_x + 0.13*inch, pill_y + 0.11*inch, 0.035*inch, stroke=0, fill=1)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(pill_x + 0.22*inch, pill_y + 0.07*inch, pill_text)

    # ── Bottom ribbon: amount + period ────────────────────────────
    ribbon_y = bottom + 0.42*inch
    c.setStrokeColor(colors.Color(1, 1, 1, alpha=0.18))
    c.setLineWidth(0.5)
    c.line(M_X, ribbon_y + 0.30*inch, PAGE_W - M_X, ribbon_y + 0.30*inch)

    # left: AMOUNT PAID + big number.
    # We draw the dollar amount (e.g. "$1.00") as one solid string at one
    # baseline so a copy/paste of the PDF text doesn't end up as "$1 .00",
    # then a separate, smaller "USD" suffix sharing the same baseline so
    # the spacing reads naturally instead of floating above the digits.
    c.setFillColor(colors.Color(1, 1, 1, alpha=0.6))
    c.setFont('Helvetica-Bold', 8)
    amt_label = 'AMOUNT PAID' if status_lower == 'paid' else 'AMOUNT DUE'
    c.drawString(M_X, ribbon_y + 0.10*inch, amt_label)

    big_y = ribbon_y - 0.20*inch
    big   = f"${ctx['inv_amount']}.00"
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 26)
    c.drawString(M_X, big_y, big)
    big_w = c.stringWidth(big, 'Helvetica-Bold', 26)
    c.setFillColor(colors.Color(1, 1, 1, alpha=0.55))
    c.setFont('Helvetica', 12)
    c.drawString(M_X + big_w + 6, big_y, 'USD')

    # right: BILLING PERIOD
    c.setFillColor(colors.Color(1, 1, 1, alpha=0.6))
    c.setFont('Helvetica-Bold', 8)
    c.drawRightString(PAGE_W - M_X, ribbon_y + 0.10*inch, 'BILLING PERIOD')
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 11)
    period = ctx.get('inv_period') or ctx.get('inv_date') or ''
    c.drawRightString(PAGE_W - M_X, ribbon_y - 0.10*inch, period)


def _draw_paid_stamp(c: rl_canvas.Canvas) -> None:
    """Diagonal embossed PAID seal — big, centered, very low opacity so it
    sits behind the content as a true watermark. Drawn first (before the
    flowable story renders) so even where the rings/text overlap data
    rows, the data stays readable on top."""
    c.saveState()
    # Centered on the page body (slightly below geometric center to
    # compensate for the visually heavy navy hero at the top).
    cx, cy = PAGE_W / 2, PAGE_H / 2 - 0.6*inch
    c.translate(cx, cy)
    c.rotate(-16)
    # Watermark alpha — pushed even lower so the stamp sits as a faint
    # ghost behind the data; totals / line items remain crisply legible
    # right through it.
    a_ring = 0.045
    a_text = 0.05
    c.setStrokeColor(colors.Color(*PAID_GR.rgb(), alpha=a_ring))
    c.setLineWidth(4.5)
    c.circle(0, 0, 2.7*inch, stroke=1, fill=0)
    # outer dashed ring
    c.setLineWidth(1.0)
    c.setDash(3, 6)
    c.circle(0, 0, 3.0*inch, stroke=1, fill=0)
    c.setDash()
    # PAID text — wider via generous character spacing so the word
    # spans the full inner ring. ``setCharSpace`` lives on TextObject,
    # not on Canvas, so we draw each glyph individually with explicit
    # spacing instead — same visual result, no AttributeError risk.
    c.setFillColor(colors.Color(*PAID_GR.rgb(), alpha=a_text))
    font_name, font_size = 'Helvetica-Bold', 120
    c.setFont(font_name, font_size)
    char_space = 22
    chars = list('PAID')
    widths = [c.stringWidth(ch, font_name, font_size) for ch in chars]
    total_w = sum(widths) + char_space * (len(chars) - 1)
    x = -total_w / 2
    y = -0.42*inch
    for ch, w in zip(chars, widths):
        c.drawString(x, y, ch)
        x += w + char_space
    c.restoreState()


def _draw_footer(c: rl_canvas.Canvas, ctx: dict[str, Any]) -> None:
    """Soft footer band at the bottom of every page with the document URL
    and the standard 'computer-generated' disclaimer."""
    foot_h = 0.55*inch
    c.setFillColor(SOFT_BG)
    c.rect(0, 0, PAGE_W, foot_h, stroke=0, fill=1)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.4)
    c.line(0, foot_h, PAGE_W, foot_h)

    c.setFillColor(INK3)
    c.setFont('Helvetica', 8)
    c.drawString(M_X, 0.22*inch, f"Permitlify, Inc.  -  Tax ID 88-4102837  -  permitlify.com/invoices/{ctx.get('inv_num_short', ctx['inv_id'])}")
    c.setFont('Helvetica-Oblique', 7.5)
    c.drawRightString(PAGE_W - M_X, 0.22*inch, 'This invoice is computer-generated and valid without a signature.')


# ── Page template ──────────────────────────────────────────────────

def _make_doc(buf: BytesIO, ctx: dict[str, Any]) -> BaseDocTemplate:
    doc = BaseDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=M_X,
        rightMargin=M_X,
        topMargin=HERO_H + 0.30*inch,
        bottomMargin=0.75*inch,
        title=f"Permitlify Invoice {ctx['inv_id']}",
        author='Permitlify, Inc.',
        subject=f"Invoice {ctx['inv_id']}",
    )

    frame_first = Frame(
        M_X, 0.65*inch,
        PAGE_W - 2*M_X,
        PAGE_H - HERO_H - 0.30*inch - 0.65*inch,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id='first',
    )
    frame_rest = Frame(
        M_X, 0.65*inch,
        PAGE_W - 2*M_X,
        PAGE_H - 0.5*inch - 0.65*inch,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id='rest',
    )

    def on_first(c: rl_canvas.Canvas, _doc: BaseDocTemplate) -> None:
        _draw_hero(c, ctx)
        if (ctx.get('inv_status') or '').lower() == 'paid':
            _draw_paid_stamp(c)
        _draw_footer(c, ctx)

    def on_later(c: rl_canvas.Canvas, _doc: BaseDocTemplate) -> None:
        # subtle navy strip header on continuation pages
        c.setFillColor(NAVY)
        c.rect(0, PAGE_H - 0.35*inch, PAGE_W, 0.35*inch, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont('Helvetica-Bold', 10)
        c.drawString(M_X, PAGE_H - 0.24*inch, 'Permitlify')
        c.setFillColor(colors.Color(1, 1, 1, alpha=0.7))
        c.setFont('Helvetica', 9)
        c.drawRightString(PAGE_W - M_X, PAGE_H - 0.24*inch, f"Invoice {ctx['inv_id']}  -  cont.")
        _draw_footer(c, ctx)

    doc.addPageTemplates([
        PageTemplate(id='first', frames=[frame_first], onPage=on_first),
        PageTemplate(id='rest',  frames=[frame_rest],  onPage=on_later),
    ])
    return doc


# ── Body builder ────────────────────────────────────────────────────

def _meta_table(ctx: dict[str, Any], st: dict[str, ParagraphStyle]) -> Table:
    """Three columns: Billed To / From / Details."""
    def billed_to():
        lines = []
        if ctx.get('company'):
            lines.append(f"<b>{ctx['company']}</b>")
        if ctx.get('user_name'):
            lines.append(ctx['user_name'])
        if ctx.get('bill_street'):
            lines.append(ctx['bill_street'])
        city_zip = []
        if ctx.get('bill_city'): city_zip.append(ctx['bill_city'])
        if ctx.get('bill_state_zip'): city_zip.append(ctx['bill_state_zip'])
        if city_zip: lines.append(' '.join(city_zip))
        if ctx.get('bill_ein'):
            lines.append(f"EIN: {ctx['bill_ein']}")
        if ctx.get('user_email'):
            lines.append(f"<font color='#475569'>{ctx['user_email']}</font>")
        return '<br/>'.join(lines)

    sender = (
        '<b>Permitlify, Inc.</b><br/>'
        '2101 E 5th Street<br/>'
        'Austin, TX 78702<br/>'
        'Tax ID: 88-4102837'
    )
    details = (
        f"<b>Issued</b>  {ctx.get('inv_date','')}<br/>"
        f"<b>Due</b>  {ctx.get('inv_due','')}<br/>"
        f"<b>Terms</b>  Auto-charge<br/>"
        f"<b>Currency</b>  USD"
    )

    body = [[
        Paragraph(billed_to(), st['h_meta_v']),
        Paragraph(sender,      st['h_meta_v']),
        Paragraph(details,     st['h_meta_v']),
    ]]
    head = [[
        Paragraph('BILLED TO', st['h_meta_k']),
        Paragraph('FROM',      st['h_meta_k']),
        Paragraph('DETAILS',   st['h_meta_k']),
    ]]
    col_w = (PAGE_W - 2 * M_X) / 3
    t = Table(head + body, colWidths=[col_w, col_w, col_w])
    t.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,0), 6),
        ('TOPPADDING', (0,0), (-1,-1), 0),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ]))
    return t


def _items_table(ctx: dict[str, Any], st: dict[str, ParagraphStyle]) -> Table:
    """Description / Qty / Rate / Amount."""
    desc_t = Paragraph(
        f"Permitlify {ctx.get('user_plan','')} - Subscription",
        st['item_t'],
    )
    desc_s = Paragraph(ctx.get('plan_features', ''), st['item_s'])
    desc = Table([[desc_t], [Spacer(1, 4)], [desc_s]], colWidths=['*'])
    desc.setStyle(TableStyle([('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]))

    amt = ctx.get('inv_amount', 0)
    head = [
        Paragraph('DESCRIPTION', st['h_meta_k']),
        Paragraph('QTY',         ParagraphStyle('q', parent=st['h_meta_k'], alignment=1)),
        Paragraph('RATE',        ParagraphStyle('r', parent=st['h_meta_k'], alignment=2)),
        Paragraph('AMOUNT',      ParagraphStyle('a', parent=st['h_meta_k'], alignment=2)),
    ]
    row = [
        desc,
        Paragraph('1', st['qty']),
        Paragraph(f"${amt}.00", st['amt_lite']),
        Paragraph(f"${amt}.00", st['amt']),
    ]
    avail = PAGE_W - 2 * M_X
    cw = [avail - 1.7*inch, 0.45*inch, 0.625*inch, 0.625*inch]
    t = Table([head, row], colWidths=cw)
    t.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LINEBELOW', (0,0), (-1,0), 1.4, INK),
        ('LINEBELOW', (0,1), (-1,1), 0.5, LINE),
        ('TOPPADDING', (0,0), (-1,0), 8),
        ('BOTTOMPADDING', (0,0), (-1,0), 8),
        ('TOPPADDING', (0,1), (-1,1), 12),
        ('BOTTOMPADDING', (0,1), (-1,1), 14),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    return t


def _totals_table(ctx: dict[str, Any], st: dict[str, ParagraphStyle]) -> Table:
    amt = ctx.get('inv_amount', 0)
    rows = [
        [Paragraph('Subtotal',         st['h_meta_v']),     Paragraph(f"${amt}.00", st['amt_lite'])],
        [Paragraph('Tax (0%)',         st['h_meta_v']),     Paragraph('$0.00',      st['amt_lite'])],
        [Paragraph('Credits applied',  ParagraphStyle('cr', parent=st['h_meta_v'], textColor=INK3)),
         Paragraph('- $0.00',          ParagraphStyle('crv', parent=st['amt_lite'], textColor=INK3))],
        [Paragraph('<b>Total</b>',     ParagraphStyle('gt', parent=st['h_meta_v'], fontName='Helvetica-Bold', fontSize=12)),
         Paragraph(f"<b>${amt}.00</b>", ParagraphStyle('gtv', parent=st['amt'], textColor=BLUE, fontSize=18, leading=20))],
    ]
    t = Table(rows, colWidths=[1.7*inch, 1.5*inch], hAlign='RIGHT')
    t.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-2), 3),
        ('BOTTOMPADDING', (0,0), (-1,-2), 3),
        ('LINEABOVE', (0,-1), (-1,-1), 1.2, INK),
        ('TOPPADDING', (0,-1), (-1,-1), 8),
        ('BOTTOMPADDING', (0,-1), (-1,-1), 0),
    ]))
    return t


def _payment_card(ctx: dict[str, Any], st: dict[str, ParagraphStyle]) -> Table:
    status_lower = (ctx.get('inv_status') or 'paid').lower()
    if status_lower == 'paid':
        b = '<b>Paid via Whop</b>'
        s = f"Auto-charged on {ctx.get('inv_date','')}<br/>"
        s += f"<font face='Courier' size='8' color='#94a3b8'>Reference: {ctx.get('inv_id','')}  -  Secure payment processor</font>"
    elif status_lower in ('upcoming', 'pending'):
        b = '<b>Scheduled</b>'
        s = f"Auto-charge on {ctx.get('inv_date','')} via Whop<br/>"
        s += "<font color='#94a3b8'>Auto-renews - Cancel anytime from Settings &rarr; Billing</font>"
    else:
        return None  # type: ignore[return-value]

    # Helvetica has poor unicode coverage so we avoid &#9873; etc and draw a
    # solid blue circle as the icon via a tiny inline-cell ParagraphStyle.
    icon_style = ParagraphStyle(
        'icon', fontName='Helvetica-Bold', fontSize=18,
        textColor=BLUE, alignment=1, leading=18,
    )
    icon = Paragraph('&bull;', icon_style)
    body = Paragraph(b + '<br/>' + s, st['pay_s'])
    t = Table([[icon, body]], colWidths=[0.5*inch, PAGE_W - 2*M_X - 0.5*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), LINE_SOFT),
        ('BOX',        (0,0), (-1,-1), 0.5, LINE),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 14),
        ('RIGHTPADDING', (0,0), (-1,-1), 14),
        ('TOPPADDING', (0,0), (-1,-1), 12),
        ('BOTTOMPADDING', (0,0), (-1,-1), 12),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
    ]))
    return t


def _footer_block(ctx: dict[str, Any], st: dict[str, ParagraphStyle]) -> Table:
    note = (
        '<b>Thank you for your business.</b><br/>'
        'Questions about this invoice? Email '
        '<font color="#1d4ed8"><b>billing@permitlify.com</b></font> '
        'or reply to your monthly receipt. Subscription renews automatically '
        'until cancelled from Settings &rarr; Billing.'
    )
    sig = (
        f"<b>Generated</b>  {ctx.get('inv_date','')}<br/>"
        f"permitlify.com/invoices/{ctx.get('inv_num_short', ctx['inv_id'])}<br/>"
        "This invoice is computer-generated."
    )
    t = Table([[
        Paragraph(note, st['foot']),
        Paragraph(sig,  st['foot_sig']),
    ]], colWidths=[3.6*inch, PAGE_W - 2*M_X - 3.6*inch])
    t.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    return t


# ── Public API ─────────────────────────────────────────────────────

def build_invoice_pdf(ctx: dict[str, Any]) -> bytes:
    """Render an invoice context dict (same one we pass to ``invoice_print.html``)
    into a real PDF and return the bytes. Caller is responsible for HTTP
    headers / ownership checks."""
    buf = BytesIO()
    doc = _make_doc(buf, ctx)
    st  = _styles()

    story: list[Any] = [
        _meta_table(ctx, st),
        Spacer(1, 18),
        _items_table(ctx, st),
        Spacer(1, 14),
        _totals_table(ctx, st),
        Spacer(1, 22),
    ]
    pay = _payment_card(ctx, st)
    if pay is not None:
        story.extend([pay, Spacer(1, 22)])
    story.append(_footer_block(ctx, st))

    doc.build(story)
    return buf.getvalue()
