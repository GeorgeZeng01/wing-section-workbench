"""Build the Wing Section Studio guide PDF.

    .venv\\Scripts\\python.exe docs\\build_guide.py

Regenerates every figure from the live app.core (guide_figures.py) and
composes the guide with reportlab. Two parts: a workflow guide for new
users, and a deep "how it works" reference on the backend. Segoe UI and
Consolas are embedded for full glyph coverage; the table of contents is
built with a two-pass layout.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Image,
    NextPageTemplate, PageBreak, Table, TableStyle, KeepTogether,
    HRFlowable, ListFlowable, ListItem,
)
from reportlab.platypus.tableofcontents import TableOfContents

import guide_figures

# ---------------------------------------------------------------- palette
INK = colors.HexColor("#1b2433")
INK2 = colors.HexColor("#44506180")   # unused alpha guard; see INK_SUB
INK_SUB = colors.HexColor("#55606f")
INK_FAINT = colors.HexColor("#8a94a3")
BLUE = colors.HexColor("#1f4e8c")
GREEN = colors.HexColor("#1e6f43")
GOLD = colors.HexColor("#9a7a12")
RED = colors.HexColor("#b03a2e")
RULE = colors.HexColor("#d7dce3")
CALLOUT_BG = colors.HexColor("#f2f6fb")
CALLOUT_BAR = colors.HexColor("#1f4e8c")
CODE_BG = colors.HexColor("#f4f6f8")
NOTE_BG = colors.HexColor("#fbf7ec")
NOTE_BAR = colors.HexColor("#9a7a12")

FONTS = Path("C:/Windows/Fonts")


def register_fonts():
    reg = [
        ("SegoeUI", "segoeui.ttf"), ("SegoeUI-Bold", "segoeuib.ttf"),
        ("SegoeUI-Light", "segoeuil.ttf"), ("SegoeUI-Semibold", "seguisb.ttf"),
        ("Consola", "consola.ttf"), ("Consola-Bold", "consolab.ttf"),
    ]
    ok = True
    for name, fn in reg:
        p = FONTS / fn
        if p.exists():
            pdfmetrics.registerFont(TTFont(name, str(p)))
        else:
            ok = False
    if ok:
        pdfmetrics.registerFontFamily(
            "SegoeUI", normal="SegoeUI", bold="SegoeUI-Bold",
            italic="SegoeUI", boldItalic="SegoeUI-Bold")
    return ok


HAVE_FONTS = register_fonts()
BODY = "SegoeUI" if HAVE_FONTS else "Helvetica"
BOLD = "SegoeUI-Bold" if HAVE_FONTS else "Helvetica-Bold"
LIGHT = "SegoeUI-Light" if HAVE_FONTS else "Helvetica"
SEMI = "SegoeUI-Semibold" if HAVE_FONTS else "Helvetica-Bold"
MONO = "Consola" if HAVE_FONTS else "Courier"
MONO_B = "Consola-Bold" if HAVE_FONTS else "Courier-Bold"

# ---------------------------------------------------------------- styles
from reportlab.lib.styles import ParagraphStyle  # noqa: E402

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm

styles = {
    "body": ParagraphStyle(
        "body", fontName=BODY, fontSize=9.6, leading=14.2, textColor=INK,
        alignment=TA_JUSTIFY, spaceAfter=7),
    "body_l": ParagraphStyle(
        "body_l", fontName=BODY, fontSize=9.6, leading=14.2, textColor=INK,
        alignment=TA_LEFT, spaceAfter=7),
    "h1": ParagraphStyle(
        "h1", fontName=BOLD, fontSize=17, leading=21, textColor=INK,
        spaceBefore=6, spaceAfter=10),
    "h2": ParagraphStyle(
        "h2", fontName=SEMI, fontSize=12.5, leading=16, textColor=BLUE,
        spaceBefore=13, spaceAfter=5),
    "h3": ParagraphStyle(
        "h3", fontName=SEMI, fontSize=10.3, leading=13.5, textColor=INK,
        spaceBefore=8, spaceAfter=3),
    "caption": ParagraphStyle(
        "caption", fontName=BODY, fontSize=8.2, leading=11, textColor=INK_SUB,
        alignment=TA_CENTER, spaceBefore=3, spaceAfter=10),
    "code": ParagraphStyle(
        "code", fontName=MONO, fontSize=8.6, leading=12.4, textColor=INK,
        backColor=CODE_BG, borderPadding=(6, 7, 6, 7), spaceBefore=3,
        spaceAfter=8, leftIndent=2, firstLineIndent=0),
    "callout": ParagraphStyle(
        "callout", fontName=BODY, fontSize=9.2, leading=13.4, textColor=INK,
        alignment=TA_LEFT, leftIndent=8, rightIndent=6,
        spaceBefore=2, spaceAfter=2),
    "callout_h": ParagraphStyle(
        "callout_h", fontName=SEMI, fontSize=9.2, leading=13, textColor=BLUE,
        leftIndent=8, spaceBefore=2, spaceAfter=1),
    "note_h": ParagraphStyle(
        "note_h", fontName=SEMI, fontSize=9.2, leading=13, textColor=GOLD,
        leftIndent=8, spaceBefore=2, spaceAfter=1),
    "bullet": ParagraphStyle(
        "bullet", fontName=BODY, fontSize=9.5, leading=13.4, textColor=INK,
        alignment=TA_LEFT),
    "toc1": ParagraphStyle(
        "toc1", fontName=SEMI, fontSize=10.5, leading=18, textColor=INK),
    "toc2": ParagraphStyle(
        "toc2", fontName=BODY, fontSize=9.6, leading=15, textColor=INK_SUB,
        leftIndent=12),
    "cover_title": ParagraphStyle(
        "cover_title", fontName=BOLD, fontSize=34, leading=38, textColor=INK),
    "cover_sub": ParagraphStyle(
        "cover_sub", fontName=LIGHT, fontSize=15, leading=21,
        textColor=BLUE),
    "cover_meta": ParagraphStyle(
        "cover_meta", fontName=BODY, fontSize=10, leading=15,
        textColor=INK_SUB),
    "part": ParagraphStyle(
        "part", fontName=LIGHT, fontSize=12, leading=15, textColor=INK_FAINT,
        spaceAfter=1),
    "tbl": ParagraphStyle(
        "tbl", fontName=BODY, fontSize=9, leading=12.4, textColor=INK),
    "tbl_k": ParagraphStyle(
        "tbl_k", fontName=SEMI, fontSize=9, leading=12.4, textColor=INK),
    "tbl_code": ParagraphStyle(
        "tbl_code", fontName=MONO, fontSize=8.3, leading=12, textColor=BLUE),
}


def C(t):
    """Inline code span."""
    return f'<font face="{MONO}" color="#1f4e8c">{t}</font>'


def B(t):
    return f'<b>{t}</b>'


# ---------------------------------------------------------------- doc scaffold

class GuideDoc(BaseDocTemplate):
    def __init__(self, path, **kw):
        super().__init__(path, **kw)
        self._toc_entries = []

    def afterFlowable(self, flowable):
        if not hasattr(flowable, "_bookmark"):
            return
        level, text, key = flowable._bookmark
        self.notify("TOCEntry", (level, text, self.page, key))
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(text, key, level=level, closed=(level > 0))


_section_counter = [0]
_sub_counter = [0]


def heading(text, level=1):
    """A numbered heading that also emits a bookmark + TOC entry."""
    if level == 1:
        _section_counter[0] += 1
        _sub_counter[0] = 0
        num = f"{_section_counter[0]}"
        style, lvl = styles["h1"], 0
        label = f"{num} {text}"
    else:
        _sub_counter[0] += 1
        num = f"{_section_counter[0]}.{_sub_counter[0]}"
        style, lvl = styles["h2"], 1
        label = f"{num} {text}"
    key = f"sec{_section_counter[0]}_{_sub_counter[0]}_{level}"
    p = Paragraph(label, style)
    p._bookmark = (lvl, f"{num}  {text}", key)
    return p


def body(text):
    return Paragraph(text, styles["body"])


def h3(text):
    return Paragraph(text, styles["h3"])


def code(text):
    return Paragraph(text.replace(" ", "&nbsp;").replace("\n", "<br/>"),
                     styles["code"])


def bullets(items, style="bullet"):
    return ListFlowable(
        [ListItem(Paragraph(t, styles[style]), leftIndent=14,
                  value="•", spaceAfter=3) for t in items],
        bulletType="bullet", start="•", leftIndent=8)


def callout(title, *paras, kind="info"):
    bar = NOTE_BAR if kind == "note" else CALLOUT_BAR
    bg = NOTE_BG if kind == "note" else CALLOUT_BG
    hstyle = styles["note_h"] if kind == "note" else styles["callout_h"]
    inner = [Paragraph(title, hstyle)]
    for p in paras:
        inner.append(Paragraph(p, styles["callout"]))
    t = Table([[inner]], colWidths=[PAGE_W - 2 * MARGIN - 6])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 2.4, bar),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return KeepTogether([Spacer(1, 2), t, Spacer(1, 6)])


FIGW = PAGE_W - 2 * MARGIN


def figure(name, caption, width=None, made=None):
    path = made[name]
    from PIL import Image as PILImage
    with PILImage.open(path) as im:
        iw, ih = im.size
    w = width or FIGW
    h = w * ih / iw
    img = Image(str(path), width=w, height=h)
    img.hAlign = "CENTER"
    return KeepTogether([Spacer(1, 3), img,
                         Paragraph(caption, styles["caption"])])


def kv_table(rows, kw=42 * mm):
    """Two-column key/description table."""
    data = []
    for k, v in rows:
        kstyle = styles["tbl_code"] if k.startswith("`") else styles["tbl_k"]
        kk = k.strip("`")
        data.append([Paragraph(kk, kstyle), Paragraph(v, styles["tbl"])])
    t = Table(data, colWidths=[kw, PAGE_W - 2 * MARGIN - kw])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, RULE),
    ]))
    return t


# ---------------------------------------------------------------- page furniture

def _header_footer(canvas, doc, title="Wing Section Studio  ·  Guide"):
    canvas.saveState()
    if doc.page > 1:
        canvas.setFont(BODY, 7.8)
        canvas.setFillColor(INK_FAINT)
        canvas.drawString(MARGIN, PAGE_H - 12 * mm, title)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 12 * mm,
                               f"{doc.page}")
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, PAGE_H - 13.5 * mm, PAGE_W - MARGIN,
                    PAGE_H - 13.5 * mm)
    canvas.restoreState()


def _cover(canvas, doc):
    canvas.saveState()
    # top accent band
    canvas.setFillColor(BLUE)
    canvas.rect(0, PAGE_H - 8 * mm, PAGE_W, 8 * mm, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor("#0e2748"))
    canvas.rect(0, PAGE_H - 8 * mm, PAGE_W * 0.32, 8 * mm, fill=1, stroke=0)
    # a simple wing glyph
    canvas.setStrokeColor(colors.HexColor("#cfd8e6"))
    canvas.setLineWidth(1.0)
    y0 = 48 * mm
    canvas.setFillColor(colors.HexColor("#eaf0f8"))
    p = canvas.beginPath()
    p.moveTo(MARGIN, y0)
    p.curveTo(MARGIN + 40 * mm, y0 + 26 * mm, MARGIN + 130 * mm,
              y0 + 20 * mm, PAGE_W - MARGIN, y0 + 6 * mm)
    p.curveTo(MARGIN + 120 * mm, y0 + 2 * mm, MARGIN + 50 * mm,
              y0 - 2 * mm, MARGIN, y0)
    canvas.setFillColor(colors.HexColor("#dbe6f4"))
    canvas.setStrokeColor(BLUE)
    canvas.drawPath(p, fill=1, stroke=1)
    canvas.setStrokeColor(colors.HexColor("#9aa4b2"))
    canvas.setLineWidth(1.2)
    canvas.line(MARGIN, y0 - 12 * mm, PAGE_W - MARGIN, y0 - 12 * mm)
    for x in range(int(MARGIN), int(PAGE_W - MARGIN), 12):
        canvas.setLineWidth(0.4)
        canvas.line(x, y0 - 12 * mm, x - 4, y0 - 15 * mm)
    canvas.restoreState()


def build():
    out = ROOT / "docs" / "_build"
    print("generating figures from app.core …")
    made = guide_figures.build_all(out)

    pdf_path = ROOT / "docs" / "Wing Section Studio - Workflow Guide.pdf"
    doc = GuideDoc(
        str(pdf_path), pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title="Wing Section Studio — Guide",
        author="Wing Section Studio")

    frame = Frame(MARGIN, 18 * mm, PAGE_W - 2 * MARGIN,
                  PAGE_H - 34 * mm, id="main")
    cover_frame = Frame(MARGIN, 18 * mm, PAGE_W - 2 * MARGIN,
                        PAGE_H - 36 * mm, id="cover")
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[cover_frame], onPage=_cover),
        PageTemplate(id="body", frames=[frame], onPage=_header_footer),
    ])

    story = []
    story += cover()
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())
    story += toc()
    story.append(PageBreak())
    story += part_i(made)
    story += part_ii(made)
    story += reference()

    doc.multiBuild(story)
    size_kb = pdf_path.stat().st_size / 1024
    print(f"wrote {pdf_path}  ({size_kb:.0f} kB, {doc.page} pages)")
    return pdf_path


# ---------------------------------------------------------------- content

def cover():
    return [
        Spacer(1, 66 * mm),
        Paragraph("Wing Section&nbsp;Studio", styles["cover_title"]),
        Spacer(1, 3 * mm),
        Paragraph("User &amp; Reference Guide", styles["cover_sub"]),
        Spacer(1, 10 * mm),
        Paragraph(
            "Designing multi-element wing sections in ground effect — "
            "configuration, analysis, target-downforce optimization, "
            "airfoil shape refinement, manufacturing preparation, and "
            "CAD-ready export — together with a complete account of how "
            "the aerodynamic engine works.", styles["cover_meta"]),
        Spacer(1, 36 * mm),
        Paragraph("Part I — Using the application &nbsp;·&nbsp; "
                  "Part II — How it works", styles["cover_meta"]),
        Paragraph("Revision July 2026", styles["cover_meta"]),
    ]


def toc():
    t = TableOfContents()
    t.levelStyles = [styles["toc1"], styles["toc2"]]
    t.dotsMinLevel = 0
    head = Paragraph("Contents", styles["h1"])
    return [head, HRFlowable(width="100%", thickness=0.6, color=RULE,
                             spaceBefore=2, spaceAfter=10), t]


def part_header(label, title):
    return KeepTogether([
        Spacer(1, 2),
        Paragraph(label, styles["part"]),
        Paragraph(title, ParagraphStyle(
            "pt", fontName=BOLD, fontSize=15, leading=19, textColor=BLUE,
            spaceAfter=2)),
        HRFlowable(width="100%", thickness=1.0, color=BLUE, spaceAfter=8),
    ])


def part_i(made):
    s = [part_header("PART I", "Using the application")]

    # 1 Overview
    s.append(heading("Overview"))
    s.append(body(
        "Wing Section Studio designs the two-dimensional section of a "
        "multi-element racing wing operating in ground effect — the front "
        "wing of a Formula-style car being the canonical case. It covers the "
        "fast, iterative phase of wing design: choosing airfoils, arranging "
        "one to four elements with correct slot geometry, predicting "
        "downforce and drag at the operating point, optimizing the "
        "arrangement (and, if you wish, the airfoil shapes themselves) to a "
        "target load, preparing the section for manufacture, and exporting "
        "CAD-ready geometry."))
    s.append(body(
        "The application is a screening and optimization layer, not a "
        "replacement for high-fidelity analysis. Its solver produces exact "
        "inviscid solutions of the full multi-element interaction in "
        "milliseconds, corrected to realistic values by transparent, "
        "user-adjustable calibration factors. Designs shortlisted here are "
        "meant to be confirmed with RANS CFD or wind-tunnel measurement, and "
        "the calibration factors re-tuned against those results. Part II of "
        "this guide explains every step of that computation."))
    s.append(h3("Capabilities at a glance"))
    s.append(bullets([
        f"One to four elements; 2,174 bundled UIUC airfoils, 4-digit NACA "
        f"generation, and Selig {C('.dat')} upload.",
        "Slot geometry set directly as gap and overlap (percent of chord); "
        "placement is solved so the achieved values equal the requested ones.",
        "Coupled inviscid analysis of all elements in free air and in ground "
        "effect, with per-element viscous loading limits and a realistic "
        "profile-plus-induced drag estimate.",
        "Target-downforce optimization over stack angle, deflections, slot "
        "geometry, chords, airfoil choice, and airfoil shape — returning a "
        "set of distinct candidate designs.",
        "Manufacturing preparation: buildable trailing edges and thickness "
        "checks, folded through the whole pipeline so what you analyze is "
        "what you cut.",
        "Operating maps sweeping ride height or speed around the design "
        "point, with the current point and the downforce peak marked.",
        "Export to DXF, Selig .dat, XYZ points, CSV, SVG, and a JSON "
        "manifest, in both the installed and upright orientations — plus a "
        "ready-to-run OpenFOAM 2D RANS case.",
    ]))

    # 2 Workflow
    s.append(heading("The workflow at a glance"))
    s.append(figure("pipeline",
                    "Figure 1 — Inside a single analysis: validated inputs "
                    "become a placed, solved stack, an inviscid pair of "
                    "solutions, a corrected estimate, and viscous loading and "
                    "drag — described in full in Part II.", made=made))
    s.append(body(
        "A typical session runs: pick a starting template; set the operating "
        "point (speed, ride height, chord, span); adjust elements and slots "
        "while watching the live drawing and loading meters; press "
        f"{B('Analyze')} to get forces; optimize to a target; optionally "
        "refine the airfoil shapes; enable manufacturing preparation; and "
        "export. Analysis takes well under a second, so the intended rhythm "
        "is conversational — adjust, analyze, repeat."))

    # 3 Getting started
    s.append(heading("Getting started"))
    s.append(body(
        f"Launch by double-clicking {B('Wing Section Studio')} (desktop "
        "shortcut or project folder). A window opens immediately with a "
        "loading screen while the aerodynamic models start — a few seconds — "
        "then the workspace appears. Everything runs locally in one process; "
        "closing the window shuts it down. The last session's configuration "
        "is saved automatically and restored on the next launch."))
    s.append(kv_table([
        ("Configuration", "Operating point, target downforce, the elements, "
         "the manufacturing group, and an advanced group of air properties "
         "and model-calibration factors."),
        ("Elements", "One card per element: airfoil, chord, deflection, slot "
         "gap and overlap, with live Reynolds number and as-built badges."),
        ("Section drawing", "A dimensioned technical drawing in the "
         "as-driven orientation, with ground hatching, ride-height and "
         "length dimensions, slot callouts, and the centre-of-pressure mark. "
         "Scroll to zoom, drag to pan."),
        ("Results", "Estimated downforce against target, coefficient and "
         "drag tiles, per-element loading meters, and plain-language "
         "warnings."),
        ("Detail tabs", "Pressure distributions, section polars, the "
         "optimizer, the airfoil screener, and export."),
    ]))
    s.append(callout(
        "Two orientations, one geometry",
        "The drawing shows the wing the way it runs on the car — inverted, "
        "above the road — because that is the orientation the analysis uses. "
        "The <b>Upright</b> toggle mirrors it into the convention airfoil "
        "catalogs and CAD sketches use. Both are exact rigid transforms of "
        "the same section; nothing about the physics changes."))

    # 4 Configure
    s.append(heading("Step 1 — Configure the section"))
    s.append(h3("Operating point"))
    s.append(kv_table([
        ("Speed", "Design airspeed. Forces scale with dynamic pressure; each "
         "element's Reynolds number follows from speed and its chord."),
        ("Ride height", "Clearance from the road to the lowest point of the "
         "section, in millimeters — the dominant ground-effect parameter."),
        ("Main chord / Span", "Main-element chord and wing span. Span enters "
         "the reference area and the induced-drag aspect ratio."),
        ("Stack angle", "Incidence of the whole section. Positive is nose-up "
         "— more downforce — the same sense as flap deflection."),
        ("Ncrit", "Transition criterion for the viscous model. 9 is a clean "
         "tunnel; 4–7 represents on-track turbulence."),
        ("Air properties &amp; calibration", "Density, viscosity, the two "
         "calibration factors of the estimate — the free-air viscous "
         "realization and the ground-gain realization factor, left blank for "
         "the automatic ride-height curve (section 14) — the finite-span "
         "lift efficiency, the span efficiency for induced drag, and panel "
         "resolution."),
    ]))
    s.append(h3("Elements and slot geometry"))
    s.append(body(
        "Each element card selects an airfoil — type to search the library, "
        f"press {C('.dat')} to load a coordinate file — and, for flaps, sets "
        "chord (percent of main chord), deflection (degrees, trailing edge "
        "down), and the two slot parameters used throughout the high-lift "
        "literature:"))
    s.append(figure("slot_definition",
                    "Figure 2 — Slot definitions on a real placement. Gap is "
                    "the smallest clearance from the previous trailing edge "
                    "to the flap surface; overlap is the horizontal tuck of "
                    "the flap nose under that trailing edge.", made=made,
                    width=FIGW * 0.82))
    s.append(body(
        "Placement is solved so the section achieves exactly the requested "
        "gap and overlap. Healthy starting ranges are 1–2.5&nbsp;%c gap and "
        "2–4&nbsp;%c overlap; the card badges always show the achieved "
        "values, and the analysis warns when a slot chokes (gap below "
        "0.5&nbsp;%c) or a requested gap cannot be reached. The drawing "
        "updates live with every edit."))

    # 5 Analyze
    s.append(heading("Step 2 — Analyze"))
    s.append(body(
        f"{B('Analyze section')} solves the coupled inviscid flow around all "
        "elements twice — once in free air and once in ground effect — and "
        "combines the results with per-element viscous data into the numbers "
        "on the right-hand panel:"))
    s.append(bullets([
        f"{B('Estimated downforce')}, compared against the target as an "
        "on-target / over / under pill. The small information button beside "
        "the label opens the exact computation.",
        f"{B('Coefficient tiles')} — the corrected estimate alongside the "
        "raw inviscid coefficients (ground and free air) it derives from, "
        "and the inviscid ground-effect gain.",
        f"{B('Drag estimate')} — induced plus profile, with the split shown "
        "beneath the total and the induced-drag details on hover.",
        f"{B('Element loading meters')} — each element's working lift "
        "against the isolated maximum of its airfoil at its own Reynolds "
        "number. Warnings begin at 90&nbsp;% of the limit, criticals above "
        "110&nbsp;%: the classical budget for slotted high-lift systems. A "
        "second check watches the realized ground-effect operating point "
        "(section 16).",
        f"{B('Pressure tab')} — the inviscid surface-pressure distribution "
        "of every element, the physics-level view of how the elements share "
        "load.",
    ]))
    s.append(figure("loading",
                    "Figure 3 — Loading meters from a real analysis. The "
                    "main element runs above its isolated maximum (critical, "
                    "red) — normal for a driven main plane — while the flap "
                    "sits comfortably under (green). The dotted line is the "
                    "90&nbsp;% warning threshold.", made=made,
                    width=FIGW * 0.86))

    # 6 Optimize
    s.append(heading("Step 3 — Optimize to a target"))
    s.append(body(
        "Set the target downforce and choose the free variables: stack "
        "angle, flap deflections, slot gap and overlap, flap chords, the "
        "airfoil of each element, and the airfoil shape (section 7). The "
        "search drives predicted downforce to the target while penalizing "
        "drag, slot geometry outside the healthy band, element overload, and "
        "intersecting geometry."))
    s.append(figure("optimizer",
                    "Figure 4 — A real optimization run: downforce converging "
                    "to the 250&nbsp;N target (left) and the objective "
                    "descending on a log scale (right).", made=made))
    s.append(h3("Candidates, not a single answer"))
    s.append(body(
        "A finished run returns the best design plus up to three "
        f"{B('candidate designs')} — distinct set-ups that also hit the "
        "target, chosen to be genuinely different from one another (different "
        "slot geometry, incidence, or airfoils) rather than trivial "
        "variations around one optimum. Each candidate card shows its own "
        "analyzed downforce, drag, and lift-to-drag, and applies to the "
        "configuration with one click — so build-tolerance and packaging "
        "trade-offs stay visible instead of being hidden inside a single "
        "number."))
    s.append(h3("Effort and reproducibility"))
    s.append(kv_table([
        ("Fast / Standard", "Stop as soon as the target is reached cleanly — "
         "a few seconds to half a minute. Quick, but because the search "
         "halts in the first basin that meets the target, two runs from "
         "slightly different starting points can land on different "
         "(similarly-good) designs."),
        ("Thorough", "Searches the whole space before refining, with a wider "
         "population and a multi-start final polish at full solver "
         "resolution. It takes minutes, and repeated runs converge to the "
         "same lowest-drag design — use it for a design you intend to keep."),
    ]))
    s.append(body(
        "When a target is unrealistic in either direction, the result panel "
        "says so in plain language — for example, that the stack met the "
        "target only by parking its loading variables at their minimum, so "
        "the target could be raised or an element removed. "
        f"{B('Apply to configuration')} writes the chosen design, including "
        "any airfoil or shape changes, back into the element cards and "
        "re-analyzes it."))

    # 7 Shape refinement
    s.append(heading("Step 4 — Airfoil shape refinement"))
    s.append(body(
        "Airfoil selection chooses the best existing section for each "
        f"element. {B('Airfoil shape')} goes further: it lets the optimizer "
        "modify that section — pushing camber where the loading wants it, "
        "thinning or thickening the envelope — to squeeze out more "
        "performance. Enable it as a free variable alongside the others."))
    s.append(figure("shaping",
                    "Figure 5 — Shape refinement. Three camber levers "
                    "(front, mid, aft loading) plus a thickness scale (left) "
                    "modify whatever section the element uses (right); the "
                    "modification is applied to a real S1223 here.",
                    made=made))
    s.append(body(
        "Each element gets three camber adjustments (near 25, 55, and "
        "80&nbsp;% chord — the classical front-, mid-, and aft-loading "
        "levers) and one thickness scale. Every constraint still holds: "
        "manufacturing trailing-edge preparation wraps the modified section, "
        "the buildable-thickness floor limits how far it may be thinned, and "
        "the stall budget uses the modified section's own polar. Shaped "
        "sections carry through polars, exports, and saved projects; "
        "re-running the optimizer keeps refining the current shape rather "
        "than stacking changes."))
    s.append(callout(
        "Shape refinement is slower",
        "Every new shape needs fresh aerodynamic evaluation (there is no "
        "library polar to reuse), so shape runs are somewhat slower per "
        "evaluation — roughly 1.3–1.7× — than geometry-only runs. Pair it "
        "with the "
        "<b>Thorough</b> effort setting when you want a final, reproducible "
        "shape, and expect a run to take a few minutes."))

    # 8 Manufacturing
    s.append(heading("Step 5 — Manufacturing preparation"))
    s.append(body(
        "Theoretical airfoil coordinates close to a knife edge at the "
        "trailing edge — zero thickness. No layup, print, or hot-wire cut "
        f"can produce that. Enable {B('Manufacturing')} to open every "
        "element's trailing edge to a minimum buildable thickness in "
        "millimeters, and to check that each section is thick enough to make."))
    s.append(figure("te_treatment",
                    "Figure 6 — The two trailing-edge treatments applied to "
                    "a real section, zoomed on the aft. Thicken adds material "
                    "over the rear while keeping the chord; Cut off truncates "
                    "the tip and rescales.", made=made))
    s.append(kv_table([
        ("Thicken", "Adds thickness over the rear of the section, keeping "
         "chord and camber. Costs a fraction of a percent of downforce at a "
         "typical 1–1.5&nbsp;mm trailing edge (about 0.3&nbsp;% on the "
         "baseline section), rising to roughly 2&nbsp;% at 3&nbsp;mm. This "
         "is the default and the recommended aerodynamic preparation."),
        ("Cut off", "Truncates the section where it reaches the target "
         "thickness and rescales to the original chord — for when a part or "
         "mold must literally be cut back. Thin-tailed sections lose far "
         "more downforce than the thicken treatment — roughly 17&nbsp;% on "
         "the baseline section at 1.5&nbsp;mm — and the tooltip says so."),
        ("Min thickness", "An optional buildable-minimum check on the whole "
         "section. It warns when an element's thickest point is below the "
         "minimum and keeps thinner airfoils out of the optimizer and "
         "screener — it never silently reshapes a section you chose."),
    ]))
    s.append(body(
        "Thicken does more than open the trailing edge: it also raises the "
        "thickness of any station behind the section's nose that would "
        "otherwise be thinner than the trailing edge itself — a thin "
        "sail-like section would otherwise keep an unbuildable waist ahead "
        "of its blunt tail. A section whose thickest point is not "
        "meaningfully above the requested trailing edge is flagged "
        f"{B('critically as a plate')}: the treatment still opens and floors "
        "it, but the result is a near-constant-thickness plate the tool warns "
        "you about rather than a section you would have chosen (Figure 7)."))
    s.append(figure("thickness_floor",
                    "Figure 7 — As-built thickness distributions. Left: a "
                    "buildable section, floored so nothing behind the nose is "
                    "thinner than the 3&nbsp;mm trailing edge. Right: a "
                    "section thinner than the requested edge everywhere — a "
                    "plate: still treated, but flagged critically.", made=made))
    s.append(callout(
        "Manufacturing preparation is fully reversible",
        "Turning the checkbox on never alters your configuration — the "
        "treated shapes are derived on the fly, and turning it off returns "
        "the exact original geometry. Each element card shows the as-built "
        "trailing-edge thickness, maximum thickness, and the thinnest "
        "station behind the nose (the <b>waist</b> badge). Every downstream "
        "step — the solver, polars, the optimizer, and all exports — uses "
        "the as-built shapes, so what you cut is what was analyzed. If a "
        "design is destined for manufacture, keep the box on while "
        "optimizing so the search tunes the real part."))

    # 9 Screener + polars
    s.append(heading("Support tools — screener and polars"))
    s.append(h3("Airfoil screener"))
    s.append(body(
        "The screener sweeps all bundled sections (plus any uploads) at a "
        "chosen Reynolds number and tabulates maximum lift, best efficiency, "
        "drag at a reference lift coefficient, thickness, camber, and the "
        f"surrogate model's confidence. Columns sort on click; {B('Use')} "
        "assigns a section to an element. Screening the full library takes a "
        "few seconds and is cached, so re-sorting and re-screening are "
        "instant. When manufacturing preparation is on, the thickness filter "
        "is pre-set so sections too thin to build are excluded up front."))
    s.append(h3("Polars"))
    s.append(body(
        "The polar tab plots each element's lift curve and drag polar from "
        "the neural-network surrogate (trained on XFOIL data; instant), for "
        "the section as actually built. An "
        f"{B('Add XFOIL reference')} button runs the locally installed XFOIL "
        "executable, when present, for an independent cross-check — the two "
        "typically agree within a few percent below stall."))

    # 10 Export
    s.append(heading("Step 6 — Export to CAD"))
    s.append(kv_table([
        ("DXF", "True-scale millimeters, one closed contour per element on "
         "its own layer, as smooth splines (for lofting) or exact polylines. "
         "Opens directly as a sketch in SolidWorks, Fusion, Onshape, NX."),
        ("Complete bundle (ZIP)", "DXF in both orientations, per-element XYZ "
         "point files, Selig .dat contours (main-chord units, for meshing "
         "and panel workflows), CSV, an SVG drawing, and a JSON manifest "
         "with the full configuration and analysis snapshot."),
        ("SVG drawing", "True-scale vector drawing with the ground line, for "
         "documentation or 1:1 print checks."),
        ("Point table (CSV)", "Every contour point in millimeters with "
         "element metadata."),
        ("OpenFOAM case", "A complete, ready-to-run 2D RANS case — mesh, "
         "boundary conditions, solver settings, and a run script — at a "
         "chosen mesh resolution. The handoff workflow is section 11."),
    ]))
    s.append(body(
        "Exports can be written in the as-driven orientation (ground at "
        "<i>y</i>&nbsp;=&nbsp;0, ready for CFD domains) or upright (the CAD "
        "sketch convention). Each export is saved to the project's "
        f"{C('exports/')} folder with a timestamped name, and a dialog shows "
        f"the exact location with a {B('Show in folder')} button; a browser "
        "download of a copy is offered alongside. With manufacturing "
        "preparation enabled, every exported contour is the as-built "
        "profile, and the manifest records the achieved trailing-edge "
        "thickness per element."))

    # 11 Maps + RANS handoff
    s.append(heading("Operating maps and the RANS handoff"))
    s.append(body(
        "Two closing tools round out the workflow. The operating maps show "
        "how a finished design behaves away from its design point — a wing "
        "tuned at one ride height rides at many — and the OpenFOAM export "
        "hands a shortlisted section to a genuine RANS solver for "
        "confirmation."))
    s.append(h3("Operating maps"))
    s.append(body(
        f"The {B('Maps')} tab (beside the Airfoil screener) sweeps the "
        "current design across ride height or speed — by default 10 to "
        "120&nbsp;mm of ride height in 23 steps, up to 40 points — and plots "
        "downforce and lift-to-drag against the swept value, marking the "
        "current operating point and the downforce peak. A ride-height sweep "
        "re-solves the flow at every point and takes a second or two at the "
        "sweep's capped panel resolution (reported under the charts; "
        f"{B('Analyze')} always runs at full resolution). A speed sweep is "
        "near-instant: the inviscid solution does not depend on speed, so "
        "only the Reynolds numbers and the dynamic pressure vary."))
    s.append(figure("maps",
                    "Figure 8 — The Maps tab's ride-height sweep of the "
                    "default template, generated by the same sweep the "
                    "application runs. Downforce peaks near 20&nbsp;mm and "
                    "falls closer to the road; the dotted line is the "
                    "configured operating point.", made=made))
    s.append(body(
        "The force-reduction peak the correction model predicts (section 14) "
        "is now directly visible instead of being a property taken on trust: "
        "on the default two-element template the peak sits near 20&nbsp;mm "
        "ride height — running lower loses downforce. Swept points that push "
        "an element past twice its isolated stall limit are flagged beneath "
        "the charts: the model is optimistic there, so treat those downforce "
        "values as upper bounds."))
    s.append(h3("The OpenFOAM handoff"))
    s.append(body(
        "Everything this tool reports screens and ranks; RANS decides. The "
        f"{B('OpenFOAM case')} card in the Export tab writes a complete, "
        "ready-to-run two-dimensional RANS case for the current section: a "
        "mesh with boundary layers resolved down to the wall on the wing "
        "surfaces and on the ground beneath the wing, the steady "
        "k–ω&nbsp;SST setup, a ground plane moving at the freestream speed "
        "(the road, in the wing's frame), and automatic force-coefficient "
        "extraction. The reported lift coefficient is downforce-positive and "
        "referenced to the main chord — directly comparable to the "
        "coefficients this guide has used throughout."))
    s.append(kv_table([
        ("Coarse (~15k cells)", "A first look — does the flow stay attached, "
         "is the case healthy. Runs in minutes."),
        ("Medium (~40k cells)", "The default; the standard confirmation run "
         "for a shortlisted design."),
        ("Fine (~90k cells)", "Final numbers, and the grid-sensitivity check "
         "for a design you intend to commit to."),
    ]))
    s.append(body(
        "The workflow: generate the case (it lands in the exports folder "
        "like any other export), then run it from the case folder under "
        "WSL — the one-time OpenFOAM installation is described in the "
        "repository README:"))
    s.append(code("wsl -d Ubuntu -- bash run.sh"))
    s.append(body(
        "The script prepares the OpenFOAM environment, converts and checks "
        "the mesh, runs the solver, and writes the final coefficients to "
        f"{C('results.txt')}; each case's {C('README.txt')} records its "
        "exact numbers and conventions. Compare the RANS downforce with the "
        "estimate here, and re-tune the calibration factors (section 14) "
        "against it — closing exactly the loop those factors were built "
        "for."))
    return s


def part_ii(made):
    s = [PageBreak(), part_header("PART II", "How it works")]

    # 11 Architecture
    s.append(heading("Architecture"))
    s.append(body(
        "The application is one local process. A thin desktop shell (a "
        "WebView2 window) hosts a browser user interface built from plain "
        "HTML, CSS, and JavaScript with no build step. That interface talks "
        "to an in-process web server over a loopback-only HTTP connection; "
        "the server exposes every capability as a small REST API and "
        "delegates all real work to a framework-free Python core."))
    s.append(figure("architecture",
                    "Figure 9 — The whole application in one process. All "
                    "aerodynamics lives in app.core, framework-free Python, "
                    "reachable identically from the window or from scripts.",
                    made=made))
    s.append(body(
        f"Everything aerodynamic lives in {C('app.core')}: the panel solver "
        "is pure NumPy, with NeuralFoil and AeroSandbox for viscous data and "
        "geometry, SciPy for the optimizer's global search, and Matplotlib "
        "and ezdxf for geometry tests and DXF output. No web framework reaches "
        "into it. The web layer is a wrapper: the same functions "
        "that draw the live section and run the optimizer are importable "
        "directly for scripting, and the regression suites exercise them "
        "without a server. The server binds only to the loopback address and "
        "rejects any request whose Host header is not local, closing the one "
        "route by which a web page could otherwise reach it."))
    s.append(h3("Frames of reference"))
    s.append(body(
        "Two coordinate frames recur throughout. The "
        f"{B('design frame')} is upright — lift positive-up, the main "
        "leading edge at the origin, the main chord equal to one. The "
        f"{B('installed frame')} is the design frame flipped so downforce "
        "points down, with the ground plane at <i>y</i>&nbsp;=&nbsp;0 and "
        "the whole section translated so its lowest point sits at the ride "
        "height. The panel solver runs in the installed frame; the two "
        "orientations you can draw and export are exactly these frames. All "
        "stack coefficients are referenced to the main chord; per-element "
        "lift coefficients are referenced to each element's own chord."))

    # 12 Panel method
    s.append(heading("The inviscid panel method"))
    s.append(body(
        "The core solver is a multi-element Hess–Smith panel method. Each "
        "element contour, in Selig ordering, is discretized into flat panels. "
        "Every panel carries a constant-strength source distribution (one "
        "unknown per panel), and all panels of a given element share a single "
        "vortex strength (one more unknown per element). Sources set the "
        "shape of the flow; the per-element circulation sets its lift."))
    s.append(figure("panel_method",
                    "Figure 10 — A real section discretized into flat panels. "
                    "The solver enforces the boundary conditions at the "
                    "collocation point of each panel, using the outward "
                    "normals shown.", made=made, width=FIGW * 0.9))
    s.append(body(
        "The unknowns are fixed by two sets of conditions. At the "
        f"collocation point of every panel, {B('flow tangency')} requires "
        "zero velocity normal to the surface — the solid-wall condition. For "
        f"each element, one {B('Kutta condition')} requires the flow to "
        "leave the trailing edge smoothly, expressed as equal-magnitude "
        "tangential velocities on the two panels meeting there. Together "
        "these give a single dense linear system spanning all elements at "
        "once, so every slot and stack interaction is captured exactly — "
        "there is no assumption that the elements are independent."))
    s.append(h3("Ground effect by images"))
    s.append(body(
        "The ground plane is introduced by the method of images: the "
        "velocity the real system induces at a point equals what a mirror "
        "system induces at the mirrored point, under a single reflection "
        "identity. Reflecting a source keeps its sign; reflecting a vortex "
        "flips it; both facts fall out of the same identity, so the ground "
        "adds no new unknowns — only an extra influence term. This is "
        "important because the naive alternative (a lift-from-circulation "
        "shortcut) is simply wrong near the ground, where image-induced "
        "velocities are large."))
    s.append(h3("Forces from pressure"))
    s.append(body(
        "Once the strengths are known, the surface pressure coefficient at "
        "each panel follows from its tangential velocity, and the forces and "
        f"moment come from {B('integrating that pressure')} over the "
        "surface — not from the circulation. Pressure integration remains "
        "valid in ground effect, where the circulation-based shortcut "
        "over-predicts downforce by roughly a factor of two because it "
        "ignores the image-induced velocity field. The moment is reported "
        "nose-up-positive, and the centre of pressure follows from it. A "
        "small numerical drag residual (exactly zero for ideal inviscid "
        "flow) is reported as a discretization diagnostic."))
    s.append(figure("cp",
                    "Figure 11 — Real inviscid surface pressure for a "
                    "two-element stack, free air (dashed) versus ground "
                    "effect (solid). Ground effect deepens the suction on "
                    "the main element's lower surface — the venturi that "
                    "makes downforce.", made=made, width=FIGW * 0.88))

    # 13 Ground effect / correction
    s.append(heading("Ground effect and the correction model"))
    s.append(body(
        "The panel solution is exact for inviscid flow, but inviscid ground "
        "effect is unphysical in the limit: as the ride height shrinks, the "
        "predicted venturi gain grows without bound, while a real flow "
        "chokes on boundary-layer growth in the gap. The tool therefore "
        "reports a corrected estimate built from transparent, "
        "user-adjustable factors:"))
    s.append(code(
        "C_est  = eta_visc · [ C_free + G_real ]\n"
        "G_real = cap · tanh( k_g · (C_ground − C_free) / cap ) "
        "· tanh( (h/c) / 0.045 )\n"
        "cap    = 3.0 · |C_free|"))
    s.append(body(
        f"Here {C('C_free')} and {C('C_ground')} are the exact inviscid "
        f"downforce coefficients in free air and in ground effect. "
        f"{C('eta_visc')} (default 0.85) is the free-air viscous "
        f"realization — how much of the inviscid free-air load a real "
        f"viscous flow achieves. {C('k_g')} is the fraction of the "
        f"<i>additional</i> ground-effect gain that is realized; at ordinary "
        "ride heights both saturation terms are near-linear and near-one, so "
        "the model reduces to the familiar "
        f"{C('eta_visc · [C_free + k_g · (C_ground − C_free)]')}. The first "
        "saturation caps the realized gain at three times the free-air "
        "load; the second chokes it off below "
        f"<i>h/c</i>&nbsp;≈&nbsp;0.045, where the real venturi flow stalls "
        f"on boundary-layer growth. Left blank, {C('k_g')} follows an "
        "automatic ride-height curve:"))
    s.append(code("k_g = 0.85 · tanh( (1.4 / 0.85) · h/c )"))
    s.append(figure("ground_model",
                    "Figure 12 — Left: the automatic realization-factor "
                    "curve, which preserves a calibrated small-height slope "
                    "and an 0.85 ceiling but vanishes at the ground. Right: "
                    "on a real stack, the raw inviscid gain diverges as ride "
                    "height shrinks while the realized estimate peaks and "
                    "then falls toward the road.",
                    made=made))
    s.append(body(
        "Together the saturations keep the calibrated slope at racing ride "
        "heights while genuinely reversing at the bottom of the travel: the "
        "estimate peaks near <i>h/c</i>&nbsp;≈&nbsp;0.065 on the "
        "two-element baseline and decreases as the wing drops further — the "
        "force-reduction behaviour measured on wings in strong ground "
        "effect — instead of growing without bound. Both knobs are exposed "
        "in the calibration group beside the raw inviscid numbers they "
        f"modify: {C('eta_visc')} directly, and {C('k_g')}, which follows "
        "the automatic curve when left blank and is pinned when a value is "
        "entered — useful for matching RANS or tunnel data at a known ride "
        "height. The analysis always reports which source it used. The cap "
        "ratio and choke height ("
        f"{C('gain_cap_ratio')}, {C('choke_h_c')}) are configuration fields "
        "with conservative defaults, adjustable at the config/API level."))

    # 14 Viscous
    s.append(heading("Viscous section data"))
    s.append(body(
        "The panel method knows nothing of viscosity, so the section-level "
        "viscous quantities — maximum lift, profile drag, the angle for a "
        "given lift — come from NeuralFoil, a neural-network surrogate "
        "trained on large sweeps of XFOIL solutions. It evaluates a complete "
        "polar in milliseconds, which is what makes library-wide screening "
        "and thousands of optimizer evaluations feasible, and it reports a "
        f"{B('confidence')} value that falls for unusual sections and "
        "operating points."))
    s.append(body(
        "A locally installed XFOIL executable — dropped into the "
        f"application's {C('xfoil/')} folder; it is not redistributed with "
        "the tool — serves as an optional independent cross-check through "
        "the same interface; it is slower and "
        "single-element, but it is the reference the surrogate approximates. "
        "Polars are cached by section, Reynolds number (bucketed to three "
        "significant figures), transition criterion, and model size, so a "
        "repeated operating point is free. The optimizer and the final "
        "analysis read the same fast surrogate; the polar tab reads a larger, "
        "more accurate one. The optimizer's speed comes instead from coarser "
        "panel resolution during the search."))
    s.append(callout(
        "Where to be sceptical",
        "The surrogate is most trustworthy for conventional sections at "
        "moderate Reynolds numbers. Very unusual shapes or very low Reynolds "
        "numbers deserve the XFOIL cross-check. When a section's polar never "
        "stalls within the analyzed angle range, the reported maximum lift "
        "is a lower bound rather than a true stall value, and the loading "
        "warnings say so."))

    # 15 Loading budget
    s.append(heading("Element loading budget"))
    s.append(body(
        "A slotted high-lift system can carry more total lift than the sum "
        "of its isolated elements, but each element still has a viscous "
        "ceiling. Following classical high-lift practice, the tool budgets "
        f"each element's {B('free-air')} inviscid load — knocked down by the "
        "viscous realization factor — against that element's isolated "
        "maximum lift coefficient, evaluated at its own Reynolds number "
        "(Figure 3). Warnings begin at 90&nbsp;% of the limit and criticals "
        "at 110&nbsp;%, since slotted elements tolerate somewhat more than "
        "isolated maximum."))
    s.append(body(
        "Free-air load is used for that budget deliberately: raw inviscid "
        "ground-effect loads grow without bound near the road and would "
        "make the check meaningless. A second indicator watches each "
        f"element's {B('realized ground-effect operating point')} — the "
        "same lift coefficient the profile-drag lookup uses, so it is "
        "consistent with the reported estimate by construction. Sections "
        "near the ground have been measured carrying roughly up to twice "
        "their isolated maximum before the flow gives up, so an element "
        "past that allowance draws a screening-validity warning: treat the "
        "downforce estimate as optimistic there, and the profile drag — "
        "capped at the pre-stall polar — as understated. Each element's "
        "inviscid ground-to-free multiplier is reported alongside, so the "
        "loading picture stays honest while the ground gain remains "
        "visible."))

    # 16 Drag
    s.append(heading("The drag model"))
    s.append(body(
        "Reported drag has two parts. "
        f"{B('Profile drag')} is each element's own section drag, read from "
        "its polar at the lift coefficient it actually works at in ground "
        "effect (its free-air load plus the realized share of its "
        "ground-effect gain, viscous-realized), then summed over the "
        "elements weighted by "
        f"chord. {B('Induced drag')} is computed from the very downforce the "
        "tool reports, so lift and induced drag are always consistent:"))
    s.append(code("CD_i = C_L² / (pi · AR · e) · phi"))
    s.append(body(
        f"where {C('AR')} is the wing aspect ratio, {C('e')} the "
        f"span-efficiency input (endplates push it toward one), and "
        f"{C('phi')} the McCormick ground-effect factor evaluated at the "
        f"section's mid-height — wings near the ground shed much weaker "
        f"trailing vorticity, so induced drag falls as the road approaches:"))
    s.append(code("phi = (16 h/b)² / (1 + (16 h/b)²)"))
    s.append(figure("induced",
                    "Figure 13 — The McCormick factor. Close to the ground, "
                    "the trailing vortex system weakens and induced drag "
                    "drops sharply.", made=made, width=FIGW * 0.56))
    s.append(body(
        "For a loaded finite-span front wing, induced drag dominates the "
        "total — often by an order of magnitude over profile drag — so "
        "leaving it out (as a profile-only figure would) produces fantasy "
        "efficiency numbers. Interference and mounting drag are not modeled."))

    # 17 Spec system
    s.append(heading("The airfoil spec system"))
    s.append(body(
        "Every airfoil in the application — a library section, an upload, a "
        "manufactured section, a shape-modified section — is named by a "
        f"single {B('spec string')}, and one resolver turns any spec into "
        "coordinates. Modifiers are prefixes that wrap a base spec, so they "
        "compose:"))
    s.append(figure("spec_chain",
                    "Figure 14 — Spec resolution. Each modifier unwraps to "
                    "its base and re-applies its transformation, so a shaped, "
                    "manufactured library section is one string that every "
                    "part of the system understands.", made=made,
                    width=FIGW * 0.92))
    s.append(body(
        "Because resolution is centralized, a modified section is a "
        "first-class airfoil everywhere with no special cases: the panel "
        "solver, the NeuralFoil polars, the XFOIL cross-check, the screener, "
        "and every exporter all see the same as-built coordinates. Modifier "
        "parameters are quantized (trailing-edge gaps and shape parameters "
        "each snapped to a fixed grid) so the spec strings — and the repanel "
        "and "
        "polar caches keyed on them — stay stable while the optimizer sweeps "
        "continuous values."))

    # 18 Manufacturing geometry
    s.append(heading("Manufacturing geometry"))
    s.append(body(
        f"The {B('thicken')} treatment adds thickness symmetrically about "
        "the camber line, blended in over the rear of the section with a "
        "smooth ramp, so the added opening between the two surfaces at the "
        "trailing edge equals the requested gap exactly. It then applies a "
        f"{B('thickness floor')}: from the first station where the section "
        "reaches the requested gap, back to the trailing edge, no station is "
        "left thinner than the gap. This is what removes the mid-chord waist "
        "that a naive ramp leaves on thin sections (Figure 7). The treatment "
        "works on a densely repaneled copy of the base section, and the "
        "floor is guaranteed on the geometry every consumer actually sees: "
        "the floored contour is verified through a spline-repanel "
        "round-trip, with the floor raised adaptively until it survives the "
        "fit."))
    s.append(body(
        f"The {B('cut off')} treatment truncates the section at the station "
        "where it reaches the target thickness — solved so that after "
        "rescaling to the original chord the trailing edge is exactly the "
        "requested thickness — and closes it with a flat base. It applies no "
        "floor, because its purpose is to cut a real part back; instead the "
        "report measures any remaining waist and warns. Because camber is "
        f"preserved by the symmetric construction, the treatment is a "
        f"{B('manufacturing operation, not a hidden aerodynamic re-trim')}: "
        "shifting material onto one surface instead would just be a camber "
        "change, which the optimizer's real variables already control."))
    s.append(body(
        "Exports close the blunt base explicitly. In the spline DXF the base "
        "is a separate straight line between the two surface endpoints, "
        "keeping the corners sharp instead of rounding them; the polyline "
        "DXF and the point files carry the exact open contour. The manifest "
        "records the achieved trailing-edge thickness, maximum thickness, and "
        "the measured minimum thickness behind the nose for every element."))

    # 19 Shape math
    s.append(heading("Shape refinement mathematics"))
    s.append(body(
        "Shape refinement modifies a section with a small, smooth, "
        "airfoil-preserving parameterization. The camber line is displaced "
        f"by a sum of three {B('Hicks–Henne bump functions')} centred near "
        "25, 55, and 80&nbsp;% chord — each a smooth hump that is zero at "
        "both ends of the chord and peaks at its centre — and the thickness "
        "envelope (each surface's offset from the camber line) is scaled by "
        "a single factor. Four numbers per element (Figure 5)."))
    s.append(body(
        "This choice is deliberate. Four smooth parameters keep the added "
        "search dimensions cheap and keep every generated shape recognizably "
        "an airfoil, so the NeuralFoil surrogate stays inside the space it "
        "was trained on and its confidence remains meaningful — a "
        "free-form coordinate parameterization would wander into shapes the "
        "surrogate cannot rate. The shape wraps as a spec modifier (section "
        "18), so manufacturing preparation applies on top of the shaped "
        "section, and the buildable-thickness minimum limits how far the "
        "thickness scale may thin an element. Re-optimizing a shaped design "
        "unwraps and seeds from its current parameters rather than nesting a "
        "second modification."))

    # 20 Optimizer
    s.append(heading("The optimizer"))
    s.append(body(
        "The optimizer treats the design as a vector of continuous "
        "variables and minimizes a single objective. Continuous quantities — "
        "stack angle, deflections, slot gap and overlap, chords, shape "
        f"parameters — map directly. {B('Airfoil choice')} is encoded as one "
        "continuous variable per element mapped onto a shortlist of "
        "candidate sections ranked by maximum lift, so that neighbouring "
        "values are similar airfoils and the search stays meaningful. The "
        "shortlist is built by the screener at each element's Reynolds "
        "number, includes the currently selected section (unless the "
        "candidate pool is restricted to your uploads), and — when "
        "manufacturing sets a buildable minimum — excludes sections too thin "
        "to make at that element's chord."))
    s.append(h3("Objective and constraints"))
    s.append(body(
        "The objective is dominated by the squared relative error to the "
        "target downforce, so the search first and foremost hits the number "
        "you asked for. Added to it are a weighted drag term (the "
        "adjustable drag penalty, trading residual drag against exactness on "
        "target) and soft penalties that keep candidates inside the healthy "
        "high-lift envelope: slot gap and overlap bands, element loading "
        "below about 105&nbsp;% of each airfoil's isolated maximum, and a "
        "hard penalty on intersecting geometry or a failed solve. The "
        "loading penalty is weighted so that an honest miss of the target "
        "beats a design that only reaches it by driving an element deep into "
        "stall."))
    s.append(h3("Search, candidates, and reproducibility"))
    s.append(body(
        "The search is a seeded global phase (differential evolution) "
        "followed by a local refinement (Nelder–Mead), evaluating a few "
        "hundred to a few thousand complete analyses. Identical inputs give "
        "identical results — the search is fully seeded. Every feasible "
        "evaluation is archived; at the end, the best design plus a few "
        "maximally-different on-target designs are selected from that archive "
        "as the candidate set, each re-analyzed at full resolution."))
    s.append(body(
        "Fast and Standard runs stop as soon as the target is met, which is "
        "quick but means the design depends on which basin the search "
        "reached first. Thorough runs remove that dependence: the global "
        "phase runs to completion with a wider population, and the final "
        "polish runs from several diverse starting points at full panel "
        "resolution, with the reported best required to come from "
        "full-resolution evaluations. The result is that two Thorough runs "
        "from different starting configurations converge to the same "
        "lowest-drag design, where Fast runs might differ."))

    # 21 Validation
    s.append(heading("Numerical accuracy and validation"))
    s.append(body(
        "The panel method is validated against an independent "
        "linear-vorticity formulation (with forces taken by pressure "
        "integration) and against XFOIL's inviscid solution on single "
        "elements, agreeing to within a few percent in free air and in "
        "ground effect. The image system is checked against an explicitly "
        "modelled mirror geometry, and the slot gap/overlap solver against "
        "direct geometric measurement of the placed sections."))
    s.append(body(
        "The repository ships regression suites that exercise the solver "
        "physics, the manufacturing geometry (including the thickness floor "
        "and plate detection), the optimizer (including determinism and "
        "Thorough reproducibility), the shape system, and the API's failure "
        "behaviour under hostile input. They run from a single command and "
        "form the contract the application is held to; every figure in Part "
        "II of this guide is generated from the same core the suites test."))
    s.append(callout(
        "What the numbers are for",
        "Treat every downforce and drag figure as a screening and ranking "
        "value. The tool is calibrated to land a typical two-element section "
        "at racing ride heights in the right physical band, but it resolves "
        "neither the boundary layer, slot merging and separation, nor "
        "three-dimensional effects. Confirm a shortlisted design with RANS "
        "CFD or measurement, and re-tune the calibration factors against "
        "those results, before committing to it.", kind="note"))

    # 22 Limitations
    s.append(heading("Model scope and limitations"))
    s.append(bullets([
        f"{B('Two-dimensional section physics')} with classical finite-span "
        "corrections. Spanwise design — endplates, footplates, local twist — "
        "is out of scope.",
        f"{B('Inviscid pressure field.')} Boundary-layer displacement, slot "
        "merging, and separation are represented only through the calibrated "
        "corrections and the loading limits, not resolved.",
        f"{B('Calibrated ground effect.')} The correction factors are tuned "
        "to a physical band, not derived; strong ground effect in particular "
        "should be confirmed against higher-fidelity data.",
        f"{B('Surrogate viscous data.')} Section polars come from a neural "
        "surrogate of XFOIL; its confidence is reported and a cross-check is "
        "one click away, but unusual sections at low Reynolds numbers "
        "deserve scrutiny.",
        f"{B('No interference or mounting drag')}, and no compressibility "
        "(irrelevant at these speeds).",
    ]))
    return s


def reference():
    s = [heading("Reference")]
    s.append(kv_table([
        ("Project files", "Save and Open write a portable JSON project "
         "containing the full configuration, target, and any uploaded "
         "airfoil geometry (including inside shape and manufacturing "
         "wrappers). The working session is also autosaved server-side, in "
         "app_data/ beside the application, and restored on launch."),
        ("Exports folder", "Exports are written to exports/ beside the "
         "application, with timestamped names; the export dialog links to "
         "the exact file."),
        ("Automation / API", "Every function is available over the local "
         "REST API; interactive documentation is served at /api/docs while "
         "the application or the headless server is running."),
        ("Headless use", "The same server runs without a window for "
         "scripting: python -m uvicorn app.server:app --port 8642."),
        ("Verification", "The regression suites run from a single command: "
         "python app/tests/run_all.py (it starts its own scratch server for "
         "the API checks)."),
        ("Design record", "Every design decision behind the "
         "application is recorded, with the options considered, in "
         "DECISIONS.md at the repository root."),
    ]))
    s.append(Spacer(1, 6))
    s.append(HRFlowable(width="100%", thickness=0.6, color=RULE))
    s.append(Spacer(1, 4))
    s.append(Paragraph(
        "Wing Section Studio — User &amp; Reference Guide. Every figure in "
        "this document is generated directly from the application's "
        "aerodynamic core, so the guide tracks the shipping behaviour.",
        styles["caption"]))
    return s


if __name__ == "__main__":
    build()
